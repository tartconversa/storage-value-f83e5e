"""反事实评估产品的业务口径测试。

覆盖：资料读取口径、电池物理投影、保供时点替换、策略因果纪律、
同情景回放、月需量整体核算、计量缺口不填补、评审档案冻结与对账归因。
"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from storage_eval import backtest, history, horizon, identifiability, obligations, physics, review
from storage_eval.datafeed import (
    BatterySpec,
    DataFeed,
    PolicySpec,
    load_feed,
)
from storage_eval.physics import BatteryState, clip_power, initial_state
from storage_eval.policy import decide, make_context
from storage_eval.simulator import simulate, simulate_no_battery

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))


def feed():
    return load_feed(ROOT / "data")


class DataFeedTests(unittest.TestCase):
    def test_missing_values_stay_none(self):
        """空单元是未知，不是 0；指令不能顶替实际计量。"""
        data = feed()
        p0 = data.period_at(dt.datetime(2025, 1, 1, 0, 0, tzinfo=TZ))
        self.assertIsNone(p0.actual_kw)
        self.assertEqual(p0.command_kw, 80.0)
        self.assertFalse(p0.has_actual)

    def test_timeline_continuous_and_forecast_lead(self):
        data = feed()
        self.assertEqual(len(data.periods), 3120)
        for target, forecast in data.forecasts.items():
            self.assertLessEqual(forecast.known_at, target)
            self.assertEqual(
                (target - forecast.issued_at), dt.timedelta(hours=3)
            )

    def test_policy_threshold_validation(self):
        with self.assertRaisesRegex(ValueError, "放电阈值"):
            PolicySpec("X", 1.0, 0.5, 700, 0)


class PhysicsTests(unittest.TestCase):
    def setUp(self):
        self.spec = BatterySpec("B", 800, 80, 220, 0.94, 0.92, 0.04, 420)

    def test_charge_efficiency_and_soc_max(self):
        state = BatteryState(790, 800, 0)
        # 只剩 10 kWh 池容：最大充电 10/(0.5*0.94)=21.28 kW
        power, notes = clip_power(self.spec, state, 220, headroom_kw=500,
                                  reserve_keep_kwh=80)
        self.assertIn("soc_max", notes)
        nxt = physics.apply_power(self.spec, state, power)
        self.assertAlmostEqual(nxt.soc_kwh, 800, places=6)

    def test_reserve_floor_blocks_discharge(self):
        state = BatteryState(300, 800, 0)
        keep = 280  # 仅 20 kWh 可放 -> 36.8 kW
        power, notes = clip_power(self.spec, state, -220, headroom_kw=None,
                                  reserve_keep_kwh=keep)
        self.assertIn("soc_min_or_reserve", notes)
        self.assertAlmostEqual(power, -36.8, places=4)
        nxt = physics.apply_power(self.spec, state, power)
        self.assertAlmostEqual(nxt.soc_kwh, 280, places=6)

    def test_transformer_headroom_caps_charge(self):
        state = BatteryState(400, 800, 0)
        power, notes = clip_power(self.spec, state, 220, headroom_kw=60,
                                  reserve_keep_kwh=80)
        self.assertIn("transformer", notes)
        self.assertEqual(power, 60.0)

    def test_degradation_tracks_discharge_only(self):
        state = initial_state(self.spec)
        nxt = physics.apply_power(self.spec, state, -100)  # 放 50 kWh 交流
        self.assertAlmostEqual(nxt.discharged_mwh, 50 / 0.92 / 1000, places=8)
        self.assertAlmostEqual(
            nxt.capacity_kwh,
            800 - 0.04 * (50 / 0.92 / 1000),
            places=8,
        )


class ObligationTests(unittest.TestCase):
    def test_notice_splits_window_and_changes_parameters(self):
        data = feed()
        timeline = obligations.build_timeline(data)
        sources = [(item.source, item.required_kw, item.duration_h)
                   for item in timeline]
        self.assertIn(("reserve", 80.0, 3.0), sources)
        self.assertIn(("notice:N1", 130.0, 4.0), sources)
        # 通知段从 2025-02-10T08:00 起
        notice_seg = next(s for s in timeline if s.source == "notice:N1")
        self.assertEqual(
            notice_seg.window_start,
            dt.datetime(2025, 2, 10, 8, 0, tzinfo=TZ),
        )

    def test_obligation_not_known_before_known_at(self):
        data = feed()
        timeline = obligations.build_timeline(data)
        # R1 在 2025-01-05T08:00 才可知
        early = dt.datetime(2025, 1, 4, 0, 0, tzinfo=TZ)
        target = dt.datetime(2025, 1, 7, 0, 0, tzinfo=TZ)
        self.assertIsNone(obligations.known_obligation(timeline, target, early))
        known = obligations.known_obligation(
            timeline, target, dt.datetime(2025, 1, 6, 0, 0, tzinfo=TZ)
        )
        self.assertIsNotNone(known)
        self.assertEqual(known.required_kw, 80.0)

    def test_notice_known_before_effective_changes_choice(self):
        """通知在生效前一天已知：生效前的决策就应按新参数保留电量。"""
        data = feed()
        timeline = obligations.build_timeline(data)
        target = dt.datetime(2025, 2, 10, 8, 0, tzinfo=TZ)
        now = dt.datetime(2025, 2, 9, 12, 0, tzinfo=TZ)
        item = obligations.known_obligation(timeline, target, now)
        self.assertEqual(item.source, "notice:N1")
        self.assertEqual(item.required_kw, 130.0)


class PolicyCausalityTests(unittest.TestCase):
    def test_no_forecast_means_hold_not_perfect_foresight(self):
        data = feed()
        timeline = obligations.build_timeline(data)
        # 决策时刻早于所有预报发布（提前 3 小时），预报不可知
        target = dt.datetime(2025, 1, 10, 0, 0, tzinfo=TZ)
        now = target - dt.timedelta(hours=4)
        state = initial_state(data.battery)
        ctx = make_context(data, timeline, target, now, state)
        self.assertIsNone(ctx.forecast)
        decision = decide(data.policies["A"], data, ctx, state)
        self.assertEqual(decision.request_kw, 0.0)
        self.assertEqual(decision.reason, "no_known_forecast")

    def test_valley_charge_and_peak_discharge_use_forecast(self):
        data = feed()
        timeline = obligations.build_timeline(data)
        state = BatteryState(500, 800, 0)
        # 谷时 01:00、预报电价 0.35 < 充电阈值 0.4
        target = dt.datetime(2025, 1, 10, 1, 0, tzinfo=TZ)
        ctx = make_context(data, timeline, target, target, state)
        d_charge = decide(data.policies["A"], data, ctx, state)
        self.assertEqual(d_charge.reason, "price_charge")
        self.assertGreater(d_charge.request_kw, 0)
        # 尖峰 17:00、预报电价 1.05 >= 0.95
        target2 = dt.datetime(2025, 1, 10, 17, 0, tzinfo=TZ)
        ctx2 = make_context(data, timeline, target2, target2, state)
        d_dis = decide(data.policies["A"], data, ctx2, state)
        self.assertEqual(d_dis.reason, "price_discharge")
        self.assertLess(d_dis.request_kw, 0)

    def test_reserve_blocks_discharge_when_soc_insufficient(self):
        data = feed()
        timeline = obligations.build_timeline(data)
        # N1 窗口内：需保留 565.22 + margin20 = 585.22；电量仅略高时放电被收浅
        target = dt.datetime(2025, 2, 12, 17, 0, tzinfo=TZ)
        state = BatteryState(590, 800, 0)
        ctx = make_context(data, timeline, target, target, state)
        decision = decide(data.policies["A"], data, ctx, state)
        self.assertIn("reserve_limited", decision.reason)
        self.assertGreater(decision.request_kw, -data.battery.max_power_kw)
        # 电量低于保留线时完全禁止放电
        low = BatteryState(580, 800, 0)
        blocked = decide(data.policies["A"], data,
                         make_context(data, timeline, target, target, low), low)
        self.assertEqual(blocked.request_kw, 0.0)
        self.assertTrue(blocked.reason.startswith("blocked_by_reserve"))


class SimulationTests(unittest.TestCase):
    def test_all_policies_same_initial_state_and_continuous_trajectory(self):
        """跨月不重置：各策略初始电量一致，轨迹始终在物理边界内。"""
        data = feed()
        baseline = simulate_no_battery(data)
        for spec in data.policies.values():
            result = simulate(data, spec)
            self.assertEqual(
                result.initial_state.soc_kwh, data.battery.initial_soc_kwh
            )
            self.assertEqual(
                result.initial_state.soc_kwh, baseline.initial_state.soc_kwh
            )
            for record in result.records:
                self.assertGreaterEqual(record.soc_after,
                                        data.battery.min_soc_kwh - 1e-6)
                self.assertLessEqual(record.soc_after,
                                     record.capacity_kwh + 1e-6)
                self.assertLessEqual(abs(record.executed_kw),
                                     data.battery.max_power_kw + 1e-9)

    def test_reserve_shortfall_detected_with_impossible_policy(self):
        """构造一个余量超过容量的策略，回放必须报保供缺口并判为不可接受。"""
        data = feed()
        impossible = PolicySpec("HARD", 0.4, 0.95, 700, reserve_margin_kwh=400)
        result = simulate(data, impossible)
        self.assertGreater(result.reserve_shortfall_slots, 0)
        summary = backtest.summarize(data, result)
        self.assertFalse(summary.feasible)
        self.assertTrue(summary.infeasible_reasons)

    def test_demand_charge_uses_monthly_max_not_daily_sum(self):
        data = feed()
        comparison = backtest.run_comparison(data)
        for policy_id, summary in comparison.summaries.items():
            for month, bill in summary.months.items():
                tariff = data.tariffs[month]
                # 计费峰值不低于评审前已发生峰值，且是月内取 max 的结果
                self.assertGreaterEqual(
                    bill.billed_peak_kw, tariff.already_peak_kw - 1e-9
                )
                self.assertGreaterEqual(
                    bill.billed_peak_kw, bill.model_peak_kw - 1e-9
                )
                self.assertAlmostEqual(
                    bill.demand_yuan,
                    bill.billed_peak_kw * tariff.demand_yuan_kw,
                    places=4,
                )

    def test_policy_b_creates_new_monthly_peak_but_stays_feasible(self):
        """B 的谷时充电叠加其他负荷形成新月峰值：需量费用可能反升。"""
        data = feed()
        result = simulate(data, data.policies["B"])
        summary = backtest.summarize(data, result)
        self.assertTrue(summary.feasible)
        jan = summary.months["2025-01"]
        self.assertGreater(jan.billed_peak_kw, data.tariffs["2025-01"].already_peak_kw)

    def test_no_policy_uses_actual_load_as_forecast(self):
        """回放记录里的决策依据只来自预报列，绝不等于真实负荷列。"""
        data = feed()
        result = simulate(data, data.policies["A"])
        actual = {p.period_id: p.other_load_kw for p in data.periods}
        for record in result.records:
            if record.forecast_load_kw is not None:
                self.assertNotAlmostEqual(
                    record.forecast_load_kw, actual[record.period_id], places=2
                )


class HistoryCoverageTests(unittest.TestCase):
    def test_missing_actual_not_filled_by_command(self):
        data = feed()
        energy = history.historical_energy(data)
        # 31 个缺测时段不进入任何实际能量汇总
        known_slots = energy.coverage.actual_known_slots
        self.assertEqual(known_slots, 3120 - 31)
        # 缺测时段的指令功率只用于范围上界，且被单独披露
        self.assertGreater(energy.coverage.missing_command_charge_kwh, 0)

    def test_joint_meter_scope_is_disclosed(self):
        data = feed()
        energy = history.historical_energy(data)
        self.assertEqual(energy.coverage.joint_meter_slots, 27)
        self.assertGreaterEqual(energy.joint_nonzero_slots, 1)


class IdentifiabilityTests(unittest.TestCase):
    def test_savings_split_into_identified_and_model_dependent(self):
        data = feed()
        for spec in data.policies.values():
            split = identifiability.split_identifiability(
                data, simulate(data, spec)
            )
            # 每个时段都必须归入某一层，两层之和等于总时段
            self.assertEqual(
                split.identified_slots + split.partial_slots, len(data.periods)
            )
            # 本资料每月都有总表缺测，需量结论不可能整体可核验
            self.assertFalse(split.demand_fully_verifiable)
            self.assertTrue(split.assumptions)

    def test_identified_plus_partial_equals_total_energy_delta(self):
        data = feed()
        comparison = backtest.run_comparison(data)
        for spec in data.policies.values():
            split = identifiability.split_identifiability(
                data, simulate(data, spec)
            )
            total = split.identified_energy_delta_yuan + split.partial_energy_delta_yuan
            self.assertAlmostEqual(
                total, comparison.energy_savings[spec.policy_id], places=4
            )


class HorizonTests(unittest.TestCase):
    def test_envelope_respects_reserve_and_flags_unchecked_meter(self):
        data = feed()
        as_of = dt.datetime(2025, 3, 7, 0, 0, tzinfo=TZ)
        # 电量低于 N1 保留线（565.22 + 最严 margin 60 = 625.22）
        state = BatteryState(516.75, 800, 0)
        outlook = horizon.build_outlook(data, as_of, state, horizon_slots=4)
        self.assertTrue(outlook.obligations_ahead)
        for envelope in outlook.envelopes:
            self.assertFalse(envelope.feasible)
            self.assertEqual(envelope.min_power_kw, 0.0)
            self.assertTrue(any("未核验" in n for n in envelope.notes))

    def test_envelope_allows_discharge_when_soc_above_keep(self):
        data = feed()
        as_of = dt.datetime(2025, 3, 7, 0, 0, tzinfo=TZ)
        state = BatteryState(750, 800, 0)
        outlook = horizon.build_outlook(data, as_of, state, horizon_slots=2)
        envelope = outlook.envelopes[0]
        self.assertTrue(envelope.feasible)
        self.assertLess(envelope.min_power_kw, 0)


class ReviewTests(unittest.TestCase):
    def test_review_record_freezes_and_roundtrips(self):
        data = feed()
        comparison = backtest.run_comparison(data)
        coverage = history.coverage_report(data)
        record = review.build_review(
            data, comparison, coverage,
            created_at=dt.datetime(2025, 3, 7, tzinfo=TZ),
            assumptions=["测试假设"],
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "review.json"
            record.save(path)
            loaded = review.ReviewRecord.load(path)
        self.assertEqual(loaded.ranking, record.ranking)
        self.assertEqual(loaded.expected_savings_yuan,
                         record.expected_savings_yuan)
        self.assertIsNone(loaded.selected_policy_id)

    def test_reconcile_attributes_missing_meter_as_support_gap(self):
        data = feed()
        planned = simulate(data, data.policies["A"])
        rows = review.reconcile(data, planned)
        by_id = {row.period_id: row for row in rows}
        self.assertEqual(by_id["P0"].attribution, "估计支持不足")
        self.assertIsNone(by_id["P0"].actual_kw)
        counts = review.reconcile_summary(rows)
        self.assertEqual(counts["估计支持不足"], 31)

    def test_execution_gap_matches_data_deviation_count(self):
        """执行偏离时段数应等于资料里 command != actual 的 62 个时段。"""
        data = feed()
        rows = review.reconcile(data, simulate(data, data.policies["A"]))
        counts = review.reconcile_summary(rows)
        self.assertEqual(counts["执行偏离"], 62)

    def test_select_policy_rejects_infeasible_and_marks_choice(self):
        data = feed()
        comparison = backtest.run_comparison(data)
        coverage = history.coverage_report(data)
        record = review.build_review(
            data, comparison, coverage,
            created_at=dt.datetime(2025, 3, 7, tzinfo=TZ), assumptions=[],
        )
        chosen = review.select_policy(record, "A", "套利节约最高且不新增需量")
        self.assertEqual(chosen.selected_policy_id, "A")
        self.assertIsNone(record.selected_policy_id)  # 原档案不被改写
        # 构造违约策略后，选择必须被拒绝
        data.policies["BAD"] = PolicySpec("BAD", 0.4, 0.95, 700, 400.0)
        bad_cmp = backtest.run_comparison(data)
        bad_record = review.build_review(
            data, bad_cmp, coverage,
            created_at=dt.datetime(2025, 3, 7, tzinfo=TZ), assumptions=[],
        )
        with self.assertRaisesRegex(ValueError, "不可接受"):
            review.select_policy(bad_record, "BAD", "尝试违约")


if __name__ == "__main__":
    unittest.main()
