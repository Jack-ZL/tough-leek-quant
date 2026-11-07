"""IM 消息分段工具 — 超长 Markdown 按平台单条上限切成多段, 不丢尾部内容。

背景: 各 IM 通道单条消息有硬上限 (钉钉智能机器人 4000 字符、企微智能机器人
20480 字节、企微群推送 markdown 4096 字节、钉钉群推送 20000 字符)。此前超长
一律截断丢尾, 而报告的「综合研判 / 免责声明」恰在尾部, 损失最大。本模块把
长文切成多段供调用方依次发送 (分段发送兜底), 每段末尾附 "(i/N)" 续接标记。

切分策略 (贪心装箱, 尽量不破坏 Markdown 结构):
  1. 按空行切成段落块; 围栏代码块(```)内部的空行不算分隔, 整块保持完整;
  2. 段落块装箱进各段; 单块超限时降级按行装箱;
  3. 单行仍超限 (超长表格行/URL, 罕见) 才按上限硬切 — 字节模式不切断多字节字符;
  4. 只在边界处切分, 不丢弃任何内容。

调用方注意: 连续多条消息受平台频率限制 (企微/钉钉群机器人 20 条/分钟),
正常报告切出 2-5 段不会触顶; 不要用它切超大文本刷屏。
"""
from __future__ import annotations

from collections.abc import Callable

_FENCE = "```"
_JOIN_PARA = "\n\n"
_JOIN_LINE = "\n"
# 续接标记 "(i/N)" 加空行的预留额度 ("(99/99)" + 两个换行 = 11, 留余量)
_MARKER_RESERVE = 16


def _size_chars(s: str) -> int:
    return len(s)


def _size_bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _clip_chars(s: str, budget: int) -> str:
    return s[:budget]


def _clip_bytes(s: str, budget: int) -> str:
    """按 UTF-8 字节数取前缀, errors='ignore' 丢弃被切的半个多字节字符。"""
    enc = s.encode("utf-8")
    if len(enc) <= budget:
        return s
    return enc[:budget].decode("utf-8", errors="ignore")


def _blocks(text: str) -> list[str]:
    """按空行切成原子段落块; 围栏代码块内部空行不作为分隔 (整块保留)。"""
    out: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith(_FENCE):
            in_fence = not in_fence
            cur.append(line)
            continue
        if not line.strip() and not in_fence:
            if cur:
                out.append(_JOIN_LINE.join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        out.append(_JOIN_LINE.join(cur))
    return [b for b in out if b.strip()]


def _pack_lines(
    block: str,
    budget: int,
    size_of: Callable[[str], int],
    clip: Callable[[str, int], str],
) -> tuple[list[str], str]:
    """把超限段落块按行装箱。返回 (已装满的段列表, 未满的尾段)。

    尾段返回给上层继续参与装箱, 避免段间留大片空白。
    """
    chunks: list[str] = []
    cur = ""
    for line in block.split("\n"):
        cand = line if not cur else cur + _JOIN_LINE + line
        if size_of(cand) <= budget:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if size_of(line) <= budget:
            cur = line
            continue
        # 单行超限: 按上限硬切 (不丢内容), 剩余部分作为尾段继续
        remaining = line
        while size_of(remaining) > budget:
            piece = clip(remaining, budget)
            if not piece:  # 防御: 预算过小导致切不动时强制前进, 避免死循环
                piece = remaining[:1]
            chunks.append(piece)
            remaining = remaining[len(piece):]
        cur = remaining
    return chunks, cur


def _split_core(
    text: str,
    budget: int,
    size_of: Callable[[str], int],
    clip: Callable[[str, int], str],
) -> list[str]:
    """按段落块贪心装箱切分, 每段 ≤ budget (不含续接标记)。"""
    if size_of(text) <= budget:
        return [text]
    chunks: list[str] = []
    cur = ""
    for block in _blocks(text):
        cand = block if not cur else cur + _JOIN_PARA + block
        if size_of(cand) <= budget:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if size_of(block) <= budget:
            cur = block
        else:
            line_chunks, cur = _pack_lines(block, budget, size_of, clip)
            chunks.extend(line_chunks)
    if cur:
        chunks.append(cur)
    return chunks


def _finalize(
    text: str,
    limit: int,
    size_of: Callable[[str], int],
    clip: Callable[[str, int], str],
) -> list[str]:
    text = (text or "").strip()
    if size_of(text) <= limit:
        return [text]
    budget = max(limit - _MARKER_RESERVE, 1)
    chunks = _split_core(text, budget, size_of, clip)
    total = len(chunks)
    if total <= 1:
        return chunks
    return [f"{c}{_JOIN_PARA}({i + 1}/{total})" for i, c in enumerate(chunks)]


def split_by_chars(text: str, max_chars: int) -> list[str]:
    """按字符数上限切分 (钉钉: 智能机器人 4000 / 群推送 markdown 20000)。

    未超限返回单元素列表 (无续接标记); 超限切成多段, 每段 ≤ max_chars。
    """
    return _finalize(text, max_chars, _size_chars, _clip_chars)


def split_by_bytes(text: str, max_bytes: int) -> list[str]:
    """按 UTF-8 字节上限切分 (企微: 智能机器人 20480 / 群推送 markdown 4096)。

    不切断多字节字符; 语义同 split_by_chars。
    """
    return _finalize(text, max_bytes, _size_bytes, _clip_bytes)
