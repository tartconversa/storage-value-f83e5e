# 储能运行价值的可执行反事实评估

此目录保存用于产品开发的合成业务资料、字段契约、资料包自检工具，以及离线分析产品
`storage_eval/`。资料说明见 docs/资料口径.md，文件目录及完整性摘要见 data/catalog.json。

## 要回答的问题

园区需要知道储能在实际运行中**究竟节省多少费用**，而不是把历史操作员"预见尖峰才
充放电"的择时能力误算成电池收益。产品用同一条外部情景回放不同候选策略，比较费用
分布、月需量、循环损耗与保供缺口，并区分历史资料支持内可估计的价值与依赖外推的部分。

## 环境与资料检查

使用 Python 3.11.2，依赖在 requirements.txt 中锁定：

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack          # 资料包字段/键/引用检查
    python3 -m unittest discover -s tests -v

> 已知资料问题：当前 data/catalog.json 内登记的 sha256 与 8 个 CSV 实际摘要均不匹配
> （在初始提交中即如此，工作区未改动数据）。行数、列名、记录键与类型均与 catalog
> 声明一致，分析不受影响；正式使用前应由资料提供方重新生成摘要。

## 分析产品 storage_eval/

| 模块 | 职责 |
| --- | --- |
| datafeed | 读取资料并保留每条记录的可知时刻；空单元保持 None，指令不顶替缺测实际值 |
| physics | 效率、功率、荷电上下限、共享变压器余量、按累计放电量的容量衰减 |
| obligations | 保供义务窗口与优先通知的时点替换；未可知的义务不得用于决策 |
| policy | 候选规则转动作请求，只读取时段起点前已可知的预报，无预报则静置 |
| simulator | 单情景因果回放，动作前做保供审计与物理投影，跨月不重置电量 |
| backtest | 同情景、同初始电量的多策略费用比较，月需量按月整体取 max |
| history | 历史行动的可识别范围：计量缺口、联合表计、override 与指令偏离 |
| identifiability | 节约按资料支持强度分层：可识别 / 依赖模型 / 窗口外外推不计入 |
| horizon | 评审通过后下一周期在现状（当前电量/已发生峰值/剩余义务）下的可执行包络 |
| review | 冻结评审档案（可知信息+承诺），新运行到达后按预报/执行/支持不足归因 |

### 命令行

    python3 -m storage_eval inspect   --data data --output output   # 覆盖与可识别性体检
    python3 -m storage_eval history   --data data --output output   # 历史可识别能量与账单
    python3 -m storage_eval identifiability --data data --output output
    python3 -m storage_eval backtest  --data data --output output --save-review
    python3 -m storage_eval approve   --data data --output output \
        --review-file output/review_record.json --policy A --reason "..."
    python3 -m storage_eval horizon   --data data --output output \
        --as-of "2025-03-07T00:00:00+08:00" --soc 516.75 --slots 48
    python3 -m storage_eval reconcile --data data --output output \
        --review-file output/review_record.json
    python3 -I scripts/run_demo.py                                  # 端到端演示

## 口径保证（对应资料说明的硬要求）

1. **同情景同起点**：全部策略与无电池基线共用 other_load、价格、预报发布时刻与保供
   通知时间线，统一从 battery.initial_soc_kwh 出发，跨月不重置电量与衰减。
2. **因果纪律**：决策只通过预报的发布/可知时刻取信息；预报不可知时静置，禁止用事后
   真实负荷充当完美预测（有测试锁定）。
3. **缺口不造假**：actual_kw 缺测的 31 个时段不进入实际能量汇总，只用 command_kw
   界定功率范围并单独披露；joint 表计的 27 个时段显式标注可识别边界。
4. **月需量整体核算**：削峰可能把月峰值移到另一日，账单峰值取月内模型峰值与
   tariffs.already_peak_kw 的较大者，绝不逐日独立相加。
5. **保供是硬约束**：动作前审计池内电量是否满足义务所需（含策略余量），违约策略
   计入缺口并被排除出排名，不允许事后打提示蒙混。
6. **通知改变未来选择**：priority_notice 在可知时刻起即按新参数保留电量，生效点
   切开义务窗口；已执行的历史行动不回改。
7. **可识别与外推分离**：总表缺测月份的模型峰值标注"含估计成分"，节约结论只在
   可识别范围内承诺。

## 合成资料上的示例结论（随数据变化，以实际运行为准）

- 策略 A（阈值 0.4/0.95）在窗口内节约约 1.57 万元，全部来自电能量套利；其充电峰值
  未超过评审前已发生月峰值，需量费用不变。
- 策略 B（0.5/0.85、削峰阈值 650）虽然套利电量相近，但谷时 06:30 满功率充电叠加
  其他负荷，在三个月都制造了新月峰值（最高 838.8 kW），需量费用反增约 7365 元——
  正是"峰值跨日转移"不能按日加总的实证。
- 策略 C（放电阈值 1.10）在本资料价格分布下永不触发放电，只充电不循环，节约为负；
  规则可运行不等于历史资料证明其有效。
- 评审时点 2025-03-07 若电池只剩 516.75 kWh，低于 N1 通知要求的保留线
  625.22 kWh，horizon 包络直接判定不可放电、需优先补电。

## 测试与语法构建

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack storage_eval tests scripts

`tests/test_storage_eval.py` 锁定 28 项业务口径，包括物理投影、保供时点替换、
无预报不预知、月需量取 max、缺口不填补、违约策略被拒、可识别性分层、评审档案
往返与对账归因。
数值分析依赖已登记在 requirements.txt；运行无需访问外部业务系统。
