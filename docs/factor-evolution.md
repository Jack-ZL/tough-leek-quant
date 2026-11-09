# 公式进化 (GP)

本文说明 P2-22a「公式进化」的算法口径、训练/保留纪律、幸存者裁决、发布与下游验证、任务隔离和 API 契约。

## 这是什么

在 DSL 公式空间里跑**遗传算法 (Genetic Programming)**："养蛊"式地随机生成一批候选公式，用训练窗的截面 IC 适应度筛选，再通过交叉/变异繁殖下一代，逐代进化。最后用一段**从未参与进化的保留窗 (holdout)** 对幸存者做一次性统计裁决，通过者发布为草稿因子。

它**不部署任何本地大模型，纯本地算力**。公式的生成、变异、评估全部是确定性的程序化操作，只依赖已有的日线 enriched 行情面板。可选开启 **AI 提议通道（22b，已落地）**：调用系统已配置的 LLM 提议一批 DSL 公式种子注入初始种群，与本地 GP 互补；统计纪律与裁决口径完全不变，详见下文专节。

## 功能边界

V1 完成以下闭环：

1. 从 12 条手工种子公式 + 随机树构成初始种群（开启 AI 提议时，通过编译闸的 LLM 提案一并注入种群前部）。
2. 在**训练窗**逐日计算每个公式的截面 Rank IC，以 `|IR| × 覆盖率` 为适应度。
3. 精英保留 + 交叉/变异/随机补充繁殖下一代，按归一化键去重。
4. 逐代记录 best/mean 适应度与累计试验次数。
5. 取训练适应度 Top 24 为候选，在**保留窗**做相关性剪枝（贪心，阈值 0.8）与统计裁决，最多 8 个幸存者。
6. 幸存者计算保留窗 IC、Newey-West t、BH-FDR q 值、DSR（按全部试验次数折减）。
7. 运行、事件、结果持久化，刷新后可按 run ID 重连。
8. 幸存者一键发布为 `uf_evo_*` 自定义因子草稿。

V1 **不做**：不生成任意代码（只在固定 DSL 词表内组合）；不使用扩展数据或分钟数据；不在完整样本上选择后报告同一段表现（严格训练/保留分离）；不自动发布策略（只产出草稿因子，深度验证交给既有挖掘漏斗）；不使用嵌套 walk-forward 回测撮合（V1 是 IC 统计初筛，非可执行策略回测）。

因子词表以 `app/factors/evolution.py` 为准：行情列 `close/open/high/low/volume/amount/turnover_rate`；一元算子 `log/abs/sign/sqrt/-`；中缀二元 `+ - * /`；调用型 `min/max`；时序算子 `ts_mean/ts_std/ts_sum/ts_max/ts_min/ts_delay/ts_delta/ts_rank/ts_zscore/decay_linear/ts_ema/ts_corr/ts_cov/ts_quantile`；截面算子 `rank/zscore`；窗口 `{2,3,5,10,20,30,60}`。所有公式受既有 DSL 语义校验约束（窗口下限、常量范围、AST 深度上限），非法子代记为 invalid 并隔离，不中断整轮进化。

## 防止未来数据

进化最容易犯的错是"用答案选公式，再报告这段表现"。V1 用三条纪律杜绝：

- **训练/保留分离**：区间按时间切成训练窗（前 75%）与保留窗（后 25%，至少 20 个交易日）。适应度、方向、繁殖**只看训练窗**；保留窗在整轮进化结束后**只用一次**做裁决。`panel` 结果里 `train_end < holdout_start`，两窗交易日数之和等于总交易日数。
- **标签是唯一的"未来"**：`_fwd = close.shift(-horizon)/close - 1` 是预测目标（未来 horizon 日收益），**只用于计算 IC 标签，绝不作为因子输入**。因子本身由 DSL 时序算子构造，只回看历史窗口，不含未来信息。
- **方向来自训练窗**：幸存者的多空方向由**训练窗** IC 均值符号决定（`ic_mean ≥ 0` 为正向/high，否则反向/low），再把这个方向套到保留窗 IC 上算 t/q/DSR。方向不是在保留窗上挑出来的。

## 适应度与裁决口径

### 训练适应度

对每个公式，在训练窗逐日计算因子值与 `_fwd` 的截面 Spearman 秩相关（Rank IC），得到一条 IC 时间序列：

- `ic_mean` = IC 序列均值；`ic_std` = 标准差；`IR = ic_mean / ic_std`（IC 信息比率）。
- `coverage` = 因子有限值占比（衡量可物化程度）。
- **适应度 `fitness = |IR| × coverage`**；有效 IC 交易日不足 20 天（`MIN_IC_DAYS`）记为退化，适应度 0，自然淘汰。

### 幸存者裁决

1. **候选**：训练适应度 Top `MAX_SURVIVORS × 3 = 24`。
2. **相关性剪枝**：在保留窗物化每个候选的因子列，复用挖掘的 pairwise Spearman 秩相关口径；按适应度贪心保留，与任一已保留幸存者 `|相关| > 0.8` 即剪枝（记录相关值），最多保留 8 个。
3. **逐幸存者统计**（保留窗、方向已校正）：
   - `ic_mean / ir / coverage / n_days`；
   - `t_nw` = Newey-West t（滞后阶 = horizon），需 `n_days > horizon + 2`，否则 `null`；
   - `p_value` = 正态双侧 p；
   - `q_value` = 对**所有幸存者** p 值做 BH-FDR 校正；
   - `dsr` = Deflated Sharpe Ratio（见下）。
4. 缺失指标一律 `null`，不显示为 0。

### 试验账本与 DSR

多重检验校正是"养蛊"诚实性的核心：搜了成千上万个公式，总有几个碰巧好看。V1 维护**试验账本**——凡是真正跑过数据的去重公式都计入 `trials.evaluated`（重复键计入 `duplicates`，编译/物化失败计入 `invalid`）。

- `expected_max_sharpe(trials, var_sharpes)`：在 `trials` 次独立尝试下，纯运气能达到的期望最大 Sharpe；`var_sharpes` 用**全体试验**训练窗 IR 的方差。
- `dsr = deflated_sharpe_psr(|保留窗方向 IR|, n_days, skew, kurt, expected_max_sharpe)`：把观测到的保留窗 IR 相对"运气期望最大值"折减后的显著性概率，需 `n_days ≥ 5`。

**口径说明**：这里的 Sharpe 是 **IC 序列的 Sharpe 近似**（IC-IR），不是可执行策略的组合回测 Sharpe。V1 定位是**统计初筛**——快速从海量公式里挑出"保留窗仍显著、且相对搜索规模不算运气"的候选。深度验证（撮合、成本、涨跌停约束、嵌套样本外）由发布后的既有挖掘/自动挖掘漏斗完成。

## 发布与下游验证

幸存者通过 `POST /api/evolution/runs/{run_id}/survivors/{index}/publish` 发布为自定义因子草稿：

- 因子 ID = `uf_evo_` + `sha1(归一化键)[:12]`，**同公式幂等**（已存在返回 409）。用序号而非公式文本定位，避免 URL 编码问题。
- 定义与手工创建的自定义因子同构（`kind=custom`、`lang=dsl`、`group=进化`、`status=draft`、方向取自幸存者），落盘前经 `to_spec` 做 schema/ID/编译校验（fail-closed）。
- 发布只产出**草稿因子**，不自动激活、不自动发布策略。用户可在因子库看到 `uf_evo_*`，再走既有挖掘/自动挖掘做深度回测验证。

发布不代表样本外表现会在未来持续；它只是把"统计初筛幸存者"变成可被下游漏斗检验的候选。

## 任务与资源隔离

进化复用挖掘的任务编排（`MiningJobManager`，`kind="evolution"`，独立 `namespace="evolution"`），进程模型、取消、恢复与挖掘/守护完全一致：

- 通过 `spawn` 子进程执行，不在 FastAPI 请求线程或浏览器连接生命周期内运行。浏览器断开不取消任务；刷新后可按持久 run ID 重连。
- 取消请求进入 `cancelling`，运行时在每代与每 25 次评估检查取消位，抛 `EvolutionRuntimeCancelledError`，由 manager 落为 `cancelled` 终态。
- 与挖掘/守护**共用 heavy 槽**：全市场面板任务全局串行，一个进化任务不会与另一项大矩阵计算并发。
- 启动时 `recover_interrupted()` 把上次进程中断的运行恢复为终态。
- 生产环境同样要求单应用进程负责重任务（进程内锁），多 worker 部署需先加操作系统级文件锁。

数据可用性：`POST /runs` 用 `enriched_partition_dates` 核验区间内 enriched 分区数，不足 `MIN_WINDOW_PARTITIONS = 150` 个交易日返回 409；未显式给 `start/end` 时默认回看约 4 年（`DEFAULT_LOOKBACK_DAYS = 1460`），`end` 取最新分区。同参数（资产/区间/分区数指纹）默认去重返回既有运行，`force=true` 强制新建。

## API

前缀 `/api/evolution`，标签 `backtest`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/runs` | 启动进化。请求体 `EvolutionStartRequest`（`extra=forbid, strict`）：`asset_type`(stock/etf)、`start/end`(可选 ISO 日期)、`population_size`(20–400，默认 120)、`generations`(2–40，默认 12)、`horizon`(1–20，默认 5)、`max_symbols`(50–2000，默认 600)、`max_depth`(2–6，默认 4)、`seed`(可选)、`llm_propose`(默认 false)、`llm_count`(5–30，默认 12)、`symbols`(可选，≤10000 且去重)、`force`。数据不足 409，参数越界 422。 |
| GET | `/runs?limit=&status=` | 运行列表（compact 投影：成功带 `trials/panel/survivor_count/elapsed_ms`，进行中带 `progress`）。`limit` 1–100 默认 30，`status` 校验合法状态。 |
| GET | `/runs/{run_id}` | 运行详情（full 投影：带完整 `result`）。不存在 404。 |
| POST | `/runs/{run_id}/cancel` | 取消。不存在 404，非活动态 409。 |
| POST | `/runs/{run_id}/survivors/{index}/publish` | 发布幸存者为草稿因子。请求体 `{label?, description?}`。运行未成功 409，序号越界 404，重复发布 409。 |

`result` 结构：`status / algorithm_version(evolution-v1) / run_id / request / panel{universe_total,n_symbols,n_rows,n_days,train_days,holdout_days,train_start...holdout_end} / generations[{gen,evaluated,best,mean,best_formula,trials_total}] / trials{evaluated,duplicates,invalid} / dsr{expected_max_sharpe,variance_sharpes} / survivors[{key,formula,direction,train{ic_mean,ir,coverage,fitness,n_days},holdout{ic_mean,ir,t_nw,p_value,q_value,dsr,coverage,n_days}}] / pruned[{key,formula,reason}] / llm(未开启为 null；开启见下节) / elapsed_ms`。所有浮点经 JSON 安全化处理（NaN/Inf → null）。

## AI 提议通道（22b）

一键式：`llm_propose=true` 时，进化 worker 在行情面板就绪后、种群构建前调用一次 LLM（`app/services/evolution_llm.py`），把通过校验的提案公式注入初始种群。**LLM 只是"外援军师"出种子，不碰任何统计环节**：

- **提示词**：系统消息设定量化因子设计师角色 + 保留窗纪律警告；用户消息给出 DSL 词表（从 `evolution.py` 常量动态生成，单一事实来源）、语法规则、12 条种子示例、输出规格（`<!--EVOLUTION_FORMULAS: [...]-->` 机读块，与 stock_debate/ai_verdict 的标记解析约定一致）。
- **编译闸（fail-closed）**：每条提案过 `compile_formula`，语法/语义错误（E0xx）即拒并记录原因；与内置种子或批内重复（归一化键）也拒。
- **修复轮（最多 1 轮）**：仅当存在 E0xx 编译类失败且接受数不足时，把失败清单回喂 LLM 重修一次；修复轮自身异常不丢弃首轮结果（记录 `修复轮失败: ...`）。
- **fail-open**：未配置 AI Key、调用超时、解析失败、全部被拒 → 记入 `llm.error`，警告日志，**降级为纯 GP，任务照常完成**，绝不因 LLM 失败而 fail 整个 run。
- **统计纪律不变**：LLM 提案进入种群后与 GP 个体完全同权——适应度只看训练窗、保留窗只用一次、**每条被评估的提案都计入 trials 账本**，DSR 多重检验折减天然覆盖 LLM 通道（提议越多，折减越狠）。提案的 `direction` 仅是 LLM 的先验提示，最终方向仍由训练窗 IC 符号决定。
- **可追溯**：`result.llm` 持久化 `requested/proposed/accepted[{formula,rationale,direction,key}]/rejected[{formula,reason}]/repair_rounds/attribution{model,provider,profile_id}/error/elapsed_ms`，run JSONL 即完整审计记录。
- 调用参数：temperature 0.7、timeout 240s，走 `ai_provider.generate_ai_text`（复用系统 AI 配置与 Key）；worker 子进程内 `asyncio.run` 同步执行，懒加载 AI 栈（纯 GP 运行不 import）。

## 前端

「公式进化」作为因子发现页（FactorDiscovery）的第三个视图（与「批量筛选」「挖掘工作台」并列），组件 `EvolutionWorkbench.tsx`：

- 左侧启动表单（资产类型、种群规模、代数、预测周期、最大标的数、表达式深度、可选种子/日期、AI 提议开关+条数、强制新建）+ 运行历史列表。
- 右侧详情：状态条与逐代进度、面板/试验概览、AI 提议面板（接受/拒绝/修复轮/模型归因/错误降级提示，仅开启时显示）、逐代适应度表、幸存者表（公式/方向/训练 IC·IR·适应度/保留 IC·IR·NW-t·q·DSR/发布按钮）、剪枝列表。
- 采用 react-query **轮询**（活动运行 4 秒刷新，与挖掘工作台一致），不使用 SSE。

## V1 范围与后续

- **V1（已落地）**：IC 统计初筛闸门（保留窗 IC + NW-t + BH-FDR + 全账本 DSR + 0.8 相关性剪枝），发布为 `uf_evo_*` 草稿后由既有挖掘漏斗做深度验证。
- **22b（已落地）**：LLM 提议通道——一键式，LLM 出公式种子经编译闸注入初始种群，fail-open 降级纯 GP，统计纪律与 DSR 折减不变（见「AI 提议通道」专节）。
- 明确不做：强化学习选股（已否决）。

## 验证结果

- 遗传算子压力测试：50 组初始种群 × 8 代繁殖，子代公式 100% 通过 DSL 编译（0 失败）；`unparse → parse` 归一化键往返稳定（修复了左结合 `+`/`*` 右子括号缺失导致的结构漂移）。
- 运行时合成端到端：30 标的 × 420 日、嵌入动量信号，0.3 秒完成，状态 succeeded，逐代适应度上升，嵌入的动量种子公式进入幸存者前列，DSR/q 值正常计算，结果 JSON 可序列化。
- 后端全量测试 `5871 passed, 8 skipped, 1 xfailed`（含 46 项进化测试：算子/运行时/LLM 通道/API 四层；22b 新增 19 项——提案解析/校验去重/修复轮/fail-open/种群注入/进度事件/参数边界）；新增文件 ruff 全部干净。
- LLM 通道以 monkeypatch `ai_provider` 方式测试（开发机配置了真实 Key，严禁测试直连）；fail-open、修复轮触发（E009）、修复轮失败保留首轮、无机读块不触发修复等路径全部冒烟验证。
- 前端 `tsc --noEmit` 与生产构建均通过。
