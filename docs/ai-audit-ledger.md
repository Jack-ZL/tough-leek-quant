# AI 分析不可篡改账本（P2-23）设计说明

> 对应调研报告 P2 第 23 条：「给 AI 分析记一本不能改的账」（学 Vibe-Research）。
> AI 每次分析用了哪些原始数据，全部存档、算好指纹（哈希），记进只能追加不能修改的账本。

## 解决什么问题

AI 报告落库后，无法回答三个倒查问题：

- **当时 AI 看到的到底是什么数据？**（K 线后来复权了、财务后来更正了，报告结论还能信吗）
- **报告有没有被动过手脚？**（正文/结论事后被改，无对证）
- **这份报告是哪次分析产出的？**（报告与生成过程无绑定）

现在每次 LLM 分析（个股分析 / 多空辩论 / AI 大盘复盘 / AI 财务分析）自动记一条
`run` 条目：每份输入的 sha256 指纹 + 当时快照、prompt 哈希、模型归属（P1-15 同源）、
产出正文哈希；报告保存时再追加一条 `link` 条目绑定 `report_id`。

## 不可篡改的口径 = 篡改可发现

文件系统层做不到真不可变，本账本的承诺是**任何改动都会在校验时暴露**：

| 机制 | 实现 |
|---|---|
| 只追加 | `ledger.jsonl` 仅 `O_APPEND` 单行写 + fsync，代码层无改写/删除 API |
| 哈希链 | 每行带 `prev_hash` + 自身 `hash`（sha256 规范化 JSON），改任何一行即断链 |
| 快照指纹 | 输入快照 gz 写一次不覆盖，`verify` 时逐份重算 sha256 复核 |
| 每日锚点 | 定时管道每日追加 `checkpoint` 条目（当日 run 数 + 链尾），便于快速验尾 |
| 全链校验 | `POST /api/ai_audit/verify` 一键重算：行哈希 / prev 链接 / seq 连续 / 快照指纹 |

**快照保留期**：默认 90 天（env `TSP_AI_AUDIT_RETAIN_DAYS`，下限 1 天）。过期只删快照
文件，**账本行永不改动**——指纹仍在，读取方按文件存在性显示「快照已过期」。

## 数据布局

```
data/ai_audit/
├── ledger.jsonl                    # 账本本体（run/link/checkpoint 混排，seq 连续）
└── snapshots/{audit_id}/{name}.json.gz   # 各输入的当时快照（写一次不覆盖）
```

run 条目关键字段：`audit_id / kind / symbol / model / provider / profile_id /
inputs[{name, as_of, rows, sha256, snapshot_ref}] / prompt_sha256 / output_sha256 /
output_chars`。产出哈希由**后端**在流结束时对 delta 拼接结果计算，不依赖前端。

## 写入点（全部 fail-open）

审计链路任何异常只记日志，**绝不影响分析/保存主流程**（与 P0-7 记分卡、P1-11 体检同口径；
`data_dir=None`、目录不可写等场景均静默降级）。

| 位置 | 动作 |
|---|---|
| `services/stock_analyzer.py` | begin_run("stock_analysis") → 登记 kline/financials/quality/paper_positions/scorecard → finish_run |
| `services/stock_debate.py` | begin_run("debate") → 登记 kline/financials/quality（三方共用一份材料）→ finish_run（含终审文本） |
| `services/market_recap.py` | begin_run("market_recap", symbol=as_of) → 登记 overview/news/lhb/auction_bench/regime → finish_run |
| `services/financial_analyzer.py` | begin_run("financial") → 登记 financials → finish_run |
| 4 个保存端点 + 定时复盘归档 | `link_report(audit_id, report_id)` 追加 link 条目 |
| `jobs/daily_pipeline.py` 收尾 | `append_checkpoint` + `cleanup_snapshots`（过期快照清理） |

`audit_id` 随 `meta` 事件下发（与 P1-15 归属快照同路），前端 4 个 store 接住后在保存
报告时原样带回——旧前端不带也不报错（空串兼容）。

## API（登录保护，不进开放平台白名单）

| 端点 | 用途 |
|---|---|
| `GET /api/ai_audit/entries?symbol=&kind=&limit=` | 倒序条目列表（run/link/checkpoint 混合） |
| `GET /api/ai_audit/entries/{audit_id}` | run 详情：输入指纹 + 快照状态(ok/expired) + link 绑定 |
| `GET /api/ai_audit/entries/{audit_id}/snapshot/{name}` | 快照原文（过期返回 410，账本指纹仍在） |
| `POST /api/ai_audit/verify` | 全链校验 |
| `POST /api/ai_audit/recompare` | 当时指纹 vs 当前数据源重算指纹——回答「数据后来变没变」 |

**重算比对**目前登记了两类重取器：`kline`（复用 `stock_analyzer._load_kline/_clean_rows`）
与 `financials`（`_load_financials`）；其余输入（体检/战绩/龙虎榜上下文等）为时点数据，
如实标注 `recomparable=false`，绝不硬比。

## 前端倒查 UI

设置页新增「**AI 审计**」Tab（`pages/settings/AuditLedger.tsx`）：

1. **账本链校验**：一键校验，绿=链完整，红=逐条列出异常（哪行哈希不符/哪份快照被改）；
2. **账本条目**：按标的/类型/条数筛选；run 条目可展开看归属、prompt/产出哈希、输入指纹表、报告绑定；
3. **单输入深挖**：「快照」看当时数据原文；「重算比对」看数据源后来有没有变（一致/不一致/无法比对，如实展示）。

## 测试

`backend/tests/test_ai_audit_ledger.py`（14 例）：链完整性、改账本行/改快照/删行/半行垃圾
均被 verify 发现、fail-open（不可写目录全链路不抛）、快照写一次不覆盖、并发追加 seq 连续、
重算比对（一致/漂移/数据缺失/不可比对）、保留期清理边界（账本行不动、detail 标 expired）、
`TSP_AI_AUDIT_RETAIN_DAYS` 解析。

## 文件地图

| 文件 | 职责 |
|---|---|
| `backend/app/services/ai_audit.py` | 账本核心：append-only JSONL 哈希链 + gz 快照 + verify/recompare/cleanup |
| `backend/app/api/ai_audit.py` | 倒查/校验/快照/比对 5 端点 |
| `backend/tests/test_ai_audit_ledger.py` | 全覆盖测试 |
| `frontend/src/pages/settings/AuditLedger.tsx` | 设置页「AI 审计」倒查面板 |
| `frontend/src/lib/api.ts` | `aiAudit*` 5 个 helper + 类型；4 个保存 payload 增加 `audit_id` |
| 4 个 store（stockAnalysis/debate/aiReport/review） | meta 接住 `audit_id`，保存时带回 |
