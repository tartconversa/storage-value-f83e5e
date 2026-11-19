"""电池物理模型：效率、功率、荷电上下限、共享变压器、衰减。

约定（半小时时段，左闭右开）：
- 决策变量 p_kw 为交流侧平均功率：充电为正、放电为负，与 operations 口径一致。
- 充电入池电量 = p * 0.5 * charge_eff；放电出池电量 = p * 0.5 / discharge_eff。
- 容量按累计放电量衰减：capacity = 初始容量 - degradation * 累计放电 MWh。
- 约束全部在动作"执行前"投影，保证回放轨迹物理可行，不靠事后修正。
"""
from __future__ import annotations

import dataclasses
import math

from .datafeed import BatterySpec, SLOT

SLOT_HOURS = SLOT.total_seconds() / 3600.0


@dataclasses.dataclass
class BatteryState:
    """时段起点的电池状态。soc_kwh 为直流侧存电量。"""

    soc_kwh: float
    capacity_kwh: float
    discharged_mwh: float

    def snapshot(self) -> "BatteryState":
        return BatteryState(self.soc_kwh, self.capacity_kwh, self.discharged_mwh)


def initial_state(spec: BatterySpec) -> BatteryState:
    return BatteryState(
        soc_kwh=spec.initial_soc_kwh,
        capacity_kwh=spec.capacity_kwh,
        discharged_mwh=0.0,
    )


def apply_power(spec: BatterySpec, state: BatteryState, p_kw: float) -> BatteryState:
    """按交流侧功率推进一个时段，返回新状态。调用方应先用 clip_power 投影。"""
    energy_ac = p_kw * SLOT_HOURS
    if energy_ac >= 0:
        soc = state.soc_kwh + energy_ac * spec.charge_eff
        discharged = state.discharged_mwh
    else:
        from_battery = energy_ac / spec.discharge_eff  # 负值
        soc = state.soc_kwh + from_battery
        discharged = state.discharged_mwh + (-from_battery) / 1000.0
    capacity = spec.capacity_kwh - spec.degradation_kwh_per_mwh * discharged
    return BatteryState(soc_kwh=soc, capacity_kwh=capacity, discharged_mwh=discharged)


def reserve_energy_need(spec: BatterySpec, required_kw: float, duration_h: float,
                        margin_kwh: float) -> float:
    """履行连续保供需要在池内保留的直流侧电量（含策略额外余量）。

    保供功率是交流侧口径，折算到电池侧要除以放电效率。
    """
    return required_kw * duration_h / spec.discharge_eff + margin_kwh


def clip_power(spec: BatterySpec, state: BatteryState, request_kw: float,
               headroom_kw: float | None, reserve_keep_kwh: float) -> tuple[float, list[str]]:
    """把策略请求功率投影到全部物理与保供约束内。

    headroom_kw 为共享变压器余量（其他负荷之外还能增加的交流功率），
    None 表示该时段总表缺测、无法核验变压器约束（调用方需在结果中标注）。
    reserve_keep_kwh 为当前必须保留在池内的电量下限（取荷电下限与保供保留的较大者）。
    返回 (可行功率, 触发的约束说明列表)。
    """
    notes: list[str] = []
    power = float(request_kw)
    floor = max(spec.min_soc_kwh, reserve_keep_kwh)

    # 1) 功率上下限（交流侧）
    upper = spec.max_power_kw
    lower = -spec.max_power_kw
    if power > upper:
        power = upper
        notes.append("power_cap")
    if power < lower:
        power = lower
        notes.append("power_cap")

    # 2) 共享变压器：充电不能突破变压器余量
    if headroom_kw is not None and power > headroom_kw:
        power = max(0.0, min(power, headroom_kw))
        notes.append("transformer")

    # 3) 荷电上限：充电不得把池子充过当前容量
    if power > 0:
        max_charge_kw = (state.capacity_kwh - state.soc_kwh) / (
            SLOT_HOURS * spec.charge_eff
        )
        if power > max_charge_kw:
            power = max(0.0, min(power, max_charge_kw))
            notes.append("soc_max")

    # 4) 荷电下限/保供保留：放电后池内电量不得低于保留值
    if power < 0:
        usable = state.soc_kwh - floor
        max_discharge_kw = usable * spec.discharge_eff / SLOT_HOURS
        if -power > max_discharge_kw:
            power = -max(0.0, min(-power, max_discharge_kw))
            notes.append("soc_min_or_reserve")

    if math.isclose(power, 0.0, abs_tol=1e-9):
        power = 0.0
    return power, notes


def transformer_headroom(limit_kw: float, other_load_kw: float) -> float:
    """变压器余量；其他负荷本身已超限时返回 0（事实异常，由调用方记录）。"""
    return max(0.0, limit_kw - other_load_kw)
