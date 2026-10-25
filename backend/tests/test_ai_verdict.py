"""AI 研判卡 (ai_verdict) 测试。

覆盖:
  - parse_verdict / strip_verdict: 合法卡、枚举外、JSON 损坏、缺失、多卡取尾、reason 截断
  - verdict_ndjson_stream: 标记剥离 (客户端永不可见)、verdict 事件时序、
    标记跨 chunk 切分、meta/error 透传、error 后无 verdict、fail-honest ok=false
  - 与数字闸门的组合次序: verdict 在闸门内侧, VERDICT JSON 数字不被〖〗包裹
  - 接线不被拆断言 (源码正则, 同 test_ai_number_gate 先例)
"""
from __future__ import annotations

import inspect
import json

from app.services.ai_verdict import (
    VERDICT_DIRECTIONS,
    VERDICT_PROMPT,
    parse_verdict,
    strip_verdict,
    verdict_ndjson_stream,
)


def _delta(content: str) -> str:
    return json.dumps({"type": "delta", "content": content}, ensure_ascii=False)


def _evt(line: str) -> dict:
    return json.loads(line)


async def _collect(events: list[str]) -> list[dict]:
    async def _src():
        for e in events:
            yield e
    return [_evt(ln) async for ln in verdict_ndjson_stream(_src())]


# ---------------------------------------------------------------------------
# parse_verdict / strip_verdict
# ---------------------------------------------------------------------------

def test_parse_valid():
    text = '正文。\n<!-- VERDICT: {"direction": "偏多", "reason": "站上20日均线"} -->'
    assert parse_verdict(text) == {"direction": "偏多", "reason": "站上20日均线"}


def test_parse_all_directions():
    for d in VERDICT_DIRECTIONS:
        text = f'<!-- VERDICT: {{"direction": "{d}", "reason": "x"}} -->'
        assert parse_verdict(text)["direction"] == d


def test_parse_invalid_enum_rejected():
    """枚举外值 (含近义词) 一律 None — 绝不模糊映射/猜测。"""
    assert parse_verdict('<!-- VERDICT: {"direction": "看涨", "reason": "x"} -->') is None
    assert parse_verdict('<!-- VERDICT: {"direction": "BUY", "reason": "x"} -->') is None
    assert parse_verdict('<!-- VERDICT: {"direction": "", "reason": "x"} -->') is None


def test_parse_broken_json():
    assert parse_verdict("<!-- VERDICT: {direction: 看多} -->") is None
    assert parse_verdict("<!-- VERDICT: 看多 -->") is None


def test_parse_missing():
    assert parse_verdict("纯正文, 没有卡。") is None


def test_parse_takes_last_marker():
    """模型复述示例时以末尾为准。"""
    text = (
        '<!-- VERDICT: {"direction": "看空", "reason": "示例"} -->\n'
        '正文\n'
        '<!-- VERDICT: {"direction": "看多", "reason": "真实结论"} -->'
    )
    assert parse_verdict(text) == {"direction": "看多", "reason": "真实结论"}


def test_parse_reason_truncated():
    long = "很长" * 50
    v = parse_verdict(f'<!-- VERDICT: {{"direction": "中性", "reason": "{long}"}} -->')
    assert v is not None and len(v["reason"]) <= 61  # 60 + 省略号


def test_strip_verdict():
    text = '正文。\n<!-- VERDICT: {"direction": "看多", "reason": "突破"} -->\n'
    clean, v = strip_verdict(text)
    assert "VERDICT" not in clean and v["direction"] == "看多"


# ---------------------------------------------------------------------------
# verdict_ndjson_stream
# ---------------------------------------------------------------------------

async def test_stream_marker_never_visible_and_event_before_done():
    out = await _collect([
        _delta("结论如下。\n"),
        _delta('<!-- VERDICT: {"direction": "看多", "reason": "放量突破"} -->\n'),
        json.dumps({"type": "done"}),
    ])
    types = [e["type"] for e in out]
    assert types == ["delta", "verdict", "done"]
    # 客户端可见文本里绝无标记残片
    body = "".join(e.get("content", "") for e in out if e["type"] == "delta")
    assert "VERDICT" not in body and "<!--" not in body
    assert body == "结论如下。\n"  # 标记独占行 → 整行丢弃, 不留空行
    assert out[1] == {"type": "verdict", "ok": True, "direction": "看多", "reason": "放量突破"}


async def test_stream_marker_split_across_chunks():
    """标记被切在任意 chunk 边界 → 行缓冲保证不泄漏残片。"""
    out = await _collect([
        _delta("正文\n<!-- VERD"),
        _delta('ICT: {"direction": "偏空", "re'),
        _delta('ason": "破位"} -->\n'),
        json.dumps({"type": "done"}),
    ])
    body = "".join(e.get("content", "") for e in out if e["type"] == "delta")
    assert body == "正文\n"
    verdict = [e for e in out if e["type"] == "verdict"]
    assert verdict == [{"type": "verdict", "ok": True, "direction": "偏空", "reason": "破位"}]


async def test_stream_inline_marker_after_text():
    """标记跟在正文同一行 → 只剥标记, 正文保留。"""
    out = await _collect([
        _delta('结尾 <!-- VERDICT: {"direction": "中性", "reason": "观望"} -->\n'),
        json.dumps({"type": "done"}),
    ])
    body = "".join(e.get("content", "") for e in out if e["type"] == "delta")
    assert body == "结尾 \n"


async def test_stream_missing_marker_fail_honest():
    """没交卡 → ok=false 明示, 绝不从正文猜。"""
    out = await _collect([_delta("正文。\n"), json.dumps({"type": "done"})])
    assert out[-2] == {"type": "verdict", "ok": False}
    assert out[-1]["type"] == "done"


async def test_stream_invalid_marker_fail_honest():
    out = await _collect([
        _delta("正文\n"),
        _delta('<!-- VERDICT: {"direction": "大涨", "reason": "x"} -->\n'),
        json.dumps({"type": "done"}),
    ])
    verdict = [e for e in out if e["type"] == "verdict"]
    assert verdict == [{"type": "verdict", "ok": False}]


async def test_stream_meta_passthrough_and_error_no_verdict():
    out = await _collect([
        json.dumps({"type": "meta", "symbol": "600519.SH"}),
        _delta("半句"),
        json.dumps({"type": "error", "message": "AI 分析失败"}),
    ])
    types = [e["type"] for e in out]
    # meta 透传; error 前先冲刷已缓冲文本; error 后不产出 verdict
    assert types == ["meta", "delta", "error"]
    assert out[1]["content"] == "半句"


async def test_stream_flush_before_done_keeps_partial_line():
    """done 时冲刷无换行余量 (末尾没换行的正文不丢)。"""
    out = await _collect([_delta("结尾无换行"), json.dumps({"type": "done"})])
    body = "".join(e.get("content", "") for e in out if e["type"] == "delta")
    assert body == "结尾无换行"


# ---------------------------------------------------------------------------
# 与数字闸门的组合 (verdict 在内侧, JSON 数字不被〖〗破坏)
# ---------------------------------------------------------------------------

async def test_verdict_inside_gate_json_not_wrapped():
    from app.services.ai_number_gate import gate_ndjson_stream
    from app.services.ai_number_whitelist import collect_whitelist

    pool = collect_whitelist(texts=("涨幅 5%",))

    async def _src():
        yield _delta("涨幅 5% 属实, 目标 88.8 元。\n")
        yield _delta('<!-- VERDICT: {"direction": "看多", "reason": "站上20日均线"} -->\n')
        yield json.dumps({"type": "done"})

    out = [_evt(ln) async for ln in gate_ndjson_stream(verdict_ndjson_stream(_src()), pool)]
    body = "".join(e.get("content", "") for e in out if e["type"] == "delta")
    assert "〖5%〗" not in body and "〖5%" not in body   # 有出处放行
    assert "〖88.8 元※〗" in body                        # 无出处标记
    assert "VERDICT" not in body                          # 标记已剥离
    verdict = [e for e in out if e["type"] == "verdict"]
    # VERDICT JSON 里的 "20" 未被闸门包裹 → 解析仍成功
    assert verdict == [{"type": "verdict", "ok": True, "direction": "看多", "reason": "站上20日均线"}]


# ---------------------------------------------------------------------------
# 接线不被拆断言 (源码正则先例: test_ai_number_gate)
# ---------------------------------------------------------------------------

def test_wiring_prompt_contains_verdict_rule():
    from app.services.stock_analyzer import _build_system_prompt

    for variant in (True, False):
        assert VERDICT_PROMPT in _build_system_prompt(variant)


def test_wiring_verdict_prompt_has_no_digits():
    """提示词零阿拉伯数字 — 系统提示词进闸门白名单, 数字会留假放行口子。"""
    assert not any(c.isdigit() for c in VERDICT_PROMPT)


def test_wiring_stream_nesting_order():
    """verdict 剥离必须包在闸门内侧: gate(verdict(deltas))。"""
    import app.services.stock_analyzer as sa

    src = inspect.getsource(sa.analyze_stock_stream)
    assert "gate_ndjson_stream(verdict_ndjson_stream(" in src


def test_wiring_text_path_consumes_verdict_event():
    """IM 路径: analyze_stock_text 捕获 verdict 事件并插到闸门页脚之前。"""
    import app.services.stock_analyzer as sa

    src = inspect.getsource(sa.analyze_stock_text)
    assert '"verdict"' in src
    assert "pop_gate_footer" in src
    assert "未解析出研判结论" in src
