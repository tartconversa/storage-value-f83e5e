"""费用核算与同情景反事实比较。

费用两块：
1. 电能量费用：逐时段 (其他负荷 + 电池交流功率) * 0.5h * 电价。
   充电增购、放电抵减，同一公式自然体现套利价差与效率损失。
2. 月度需量费用：月内共享变压器总功率最大值 * 每千瓦月费。
   削峰只是把月峰值在日期间移动，因此必须按月整体取 max，
   评审点之前已发生的峰值 already_peak_kw 不可被回放"撤销"。

注意：总表缺测时段的站点功率是模型估计，需量结论只在可识别范围内成立。
"""
from __future__ import annotations

import dataclasses

from .datafeed import DataFeed
from .simulator import SimulationResult, SlotRecord, simulate, simulate_no_battery

SLOT_HOURS = 0.5


@dataclasses.dataclass
class MonthlyBill:
    month: str
    energy_yuan: float
    demand_yuan: float
    model_peak_kw: float
    billed_peak_kw: float
    already_peak_kw: float
    peak_estimated: bool
    total_yuan: float


@dataclasses.dataclass
class CostSummary:
    policy_id: str
    months: dict[str, MonthlyBill]
    energy_yuan: float
    demand_yuan: float
    total_yuan: float
    # 电池侧运行量
    charge_mwh: float
    discharge_mwh: float
    round_trip_efficiency: float
    equivalent_cycles: float
    capacity_loss_kwh: float
    # 保供与数据支持
    reserve_shortfall_kwh: float
    reserve_shortfall_slots: int
    reserve_blocked_slots: int
    unchecked_transformer_slots: int
    feasible: bool
    infeasible_reasons: list[str]


def monthly_bills(feed: DataFeed, result: SimulationResult) -> dict[str, MonthlyBill]:
    bills: dict[str, MonthlyBill] = {}
    by_month: dict[str, list[SlotRecord]] = {}
    for record in result.records:
        by_month.setdefault(record.month, []).append(record)

    for month, records in by_month.items():
        tariff = feed.tariffs.get(month)
        energy = sum(
            record.site_power_kw * SLOT_HOURS * record.actual_price
            for record in records
        )
        model_peak = max(record.site_power_kw for record in records)
        # 月内只要有一个时段总表缺测，真实月峰值就可能落在未核验时段，
        # 模型峰值只能作为可识别范围内的估计，不能当作已证实账单峰值。
        peak_estimated = any(record.transformer_unchecked for record in records)
        if tariff is not None:
            # 已发生峰值是历史事实：策略只能决定其后部分，账单峰值取两者较大
            billed_peak = max(model_peak, tariff.already_peak_kw)
            demand = billed_peak * tariff.demand_yuan_kw
            already = tariff.already_peak_kw
        else:
            billed_peak = model_peak
            demand = 0.0
            already = 0.0
        bills[month] = MonthlyBill(
            month=month,
            energy_yuan=energy,
            demand_yuan=demand,
            model_peak_kw=model_peak,
            billed_peak_kw=billed_peak,
            already_peak_kw=already,
            peak_estimated=peak_estimated,
            total_yuan=energy + demand,
        )
    return bills


def summarize(feed: DataFeed, result: SimulationResult) -> CostSummary:
    bills = monthly_bills(feed, result)
    charge = sum(r.grid_energy_kwh for r in result.records if r.grid_energy_kwh > 0)
    discharge = sum(-r.grid_energy_kwh for r in result.records if r.grid_energy_kwh < 0)
    rte = discharge / charge if charge > 0 else 0.0
    usable_capacity = result.final_state.capacity_kwh
    cycles = (discharge / usable_capacity) if usable_capacity > 0 else 0.0
    capacity_loss = (
        feed.battery.capacity_kwh - result.final_state.capacity_kwh
    )
    reasons: list[str] = []
    if result.reserve_shortfall_slots:
        reasons.append(
            f"保供硬约束缺口 {result.reserve_shortfall_slots} 时段，"
            f"累计欠 {result.reserve_shortfall_kwh:.1f} kWh"
        )
    return CostSummary(
        policy_id=result.policy_id,
        months=bills,
        energy_yuan=sum(b.energy_yuan for b in bills.values()),
        demand_yuan=sum(b.demand_yuan for b in bills.values()),
        total_yuan=sum(b.total_yuan for b in bills.values()),
        charge_mwh=charge / 1000.0,
        discharge_mwh=discharge / 1000.0,
        round_trip_efficiency=rte,
        equivalent_cycles=cycles,
        capacity_loss_kwh=capacity_loss,
        reserve_shortfall_kwh=result.reserve_shortfall_kwh,
        reserve_shortfall_slots=result.reserve_shortfall_slots,
        reserve_blocked_slots=result.reserve_blocked_slots,
        unchecked_transformer_slots=result.unchecked_transformer_slots,
        feasible=not reasons,
        infeasible_reasons=reasons,
    )


@dataclasses.dataclass
class PolicyComparison:
    baseline_id: str
    summaries: dict[str, CostSummary]
    savings: dict[str, float]
    energy_savings: dict[str, float]
    demand_savings: dict[str, float]
    monthly_savings: dict[str, dict[str, float]]
    ranking: list[str]
    rejected: dict[str, list[str]]


def run_comparison(feed: DataFeed) -> PolicyComparison:
    """同外部情景、同初始电量下回放全部候选策略与无电池基线。"""
    baseline_result = simulate_no_battery(feed)
    baseline = summarize(feed, baseline_result)

    summaries: dict[str, CostSummary] = {baseline.policy_id: baseline}
    results: dict[str, SimulationResult] = {baseline.policy_id: baseline_result}
    for policy_id, spec in feed.policies.items():
        result = simulate(feed, spec)  # 不注入状态 => 统一使用 battery.initial_soc
        summaries[policy_id] = summarize(feed, result)
        results[policy_id] = result

    savings, energy_savings, demand_savings = {}, {}, {}
    monthly_savings: dict[str, dict[str, float]] = {}
    rejected: dict[str, list[str]] = {}
    for policy_id, summary in summaries.items():
        if policy_id == baseline.policy_id:
            continue
        savings[policy_id] = baseline.total_yuan - summary.total_yuan
        energy_savings[policy_id] = baseline.energy_yuan - summary.energy_yuan
        demand_savings[policy_id] = baseline.demand_yuan - summary.demand_yuan
        monthly_savings[policy_id] = {
            month: baseline.months[month].total_yuan - summary.months[month].total_yuan
            for month in baseline.months
        }
        if not summary.feasible:
            rejected[policy_id] = summary.infeasible_reasons

    eligible = [pid for pid in savings if pid not in rejected]
    ranking = sorted(eligible, key=lambda pid: savings[pid], reverse=True)
    return PolicyComparison(
        baseline_id=baseline.policy_id,
        summaries=summaries,
        savings=savings,
        energy_savings=energy_savings,
        demand_savings=demand_savings,
        monthly_savings=monthly_savings,
        ranking=ranking,
        rejected=rejected,
    )
