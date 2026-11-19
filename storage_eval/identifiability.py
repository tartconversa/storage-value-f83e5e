"""价值可识别性分层：把回放结论按资料支持强度拆开披露。

三层：
1. 可识别（identified）：总表与电池实际计量均可知时段上的费用差，资料直接支持。
2. 部分可识别（partial）：总表缺测或联合表计时段，结论依赖模型假设，给方向不给真值。
3. 外推（extrapolated）：资料窗口之外、或义务参数在评审时点尚不可知的部分，
   不在历史节约中计入，只列依赖的假设。

历史回放节约永远只在第 1、2 层上报告，并把第 2 层的金额单独标出，
避免把"规则可运行"表述成"历史资料已证明其效果"。
"""
from __future__ import annotations

import dataclasses

from .datafeed import DataFeed
from .simulator import SimulationResult, SlotRecord, simulate_no_battery

SLOT_HOURS = 0.5


@dataclasses.dataclass
class IdentifiabilitySplit:
    policy_id: str
    identified_slots: int
    partial_slots: int
    # 可识别时段上的电能量费用差（基线 - 策略），正为节约
    identified_energy_delta_yuan: float
    # 含未核验/联合表计时段的费用差，依赖模型假设
    partial_energy_delta_yuan: float
    # 月需量结论是否整体可核验（月内无总表缺测才可核验）
    demand_fully_verifiable: bool
    assumptions: list[str]


def split_identifiability(feed: DataFeed, result: SimulationResult) -> IdentifiabilitySplit:
    """按计量支持强度分层核算策略相对无电池基线的电能量费用差。"""
    baseline = simulate_no_battery(feed).record_by_period()
    period_index = {period.period_id: period for period in feed.periods}
    identified_slots = partial_slots = 0
    identified_delta = partial_delta = 0.0
    for record in result.records:
        base_record = baseline[record.period_id]
        delta = (
            (base_record.site_power_kw - record.site_power_kw)
            * SLOT_HOURS * record.actual_price
        )
        period = period_index[record.period_id]
        # 总表缺测或联合表计时，站点功率拆分依赖模型；其余为资料直接支持
        weak = record.transformer_unchecked or period.meter_scope == "joint"
        if weak:
            partial_slots += 1
            partial_delta += delta
        else:
            identified_slots += 1
            identified_delta += delta

    months = {record.month for record in result.records}
    unchecked_months = {
        record.month for record in result.records if record.transformer_unchecked
    }
    return IdentifiabilitySplit(
        policy_id=result.policy_id,
        identified_slots=identified_slots,
        partial_slots=partial_slots,
        identified_energy_delta_yuan=identified_delta,
        partial_energy_delta_yuan=partial_delta,
        demand_fully_verifiable=not unchecked_months and months != set(),
        assumptions=[
            "总表缺测时段假设其他负荷独立计量准确，电池功率取策略执行值",
            "联合表计时段电池能量以同期 actual_kw 为准，总表不做二次拆分",
            "月内存在总表缺测时，真实月峰值可能高于模型峰值，需量节约为下界",
            "资料窗口 2025-01-01 至 2025-03-07 之外的节约属于外推，未计入历史结论",
        ],
    )
