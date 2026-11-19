"""端到端演示：在合成资料上跑完整评估链条并打印关键结论。

运行（从仓库根目录）：
    python3 -I scripts/run_demo.py

不写任何业务数据，只把各模块的输出汇总到标准输出，
便于评审会上逐步核对口径，而不是只看最终数字。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from storage_eval import backtest, history, horizon, identifiability, review
from storage_eval.datafeed import load_feed
from storage_eval.physics import BatteryState
from storage_eval.simulator import simulate

TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    feed = load_feed(ROOT / "data")

    print("=" * 72)
    print("1. 资料覆盖与可识别性")
    print("=" * 72)
    coverage = history.coverage_report(feed)
    print(f"时段总数 {coverage.total_slots}；实际计量可知 {coverage.actual_known_slots}；"
          f"缺测 {coverage.actual_missing_slots}")
    print(f"联合表计 {coverage.joint_meter_slots} 时段；总表缺测 "
          f"{coverage.transformer_missing_slots} 时段")
    print(f"缺测时段只能用指令界定范围：充电上界 "
          f"{coverage.missing_command_charge_kwh:.1f} kWh、"
          f"放电上界 {coverage.missing_command_discharge_kwh:.1f} kWh（不得当作实际电量）")

    print("\n" + "=" * 72)
    print("2. 同情景反事实比较（全部策略从同一初始电量出发，跨月不重置）")
    print("=" * 72)
    comparison = backtest.run_comparison(feed)
    base = comparison.summaries[comparison.baseline_id]
    print(f"无电池基线总费用 {base.total_yuan:,.2f} 元"
          f"（电能量 {base.energy_yuan:,.2f} + 需量 {base.demand_yuan:,.2f}）")
    for pid in comparison.ranking:
        s = comparison.summaries[pid]
        print(
            f"策略 {pid}：总费用 {s.total_yuan:,.2f}，节约 "
            f"{comparison.savings[pid]:,.2f} 元"
            f"（电能量 {comparison.energy_savings[pid]:,.2f} / "
            f"需量 {comparison.demand_savings[pid]:,.2f}），"
            f"循环 {s.equivalent_cycles:.1f} 次，衰减 {s.capacity_loss_kwh:.2f} kWh，"
            f"保供阻断 {s.reserve_blocked_slots} 时段，缺口 {s.reserve_shortfall_slots}"
        )
    if comparison.rejected:
        print("被拒绝（保供不可接受）：", comparison.rejected)

    print("\n" + "=" * 72)
    print("3. 月需量不可逐日相加的实证")
    print("=" * 72)
    for pid in comparison.ranking:
        for month, bill in comparison.summaries[pid].months.items():
            flag = "（峰值含未核验时段，仅估计）" if bill.peak_estimated else ""
            print(f"策略 {pid} {month}：模型峰值 {bill.model_peak_kw:.1f} kW，"
                  f"计费峰值 {bill.billed_peak_kw:.1f} kW，"
                  f"已发生峰值 {bill.already_peak_kw:.1f} kW{flag}")

    print("\n" + "=" * 72)
    print("3b. 节约的可识别性分层（资料直接支持 vs 依赖模型假设）")
    print("=" * 72)
    for spec in feed.policies.values():
        split = identifiability.split_identifiability(
            feed, simulate(feed, spec)
        )
        print(
            f"策略 {spec.policy_id}：可识别节约 "
            f"{split.identified_energy_delta_yuan:,.2f} 元（{split.identified_slots} 时段），"
            f"依赖模型 {split.partial_energy_delta_yuan:,.2f} 元"
            f"（{split.partial_slots} 时段），"
            f"需量完全可核验={split.demand_fully_verifiable}"
        )

    print("\n" + "=" * 72)
    print("4. 下一评估周期可执行范围（评审时点 2025-03-07，剩余 N1 义务）")
    print("=" * 72)
    as_of = dt.datetime(2025, 3, 7, 0, 0, tzinfo=TZ)
    state = BatteryState(soc_kwh=516.75, capacity_kwh=feed.battery.capacity_kwh,
                         discharged_mwh=0.0)
    outlook = horizon.build_outlook(feed, as_of, state, horizon_slots=4)
    print(f"当前电量 {outlook.soc_kwh:.2f} kWh；已发生月峰值 "
          f"{outlook.already_peak_kw:.1f} kW")
    for item in outlook.obligations_ahead:
        print(f"剩余义务 {item['reserve_id']}（{item['source']}）："
              f"{item['required_kw']:.0f} kW × {item['duration_h']:.0f} h，"
              f"池内须备 {item['battery_energy_need_kwh']:.1f} kWh")
    first = outlook.envelopes[0]
    print(f"下一时段包络：放电 {first.min_power_kw:.1f} ~ 充电 "
          f"{first.max_power_kw:.1f} kW；可行={first.feasible}；"
          f"保留线 {first.reserve_keep_kwh:.2f} kWh；提示 {first.notes}")

    print("\n" + "=" * 72)
    print("5. 评审档案与新运行对账（以排名第一策略做承诺基准）")
    print("=" * 72)
    record = review.build_review(
        feed, comparison, coverage, created_at=as_of,
        assumptions=["演示假设：预报按发布时刻可知；总表缺测不臆造；"
                     "需量按已发生峰值与模型峰值较大者计费"],
    )
    print(f"评审档案 {record.review_id}；排名 {record.ranking}；"
          f"承诺节约 { {k: round(v, 2) for k, v in record.expected_savings_yuan.items()} }")
    selected = comparison.ranking[0]
    rows = review.reconcile(feed, simulate(feed, feed.policies[selected]))
    print("对账归因计数：", review.reconcile_summary(rows))


if __name__ == "__main__":
    main()
