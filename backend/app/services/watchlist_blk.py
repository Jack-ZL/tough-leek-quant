"""通达信板块文件（``.blk`` / ``.ebk``）导入：市场前缀码 → 六位代码 → instruments 校验。

通达信自定义板块（``T0002/blocknew/*.blk``）每行一个标的，**首字符是市场标识**
（``0``=深圳 / ``1``=上海 / ``2``=北京），其余六位是证券代码；而导出的 ``.ebk``
通常是裸六位码。两种形态在同一份文件里混着出现也见过，所以按**行**分别判定。

绝不"统一砍掉首字符"：那会把 ``.ebk`` 的 ``600000`` 砍成 ``00000``，或把裸码
``000001`` 砍成 ``00001`` —— 静默错译成另一只证券。与 P1-17 通达信公式翻译层同一
口径：**错译比报错危险**。判不出形态的行原样进候选并标记未匹配 + 原因，让用户看见
"哪一行没被认出来"，而不是悄悄丢掉。

板块**中文名**存在 ``blocknew.cfg``（定长 120 字节/条：50B 名称 + 70B 文件名，GBK），
单个 ``.blk`` 文件里没有，所以这里只能用文件名主干当建议分组名，用户可改。

落点与 CSV/OCR 导入一致：只产出候选，不落盘、不建分组；写入由前端确认后走
``watchlist.add_batch`` 的 group_ids M:N 并入语义。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.watchlist_csv import _finalize, decode_csv_bytes
from app.services.watchlist_ocr.pipeline import (
    ImportCandidate,
    build_instrument_lookups,
    resolve_candidates,
)

# 通达信 .blk 行首的市场标识。与 mootdx 写盘口径一致（get_stock_market → MARKET_*）。
_MARKET_SUFFIX = {"0": ".SZ", "1": ".SH", "2": ".BJ"}

# 行内首个 token。标准 .blk 是一行一码，但用户也可能粘贴 "1600000 浦发银行"
# 或从表格复制出 "1600000,浦发银行"，取首段即可，名字交给主数据回填。
_TOKEN_SPLIT_RE = re.compile(r"[\s,|;\t]+")

# 带市场字母前缀的写法（sh600000 / SZ000001），同花顺与东财导出常见。
_PREFIXED_RE = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)

# 一次导入的候选上限，与粘贴代码路径同口径，防止超大文件把确认列表撑爆。
_MAX_CODES = 5000


@dataclass
class BlkLine:
    """一行板块文件的解析结果。``code`` 为 None 时 ``reason`` 必有值。"""

    lineno: int
    raw: str
    code: str | None
    market: str | None
    reason: str | None


def parse_blk_line(lineno: int, raw: str) -> BlkLine | None:
    """解析单行；空行返回 None（不计入候选，也不算跳过）。

    判定顺序刻意是"先精确形态、后兜底拒绝"：
    6 位裸码 / 7 位市场前缀码 / 字母前缀码三种形态之外的都不猜。
    """
    line = raw.strip()
    if not line:
        return None
    token = _TOKEN_SPLIT_RE.split(line)[0]

    if len(token) == 6 and token.isdigit():
        return BlkLine(lineno, line, token, None, None)

    if len(token) == 7 and token[0] in _MARKET_SUFFIX and token[1:].isdigit():
        return BlkLine(lineno, line, token[1:], _MARKET_SUFFIX[token[0]], None)

    m = _PREFIXED_RE.match(token)
    if m:
        return BlkLine(lineno, line, m.group(2), f".{m.group(1).upper()}", None)

    # 到这里一律拒绝，不做任何"砍一位/补一位"的猜测
    if len(token) == 7 and token[1:].isdigit():
        reason = f"市场标识 '{token[0]}' 不是 0(深)/1(沪)/2(京)"
    elif len(token) == 6:
        reason = "不是 6 位数字代码（指数如 1A0001 不支持）"
    else:
        reason = f"既不是 6 位裸码也不是 7 位市场前缀码（{len(token)} 字符）"
    return BlkLine(lineno, line, None, None, reason)


def parse_blk_lines(text: str) -> list[BlkLine]:
    """逐行解析整份文本，保留行序，空行跳过。"""
    if not text.strip():
        return []
    out: list[BlkLine] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        parsed = parse_blk_line(lineno, raw)
        if parsed is not None:
            out.append(parsed)
    return out


def suggest_group_name(filename: str | None) -> str:
    """建议分组名 = 文件名主干。

    ``.blk`` 里不带板块名，通达信把中文名放在 ``blocknew.cfg``；单文件导入拿不到，
    而外部工具批量生成的 ``.blk`` 文件名往往只是时间戳短码，所以这只是**建议**，
    前端预填后允许用户改。
    """
    if not filename:
        return ""
    # 浏览器给的是 basename，但保险起见只取最后一段，避免把路径当分组名
    return Path(filename.replace("\\", "/")).stem.strip()


def import_watchlist_blk(
    raw: bytes | str,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """解析 ``.blk`` / ``.ebk`` 内容并返回候选列表（不写入自选）。

    返回结构与 CSV/OCR 导入一致（前端复用同一套勾选确认流程），额外带
    ``suggested_group_name``。解析不出任何代码时抛 ``ValueError``（API 层转 400），
    消息里带上首个跳过原因，避免用户只看到一句"没识别到代码"。
    """
    text = raw if isinstance(raw, str) else decode_csv_bytes(raw)
    lines = parse_blk_lines(text)
    if not lines:
        raise ValueError("文件里没有非空行")

    # 去重保序：同一只股票在板块文件里重复出现是常见的（多次追加导入）
    first_line_by_code: dict[str, BlkLine] = {}
    for ln in lines:
        if ln.code:
            first_line_by_code.setdefault(ln.code, ln)
    unique_codes = list(first_line_by_code)

    if len(unique_codes) > _MAX_CODES:
        raise ValueError(f"一次最多导入 {_MAX_CODES} 个代码，已识别 {len(unique_codes)} 个")

    code_to_symbol, symbol_to_name = build_instrument_lookups(data_dir)
    existing = existing_symbols or set()
    cand_by_code = {
        c.code: c
        for c in resolve_candidates(unique_codes, code_to_symbol, symbol_to_name, existing)
    }

    candidates: list[dict[str, Any]] = []
    emitted_codes: set[str] = set()
    for ln in lines:
        if ln.code is None:
            # 认不出的行也要让用户看见：原样进候选、标记未匹配并给原因。
            # 行号天然唯一，不参与代码去重。
            candidates.append(_rejected(ln.raw, ln.reason))
            continue
        if ln.code in emitted_codes:
            continue
        emitted_codes.add(ln.code)
        cand = cand_by_code[ln.code]
        # 声明市场与主数据后缀矛盾：可能是文件损坏，也可能是指数代码
        # （沪 000001 是上证指数、深 000001 是平安银行）。不猜，报出来。
        reason = _market_conflict(ln, cand.symbol)
        if reason:
            candidates.append(_rejected(ln.code, reason))
            continue
        candidates.append(cand.to_dict())

    if not any(c["matched"] for c in candidates):
        first_reason = next((c["reason"] for c in candidates if c.get("reason")), None)
        hint = f"；首条原因：{first_reason}" if first_reason else ""
        raise ValueError(f"未识别到可匹配主数据的证券代码（{len(candidates)} 行）{hint}")

    result = _finalize("blk", text, unique_codes, candidates)
    result["suggested_group_name"] = suggest_group_name(filename)
    return result


def _rejected(code: str, reason: str | None) -> dict[str, Any]:
    """构造一条"未匹配 + 带原因"的候选，供认不出的行与市场冲突行共用。"""
    return ImportCandidate(
        code=code, symbol=None, name=None, matched=False,
        already_in_watchlist=False, reason=reason,
    ).to_dict()


def _market_conflict(ln: BlkLine, symbol: str | None) -> str | None:
    """声明的市场后缀与主数据 symbol 后缀矛盾时返回原因，否则 None。

    symbol 没有 ``.`` 后缀时不做判断 —— 主数据本身没给市场信息，无从校验，
    此时宁可放过也不要凭空报一堆假冲突。
    """
    if not ln.market or not symbol or "." not in symbol:
        return None
    actual = "." + symbol.rsplit(".", 1)[1].upper()
    if actual == ln.market:
        return None
    return f"市场标识 {ln.market} 与主数据 {symbol} 不符"
