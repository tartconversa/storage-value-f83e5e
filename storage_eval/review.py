"""评审档案与新运行资料核对。

两份产物：
1. ReviewRecord：评审时点保存的"可知信息快照 + 承诺"，进入新周期后不可改写。
2. reconcile：新实际运行到达后，把差异归因到
   - 预报偏差（决策依据与真实外部情景之差）
   - 执行偏离（指令/请求与实际计量之差）
   - 估计支持不足（计量缺口、联合表计导致无法判断）
"""
from __future__ import annotations

import dataclasses
import json
import datetime as dt
from pathlib import Path

from .backtest import CostSummary, PolicyComparison
from .datafeed import DataFeed
from .history import CoverageReport


@dataclasses.dataclass
class ReviewRecord:
    """冻结的评审档案：只记录评审时点可知的信息与策略承诺。"""

    review_id: str
    created_at: dt.datetime
    data_window: tuple[str, str]
    selected_policy_id: str | None
    ranking: list[str]
    rejected: dict[str, list[str]]
    expected_savings_yuan: dict[str, float]
    expected_monthly_savings: dict[str, dict[str, float]]
    expected_metrics: dict[str, dict]
    known_forecast_lead_hours: float
    coverage: dict
    assumptions: list[str]
    tariff_already_peak: dict[str, float]

    def to_json(self) -> str:
        payload = dataclasses.asdict(self)
        payload["data_window"] = list(self.data_window)
        payload["created_at"] = self.created_at.isoformat()
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ReviewRecord":
        payload = json.loads(text)
        payload["data_window"] = tuple(payload["data_window"])
        payload["created_at"] = dt.datetime.fromisoformat(payload["created_at"])
        return cls(**payload)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ReviewRecord":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def build_review(feed: DataFeed, comparison: PolicyComparison,
                 coverage: CoverageReport, created_at: dt.datetime,
                 assumptions: list[str]) -> ReviewRecord:
    """从一次完整比较结果冻结评审档案；选择与否由业务负责人决定，先留空。"""
    metrics = {}
    for policy_id, summary in comparison.summaries.items():
        metrics[policy_id] = _summary_metrics(summary)
    leads = {
        (f.target - f.issued_at).total_seconds() / 3600.0
        for f in feed.forecasts.values()
    }
    return ReviewRecord(
        review_id=f"review-{feed.start.strftime('%Y%m%d')}-{feed.end.strftime('%Y%m%d')}",
        created_at=created_at,
        data_window=(feed.start.isoformat(), feed.end.isoformat()),
        selected_policy_id=None,
        ranking=list(comparison.ranking),
        rejected={k: list(v) for k, v in comparison.rejected.items()},
        expected_savings_yuan=dict(comparison.savings),
        expected_monthly_savings={
            pid: dict(months) for pid, months in comparison.monthly_savings.items()
        },
        expected_metrics=metrics,
        known_forecast_lead_hours=min(leads) if leads else 0.0,
        coverage={
            "total_slots": coverage.total_slots,
            "actual_known_slots": coverage.actual_known_slots,
            "actual_missing_slots": coverage.actual_missing_slots,
            "joint_meter_slots": coverage.joint_meter_slots,
            "transformer_missing_slots": coverage.transformer_missing_slots,
            "missing_command_charge_kwh": coverage.missing_command_charge_kwh,
            "missing_command_discharge_kwh": coverage.missing_command_discharge_kwh,
        },
        assumptions=list(assumptions),
        tariff_already_peak={m: t.already_peak_kw for m, t in feed.tariffs.items()},
    )


def select_policy(record: ReviewRecord, policy_id: str,
                  reason: str) -> ReviewRecord:
    """业务负责人在评审档案上做出选择；被拒策略不可选，承诺保持冻结时口径。

    返回新档案对象（原档案不就地改写），并把选择理由追加进 assumptions 留痕。
    """
    if policy_id in record.rejected:
        raise ValueError(
            f"策略 {policy_id} 存在保供缺口，不可接受：{record.rejected[policy_id]}"
        )
    if policy_id not in record.expected_savings_yuan and policy_id != record.ranking[0]:
        if policy_id not in record.ranking:
            raise ValueError(f"策略 {policy_id} 不在可行排名内")
    return dataclasses.replace(
        record,
        selected_policy_id=policy_id,
        assumptions=list(record.assumptions)
        + [f"负责人选择 {policy_id}：{reason}"],
    )


def _summary_metrics(summary: CostSummary) -> dict:
    return {
        "total_yuan": round(summary.total_yuan, 2),
        "energy_yuan": round(summary.energy_yuan, 2),
        "demand_yuan": round(summary.demand_yuan, 2),
        "charge_mwh": round(summary.charge_mwh, 4),
        "discharge_mwh": round(summary.discharge_mwh, 4),
        "round_trip_efficiency": round(summary.round_trip_efficiency, 4),
        "equivalent_cycles": round(summary.equivalent_cycles, 3),
        "capacity_loss_kwh": round(summary.capacity_loss_kwh, 4),
        "reserve_shortfall_slots": summary.reserve_shortfall_slots,
        "reserve_blocked_slots": summary.reserve_blocked_slots,
        "unchecked_transformer_slots": summary.unchecked_transformer_slots,
    }


@dataclasses.dataclass
class SlotReconciliation:
    period_id: str
    start: str
    forecast_load_error_kw: float | None
    forecast_price_error_yuan: float | None
    requested_kw: float | None
    command_kw: float | None
    actual_kw: float | None
    # 策略请求 vs 现场指令：策略是否被采纳
    adoption_gap_kw: float | None
    # 现场指令 vs 实际计量：指令是否被执行
    execution_gap_kw: float | None
    attribution: str
    detail: str


def reconcile(feed: DataFeed, planned) -> list[SlotReconciliation]:
    """把新周期的实际运行与评审承诺逐时段对账。

    planned: simulate() 产出的 SimulationResult（策略当时会下达的请求）。
    两层偏离分开记账：
    - 采纳偏离：现场指令 command_kw 与策略请求不一致（策略没被调度采纳）；
    - 执行偏离：实际计量 actual_kw 与现场指令不一致（指令没被设备执行，含 override）。
    归因优先级：计量支持不足 > 执行偏离 > 采纳偏离 > 预报偏差 > 一致。
    """
    planned_index = planned.record_by_period()
    rows: list[SlotReconciliation] = []
    for period in feed.periods:
        record = planned_index.get(period.period_id)
        forecast = feed.forecasts.get(period.start)
        fc_load_err = fc_price_err = None
        if forecast is not None and forecast.known_at <= period.start:
            fc_load_err = forecast.load_kw - period.other_load_kw
            fc_price_err = forecast.price - period.price
        requested = record.executed_kw if record else None
        adoption_gap = (
            period.command_kw - requested if requested is not None else None
        )

        if not period.has_actual:
            attribution = "估计支持不足"
            detail = "actual_kw 缺测，无法核对实际电量；指令不得替代计量"
            execution_gap = None
            actual = None
        else:
            actual = period.actual_kw
            execution_gap = actual - period.command_kw
            if abs(execution_gap) > 1.0:
                attribution = "执行偏离"
                detail = (
                    f"实际与现场指令相差 {execution_gap:.2f} kW（容差 1 kW）"
                    + ("（现场 override）" if period.execution == "override" else "")
                )
            elif adoption_gap is not None and abs(adoption_gap) > 1.0:
                attribution = "采纳偏离"
                detail = (
                    f"现场指令与策略请求相差 {adoption_gap:.2f} kW，"
                    "策略未被调度采纳（可能为人工保供留电）"
                )
            elif fc_load_err is not None and abs(fc_load_err) > 30.0:
                attribution = "预报偏差"
                detail = f"负荷预报误差 {fc_load_err:.2f} kW 超过 30 kW 经验带"
            elif fc_price_err is not None and abs(fc_price_err) > 1e-9:
                attribution = "预报偏差"
                detail = f"电价预报误差 {fc_price_err:.3f} 元/kWh"
            else:
                attribution = "一致"
                detail = "请求、指令、执行与预报均在容差内"
        rows.append(
            SlotReconciliation(
                period_id=period.period_id,
                start=period.start.isoformat(),
                forecast_load_error_kw=(
                    round(fc_load_err, 3) if fc_load_err is not None else None
                ),
                forecast_price_error_yuan=(
                    round(fc_price_err, 4) if fc_price_err is not None else None
                ),
                requested_kw=requested,
                command_kw=period.command_kw,
                actual_kw=actual,
                adoption_gap_kw=(
                    round(adoption_gap, 3) if adoption_gap is not None else None
                ),
                execution_gap_kw=(
                    round(execution_gap, 3) if execution_gap is not None else None
                ),
                attribution=attribution,
                detail=detail,
            )
        )
    return rows


def reconcile_summary(rows: list[SlotReconciliation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.attribution] = counts.get(row.attribution, 0) + 1
    return dict(sorted(counts.items()))
