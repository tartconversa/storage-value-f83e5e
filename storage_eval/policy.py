"""候选策略：把用户规则翻译成逐时段动作请求。

因果纪律：
- 决策时刻只允许使用 target 之前已经可知的预报与已经可知的保供要求。
- 预报缺发布（或尚未到可知时刻）时，不能用真实负荷/电价补位，只能按无预报处理。
- 策略只产出"请求功率"，物理可行性由 physics.clip_power 统一投影；
  保供是硬约束，策略自身也会先扣除保留电量再决定放电深度。
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from .datafeed import DataFeed, Forecast, PolicySpec
from .obligations import EffectiveObligation, known_obligation
from .physics import BatteryState, reserve_energy_need


@dataclasses.dataclass(frozen=True)
class DecisionContext:
    """某时段决策时实际可见的信息快照。"""

    target: dt.datetime
    now: dt.datetime
    forecast: Forecast | None
    obligation: EffectiveObligation | None
    reserve_keep_kwh: float
    forecast_missing: bool = False


@dataclasses.dataclass(frozen=True)
class PolicyDecision:
    target: dt.datetime
    request_kw: float
    reason: str
    used_forecast: bool
    obligation_source: str | None


def make_context(feed: DataFeed, timeline, target: dt.datetime,
                 now: dt.datetime, state: BatteryState) -> DecisionContext:
    """组装决策时刻的可知信息；严格按可知时刻过滤。"""
    forecast = feed.forecast_known_by(target, now)
    obligation = known_obligation(timeline, target, now)
    keep = feed.battery.min_soc_kwh
    if obligation is not None:
        keep = max(
            keep,
            reserve_energy_need(
                feed.battery,
                obligation.required_kw,
                obligation.duration_h,
                margin_kwh=0.0,
            ),
        )
    return DecisionContext(
        target=target,
        now=now,
        forecast=forecast,
        obligation=obligation,
        reserve_keep_kwh=keep,
        forecast_missing=forecast is None,
    )


def decide(spec: PolicySpec, feed: DataFeed, ctx: DecisionContext,
           state: BatteryState) -> PolicyDecision:
    """按用户阈值规则给出请求功率（充电正、放电负、静置 0）。

    规则顺序（资料口径：保供是硬约束，条件冲突时先保供再套利）：
    1. 无可知预报 -> 静置（禁止用真实值充当完美预测）。
    2. 预报负荷达到 forecast_peak_kw -> 削峰放电，且放电后仍满足保供保留。
    3. 预报电价 >= 放电阈值 -> 放电套利（同样受保供保留限制）。
    4. 预报电价 < 充电阈值 -> 满功率充电请求（物理投影在模拟器完成）。
    5. 其余静置。
    reserve_margin_kwh 在硬保供保留之外再叠加用户自定余量。
    """
    battery = feed.battery
    if ctx.forecast is None:
        return PolicyDecision(ctx.target, 0.0, "no_known_forecast", False,
                              ctx.obligation.source if ctx.obligation else None)

    forecast = ctx.forecast
    obligation_source = ctx.obligation.source if ctx.obligation else None
    keep = max(ctx.reserve_keep_kwh, battery.min_soc_kwh) + spec.reserve_margin_kwh

    peak_trigger = forecast.load_kw >= spec.forecast_peak_kw
    if peak_trigger:
        request = -battery.max_power_kw
        reason = "forecast_peak_shave"
    elif forecast.price >= spec.discharge_above_price:
        request = -battery.max_power_kw
        reason = "price_discharge"
    elif forecast.price < spec.charge_below_price:
        request = battery.max_power_kw
        reason = "price_charge"
    else:
        return PolicyDecision(ctx.target, 0.0, "hold", True, obligation_source)

    # 保供硬约束：可放电量不足时把请求收浅；无电可放则静置并保留原因
    if request < 0.0:
        usable = state.soc_kwh - keep
        if usable <= 0.0:
            return PolicyDecision(ctx.target, 0.0,
                                  f"blocked_by_reserve:{reason}", True,
                                  obligation_source)
        max_kw = usable * battery.discharge_eff / 0.5
        if -request > max_kw:
            request = -max_kw
            reason = reason + "+reserve_limited"
    return PolicyDecision(ctx.target, request, reason, True, obligation_source)
