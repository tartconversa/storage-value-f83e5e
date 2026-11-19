"""命令行入口：离线反事实评估产品。

子命令：
  inspect    资料覆盖与可识别性体检（不修改 sourcepack 的摘要检查）
  backtest   同情景多策略反事实回放，输出费用/需量/循环/保供比较
  history    历史运行的可识别能量与账单范围
  horizon    下一评估周期在现状约束下的可执行包络
  review     冻结评审档案（可知信息 + 承诺）
  reconcile  新运行资料到达后与评审承诺对账归因

用法：python -m storage_eval <子命令> --data data --output output
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from . import backtest, history, horizon, identifiability, review
from .datafeed import load_feed
from .physics import BatteryState, initial_state
from .simulator import simulate


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def cmd_inspect(args) -> None:
    feed = load_feed(args.data)
    coverage = history.coverage_report(feed)
    bill = history.historical_bill(feed)
    payload = {
        "资料窗口": [feed.start.isoformat(), feed.end.isoformat()],
        "时段总数": coverage.total_slots,
        "实际计量可知时段": coverage.actual_known_slots,
        "实际计量缺测时段": coverage.actual_missing_slots,
        "联合表计时段": coverage.joint_meter_slots,
        "总表缺测时段": coverage.transformer_missing_slots,
        "缺测时段指令侧充电上界kWh": round(coverage.missing_command_charge_kwh, 2),
        "缺测时段指令侧放电上界kWh": round(coverage.missing_command_discharge_kwh, 2),
        "缺测时段电价分布": coverage.missing_at_prices,
        "历史override时段": bill.override_slots,
        "指令与实际偏离时段": bill.command_actual_deviation_slots,
        "已观测月峰值kW": {k: round(v, 2) for k, v in bill.observed_peak_kw.items()},
        "峰值未核验月份": bill.peak_unchecked_months,
        "电量观测锚点": history.state_observation_check(feed),
    }
    _write_json(Path(args.output) / "inspect.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_history(args) -> None:
    feed = load_feed(args.data)
    energy = history.historical_energy(feed)
    bill = history.historical_bill(feed)
    payload = {
        "口径": "仅汇总 actual_kw 有计量的时段；缺测不以 command_kw 填补",
        "历史充电量kWh": round(energy.charge_kwh, 2),
        "历史放电量kWh": round(energy.discharge_kwh, 2),
        "历史充电电费元": round(energy.charge_cost_yuan, 2),
        "历史放电抵减元": round(energy.discharge_value_yuan, 2),
        "可识别套利毛利元": round(
            energy.discharge_value_yuan - energy.charge_cost_yuan, 2
        ),
        "联合表计且电池非零时段": energy.joint_nonzero_slots,
        "联合表计涉及电池电量kWh": round(energy.joint_energy_kwh, 2),
        "可识别电能量费用元": round(bill.identifiable_energy_yuan, 2),
        "已观测月峰值kW": {k: round(v, 2) for k, v in bill.observed_peak_kw.items()},
        "峰值未核验月份": bill.peak_unchecked_months,
        "说明": [
            "联合表计时段总表无法在电池与其他负荷间拆分，电池能量以同期 actual 记录为准",
            "总表缺测月份的真实月峰值可能高于已观测值，需量费用只给下界",
        ],
    }
    _write_json(Path(args.output) / "history.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_backtest(args) -> None:
    feed = load_feed(args.data)
    comparison = backtest.run_comparison(feed)
    out = Path(args.output)

    summary_rows = []
    for policy_id, summary in comparison.summaries.items():
        row = {
            "policy_id": policy_id,
            "总费用元": round(summary.total_yuan, 2),
            "电能量费用元": round(summary.energy_yuan, 2),
            "需量费用元": round(summary.demand_yuan, 2),
            "相对无电池节约元": round(comparison.savings.get(policy_id, 0.0), 2),
            "其中电能量节约元": round(comparison.energy_savings.get(policy_id, 0.0), 2),
            "其中需量节约元": round(comparison.demand_savings.get(policy_id, 0.0), 2),
            "充电MWh": round(summary.charge_mwh, 4),
            "放电MWh": round(summary.discharge_mwh, 4),
            "往返效率": round(summary.round_trip_efficiency, 4),
            "等效循环": round(summary.equivalent_cycles, 3),
            "容量衰减kWh": round(summary.capacity_loss_kwh, 4),
            "保供缺口时段": summary.reserve_shortfall_slots,
            "保供缺口累计kWh": round(summary.reserve_shortfall_kwh, 2),
            "保供阻断动作时段": summary.reserve_blocked_slots,
            "变压器未核验时段": summary.unchecked_transformer_slots,
            "是否可接受": "否" if not summary.feasible else "是",
            "不可接受原因": ";".join(summary.infeasible_reasons),
        }
        summary_rows.append(row)
    _write_csv(out / "backtest_summary.csv", summary_rows)

    monthly_rows = []
    for policy_id, summary in comparison.summaries.items():
        for month, bill in summary.months.items():
            monthly_rows.append({
                "policy_id": policy_id,
                "month": month,
                "电能量费用元": round(bill.energy_yuan, 2),
                "需量费用元": round(bill.demand_yuan, 2),
                "模型峰值kW": round(bill.model_peak_kw, 2),
                "计费峰值kW": round(bill.billed_peak_kw, 2),
                "评审前已发生峰值kW": bill.already_peak_kw,
                "峰值含估计成分": bill.peak_estimated,
                "月总费用元": round(bill.total_yuan, 2),
            })
    _write_csv(out / "backtest_monthly.csv", monthly_rows)

    payload = {
        "基线": comparison.baseline_id,
        "排名(仅保供可行策略)": comparison.ranking,
        "被拒绝策略": comparison.rejected,
        "节约元": {k: round(v, 2) for k, v in comparison.savings.items()},
        "分月节约元": {
            pid: {m: round(v, 2) for m, v in months.items()}
            for pid, months in comparison.monthly_savings.items()
        },
        "同情景保证": [
            "全部策略与基线共用同一条 other_load/价格/预报发布时刻/保供通知时间线",
            "全部策略从 battery.initial_soc_kwh 出发，跨月不重置电量与衰减",
            "决策只读取时段起点前已可知的预报，未使用任何事后真实负荷",
        ],
    }
    _write_json(out / "backtest.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.save_review:
        coverage = history.coverage_report(feed)
        record = review.build_review(
            feed, comparison, coverage,
            created_at=dt.datetime.now(dt.timezone.utc).astimezone(
                dt.timezone(dt.timedelta(hours=8))),
            assumptions=[
                "预报按资料发布时刻可知，无预报时策略静置",
                "变压器总表缺测时段不核验充电余量并显式标注",
                "月需量按 already_peak_kw 与模型峰值的较大者计费",
                "电池容量按累计放电量线性衰减，效率为常数",
            ],
        )
        record.save(out / "review_record.json")
        print(f"评审档案已保存：{out / 'review_record.json'}")


def cmd_identifiability(args) -> None:
    feed = load_feed(args.data)
    rows = []
    for policy_id, spec in feed.policies.items():
        split = identifiability.split_identifiability(
            feed, simulate(feed, spec)
        )
        rows.append({
            "policy_id": policy_id,
            "可识别时段": split.identified_slots,
            "部分可识别时段": split.partial_slots,
            "可识别电能量节约元": round(split.identified_energy_delta_yuan, 2),
            "依赖模型的节约元": round(split.partial_energy_delta_yuan, 2),
            "需量结论完全可核验": split.demand_fully_verifiable,
        })
    _write_csv(Path(args.output) / "identifiability.csv", rows)
    payload = {
        "分层": rows,
        "外推声明": (
            "资料窗口 2025-01-01 至 2025-03-07 之外、以及评审时点尚不可知的"
            "保供参数，均不计入历史节约；规则可运行不等于历史已证明其效果。"
        ),
        "假设": rows and identifiability.split_identifiability(
            feed, simulate(feed, next(iter(feed.policies.values())))
        ).assumptions,
    }
    _write_json(Path(args.output) / "identifiability.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_horizon(args) -> None:
    feed = load_feed(args.data)
    as_of = dt.datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        raise ValueError("as_of 必须带时区")
    state = BatteryState(
        soc_kwh=args.soc,
        capacity_kwh=feed.battery.capacity_kwh,
        discharged_mwh=0.0,
    )
    outlook = horizon.build_outlook(
        feed, as_of, state, horizon_slots=args.slots
    )
    rows = [
        {
            "target": item.target.isoformat(),
            "最小功率kW(放电)": round(item.min_power_kw, 2),
            "最大功率kW(充电)": round(item.max_power_kw, 2),
            "保供保留电量kWh": round(item.reserve_keep_kwh, 2),
            "义务来源": item.obligation_source or "",
            "可行": item.feasible,
            "提示": ";".join(item.notes),
        }
        for item in outlook.envelopes
    ]
    _write_csv(Path(args.output) / "horizon_envelope.csv", rows)
    payload = {
        "评审时点": outlook.as_of.isoformat(),
        "当前电量kWh": outlook.soc_kwh,
        "当前容量kWh": outlook.capacity_kwh,
        "当前月份": outlook.current_month,
        "已发生月峰值kW": outlook.already_peak_kw,
        "剩余保供义务": outlook.obligations_ahead,
        "警告": outlook.warnings,
    }
    _write_json(Path(args.output) / "horizon.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_reconcile(args) -> None:
    feed = load_feed(args.data)
    record = review.ReviewRecord.load(args.review_file)
    if record.selected_policy_id is None:
        selected = record.ranking[0] if record.ranking else None
        if selected is None:
            raise ValueError("评审档案中无可行策略，无法对账")
    else:
        selected = record.selected_policy_id
    spec = feed.policies[selected]
    planned = backtest.simulate(feed, spec)
    rows = review.reconcile(feed, planned)
    out = Path(args.output)
    _write_csv(out / "reconcile_detail.csv",
              [dataclasses_to_dict(row) for row in rows])
    payload = {
        "评审档案": record.review_id,
        "对账策略": selected,
        "归因计数": review.reconcile_summary(rows),
        "处置建议": _actions(review.reconcile_summary(rows)),
    }
    _write_json(out / "reconcile.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _actions(counts: dict[str, int]) -> dict[str, str]:
    return {
        "预报偏差": "偏差持续扩大则暂停按该预报源决策，回到保守阈值",
        "采纳偏离": "核对策略未被调度采纳的原因；若为人工保供留电则更新策略余量参数",
        "执行偏离": "核对设备未按指令执行的原因（含现场 override），必要时检修限功率逻辑",
        "估计支持不足": "补装独立表计或回填计量前，该时段不纳入节约考核",
        "一致": "维持策略",
    }


def dataclasses_to_dict(instance) -> dict:
    import dataclasses
    return dataclasses.asdict(instance)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default="data", help="资料目录")
    common.add_argument("--output", default="output", help="结果输出目录")
    parser = argparse.ArgumentParser(
        description="储能运行价值的可执行反事实评估", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect", parents=[common],
                   help="资料覆盖与可识别性体检").set_defaults(func=cmd_inspect)
    sub.add_parser("history", parents=[common],
                   help="历史运行可识别核算").set_defaults(func=cmd_history)
    sub.add_parser("identifiability", parents=[common],
                   help="节约的可识别性分层").set_defaults(func=cmd_identifiability)
    bt = sub.add_parser("backtest", parents=[common],
                        help="同情景多策略反事实比较")
    bt.add_argument("--save-review", action="store_true", help="同时冻结评审档案")
    bt.set_defaults(func=cmd_backtest)

    hz = sub.add_parser("horizon", parents=[common],
                        help="下一周期可执行包络")
    hz.add_argument("--as-of", required=True, help="评审时点 ISO8601（带时区）")
    hz.add_argument("--soc", type=float, required=True, help="当前电量 kWh")
    hz.add_argument("--slots", type=int, default=48, help="前瞻半小时时段数")
    hz.set_defaults(func=cmd_horizon)

    rc = sub.add_parser("reconcile", parents=[common],
                        help="新运行资料与评审承诺对账")
    rc.add_argument("--review-file", required=True, help="评审档案 JSON 路径")
    rc.set_defaults(func=cmd_reconcile)

    ap = sub.add_parser("approve", parents=[common],
                        help="在评审档案上记录负责人的策略选择")
    ap.add_argument("--review-file", required=True, help="评审档案 JSON 路径")
    ap.add_argument("--policy", required=True, help="被选策略 id")
    ap.add_argument("--reason", required=True, help="选择理由（留痕）")
    ap.set_defaults(func=cmd_approve)
    return parser


def cmd_approve(args) -> None:
    record = review.ReviewRecord.load(args.review_file)
    updated = review.select_policy(record, args.policy, args.reason)
    updated.save(args.review_file)
    payload = {
        "评审档案": updated.review_id,
        "已选策略": updated.selected_policy_id,
        "排名": updated.ranking,
        "被拒绝策略": updated.rejected,
        "留痕": updated.assumptions[-1],
    }
    _write_json(Path(args.output) / "approve.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
