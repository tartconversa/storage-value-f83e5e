"""单情景确定性回放：策略 + 电池物理 + 保供硬约束。

回放纪律：
- 所有策略面对同一条外部情景（other_load、价格、预报发布时刻、保供通知时点）。
- 决策只通过 policy.make_context 取当时可知信息，不接触真实未来。
- 跨月、跨周不重置电量与衰减；初始状态由调用方统一注入。
- 变压器总表缺测时无法核验共享约束，该时段显式标注，而不是假装余量无限。
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from . import physics
from .datafeed import DataFeed, PolicySpec, SLOT
from .obligations import build_timeline
from .physics import BatteryState, initial_state
from .policy import decide, make_context

SLOT_HOURS = SLOT.total_seconds() / 3600.0


@dataclasses.dataclass
class SlotRecord:
    """一个时段的完整回放记录，供费用、需量、缺口与归因核算复用。"""

    period_id: str
    start: dt.datetime
    month: str
    other_load_kw: float
    actual_price: float
    forecast_known: bool
    forecast_load_kw: float | None
    forecast_price: float | None
    obligation_source: str | None
    reserve_keep_kwh: float
    request_kw: float
    executed_kw: float
    clipped_notes: tuple[str, ...]
    reason: str
    soc_before: float
    soc_after: float
    capacity_kwh: float
    headroom_kw: float | None
    transformer_unchecked: bool
    # 动作前保供审计：保留线低于义务所需即记缺口；动作被保供取消/收浅单独计数
    reserve_need_kwh: float
    pre_action_gap_kwh: float
    reserve_blocked: bool
    # 交流侧交换电量（kWh）：充电为正
    grid_energy_kwh: float
    # 站点交流总功率（其他负荷 + 电池），总表缺测时为模型估计
    site_power_kw: float
    site_power_estimated: bool


@dataclasses.dataclass
class SimulationResult:
    policy_id: str
    records: list[SlotRecord]
    final_state: BatteryState
    initial_state: BatteryState
    reserve_shortfall_kwh: float
    reserve_shortfall_slots: int
    reserve_blocked_slots: int
    unchecked_transformer_slots: int

    def record_by_period(self) -> dict[str, SlotRecord]:
        return {record.period_id: record for record in self.records}


def simulate(feed: DataFeed, spec: PolicySpec,
             state: BatteryState | None = None) -> SimulationResult:
    """对一条候选策略做全时段因果回放。"""
    timeline = build_timeline(feed)
    if state is None:
        state = initial_state(feed.battery)
    start_state = state.snapshot()
    records: list[SlotRecord] = []
    shortfall_kwh = 0.0
    shortfall_slots = 0
    blocked_slots = 0
    unchecked = 0

    for period in feed.periods:
        now = period.start  # 时段起点决策；预报均提前 3 小时发布，天然可知
        ctx = make_context(feed, timeline, period.start, now, state)
        decision = decide(spec, feed, ctx, state)

        # 动作前保供审计：义务窗口内池内电量低于义务所需（含策略余量）即缺口。
        # 这是策略"不可接受"的判定依据；clip 只能阻止本时段继续放电，
        # 不能把已经形成的欠账抹掉。
        keep = ctx.reserve_keep_kwh + spec.reserve_margin_kwh
        if ctx.obligation is not None:
            need = physics.reserve_energy_need(
                feed.battery,
                ctx.obligation.required_kw,
                ctx.obligation.duration_h,
                spec.reserve_margin_kwh,
            )
            pre_gap = max(0.0, need - state.soc_kwh)
            if pre_gap > 1e-6:
                shortfall_kwh += pre_gap
                shortfall_slots += 1
        else:
            need = keep
            pre_gap = 0.0

        # 共享变压器余量：优先用同期总表事实；缺测时不臆造
        headroom: float | None
        unchecked_flag = False
        if period.transformer_kw is not None:
            headroom = physics.transformer_headroom(
                feed.tariffs[feed.month_of(period.start)].transformer_limit_kw,
                period.other_load_kw,
            )
        else:
            headroom = None
            unchecked_flag = True
            unchecked += 1

        executed, notes = physics.clip_power(
            feed.battery, state, decision.request_kw,
            headroom_kw=headroom,
            reserve_keep_kwh=keep,
        )
        reserve_blocked = (
            "soc_min_or_reserve" in notes
            or decision.reason.startswith("blocked_by_reserve")
        )
        if reserve_blocked:
            blocked_slots += 1
        soc_before = state.soc_kwh
        new_state = physics.apply_power(feed.battery, state, executed)

        site_power = period.other_load_kw + executed
        records.append(
            SlotRecord(
                period_id=period.period_id,
                start=period.start,
                month=feed.month_of(period.start),
                other_load_kw=period.other_load_kw,
                actual_price=period.price,
                forecast_known=decision.used_forecast,
                forecast_load_kw=ctx.forecast.load_kw if ctx.forecast else None,
                forecast_price=ctx.forecast.price if ctx.forecast else None,
                obligation_source=decision.obligation_source,
                reserve_keep_kwh=keep,
                request_kw=decision.request_kw,
                executed_kw=executed,
                clipped_notes=tuple(notes),
                reason=decision.reason,
                soc_before=soc_before,
                soc_after=new_state.soc_kwh,
                capacity_kwh=new_state.capacity_kwh,
                headroom_kw=headroom,
                transformer_unchecked=unchecked_flag,
                reserve_need_kwh=need,
                pre_action_gap_kwh=pre_gap,
                reserve_blocked=reserve_blocked,
                grid_energy_kwh=executed * SLOT_HOURS,
                site_power_kw=site_power,
                site_power_estimated=unchecked_flag,
            )
        )
        state = new_state

    return SimulationResult(
        policy_id=spec.policy_id,
        records=records,
        final_state=state,
        initial_state=start_state,
        reserve_shortfall_kwh=shortfall_kwh,
        reserve_shortfall_slots=shortfall_slots,
        reserve_blocked_slots=blocked_slots,
        unchecked_transformer_slots=unchecked,
    )


def simulate_no_battery(feed: DataFeed) -> SimulationResult:
    """无电池基线：电池恒为 0，站点功率即其他负荷。

    所有策略与该基线面对相同外部情景；节约 = 基线费用 - 策略费用，
    避免把历史操作员的择时能力误算成电池本身的收益。
    """
    state = initial_state(feed.battery)
    zero = PolicySpec(
        policy_id="NO_BATTERY",
        charge_below_price=float("nan"),
        discharge_above_price=float("nan"),
        forecast_peak_kw=float("inf"),
        reserve_margin_kwh=0.0,
    )
    records: list[SlotRecord] = []
    for period in feed.periods:
        records.append(
            SlotRecord(
                period_id=period.period_id,
                start=period.start,
                month=feed.month_of(period.start),
                other_load_kw=period.other_load_kw,
                actual_price=period.price,
                forecast_known=False,
                forecast_load_kw=None,
                forecast_price=None,
                obligation_source=None,
                reserve_keep_kwh=feed.battery.min_soc_kwh,
                request_kw=0.0,
                executed_kw=0.0,
                clipped_notes=(),
                reason="no_battery_baseline",
                soc_before=state.soc_kwh,
                soc_after=state.soc_kwh,
                capacity_kwh=state.capacity_kwh,
                headroom_kw=None,
                transformer_unchecked=False,
                reserve_need_kwh=feed.battery.min_soc_kwh,
                pre_action_gap_kwh=0.0,
                reserve_blocked=False,
                grid_energy_kwh=0.0,
                site_power_kw=period.other_load_kw,
                site_power_estimated=False,
            )
        )
    return SimulationResult(
        policy_id="NO_BATTERY",
        records=records,
        final_state=state,
        initial_state=state.snapshot(),
        reserve_shortfall_kwh=0.0,
        reserve_shortfall_slots=0,
        reserve_blocked_slots=0,
        unchecked_transformer_slots=0,
    )
