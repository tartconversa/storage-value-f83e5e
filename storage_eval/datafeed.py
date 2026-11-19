"""资料读取层：把合成 CSV 变成带可知时刻的内存对象。

口径原则（见 docs/资料口径.md）：
- 空单元是"未知/不适用"，不是 0；指令 command_kw 不能替代缺失的 actual_kw。
- 每条资料都带可知时刻；预报可用时刻取 issued_at 与 available_at 的较晚者。
- 只做读取与口径标注，不做业务推断、不填补缺口。
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

UTC8 = dt.timezone(dt.timedelta(hours=8))
SLOT = dt.timedelta(minutes=30)


def parse_dt(value: str) -> dt.datetime:
    """解析带时区的 ISO 时刻，缺失值返回 None。"""
    if value is None or value == "":
        return None
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError(f"时刻缺少时区：{value}")
    return stamp


def parse_num(value: str):
    """数值列解析；空单元保留 None，绝不悄悄变成 0。"""
    if value is None or value == "":
        return None
    return float(value)


@dataclass(frozen=True)
class OperationPeriod:
    """一个半小时时段的历史记录。actual/transformer 允许为 None（缺口）。"""

    period_id: str
    start: dt.datetime
    other_load_kw: float
    price: float
    command_kw: float
    actual_kw: float | None
    execution: str
    meter_scope: str  # joint | separate
    transformer_kw: float | None
    available_at: dt.datetime

    @property
    def end(self) -> dt.datetime:
        return self.start + SLOT

    @property
    def has_actual(self) -> bool:
        return self.actual_kw is not None

    @property
    def has_transformer(self) -> bool:
        return self.transformer_kw is not None


@dataclass(frozen=True)
class Forecast:
    """目标时段的预报，发布时刻决定它何时可用于决策。"""

    forecast_id: str
    target: dt.datetime
    issued_at: dt.datetime
    load_kw: float
    price: float
    available_at: dt.datetime

    @property
    def known_at(self) -> dt.datetime:
        """资料口径：available_at 早于 issued_at 时以较晚者为准。"""
        return max(self.issued_at, self.available_at)


@dataclass(frozen=True)
class StateObservation:
    """有限的实际电量校核点，带绝对误差，不预写完整轨迹。"""

    state_id: str
    period_id: str
    soc_kwh: float
    error_kwh: float
    available_at: dt.datetime


@dataclass(frozen=True)
class TariffMonth:
    month: str  # YYYY-MM
    demand_yuan_kw: float
    transformer_limit_kw: float
    already_peak_kw: float


@dataclass(frozen=True)
class ReserveObligation:
    reserve_id: str
    start: dt.datetime
    end: dt.datetime
    required_kw: float
    duration_h: float
    known_at: dt.datetime


@dataclass(frozen=True)
class PriorityNotice:
    notice_id: str
    reserve_id: str
    required_kw: float
    duration_h: float
    effective_at: dt.datetime
    known_at: dt.datetime


@dataclass(frozen=True)
class BatterySpec:
    battery_id: str
    capacity_kwh: float
    min_soc_kwh: float
    max_power_kw: float
    charge_eff: float
    discharge_eff: float
    degradation_kwh_per_mwh: float
    initial_soc_kwh: float


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    charge_below_price: float
    discharge_above_price: float
    forecast_peak_kw: float
    reserve_margin_kwh: float

    def __post_init__(self) -> None:
        if self.discharge_above_price <= self.charge_below_price:
            raise ValueError("放电阈值应高于充电阈值，否则规则自相矛盾")


@dataclass
class DataFeed:
    """全部资料的只读容器，按时间与目标时段建索引。"""

    battery: BatterySpec
    periods: list[OperationPeriod]
    forecasts: dict[dt.datetime, Forecast]
    observations: list[StateObservation]
    tariffs: dict[str, TariffMonth]
    reserves: list[ReserveObligation]
    notices: list[PriorityNotice]
    policies: dict[str, PolicySpec]
    root: Path = field(default=Path("."))

    @property
    def start(self) -> dt.datetime:
        return self.periods[0].start

    @property
    def end(self) -> dt.datetime:
        return self.periods[-1].end

    def period_at(self, start: dt.datetime) -> OperationPeriod | None:
        for period in self.periods:
            if period.start == start:
                return period
        return None

    def forecast_known_by(self, target: dt.datetime, when: dt.datetime) -> Forecast | None:
        """返回在 when 时刻已经可知的目标时段预报；未发布则 None。"""
        forecast = self.forecasts.get(target)
        if forecast is not None and forecast.known_at <= when:
            return forecast
        return None

    def month_of(self, stamp: dt.datetime) -> str:
        return stamp.strftime("%Y-%m")


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def load_feed(root: str | Path) -> DataFeed:
    """从资料目录读取全部表，并做最小结构性校验。

    root 既可以是含 data/ 的资料包根目录，也可以直接是 data 目录。
    """
    root = Path(root).resolve()
    data_dir = root / "data" if (root / "data").is_dir() else root

    battery_rows = _read_rows(data_dir / "battery.csv")
    if len(battery_rows) != 1:
        raise ValueError("battery 表应只有一块电池的契约")
    row = battery_rows[0]
    battery = BatterySpec(
        battery_id=row["battery_id"],
        capacity_kwh=float(row["capacity_kwh"]),
        min_soc_kwh=float(row["min_soc_kwh"]),
        max_power_kw=float(row["max_power_kw"]),
        charge_eff=float(row["charge_eff"]),
        discharge_eff=float(row["discharge_eff"]),
        degradation_kwh_per_mwh=float(row["degradation_kwh_per_mwh"]),
        initial_soc_kwh=float(row["initial_soc_kwh"]),
    )
    if not (0.0 < battery.charge_eff <= 1.0 and 0.0 < battery.discharge_eff <= 1.0):
        raise ValueError("效率必须在 0 到 1 之间")
    if not (battery.min_soc_kwh <= battery.initial_soc_kwh <= battery.capacity_kwh):
        raise ValueError("初始电量超出荷电上下限")

    periods: list[OperationPeriod] = []
    for row in _read_rows(data_dir / "operations.csv"):
        periods.append(
            OperationPeriod(
                period_id=row["period_id"],
                start=parse_dt(row["start"]),
                other_load_kw=float(row["other_load_kw"]),
                price=float(row["price"]),
                command_kw=float(row["command_kw"]),
                actual_kw=parse_num(row["actual_kw"]),
                execution=row["execution"],
                meter_scope=row["meter_scope"],
                transformer_kw=parse_num(row["transformer_kw"]),
                available_at=parse_dt(row["available_at"]),
            )
        )
    periods.sort(key=lambda item: item.start)
    _check_timeline(periods)

    forecasts: dict[dt.datetime, Forecast] = {}
    for row in _read_rows(data_dir / "forecasts.csv"):
        forecast = Forecast(
            forecast_id=row["forecast_id"],
            target=parse_dt(row["target"]),
            issued_at=parse_dt(row["issued_at"]),
            load_kw=float(row["load_kw"]),
            price=float(row["price"]),
            available_at=parse_dt(row["available_at"]),
        )
        if forecast.target in forecasts:
            raise ValueError(f"目标时段存在多条预报：{forecast.target}")
        if forecast.issued_at > forecast.target:
            raise ValueError("预报发布时刻晚于目标时段")
        forecasts[forecast.target] = forecast

    observations = [
        StateObservation(
            state_id=row["state_id"],
            period_id=row["period_id"],
            soc_kwh=float(row["soc_kwh"]),
            error_kwh=float(row["error_kwh"]),
            available_at=parse_dt(row["available_at"]),
        )
        for row in _read_rows(data_dir / "state_observations.csv")
    ]
    observed_ids = {period.period_id for period in periods}
    for observation in observations:
        if observation.period_id not in observed_ids:
            raise ValueError(f"电量观测引用了不存在的时段：{observation.period_id}")

    tariffs: dict[str, TariffMonth] = {}
    for row in _read_rows(data_dir / "tariffs.csv"):
        tariff = TariffMonth(
            month=row["month"],
            demand_yuan_kw=float(row["demand_yuan_kw"]),
            transformer_limit_kw=float(row["transformer_limit_kw"]),
            already_peak_kw=float(row["already_peak_kw"]),
        )
        tariffs[tariff.month] = tariff

    reserves = [
        ReserveObligation(
            reserve_id=row["reserve_id"],
            start=parse_dt(row["start"]),
            end=parse_dt(row["end"]),
            required_kw=float(row["required_kw"]),
            duration_h=float(row["duration_h"]),
            known_at=parse_dt(row["known_at"]),
        )
        for row in _read_rows(data_dir / "reserve.csv")
    ]
    notices = [
        PriorityNotice(
            notice_id=row["notice_id"],
            reserve_id=row["reserve_id"],
            required_kw=float(row["required_kw"]),
            duration_h=float(row["duration_h"]),
            effective_at=parse_dt(row["effective_at"]),
            known_at=parse_dt(row["known_at"]),
        )
        for row in _read_rows(data_dir / "priority_notice.csv")
    ]
    reserve_ids = {item.reserve_id for item in reserves}
    for notice in notices:
        if notice.reserve_id not in reserve_ids:
            raise ValueError(f"优先通知引用了不存在的保供义务：{notice.reserve_id}")
        if notice.known_at > notice.effective_at:
            raise ValueError("优先通知的可知时刻晚于生效时刻")

    policies: dict[str, PolicySpec] = {}
    for row in _read_rows(data_dir / "policies.csv"):
        spec = PolicySpec(
            policy_id=row["policy_id"],
            charge_below_price=float(row["charge_below_price"]),
            discharge_above_price=float(row["discharge_above_price"]),
            forecast_peak_kw=float(row["forecast_peak_kw"]),
            reserve_margin_kwh=float(row["reserve_margin_kwh"]),
        )
        if spec.discharge_above_price <= spec.charge_below_price:
            raise ValueError("放电阈值应高于充电阈值，否则规则自相矛盾")
        policies[spec.policy_id] = spec

    return DataFeed(
        battery=battery,
        periods=periods,
        forecasts=forecasts,
        observations=observations,
        tariffs=tariffs,
        reserves=reserves,
        notices=notices,
        policies=policies,
        root=root,
    )


def _check_timeline(periods: list[OperationPeriod]) -> None:
    """半小时连续网格、键唯一、左闭右开。"""
    seen = set()
    for index, period in enumerate(periods):
        if period.period_id in seen:
            raise ValueError(f"时段键重复：{period.period_id}")
        seen.add(period.period_id)
        if index:
            if period.start != periods[index - 1].end:
                raise ValueError(
                    f"时间网格不连续：{periods[index - 1].period_id} -> {period.period_id}"
                )
