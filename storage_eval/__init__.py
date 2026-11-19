"""储能运行价值的可执行反事实评估。

模块分工：
- datafeed：读取合成资料，区分指令、实际执行与保供覆盖，保留可知时刻。
- physics：电池物理模型（效率、功率、荷电上下限、共享变压器、衰减）。
- obligations：保供义务与优先通知的时点解析、硬约束与缺口核算。
- policy：候选策略规则，只允许使用当时可知信息。
- simulator：单情景确定性回放（策略 + 物理 + 保供）。
- backtest：同情景、同初始电量下的多策略反事实比较与费用核算。
- history：历史行动的可识别范围核算（计量缺口、联合表计）。
- identifiability：把回放结论按资料支持强度分层（可识别/部分可识别/外推）。
- horizon：评审通过后下一评估周期的可执行范围。
- review：评审档案与新运行资料核对（预报/执行/支持不足归因）。
"""

__all__ = [
    "datafeed",
    "physics",
    "obligations",
    "policy",
    "simulator",
    "backtest",
    "history",
    "identifiability",
    "horizon",
    "review",
]
