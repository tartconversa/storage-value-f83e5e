"""评审通过后的前瞻模块：下一评估周期的可执行范围。

输入是评审时点的"现状"：当前电量、本月已发生峰值、剩余保供义务。
输出是在这些既成事实约束下，每个候选策略在下一时段的可行动作包络，
以及在剩余义务窗口内必须维持的电量底线。不假设未来负荷/价格已知。
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from . import physics
from .datafeed import DataFeed, PolicySpec
from .obligations import EffectiveObligation, build_timeline, known_obligation
from .physics import BatteryState


@dataclasses.dataclass
class Envelope:
    target: dt.datetime
    min_power_kw: float
    max_power_kw: float
    reserve_keep_kwh: float
    obligation_source: str | None
    feasible: bool
    notes: list[str]


@dataclasses.dataclass
class ForwardOutlook:
    as_of: dt.datetime
    soc_kwh: float
    capacity_kwh: float
    current_month: str
    already_peak_kw: float
    obligations_ahead: list[dict]
    envelopes: list[Envelope]
    warnings: list[str]


def obligations_ahead(feed: DataFeed, as_of: dt.datetime) -> list[dict]:
    """评审时点之后仍需履行的义务段（含通知替换后的参数）。"""
    out = []
    for item in build_timeline(feed):
        if item.window_end <= as_of:
            continue
        out.append({
            "reserve_id": item.reserve_id,
            "source": item.source,
            "window_start": item.window_start.isoformat(),
            "window_end": item.window_end.isoformat(),
            "required_kw": item.required_kw,
            "duration_h": item.duration_h,
            "known": item.known_at <= as_of,
            "known_at": item.known_at.isoformat(),
            "battery_energy_need_kwh": physics.reserve_energy_need(
                feed.battery, item.required_kw, item.duration_h, 0.0
            ),
        })
    return out


def executable_envelope(feed: DataFeed, spec: PolicySpec, target: dt.datetime,
                        as_of: dt.datetime, state: BatteryState,
                        headroom_kw: float | None) -> Envelope:
    """给定现状与策略余量，下一时段交流功率的可执行范围。

    下界：受保供保留电量限制的最大放电功率（负值）。
    上界：功率上限与变压器余量、荷电上限共同限制的最大充电功率。
    """
    timeline = build_timeline(feed)
    obligation = known_obligation(timeline, target, as_of)
    keep = feed.battery.min_soc_kwh
    source = None
    if obligation is not None:
        keep = max(keep, physics.reserve_energy_need(
            feed.battery, obligation.required_kw, obligation.duration_h, 0.0))
        source = obligation.source
    keep += spec.reserve_margin_kwh

    notes: list[str] = []
    max_charge = min(feed.battery.max_power_kw,
                     (state.capacity_kwh - state.soc_kwh) / (0.5 * feed.battery.charge_eff))
    if headroom_kw is not None:
        max_charge = min(max_charge, headroom_kw)
    else:
        notes.append("变压器总表缺测：充电上限未核验，执行前需补表")

    usable = max(0.0, state.soc_kwh - keep)
    max_discharge = usable * feed.battery.discharge_eff / 0.5
    max_discharge = min(max_discharge, feed.battery.max_power_kw)
    feasible = state.soc_kwh >= keep - 1e-9
    if not feasible:
        notes.append("当前电量已低于保供保留线：禁止放电并需优先补电")
    return Envelope(
        target=target,
        min_power_kw=-max_discharge,
        max_power_kw=max(0.0, max_charge),
        reserve_keep_kwh=keep,
        obligation_source=source,
        feasible=feasible,
        notes=notes,
    )


def build_outlook(feed: DataFeed, as_of: dt.datetime, state: BatteryState,
                  horizon_slots: int, headroom_provider=None) -> ForwardOutlook:
    """生成评审时点之后若干时段的可执行包络（逐策略）。

    headroom_provider: 可选 callable(target) -> 变压器余量；
    无外部前瞻资料时传 None，包络会如实标注未核验。
    """
    timeline = build_timeline(feed)
    month = as_of.strftime("%Y-%m")
    tariff = feed.tariffs.get(month)
    already = tariff.already_peak_kw if tariff else 0.0
    warnings: list[str] = []
    if tariff is None:
        warnings.append(f"缺少 {month} 的电价契约，需量费用无法核算")

    envelopes: list[Envelope] = []
    # 包络对所有策略共用物理边界，差异只在 reserve_margin；取最严策略做保守包络
    strict = max(feed.policies.values(), key=lambda s: s.reserve_margin_kwh)
    for index in range(horizon_slots):
        target = as_of + dt.timedelta(minutes=30 * index)
        headroom = headroom_provider(target) if headroom_provider else None
        envelopes.append(
            executable_envelope(feed, strict, target, as_of, state, headroom)
        )
    ahead = obligations_ahead(feed, as_of)
    for item in ahead:
        if not item["known"]:
            warnings.append(
                f"义务 {item['reserve_id']} 的未来参数在评审时点尚不可知，"
                "包络仅按当前可知要求计算"
            )
    return ForwardOutlook(
        as_of=as_of,
        soc_kwh=state.soc_kwh,
        capacity_kwh=state.capacity_kwh,
        current_month=month,
        already_peak_kw=already,
        obligations_ahead=ahead,
        envelopes=envelopes,
        warnings=warnings,
    )
