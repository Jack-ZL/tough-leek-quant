"""AI 研判卡 (verdict 答题卡) — 让结论变成机器可读的字段, 而不是靠猜。

背景 (同目录项目对比调研报告 P0-2, 参照 TradingAgents-AShare):
  AI 分析正文是给人看的作文, 程序无法可靠地从中读出"到底看多还是看空"。
  本模块要求 AI 在正文末尾追加一行藏在 HTML 注释里的机读标记 (答题卡),
  程序直接解析字段; 解析不出来就明确报 ok=false, 绝不从文字里猜。

设计要点:
  1. 标记形态 `<!-- VERDICT: {"direction": "看多", "reason": "…"} -->`
     — HTML 注释对 Markdown 读者不可见, 键名/枚举固定 (与 TA-AShare 的
     VERDICT 机制一致)。direction 是"研判倾向"而非操作指令, 与
     stock_analyzer 提示词"不输出操作指令"的红线不冲突。
  2. 流式安全: `verdict_ndjson_stream` 行缓冲剥离标记 — 原始注释绝不
     流给客户端 (web/IM 都看不到), 解析结果以独立 `verdict` 事件下发。
     须包在数字忠实度闸门 (ai_number_gate) 之内侧 (先剥标记再过闸门):
     否则闸门会把 VERDICT JSON 里的数字包进〖〗标记, 破坏 JSON。
  3. fail-honest: direction 不在枚举内 / JSON 损坏 / 标记缺失 → ok=false,
     由展示层明确告知"未解析出研判结论", 不做任何模糊映射。

已知取舍: reason (≤20 字短语) 不经过数字闸门 — 它随 verdict 事件旁路
下发, 不进正文; 展示上是独立的"AI 研判"徽章, 与正文数字纪律分开。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

# direction 合法枚举 — 数据有倾向必须选偏多/偏空, 确实无法判断才选中性
# (提示词与校验共用同一份, 避免"提示词说 A、校验认 B"的漂移)
VERDICT_DIRECTIONS = ("看多", "偏多", "中性", "偏空", "看空")

# 追加到系统提示词末尾的答题卡要求。
# 刻意不含阿拉伯数字 — 系统提示词会进数字闸门白名单, 示例数字会留假放行口子
# (同 NUMBER_RULE_PROMPT 的纪律, 字数上限用汉字"二十"表述)。
VERDICT_PROMPT = """

## 研判卡(必须输出)
在全文最末(免责声明之后)另起一行, 输出一个机读研判标记, 格式固定, 键名不可改动、不可省略:
<!-- VERDICT: {"direction": "看多", "reason": "一句话核心结论"} -->
- direction 只可填: 看多 / 偏多 / 中性 / 偏空 / 看空 (数据有方向倾向时必须选偏多或偏空, 仅数据确实不足以判断方向时才可选中性)
- reason 不超过二十个字, 用中文, 内部不得出现英文双引号和换行
- 该标记全文只出现一次, 且必须在最后一行; 它是给程序读的, 不要在正文里解释、引用或复述它"""

# 机读标记。DOTALL: 容忍模型违规把 reason 写出换行 (JSON 字符串内 \n 非法,
# 但注释体跨行时仍能整体捕获交给 json.loads 裁决)。
VERDICT_RE = re.compile(r"<!--\s*VERDICT:\s*(\{.*?\})\s*-->", re.DOTALL)

# reason 展示截断长度 (字符)。提示词要求二十字, 这里放宽到 60 兜底防刷屏。
_REASON_MAX = 60


def _validate(obj: object) -> dict | None:
    """校验解析出的 JSON 对象 → 合法 verdict dict 或 None (绝不猜测/映射)。"""
    if not isinstance(obj, dict):
        return None
    direction = str(obj.get("direction", "")).strip()
    if direction not in VERDICT_DIRECTIONS:
        return None
    reason = str(obj.get("reason", "")).strip()
    if len(reason) > _REASON_MAX:
        reason = reason[:_REASON_MAX] + "…"
    return {"direction": direction, "reason": reason}


def parse_verdict(text: str) -> dict | None:
    """从完整文本解析研判卡。多处标记取最后一处 (模型复述示例时以末尾为准)。

    返回 {"direction": …, "reason": …} 或 None (缺失/损坏/枚举外一律 None)。
    """
    last: re.Match | None = None
    for m in VERDICT_RE.finditer(text):
        last = m
    if last is None:
        return None
    try:
        obj = json.loads(last.group(1))
    except (json.JSONDecodeError, ValueError):
        logger.warning("verdict 标记 JSON 损坏: %s", last.group(1)[:120])
        return None
    return _validate(obj)


def strip_verdict(text: str) -> tuple[str, dict | None]:
    """剥离文本中全部研判标记 → (干净文本, verdict|None)。全文消费方用。"""
    verdict = parse_verdict(text)
    clean = VERDICT_RE.sub("", text)
    return clean, verdict


def _verdict_event(raw_jsons: list[str]) -> str:
    """把捕获的标记原文转成 verdict NDJSON 事件 (取最后一处)。"""
    verdict = None
    if raw_jsons:
        try:
            verdict = _validate(json.loads(raw_jsons[-1]))
        except (json.JSONDecodeError, ValueError):
            logger.warning("verdict 标记 JSON 损坏: %s", raw_jsons[-1][:120])
    if verdict:
        return json.dumps({"type": "verdict", "ok": True, **verdict}, ensure_ascii=False)
    # fail-honest: 解析不出就明说, 展示层负责告知用户, 绝不从正文猜
    return json.dumps({"type": "verdict", "ok": False}, ensure_ascii=False)


async def verdict_ndjson_stream(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """NDJSON 流包装器: 剥离 delta 里的研判标记, done 前追加 verdict 事件。

    - delta 事件行缓冲: 标记可能被切在任意 chunk 边界, 只有完整行才发出,
      保证客户端永远看不到标记残片 (与数字闸门同款"不核对不下发"纪律)。
    - done 事件: 先冲刷余量, 再发 verdict 事件, 最后透传 done。
    - 其余事件 (meta/error/ping…) 原样透传; 透传前先冲刷已缓冲的完整行,
      保证事件顺序与文本顺序一致。
    - 源流以 error 结束时无 verdict 事件 (分析失败, 研判无意义)。
    """
    buf = ""
    captured: list[str] = []

    def _strip(chunk: str) -> str:
        """剥掉一段文本里的全部标记, 捕获原文供 done 时解析。"""
        for m in VERDICT_RE.finditer(chunk):
            captured.append(m.group(1))
        return VERDICT_RE.sub("", chunk)

    async def _flush_buf() -> AsyncIterator[str]:
        nonlocal buf
        if buf:
            out = _strip(buf)
            buf = ""
            if out:
                yield json.dumps({"type": "delta", "content": out}, ensure_ascii=False)

    async for line in source:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            yield line  # 非协议行原样透传 (与闸门行为一致)
            continue
        etype = ev.get("type") if isinstance(ev, dict) else None

        if etype == "delta":
            buf += ev.get("content", "")
            # 只发完整行, 残行留在 buf (标记可能跨 chunk)
            while (nl := buf.find("\n")) >= 0:
                ln = buf[: nl + 1]
                buf = buf[nl + 1 :]
                had_marker = bool(VERDICT_RE.search(ln))
                ln = _strip(ln)
                # 标记独占一行时剥完只剩空白 → 整行丢弃, 不留尾部空行
                if had_marker and not ln.strip():
                    continue
                if ln:
                    yield json.dumps({"type": "delta", "content": ln}, ensure_ascii=False)
        elif etype == "done":
            async for out in _flush_buf():
                yield out
            yield _verdict_event(captured)
            yield line
            return
        else:
            # meta/error/ping…: 先冲刷余量再透传, 保持顺序
            async for out in _flush_buf():
                yield out
            yield line
            if etype == "error":
                return  # 分析失败: 不再产出 verdict

    # 源流没有 done 就结束 (异常断流): 冲刷余量, 不产出 verdict
    async for out in _flush_buf():
        yield out
