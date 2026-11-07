# AI 输出数字忠实度闸门 技术方案（design）

> 状态：**已实现**（默认关，灰度中；设置 → AI → 「数字忠实度闸门」开启）。对应《同目录项目对比调研报告》P0 第 1 项「别让 AI 瞎编数字」，参照 Vibe-Research 的 `number_gate.ts` / `number_fidelity.ts` / `arithmetic.ts` 机制，适配 TLQ v0.3.15 的流式链路。
>
> 分级：**L3（直接修改核心源码）+ 最小接线**。依据 `docs/secondary-development.md §2/§5.1`：横切校验必须介入各 `services/*_analyzer.py` 的 delta 出口，现有扩展点（后端唯一继承点 `NotificationFormatter`、3 个前端插槽）无法表达；按「优先新增独立模块，再对核心入口做最小接线」执行。首版**不预埋扩展点**（遵循 §10 YAGNI）。
>
> **实现落点**（与本设计的差异以此为准）：
> - 核心模块：`backend/app/services/ai_number_gate.py`（闸门/流式包装/豁免/算式）+ `ai_number_whitelist.py`（白名单收集）。
> - 开关：`preferences.get/set_ai_number_gate_enabled()`（默认 False）+ `PUT /api/settings/preferences/ai-number-gate`；未做设计稿中的"严格度"三档，首版只有开/关。
> - 接线：`stock_analyzer` / `market_recap` / `financial_analyzer` / `concept_rotation_analyzer` / `custom/assistant/chat_service` 五处流式出口统一走 `gate_ndjson_stream`；`NUMBER_RULE_PROMPT`（零数字约束）追加到五处系统提示词（含 `assistant/prompt.py`，自定义复盘提示词同样追加）。
> - 与设计稿的差异：① plain 池存 `(数值, 量级单位)` 对而非纯数值——"2万元"不能为"2亿元"背书；② 新闻/消息面小节按设计降级为"只记录不打码"（`mask_enabled=False`）；③ `_safe_eval` 需先 `compile(tree, ...)` 再 `eval`（Python 3.14 起 eval 不再接受 AST 对象）；④ 日期豁免正则限定年份 `(19|20)\d{2}` 并加单位护栏，避免 "1234.5" 这类四位整数小数被误判为年月日期而漏检。
> - 测试：`tests/test_ai_number_gate.py`（§12 全部 14 个对抗用例 + 豁免/算式/流式/白名单契约/接线断言）+ `tests/test_ai_number_gate_golden.py`（golden 固化，fixtures/ai_number_gate/golden_cases.json）。
> - **问题数字的展示强化**（用户反馈迭代，两轮）：① 标记方式从"替换隐藏"改为"包裹高亮"——无出处数字就地包进 `〖数字※〗`（`wrap_marked()`），**原数字保留可见**；展示层按渠道转换：web 端 `MarkdownRenderer` 渲染为琥珀色警示徽章 `⚠ 数字`（含悬停说明），钉钉 `emphasize_marked_dingtalk()` 转 `**⚠️数字**`，企微 `emphasize_marked_wecom()` 转 `<font color="warning">⚠️数字</font>`。② 页脚改为 `> ※ 另有 N 处…` 引用行并入免责声明块——web 端连续 `>` 行自动合并；IM 端由 `pop_gate_footer()` 把页脚挪到机器人统一追加的免责声明之后。

---

## 1. 先回答核心疑问：内容这么多，怎么"逐一核对"？

**关键认知：不是再叫一个 AI 来检查，而是纯程序规则，零额外模型成本、毫秒级。**

一句话原理：**AI 只能"抄"或"算"，不能"编"。**

1. 发给 AI 的 prompt 里本来就带着全部行情/财务数据（结构化 dict）——把这些数字收进一个"白名单池"。
2. AI 回答后，程序用正则把回答里**每一个数字 token**抽出来（几千字回答通常也就几十~几百个数字，纯字符串操作）。这就是"逐一"——逐 token，不是逐句逐段。
3. 每个数字先做**规范化**（全角转半角、去零宽字符、去千分位逗号、繁简统一、万/亿/% 单位换算），再到白名单池里**二分查找**。
4. 匹配容差 = `max(相对 0.2%, 写到的那一位的半格)`：写"33 亿"允许 ±0.5 亿，写"15.63 元"允许 ±0.005。
5. 找不到的数字 → **就地包进「〖…※〗」高亮标记**（连紧跟的单位一起包进标记，数字保留可见不隐藏），**只标记该数字，绝不拒绝整段回答**。
6. 回答末尾自动追加一句说明，API 响应带 `numbers_masked: N` 字段。

为什么快：白名单池 `set` 去重 → 排序数组 → `bisect` 二分；每个数字最多试 `1 + 2×单位数` 种换算 × 2（绝对值）次二分。Vibe 实测资料池 30 万个数字、60 行回答，全量核对 < 3 秒；TLQ 单股分析数据包只有几百个数字，预计单次 < 10ms。

---

## 2. 参照 Vibe-Research：取哪几层、不取哪几层

Vibe 的数字可信度是 **5 层**机制。本方案**只取与"数字忠实度"直接相关的 3 层**，其余明确不做（见 §14 非目标）：

| Vibe 层 | 文件 | 本方案是否采用 |
|---|---|---|
| A. 算式重算 | `arithmetic.ts`(334行) | ✅ 采用（Phase 2，先做四则子集） |
| B. 报告数字忠实度（same-line 引用比对） | `number_fidelity.ts`(800行) | ⚠️ 部分采用——TLQ 报告无 `[资料:ev-xxx]` 逐行引用体系，改用 Vibe **对话侧**的"整轮资料池"比对模型 |
| C. 对话数字闸门（整轮资料池 + 打码） | `number_gate.ts`(274行) | ✅ **主参照**，TLQ 全部 5 条链路都按"对话侧"模型处理 |
| D. 合规红线 gate（禁词整行移除） | `gate.ts`(216行) | ❌ 不做（是另一个 P 级议题，与数字无关） |
| E. 回测输入闸 Plan/Refusal | `backtest/gate.py`(299行) | ❌ 不做（是 P0 第 6 项"回测 gate"，独立议题） |

**重要修正**：调研报告里提到的"gate.ts 的 Plan/Refusal"实际在 `backtest/gate.py`（输入侧，与数字忠实度无关）；`gate.ts` 是合规禁词闸。本方案聚焦 C 层（+ A 层算式），即 `number_gate.ts` 的 Python 复刻。

**Python 复刻必须补上的 Vibe 缺口**：Vibe 的 `number_gate.ts`/`number_fidelity.ts` **没有做 NFKC 规范化**（JS `\d` 不匹配全角数字 `１２３`），全角数字、零宽字符插数字、繁体"億"在 Vibe 对话闸是**漏检**的。Python 版必须先 `unicodedata.normalize("NFKC", text)` 再提取，并补零宽剥离与繁简单位归一——这是 TLQ 版相对 Vibe 的**增强**，不是照抄。

---

## 3. TLQ 现状（来自链路调研，落地时直接引用）

### 3.1 5 条自由文本链路 + 唯一 LLM 入口

```
router(api/*.py 薄层)
  → with_heartbeat(services/ndjson_heartbeat.py:26)        ← 唯一统一传输包装
    → services/*_analyzer.py :: *_stream()                 ← prompt 构建 + NDJSON 事件封装（每功能一份，无公共基类）
      → services/ai_provider.py:605 stream_ai_text()       ← 唯一 LLM 入口
```

| 功能 | 端点 | service 流函数 | delta 出口（需接线处） |
|---|---|---|---|
| 助手 chat | `POST /api/custom/assistant/chat` | `custom/assistant/chat_service.py:142 chat_stream()` | `chat_service.py:264-266`（`queue.put({"type":"delta"})`） |
| 大盘复盘 | `POST /api/market-recap/analyze` | `services/market_recap.py:289 recap_market_stream()` | `market_recap.py:346` |
| 个股四维 | `POST /api/stock-analysis/analyze` | `services/stock_analyzer.py:358 analyze_stock_stream()` | `stock_analyzer.py:441` |
| 财务分析 | `POST /api/financials/analyze` | `services/financial_analyzer.py:146 analyze_financials_stream()` | `financial_analyzer.py:181` |
| 概念/行业轮动 | `POST /api/rps/rotation-analyze` | `services/concept_rotation_analyzer.py:313 analyze_rotation_stream()` | `concept_rotation_analyzer.py:383` |

### 3.2 两条容易漏的旁路（决定闸门必须放 service 层，不能放 api 层）

1. **定时复盘 SSE**：`jobs/daily_pipeline.py:1139` 直接消费 `recap_market_stream`，把原始 evt_json 转发给 SSE `/api/intraday/stream` 的 `review_progress` 事件。闸门若放 api 层会漏掉这条。
2. **IM 机器人**：`services/wecom_bot_service.py:479`、`dingtalk_bot_service.py:233` 走 `analyze_stock_text()`（复用 `analyze_stock_stream` 拼接 delta）。闸门在 service 内即自动覆盖。

→ **结论：闸门必须包在 `*_stream()` 生成器内部的 delta yield 处**，这样定时复盘、IM 机器人、手动分析三条消费路径一次性全覆盖。

### 3.3 流式语义（决定分块策略）

- 全部是 **token 级 NDJSON 流式**，服务端不缓冲整段（`ai_provider.py:770 _iter_openai_text()` 逐 `choice.delta.content` 原样 yield）。
- **一个 chunk 可能是半个数字**（如 `+2.` / `35%`）→ 闸门必须持有滑动缓冲，否则 `12.34` 会被判成 `12.` + `34` 两个数。
- Codex CLI provider 是整段一次 yield（`ai_provider.py:614`），可直接整段校验；但助手链路对 Codex CLI 已 fail-closed 拒绝（`chat_service.py:161-169`）。

### 3.4 落库与渲染（决定前端零改动）

- 报告类落库是**前端把累积全文 POST 回来**（`stockAnalysisStore.ts:218`、`review/extension.tsx:210`、`aiReportStore.ts:245`），存 `data/user_data/*.json`（`JsonReportStore` 原子写）。助手会话存 `assistant_sessions.json`。
- → **只要闸门在服务端 delta 出口生效，前端累加的、落库的、外发推送的自然都是打码后文本，存储层与推送层零改动。**
- 渲染唯一组件 `frontend/src/components/financials/MarkdownRenderer.tsx`（零依赖手写）。`〖…〗` 不含 `|`、不命中任何块级正则，行内有专门 token 渲染为警示徽章 → **安全渲染，不会被吃掉**。标记须"只包裹数字 token、保留标点/表格 `|`"，避免破坏表格行列数。

---

## 4. 总体设计

### 4.1 新增独立模块（不继承任何大型编排服务）

```
backend/app/services/ai_number_gate.py      ← 核心：提取/规范化/双池/匹配/打码/算式/流包装
backend/app/services/ai_number_whitelist.py ← 白名单采集：结构化对象 → NumberPool（形态族）
```

放 `services/` 符合 `CONTRIBUTING.md §2.3`（"组合领域能力，不复制仓库或 provider 实现"）；不放 `api/`（api 层须薄）；不继承 `StrategyEngine/BacktestEngine/ScreenerService/DataStore`（§4.1 黑名单）。

### 4.2 数据流

```
[生成前] 各 analyzer 把"拼串前的结构化数据对象"交给 collect_whitelist() → NumberPool（双池：all + plain）
[生成中] stream_ai_text() 逐 delta → gate_ndjson_stream(deltas, pool) 行缓冲闸门 → 打码后 delta
[生成后] flush 余量 → 追加"> ※ 另有 N 处…"引用行说明 → done 事件带 numbers_masked=N
[落库/外发] 前端/定时任务拿到的已是打码后全文（无需改动）
```

### 4.3 核心类型（frozen dataclass，对齐 §4.3"上下文优先用不可变 dataclass"）

```python
@dataclass(frozen=True)
class NumberPool:
    all: tuple[float, ...]      # 排序去重，含单位换算后的全部形态 + 绝对值
    plain: tuple[float, ...]    # 排序去重，只收"原样读"形态（防数量级造假）
    empty_reason: str | None    # 非 None 表示采集失败/为空 → 触发 fail-closed（§8）

@dataclass(frozen=True)
class GateResult:
    text: str
    masked: int
    violations: tuple[Violation, ...]   # 进日志/metric，不外泄给用户（§8 内部异常不外泄）

@dataclass(frozen=True)
class Violation:
    raw: str          # 原始写法，如 "2 亿元"
    value: float      # 规范化后数值
    line: int         # 行号（定位用）
    reason: str       # "no_source" | "magnitude_mismatch" | "arith_wrong" | "direction_conflict"
```

---

## 5. 核心算法规格（Python 复刻 + 增强）

### 5.1 提取（extraction）

**顺序**：先 `unicodedata.normalize("NFKC", text)`（补 Vibe 缺口）→ 剥零宽字符（`[\u200B-\u200D\u2060\uFEFF]`）→ 按豁免清单用**等长空格遮蔽**（保持 index 对齐）→ 跑千分位敏感主正则。

主正则（千分位只有逗号后正好 3 位才算，防"1,33 家"拼成 133）：
```python
NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?(?:e[+-]?\d+)?|-?\d+\.?\d*(?:e[+-]?\d+)?", re.I)
UNIT_AFTER = re.compile(r"^\s*(?:%|倍|百分点|pp|万亿元|亿元|万元|万亿|亿|万|元/股|元|美元|港元|x(?![A-Za-z0-9]))")
```

**豁免清单**（等长空格遮蔽，参照 `number_gate.ts:39-48` + `number_fidelity.ts:348-397`，按 TLQ 场景裁剪）：
- 日期时间：`2024-10-08`、`10月28日`、`10-08`、`09:35`（但"配比 35:65"仍核对——裸时刻不剥）
- 列表序号：行首 `1. `、`3、`
- 股票代码：6 位代码、`SH600519`/`SZ000001` 字母前缀（TLQ 专属 `subjectCodePatterns`）
- 指标参数组：`MACD (12,26,9)`
- 时间窗口：`近 5 日`、`过去 3 个月`、`未来 2 周`
- 小整数计数：`abs(n) <= 20` 的整数且后面**没跟** `%/倍/元/亿/万/期/x` 等单位（"3 个理由"豁免，"5%"/"11 期"不豁免）
- 闭合代码块：只豁免**闭合的、半数以上行像代码的**围栏（防"把编的数字塞进 ``` 绕过"）；` ```json {"revenue": 9876.5} ``` ` 仍核对
- 指数/均线名称数字：`沪深300`、`60 日均线`、`中证500`——需词表注册（`register_gate_labels()`）；**未注册只会多打码不会漏核对**（fail-safe 方向正确）

### 5.2 规范化匹配（bound）

```python
MULT = {"万亿":1e12, "千万":1e7, "百万":1e6, "亿":1e8, "万":1e4}

def bound(raw, unit, pool: NumberPool) -> bool:
    n = to_num(raw)                                   # 去千分位逗号
    decimals = len(re.search(r"\.(\d+)", raw).group(1)) if "." in raw else 0
    half = 0.5 * 10 ** -decimals                      # 写到的那一位的半格
    units = [unit] if unit else []
    tries = []
    # 原样读：写了量级单位时只比 plain 池（防"2 万元"替"2 亿元"作证）
    tries.append((n, half, pool.plain if (units and set(units) & MULT.keys()) else pool.all))
    for u in units:
        if u in MULT:  tries.append((n*MULT[u], half*MULT[u], pool.all))   # 1.5万 → 15000
        if u in ("%", "百分点"): tries.append((n/100, half/100, pool.all)) # 35% → 0.035
    return any(near_in_pool(v, max(abs(v)*2e-3, step)+1e-12, p) for v, step, p in tries)
```

- **容差** = `max(相对 0.2%, 半格)`。
- **负数取绝对值也入池**："下降 5.2%"匹配资料里的 -5.2。
- **方向词承载符号**（`number_fidelity.ts:453-469`）："下降/下跌/减少"命中 → 额外试 `-token`；但"同比上升 1.92%"配证据 -1.92% **不放行**（方向写反就是错，`reason="direction_conflict"`）。
- **表格单位提示传播**：表头写"(亿元)"，该列数字按亿换算；表头没写单位就不替它乘。
- 查找：池排序 + `bisect_left` 二分。

### 5.3 双池设计（防数量级造假——金融场景最危险的错误）

- `all` 池：所有换算后形态（"1.5万"→15000 也入池）。
- `plain` 池：只收"原样读"形态。
- 规则：回答写"2 亿元"时，**原样那一读只和 plain 池比**——资料里的"2 万元"不能替"2 亿元"作证（Vibe 测试 `number_gate.test.ts:36-37`）。
- 资料里的 35% **不**自动存 0.35；回答写 0.35 → 打码（反向也禁）。

### 5.4 算式重算（Phase 2，先做四则子集）

参照 `arithmetic.ts` + `number_gate.ts:236-247`：
```python
derived = set()
for c in check_stated_arithmetic(segment):    # 识别 A÷B−1=R / A−B=R / A+B=R / A×B=R / 多项加减
    if not c.ok: continue                     # 算错的不算出处（reason="arith_wrong"）
    operands = c.nums[:-1]                    # 运算数（去结果）
    if all(bound(op, ...) or abs(to_num(op)) in derived for op in operands):
        derived.add(abs(c.stated))            # 🔴 运算数都有出处，结果才有出处；支持链式
```
- **必须把算式写在回答里**，且**每个运算数自己也要有出处**："300 ÷ 8 = 37.5"（8 有出处、300 编的）→ 300 和 37.5 都打码。
- 量纲不同不认（"2 元 + 50%"返回 None，结果打码）。
- 容差 `max(|recomputed|*5e-3, 0.011)`。
- MVP 先支持单行内四则；完整量纲分析（`Term{v,k,scaled,dim,mag}`）留 Phase 2+。

### 5.5 打码（masking）

```python
MARK_OPEN, MARK_CLOSE = "〖", "〗"   # wrap_marked(span) -> "〖{span}※〗"
# 从后往前包裹（保持前面 index 有效）；数字+紧跟单位一起包进标记；区间号"-"保留
for t in sorted(violations, key=lambda x: -x.index):
    num = NUM_AT.match(out, t.index)
    start = t.index + 1 if (num.group().startswith("-") and out[t.index-1:t.index].isdigit()) else t.index
    end = t.index + len(num.group())
    m = UNIT_AFTER.match(out, end)
    if m: end = m.end()
    out = out[:start] + wrap_marked(out[start:end]) + out[end:]
```
- **只包裹数字本体（含单位），保留标点/表格 `|`**（避免破坏 MarkdownRenderer 表格行列数）。
- 末尾追加说明（引用行，与紧邻的免责声明合并进同一 blockquote）：`> ※ 另有 {N} 处数字在本轮提供的数据里找不到出处，也不是回答中算式算出来的，已标为「〖…〗」。` 页脚前导换行按正文尾字符动态决定（已以 `\n` 结尾则不再加，避免空行隔断引用块）。
- **幂等**：标记后文本再过一遍闸门不重复包裹（`〖…〗` 形态进豁免表；Vibe 测试 `number_fidelity.test.ts:155`）。

---

## 6. 白名单采集（TLQ 专属：收"形态族"而非原值）

**统一接口**：`collect_whitelist(objs: list[Any]) -> NumberPool`。各 analyzer 在"结构化对象 → 拼串"两步之间调用（零成本，数据已在作用域内）。

**必须收录的形态族**（来自调研 §3/§9 的单位陷阱）：

| 数据 | 原始口径 | 需入池形态 | 原因 |
|---|---|---|---|
| 个股 `change_pct`/`amplitude` | 小数制 0.0366 | {0.0366, 3.66} | 模型写"3.66%"或"+3.66%"（CONTRIBUTING §3.1） |
| 个股 `turnover_rate` | 百分数 5 | {5, 0.05} | enriched 是百分数，模型可能写小数 |
| 复盘指数 `change_pct` | 百分数（与个股相反） | {原值, 原值/100} | 口径与个股相反，必须双收 |
| 财务金额 | 元 | 原值（亿/万靠 bound 单位换算） | 模型写"120.5 亿"，bound 试 120.5e8 |
| 成交额 | 元（已 /1e8 显示"亿"） | {元值, 亿值} | `market_recap.py:275` 已换算 |
| levels 关键价位 | `round(v,2)` | 原值 | 数据层已 round，白名单用同精度 |
| K 线 OHLC | `round(v,4)` | 原值 | 同上 |
| 轮动排名哨兵 999 | 999 | **不入池** | 渲染成"—"，不是真数值 |

**助手 chat 特殊**：白名单 = 逐轮累积的所有 `role:tool` 消息里的数值（`req_messages` 就是现成容器，`chat_service.py:334-338`）。工具返回 `{rows:[dict], count:n}`，`collect_whitelist` 递归扫 dict/list 里的所有数值（复用 `tools.py:_clean_obj` 的遍历形态）。

**采集失败/为空 → `empty_reason` 非 None → 触发 fail-closed（§8），绝不"放行全部数字"。**

---

## 7. 流式处理：行缓冲闸门（关键工程决策）

**问题**：delta 是 token 级、会切断数字；算式/数字+单位序列可能跨 delta。

**方案**：`gate_ndjson_stream(events: AsyncIterator[dict], pool: NumberPool) -> AsyncIterator[dict]`，照抄 `with_heartbeat`（`ndjson_heartbeat.py:26`）的"包装 AsyncIterator、按行解析事件、透传异常、消费端停止即取消"范式。

**缓冲策略——按行边界冲刷**：
- 累积 delta 到 pending buffer。
- 遇 `\n`（行边界）或流结束时，对**完整行**跑全量闸门（提取→白名单→算式→打码），yield 打码后的行。
- 行内数字+单位+算式几乎都在同一行；跨行的链式派生极少，用"上一行 derived 集合 carry-over"兜底。
- **数字永远不会未经检查就 emit**（无闪烁），延迟 < 1 行，流式 UX 保留。
- 非 delta 事件（notice/tool_call/tool_result/action_confirm/done/error/ping）**原样透传**，只拦 `{"type":"delta"}`。

**为什么不在 api 层包 `with_number_gate`**：会漏掉定时复盘 SSE（`daily_pipeline.py:1139` 直接消费 service 流）和 IM 机器人（§3.2）。service 层是唯一全覆盖点。

**6 个接线点**（调研 §9.1）：`stock_analyzer.py:441`、`market_recap.py:346`、`financial_analyzer.py:181`、`concept_rotation_analyzer.py:383`、`chat_service.py:264-266`、（可选）`api/strategy.py:983`（策略迭代 `change_summary` 自由文本）。

---

## 8. fail-closed 与降级哲学（对齐 TLQ 既有先例）

参照调研 §8（`enriched_generation.py` 世代协议、`factors/dsl.py:559` 错误即数据、`regime_alignment.py:47/80` 边界宽内部严、`chat_service.py:8-9` LLM 链路 fail-closed）：

| 情形 | 行为 | 对齐先例 |
|---|---|---|
| 单个数字无出处 | **打码（降级），不报错、不中断流** | `factors/dsl.py:559`"不抛异常，用结构化结果表达" |
| 白名单采集失败/为空 | **fail-closed：打码全部数字 + warning 日志**，绝不放行 | `enriched_generation.py:248`"无法证明 → 保持 fail-closed" |
| 闸门自身抛异常 | **fail-closed：该段全部打码 + error 日志**，流不中断（打码是安全侧） | `regime_alignment.py`"内部缺口仍 fail-closed" |
| 联网搜索/消息面章节 | **边界放宽**：该章节数字豁免或宽松（外部新闻数字不在本地白名单） | `regime_alignment.py:47`"边界首日降级为预热" |
| violations 详情 | **只进服务端日志/metric，不透给客户端** | `api/strategy.py:1050`"内部异常详情只进日志"（CONTRIBUTING §8） |

**可自愈**：preferences 开关 + "闸门未生效"告警日志，避免静默失效（参照 `CLAIM_STALE_SECONDS` 思路）。

---

## 9. 配置与开关

用户偏好开关（`services/preferences.py`，`data/user_data/preferences.json`）：
```python
get_ai_number_gate_enabled() -> bool      # 默认 False（Phase 1 灰度），burn-in 后翻 True
set_ai_number_gate_enabled(v: bool)
get_ai_number_gate_strictness() -> str    # "mask"（默认）| "off"；预留"reject"不实现
```
端点 `api/settings.py` 的 `PUT /preferences/*` 系列；前端 `pages/settings/AI.tsx` 加开关。

**不做成 `config.py`/`secrets_store` 的 `ai_*` 键**——那是模型/密钥配置；闸门是用户偏好，归 preferences。

**首版不预埋扩展点**（§10 YAGNI）：直接核心 service + preferences 开关。未来若真有二开替换需求，再按 `NotificationFormatter` 形态提取小粒度 ABC。

---

## 10. 提示词配套规则（显著降低打码率）

在 4 个 analyzer 的 system prompt + 助手 `prompt.py:_SYSTEM_TEMPLATE` 里加一段（等价 Vibe `NUMBER_GATE_RULE`）：

> 你回答中的每个数字必须来自本轮提供的数据，或由你写出的算式计算得到。引用计算结果时把算式一起写出来（如：涨幅 = (110.5 − 100) ÷ 100 = 10.5%）。找不到出处的数字不要写。

Vibe 实测：这条规则让模型主动写算式，打码率显著下降（模型配合度提高）。

---

## 11. 前端改动

- **零改动即可生效**：打码在服务端 delta 出口 → 前端累加的、落库的、外发的都是标记后文本。`〖…〗` 在 `MarkdownRenderer` 渲染为警示徽章（§3.4）。
- **可选 UI 提示**（Phase 3）：新增 NDJSON 事件 `{"type":"number_gate","masked":N}`。`ndjson_heartbeat.py:5-7` 已注明"前端各消费方对未知事件类型静默忽略，新增类型无兼容影响"→ 可安全新增。助手侧需在 `custom/assistant/client.ts:43 AssistantEvent` 联合类型 + `store.ts` switch 加分支。

---

## 12. 测试方案（CONTRIBUTING §9/§10：测试须能在未修复代码上失败、覆盖负例边界）

### 12.1 单元测试 `backend/tests/test_ai_number_gate.py`

把 Vibe 对抗用例（调研 §8）逐条翻成 pytest，**并补 Python 增强项**：

| # | 用例 | 期望 |
|---|---|---|
| 1 | "营收 2 亿元" vs 资料"营收 2 万元" | 打码（数量级） |
| 2 | "300 ÷ 8 = 37.5"（8 有出处、300 编的） | 两处打码 |
| 3 | "100 ÷ 8 = 13.5"（算错） | 结果打码 |
| 4 | "2 元 + 50% = 2.5 元"（量纲混加） | 打码 |
| 5 | ` ```json {"revenue": 9876.5} ``` ` | 仍核对 |
| 6 | "区间 2.5-3.75 倍" | 保留"-"，打码 3.75 |
| 7 | "配比 35:65" | 核对；"09:35" 豁免 |
| 8 | "沪深300 涨 1.2%"（词表未注册） | 都打码（宁严勿漏） |
| 9 | **全角"１５６.３"** | 必须抓到（Vibe 漏检，Python 增强） |
| 10 | **零宽"1\u200b56.3"** | 必须抓到 |
| 11 | **繁体"2 億元"** | 必须抓到 |
| 12 | 幂等：打码结果再过一遍 | 不变 |
| 13 | 白名单为空 | 全部打码（fail-closed） |
| 14 | "下降 5.2%" vs 资料 -5.2 | 放行；"同比上升 1.92%" vs -1.92% | 打码（方向反） |

### 12.2 golden-vector `backend/tests/test_ai_number_gate_golden.py` + `backend/tests/fixtures/ai_number_gate/*.json`

参照 `test_stats_v2.py:72 test_bh_fdr_golden` 先例 + CONTRIBUTING §9"固定样本数值断言"。每个 fixture = `{模型输出片段, 白名单, 期望打码结果, 期望 violations}` 三元组，锁定算法回归。

### 12.3 白名单采集契约测试

套用 `test_ai_analysis_focus.py` 形态（跨 4 个 analyzer 直接 import 私有 `_build_user_prompt` 断言）：验证每个 analyzer 的 `collect_whitelist` 收录了正确的形态族（双口径、亿/万、哨兵 999 排除）。

### 12.4 接线不被拆断言（Vibe `number_gate.test.ts:254-259` 源码正则断言）

```python
def test_number_gate_wired_into_all_streams():
    for path in ["stock_analyzer.py", "market_recap.py", "financial_analyzer.py",
                 "concept_rotation_analyzer.py", "chat_service.py"]:
        src = (REPO_ROOT / "backend/app" / ... / path).read_text()
        assert "gate_ndjson_stream(" in src, f"{path} 数字闸门接线被移除"
```

### 12.5 验证命令

```bash
cd backend
uv run --frozen pytest tests/test_ai_number_gate.py tests/test_ai_number_gate_golden.py -q
uv run --frozen ruff check app/services/ai_number_gate.py app/services/ai_number_whitelist.py tests/test_ai_number_gate*.py
cd ../frontend && pnpm build   # 若做 Phase 3 UI 提示
cd .. && git diff --check
```

---

## 13. 分阶段实施

| 阶段 | 内容 | 默认状态 | 验收 |
|---|---|---|---|
| **Phase 0** | 模块脚手架 + preferences 开关 + `NumberPool`/`GateResult` 类型 + golden fixtures 目录 + 接线断言骨架 | 关（行为无变化） | 测试框架就绪，主流程零影响 |
| **Phase 1（MVP）** | 提取 + NFKC/零宽/繁简规范化 + 双池白名单 + bound 匹配 + 打码 + 行缓冲流包装 + prompt 规则；接线 4 analyzer + chat_service | 关 → 灰度开 | 用例 1/5/6/7/8/9/10/11/12/13 通过；定时复盘+IM 机器人旁路覆盖 |
| **Phase 2** | 算式重算（四则子集 + 链式 derived）+ 派生值放行 + 消息面/联网章节边界放宽 + 方向词符号 | 开 | 用例 2/3/4/14 通过；正常派生数字不被误杀 |
| **Phase 3（可选）** | `numbers_masked` UI 事件 + 前端提示"本文有 N 处数字未通过校验" + metric 看板 | 开 | 前端 build 通过；未知事件静默忽略验证 |

每阶段独立 PR，遵循 CONTRIBUTING §10（一个 PR 聚焦一个问题，附验证结果、兼容性、风险回滚）。

---

## 14. 风险、边界与非目标

### 14.1 已知风险与缓解

| 风险 | 缓解 |
|---|---|
| **联网搜索/消息面数字**不在本地白名单 → 整段被打码 | Phase 2 章节级豁免（`_NEWS_SECTION_SEARCH` 标记的章节宽松）；参照 Vibe `fidelityExcludedSections` |
| **模型合法派生值**（均值/累计/涨跌幅）被误杀 | Phase 2 算式重算 + prompt 规则要求写算式；burn-in 期观察打码率调容差 |
| **不同厂商数值格式习惯差异**（千分位/正负号/亿万） | bound 规范化比对（非字符串精确匹配）；golden fixture 覆盖多厂商输出样本 |
| **行缓冲切断跨行算式** | derived 集合 carry-over；算式几乎都在单行内 |
| **标记破坏 Markdown 表格** | 只包裹数字 token、保留 `|`/单位/标点；MarkdownRenderer 块级正则不命中 `〖…〗` |
| **词表未注册导致过度打码**（"沪深300"） | fail-safe 方向正确（宁严勿漏）；逐步注册 TLQ 指数/均线/指标名词表 |

### 14.2 非目标（本方案明确不做）

- ❌ 合规红线禁词闸（Vibe `gate.ts`）——独立议题。
- ❌ 回测输入 Plan/Refusal 闸（Vibe `backtest/gate.py`）——P0 第 6 项。
- ❌ VERDICT 机读块信号解析（AShare）——P0 第 2 项。
- ❌ 报告 same-line `[资料:ev-xxx]` 逐行引用体系（Vibe `number_fidelity.ts` 完整形态）——TLQ 无此引用基础设施，用对话侧"整轮资料池"模型替代。
- ❌ 证据链审计 hash 链（Vibe `audit_chain.ts`）——P2 第 23 项。
- ❌ 完整量纲分析算式（`Term{v,k,scaled,dim,mag}`）——Phase 2 只做四则子集。

---

## 15. 一句话总结

把 Vibe 的 `number_gate.ts`（对话侧整轮资料池比对 + 打码）用 Python 复刻并增强（补 NFKC/零宽/繁简），做成 `services/ai_number_gate.py` 独立模块，以行缓冲流包装器接进 5 条 analyzer 的 delta 出口（service 层，一次性覆盖手动/定时/IM 三条消费路径），白名单从"拼串前的结构化数据"零成本采集并收"形态族"，对不上出处的数字就地标记「〖…〗」高亮而非拒答，闸门自身故障 fail-closed 全打码，前端与存储层零改动。
