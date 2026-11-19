"""历史运行的可识别范围核算。

资料口径要求：
- actual_kw 缺测时不能用 command_kw 假造实际电量，缺口区间只给范围不给点估。
- meter_scope=joint 时总表覆盖电池与其他负荷，电池电量不可单独识别；
  若同期 actual_kw 有独立记录则仍以 actual 为准，joint 只影响总表侧推断。
- 历史费用只在可识别时段上核算，并显式列出受缺口影响的电量与费用范围。
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from .datafeed import DataFeed, OperationPeriod

SLOT_HOURS = 0.5


@dataclasses.dataclass
class CoverageReport:
    total_slots: int
    actual_known_slots: int
    actual_missing_slots: int
    joint_meter_slots: int
    transformer_missing_slots: int
    # 缺口期间只能用指令界定的功率边界
    missing_command_charge_kwh: float
    missing_command_discharge_kwh: float
    missing_at_prices: dict[str, int]


@dataclasses.dataclass
class HistoricalEnergy:
    """仅基于实际计量的电池交换电量（交流侧 kWh，充电为正）。"""

    charge_kwh: float
    discharge_kwh: float
    charge_cost_yuan: float
    discharge_value_yuan: float
    # 联合表计且电池非零的时段：总表侧无法拆分，单列
    joint_nonzero_slots: int
    joint_energy_kwh: float
    coverage: CoverageReport


def coverage_report(feed: DataFeed) -> CoverageReport:
    periods = feed.periods
    missing_charge = missing_discharge = 0.0
    missing_prices: dict[str, int] = {}
    for period in periods:
        if not period.has_actual:
            # 缺测时段唯一可知的功率信息是指令，只能用于界定范围
            energy = period.command_kw * SLOT_HOURS
            if energy > 0:
                missing_charge += energy
            elif energy < 0:
                missing_discharge += -energy
            missing_prices[str(period.price)] = missing_prices.get(str(period.price), 0) + 1
    return CoverageReport(
        total_slots=len(periods),
        actual_known_slots=sum(1 for p in periods if p.has_actual),
        actual_missing_slots=sum(1 for p in periods if not p.has_actual),
        joint_meter_slots=sum(1 for p in periods if p.meter_scope == "joint"),
        transformer_missing_slots=sum(1 for p in periods if not p.has_transformer),
        missing_command_charge_kwh=missing_charge,
        missing_command_discharge_kwh=missing_discharge,
        missing_at_prices=dict(sorted(missing_prices.items())),
    )


def historical_energy(feed: DataFeed) -> HistoricalEnergy:
    """按实际计量汇总历史电池能量与套利毛利，拒绝以指令填补缺口。"""
    charge = discharge = 0.0
    charge_cost = discharge_value = 0.0
    joint_nonzero = 0
    joint_energy = 0.0
    for period in feed.periods:
        if not period.has_actual:
            continue
        energy = period.actual_kw * SLOT_HOURS
        if energy > 0:
            charge += energy
            charge_cost += energy * period.price
        elif energy < 0:
            discharge += -energy
            discharge_value += (-energy) * period.price
        if period.meter_scope == "joint" and abs(energy) > 1e-9:
            joint_nonzero += 1
            joint_energy += energy
    return HistoricalEnergy(
        charge_kwh=charge,
        discharge_kwh=discharge,
        charge_cost_yuan=charge_cost,
        discharge_value_yuan=discharge_value,
        joint_nonzero_slots=joint_nonzero,
        joint_energy_kwh=joint_energy,
        coverage=coverage_report(feed),
    )


@dataclasses.dataclass
class HistoricalBill:
    """历史账单的可识别部分与不确定范围。"""

    identifiable_energy_yuan: float
    # 总表缺测时段的其他负荷电费仍可识别（other_load 独立记录），
    # 但站点峰值可能落在缺测时段，故峰值只给已观测下界
    observed_peak_kw: dict[str, float]
    peak_unchecked_months: list[str]
    override_slots: int
    command_actual_deviation_slots: int


def historical_bill(feed: DataFeed) -> HistoricalBill:
    energy = 0.0
    observed_peak: dict[str, float] = {}
    unchecked_months: set[str] = set()
    overrides = deviations = 0
    for period in feed.periods:
        month = feed.month_of(period.start)
        # 电能量：其他负荷始终可识别；电池部分仅在有实际计量时计入
        battery_kw = period.actual_kw if period.has_actual else 0.0
        energy += (period.other_load_kw + battery_kw) * SLOT_HOURS * period.price
        if period.has_transformer:
            observed_peak[month] = max(observed_peak.get(month, 0.0),
                                       period.transformer_kw)
        else:
            unchecked_months.add(month)
        if period.execution == "override":
            overrides += 1
        if period.has_actual and abs(period.command_kw - period.actual_kw) > 1e-9:
            deviations += 1
    return HistoricalBill(
        identifiable_energy_yuan=energy,
        observed_peak_kw=dict(sorted(observed_peak.items())),
        peak_unchecked_months=sorted(unchecked_months),
        override_slots=overrides,
        command_actual_deviation_slots=deviations,
    )


def state_observation_check(feed: DataFeed) -> list[dict]:
    """电量观测锚点清单：实际 SOC 真值只在这些点（±error_kwh）可知。"""
    period_index = {p.period_id: p for p in feed.periods}
    out = []
    for observation in feed.observations:
        period = period_index[observation.period_id]
        out.append({
            "state_id": observation.state_id,
            "period_id": observation.period_id,
            "start": period.start.isoformat(),
            "soc_kwh": observation.soc_kwh,
            "error_kwh": observation.error_kwh,
            "known_at": observation.available_at.isoformat(),
        })
    return out
