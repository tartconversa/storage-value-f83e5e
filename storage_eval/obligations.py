"""保供义务与优先通知的时点解析。

口径：
- reserve 给出窗口 [start, end) 内需要可连续供应 required_kw 持续 duration_h。
- priority_notice 在 effective_at 起替换被引用义务的功率/时长，且不改变已执行的历史。
- 任何义务只在 known_at 之后可用于决策；通知在 known_at 之后、生效之前就应改变充电选择。
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from .datafeed import DataFeed, PriorityNotice, ReserveObligation


@dataclasses.dataclass(frozen=True)
class EffectiveObligation:
    """某一时点实际生效的保供要求（已套用优先通知替换）。"""

    reserve_id: str
    required_kw: float
    duration_h: float
    window_start: dt.datetime
    window_end: dt.datetime
    known_at: dt.datetime
    source: str  # reserve | notice:<notice_id>


def build_timeline(feed: DataFeed) -> list[EffectiveObligation]:
    """把义务与通知展开成按窗口排列的生效要求。

    通知只替换 effective_at 之后的部分：原义务窗口被切成
    [原 start, effective_at) 与 [effective_at, 原 end) 两段。
    """
    timeline: list[EffectiveObligation] = []
    notices_by_reserve: dict[str, list[PriorityNotice]] = {}
    for notice in feed.notices:
        notices_by_reserve.setdefault(notice.reserve_id, []).append(notice)

    for obligation in feed.reserves:
        pending = sorted(notices_by_reserve.get(obligation.reserve_id, []),
                         key=lambda item: item.effective_at)
        cursor = obligation.start
        current = EffectiveObligation(
            reserve_id=obligation.reserve_id,
            required_kw=obligation.required_kw,
            duration_h=obligation.duration_h,
            window_start=obligation.start,
            window_end=obligation.end,
            known_at=obligation.known_at,
            source="reserve",
        )
        for notice in pending:
            if notice.effective_at <= cursor:
                # 通知生效点早于当前段起点（理论上不会发生，防御处理）
                current = dataclasses.replace(
                    current,
                    required_kw=notice.required_kw,
                    duration_h=notice.duration_h,
                    known_at=min(current.known_at, notice.known_at),
                    source=f"notice:{notice.notice_id}",
                )
                continue
            if notice.effective_at >= obligation.end:
                continue
            # 闭合通知前的旧段
            timeline.append(dataclasses.replace(current, window_end=notice.effective_at))
            cursor = notice.effective_at
            current = EffectiveObligation(
                reserve_id=obligation.reserve_id,
                required_kw=notice.required_kw,
                duration_h=notice.duration_h,
                window_start=notice.effective_at,
                window_end=obligation.end,
                # 新要求在通知可知时刻即可用于决策
                known_at=notice.known_at,
                source=f"notice:{notice.notice_id}",
            )
        timeline.append(current)
    timeline.sort(key=lambda item: item.window_start)
    return timeline


def active_obligation(timeline: list[EffectiveObligation],
                      stamp: dt.datetime) -> EffectiveObligation | None:
    """stamp 所处窗口内生效的义务；窗口外返回 None。"""
    for item in timeline:
        if item.window_start <= stamp < item.window_end:
            return item
        if item.window_start > stamp:
            break
    return None


def known_obligation(timeline: list[EffectiveObligation],
                     stamp: dt.datetime,
                     now: dt.datetime) -> EffectiveObligation | None:
    """决策时刻 now 已经可知、且覆盖 stamp 的义务。

    用于区分：义务已存在但尚未通知时，策略不允许"预知"；
    通知在生效前已知，则从可知时刻起就按新参数保留电量。
    """
    for item in timeline:
        if item.known_at <= now and item.window_start <= stamp < item.window_end:
            return item
    return None
