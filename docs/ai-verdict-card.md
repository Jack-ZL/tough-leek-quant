# AI 研判卡（答题卡）设计说明

> 对应调研报告 P0 第二条：「让 AI 把结论填在『答题卡』里，别让它写作文」。
> 借鉴 TradingAgents-AShare 的 `<!-- VERDICT: ... -->` 机读标记，但按本项目红线做了改造。

## 解决什么问题

以前的"固定格式"只是 prompt 里的 Markdown 章节模板，是给人看的——程序从来不读 AI 的结论。
这导致：

- 结论无法落库、无法统计（比如以后想算"AI 研判的胜率"就没有数据）；
- IM 消息里用户要通读全文才知道 AI 到底偏多还是偏空。

现在 AI 必须在全文最末额外输出一行机读标记：

```
<!-- VERDICT: {"direction": "偏多", "reason": "站上20日均线"} -->
```

程序解析后作为独立 `verdict` 事件下发、随报告落库。

## 与 TradingAgents-AShare 的区别

| 点 | TA-AShare | 本项目 |
|---|---|---|
| direction 取值 | BUY/SELL/HOLD（操作指令） | 看多/偏多/中性/偏空/看空（研判倾向，不违反"不输出操作指令"红线） |
| 解析失败 | 兜底为 HOLD（猜一个） | `ok=false`，界面明示"未解析出研判结论"（诚实失败，绝不猜） |
| 同义词映射 | BULLISH→BUY 等模糊映射 | 无映射，枚举外一律拒绝 |

## 关键实现点（backend/app/services/ai_verdict.py）

1. **流式嵌套顺序**：`gate_ndjson_stream(verdict_ndjson_stream(...))` —— 研判卡剥离在数字闸门
   *内侧*。若反过来，闸门会把 VERDICT JSON 里的数字（如 reason 中的"20日均线"）包进〖〗，
   JSON 就坏了。verdict 事件作为非 delta 事件被闸门透传。
2. **行缓冲剥离**：标记可能被 LLM 分块劈在任意位置（甚至 `--` 与 `>` 之间），按行缓冲，
   只发完整行；纯标记行整行丢弃，行内标记剥离后保留周围文字。
3. **事件顺序**：`deltas… → verdict → 闸门页脚 delta → done`。`error` 时不发 verdict。
4. **prompt 无阿拉伯数字**：VERDICT_PROMPT 里"二十个字"用汉字——system prompt 会进数字
   闸门白名单，混入数字会开出误放行口子（与 NUMBER_RULE_PROMPT 同一纪律）。
5. **reason 截断**：超 60 字截断加省略号，防止 AI 借 reason 写小作文。

## 已知取舍

- **reason 绕过数字闸门**：reason 走 verdict 事件而非正文，其中的数字不经过出处核对。
  接受该风险的理由：reason 限 60 字、独立徽章展示（不与正文混淆）、方向枚举本身无数字。
- IM 路径（钉钉/企微）在正文末尾、闸门页脚之前插入一行人类可读的
  `🧭 AI 研判: {direction} —— {reason}`（`analyze_stock_text` 内用 `pop_gate_footer` 保证
  页脚仍是最后一行，机器人依赖此约定）。

## 数据流

```
LLM 流 → verdict_ndjson_stream(剥标记/攒事件) → gate_ndjson_stream(数字核对) → NDJSON
  ├─ Web: stockAnalysisStore case 'verdict' → 弹窗徽章(红多绿空) + 历史侧栏 🧭 标签
  ├─ IM:  analyze_stock_text 拼 🧭 行
  └─ 落库: SaveReportRequest.verdict → stock_reports（为将来胜率统计留数据）
```

## 测试

- `backend/tests/test_ai_verdict.py`：解析/剥离/流式/闸门组合/接线共 20 例。
- 前端 `MarkdownRenderer.test.tsx` 覆盖正文渲染；徽章为纯展示逻辑。
