"""AI 输出数字忠实度闸门 (app/services/ai_number_gate.py) 契约测试。

覆盖设计文档 docs/ai-number-fidelity-plan.md §12 测试矩阵:
- 14 条对抗用例 (Vibe number_gate 移植 + Python 增强项)
- 规范化 / 豁免 / bound 匹配 / 算式重算 / 方向词
- 流式行缓冲包装 (gate_ndjson_stream) 与 fail-closed 语义
- 白名单采集契约 (ai_number_whitelist)
- 接线不被拆断言 (源码正则, 同 Vibe number_gate.test.ts 先例)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import preferences
from app.services.ai_number_gate import (
    MARK_OPEN,
    MARKED_RE,
    NUMBER_RULE_PROMPT,
    NumberPool,
    gate_ndjson_stream,
    gate_text,
    register_gate_labels,
    reset_gate_labels,
    scan_text_numbers,
    wrap_marked,
)
from app.services.ai_number_whitelist import collect_whitelist


@pytest.fixture(autouse=True)
def _isolate_labels():
    """词表注册是模块级全局态 — 每条用例前后恢复内置词表, 保证隔离。"""
    yield
    reset_gate_labels()


def _pool(*texts: str, objs=()) -> NumberPool:
    return collect_whitelist(objs, texts=texts)


# ---------------------------------------------------------------------------
# 设计文档 §12.1 对抗用例 1-14
# ---------------------------------------------------------------------------

def test_case01_magnitude_swap_masked():
    """用例 1: 资料"2 万元", 回答说"2 亿元" → 数量级造假, 标出 (数字保留可见)。"""
    pool = _pool("营收 2 万元")
    res = gate_text("营收 2 亿元", pool)
    assert res.masked == 1
    assert wrap_marked("2 亿元") in res.text
    # 标记外的裸数字不复存在 (已包进 〖…〗)
    assert "2 亿元" not in MARKED_RE.sub("", res.text)


def test_case02_fabricated_operand_masked():
    """用例 2: "300 ÷ 8 = 37.5", 8 有出处、300 编的 → 300 与结果都打码。"""
    pool = _pool("共 8 只个股")  # 8 是小整数, 豁免形态进不了池, 但算式核对认可豁免运算数
    res = gate_text("300 ÷ 8 = 37.5", pool)
    assert res.masked == 2
    assert wrap_marked("300") in res.text
    assert "300" not in MARKED_RE.sub("", res.text)
    assert "37.5" not in MARKED_RE.sub("", res.text)


def test_case03_arith_wrong_result_masked():
    """用例 3: "100 ÷ 8 = 13.5" 算错 (应为 12.5) → 结果打码。"""
    pool = _pool("总量 100, 共 8 只")
    res = gate_text("100 ÷ 8 = 13.5", pool)
    assert res.masked == 1
    assert "13.5" not in MARKED_RE.sub("", res.text)
    assert any(v.reason == "arith_wrong" for v in res.violations)


def test_case04_dim_mismatch_masked():
    """用例 4: "2 元 + 50% = 2.5 元" 量纲混加 → 结果打码。"""
    pool = _pool("现价 2 元, 涨幅 50%")
    res = gate_text("2 元 + 50% = 2.5 元", pool)
    assert res.masked == 1
    assert "2.5" not in MARKED_RE.sub("", res.text)


def test_case05_json_fence_still_checked():
    """用例 5: ```json 围栏永远核对 (数据主张常伪装成 JSON)。"""
    pool = _pool("营收 1234.5 万元")
    res = gate_text('```json\n{"revenue": 9876.5}\n```', pool)
    assert res.masked == 1
    assert "9876.5" not in MARKED_RE.sub("", res.text)


def test_case06_range_dash_preserved():
    """用例 6: "区间 2.5-3.75 倍", 3.75 无出处 → 标出但保留区间横线。"""
    pool = _pool("下沿 2.5 倍")
    res = gate_text("区间 2.5-3.75 倍", pool)
    assert res.masked == 1
    assert f"2.5-{wrap_marked('3.75 倍')}" in res.text


def test_case07_ratio_colon_checked_time_exempt():
    """用例 7: "配比 35:65" 不是合法时刻 → 核对; "09:35" 是时刻 → 豁免。"""
    pool = _pool("Some text without numbers")  # 池非空但无 35/65
    res = gate_text("配比 35:65", pool)
    assert res.masked == 2
    res2 = gate_text("09:35 开盘拉升", pool)
    assert res2.masked == 0


def test_case08_unregistered_label_checked():
    """用例 8: 词表未注册的名词数字照常核对 (宁严勿漏); 注册后豁免。"""
    pool = _pool("涨幅 1.2%")
    res = gate_text("XX500 涨 1.2%", pool)
    assert res.masked == 1  # 500 无出处被打码, 1.2 有出处放行
    register_gate_labels("XX500")
    res2 = gate_text("XX500 涨 1.2%", pool)
    assert res2.masked == 0
    # 内置词表: 沪深300 默认注册
    res3 = gate_text("沪深300 涨 1.2%", pool)
    assert res3.masked == 0


def test_case09_fullwidth_digits_caught():
    """用例 9 (Python 增强): 全角"１５６.３"必须抓到 (Vibe 漏检)。"""
    pool = _pool("股价 10.5 元")
    res = gate_text("股价１５６.３元", pool)
    assert res.masked == 1
    assert "１５６.３" not in MARKED_RE.sub("", res.text)


def test_case10_zero_width_caught():
    """用例 10 (Python 增强): 零宽字符切分的"1\\u200b56.3"必须抓到。"""
    pool = _pool("股价 10.5 元")
    res = gate_text("股价 1\u200b56.3 元", pool)
    assert res.masked == 1
    assert "56.3" not in MARKED_RE.sub("", res.text)


def test_case11_traditional_unit_caught():
    """用例 11 (Python 增强): 繁体"2 億元"归一化后照常核对。"""
    pool = _pool("营收 2 万元")
    res = gate_text("營收 2 億元", pool)
    assert res.masked == 1
    # 简体资料能替繁体回答作证 (同一形态族)
    pool2 = _pool("营收 2 亿元")
    res2 = gate_text("營收 2 億元", pool2)
    assert res2.masked == 0


def test_case12_idempotent():
    """用例 12: 标记结果再过一遍闸门 → 不变 (页脚/〖…〗标记自身豁免)。"""
    pool = _pool("营收 2 万元")
    once = gate_text("营收 2 亿元, 净利 999 万", pool)
    assert once.masked == 2
    footer = f"\n> ※ 另有 {once.masked} 处数字无出处, 已标为「〖…〗」。"
    twice = gate_text(once.text + footer, pool)
    assert twice.masked == 0
    assert twice.text == once.text + footer


def test_case13_empty_pool_fail_closed():
    """用例 13: 白名单为空 → 非豁免数字全部打码, 日期等豁免形态保留。"""
    pool = collect_whitelist()
    assert pool.empty_reason is not None
    res = gate_text("2024-10-08 收盘 10.5 元, 涨 5.2%", pool)
    assert res.masked == 2
    assert "2024-10-08" in res.text  # 日期豁免不受 fail-closed 影响
    assert all(v.reason == "no_whitelist" for v in res.violations)


def test_case14_direction_words():
    """用例 14: "下降 5.2%" vs 资料 -5.2 → 放行; "上升 1.92%" vs -1.92 → 方向反打码。"""
    pool = collect_whitelist(({"pct_chg": -5.2, "chg": -1.92},))
    res = gate_text("下降 5.2%", pool)
    assert res.masked == 0
    res2 = gate_text("同比上升 1.92%", pool)
    assert res2.masked == 1
    assert any(v.reason == "direction_conflict" for v in res2.violations)


# ---------------------------------------------------------------------------
# 豁免清单
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "2024-10-08 大涨",
    "10月28日 大涨",
    "10-08 大涨",
    "09:35 拉升, 14:30:00 回落",
    "1. 第一点\n2. 第二点",
    "3、第三点",
    "股票代码 600519 与 SZ000001 与 600519.SH",
    "MACD(12,26,9) 金叉",
    "MA5 上穿 60日均线",
    "近5日 连续 3 个交易日上涨",
    "第三章 讲了 5 个理由",
])
def test_exempt_forms_not_checked(text):
    """豁免形态即使白名单为空 (fail-closed) 也不打码。"""
    res = gate_text(text, collect_whitelist())
    assert res.masked == 0, f"{text!r} 被误打码: {res.text!r}"


def test_code_with_unit_is_amount_not_code():
    """6 位数字后跟单位 → 是金额不是股票代码, 照常核对。"""
    res = gate_text("流出 123456元", _pool("无关 9.9"))
    assert res.masked == 1


def test_small_int_exempt_but_with_unit_checked():
    """小整数计数豁免 (|n|<=20 无单位); 带数据单位的照常核对。"""
    pool = _pool("无关 9.9")
    assert gate_text("给出 3 个理由", pool).masked == 0
    assert gate_text("涨幅 5%", pool).masked == 1
    assert gate_text("成交 11 期", pool).masked == 1


# ---------------------------------------------------------------------------
# bound 匹配 / 双口径 / 容差
# ---------------------------------------------------------------------------

def test_bound_tolerance_and_decimals():
    """展示几位小数, 容差就是半个末位 (0.5*10^-d) 与相对容差取大。"""
    pool = _pool("收盘 10.50 元")
    assert gate_text("收盘 10.5 元", pool).masked == 0
    assert gate_text("收盘 10.6 元", pool).masked == 1


def test_percent_dual_caliber():
    """结构化 pct 字段双口径: 0.052 与 5.2 都能替 "5.2%" 作证。"""
    pool = collect_whitelist(({"pct_chg": 0.052},))
    assert gate_text("涨幅 5.2%", pool).masked == 0
    pool2 = collect_whitelist(({"pct_chg": 5.2},))
    assert gate_text("涨幅 5.2%", pool2).masked == 0
    assert gate_text("涨幅 0.052", pool2).masked == 0


def test_plain_unit_no_cross_magnitude_vouching():
    """写了量级单位只认同单位原样读或换算读 — "2 万" 不能替 "2 亿" 作证。"""
    pool = _pool("市值 2 万")
    assert gate_text("市值 2 万", pool).masked == 0
    assert gate_text("市值 2 亿", pool).masked == 1
    # 换算读: 资料 20000 (裸), 回答 "2 万" → 2e4 命中 all 池
    pool2 = collect_whitelist(({"amount": 20000},))
    assert gate_text("成交 2 万", pool2).masked == 0


def test_thousands_separator():
    """千分位写法 "1,234.5" 与裸值同族。"""
    pool = collect_whitelist(({"amount": 1234.5},))
    assert gate_text("成交 1,234.5 手", pool).masked == 0


# ---------------------------------------------------------------------------
# 算式重算 / 链式派生
# ---------------------------------------------------------------------------

def test_arith_chain_derived():
    """算式结果进 derived 集合, 后文引用不再要求原始出处 (链式)。"""
    pool = collect_whitelist(({"close": 110.5, "pre_close": 100},))
    text = "(110.5 - 100) ÷ 100 = 10.5%\n涨幅 10.5% 属实"
    res = gate_text(text, pool)  # 单段调用: derived 在同一次内跨行携带
    assert res.masked == 0


async def test_arith_result_reused_across_stream_lines(gate_on):
    """流式下上一行算式结果可为下一行作证 (derived 跨行携带)。"""
    pool = collect_whitelist(({"a": 300, "b": 8},))

    async def _src():
        yield json.dumps({"type": "delta", "content": "300 ÷ 8 = 37.5\n"}, ensure_ascii=False)
        yield json.dumps({"type": "delta", "content": "均值 37.5 元\n"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    lines = await _arun(_src(), pool)
    joined = "".join(_delta_content(ln) for ln in lines if _evt_type(ln) == "delta")
    assert MARK_OPEN not in joined


def test_safe_eval_rejects_dangerous_expr():
    """算式求值只放行四则子集 — 名字/属性/下标/调用一律拒绝。"""
    from app.services.ai_number_gate import _safe_eval

    assert _safe_eval("1+2*3") == 7
    assert _safe_eval("__import__('os').system('ls')") is None
    assert _safe_eval("open('/etc/passwd')") is None


# ---------------------------------------------------------------------------
# 流式包装 gate_ndjson_stream
# ---------------------------------------------------------------------------

@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(preferences, "get_ai_number_gate_enabled", lambda: True)


def _evt_type(line: str) -> str:
    return json.loads(line).get("type", "")


def _delta_content(line: str) -> str:
    return json.loads(line).get("content", "")


async def _arun(src, pool) -> list[str]:
    return [ln async for ln in gate_ndjson_stream(src, pool)]


async def test_stream_disabled_passthrough(monkeypatch):
    """开关关闭 → 纯透传, 一个字节都不改 (灰度默认态零风险)。"""
    monkeypatch.setattr(preferences, "get_ai_number_gate_enabled", lambda: False)

    async def _src():
        yield json.dumps({"type": "delta", "content": "编造 999.9 万"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), None)
    assert out[0] == json.dumps({"type": "delta", "content": "编造 999.9 万"}, ensure_ascii=False)
    assert "numbers_masked" not in out[1]


async def test_stream_buffers_split_number(gate_on):
    """chunk 把数字劈成两半 ("+2." / "35%") → 行缓冲后按完整数字核对。"""
    pool = _pool("涨幅 5%")

    async def _src():
        yield json.dumps({"type": "delta", "content": "今日+2."}, ensure_ascii=False)
        yield json.dumps({"type": "delta", "content": "35% 无出处\n"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), pool)
    deltas = [_delta_content(ln) for ln in out if _evt_type(ln) == "delta"]
    body = "".join(deltas)
    assert MARK_OPEN in body
    # 数字绝不在核对前流出: 第一条 delta 已是完整核对过的行;
    # 无出处数字只以 〖…〗 标记形式出现, 标记外无裸数字
    assert "+2.35%" not in MARKED_RE.sub("", body)
    assert "35" not in MARKED_RE.sub("", deltas[0])


async def test_stream_done_carries_count_and_footer(gate_on):
    """done 事件注入 numbers_masked, 页脚 delta 在 done 之前追加。"""
    pool = _pool("涨幅 5%")

    async def _src():
        yield json.dumps({"type": "delta", "content": "编造 999.9 万"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), pool)
    types = [_evt_type(ln) for ln in out]
    assert types[-1] == "done"
    done = json.loads(out[-1])
    assert done["numbers_masked"] == 1
    footer = _delta_content(out[-2])
    assert "※ 另有 1 处" in footer
    # 正文不以换行结尾 → 页脚自带单个前导换行 (引用行, 与紧邻免责声明合并)
    assert footer.startswith("\n> ")
    # 页脚只出现一次 (done 与 finish 不重复追加)
    assert sum("※ 另有" in _delta_content(ln) for ln in out if _evt_type(ln) == "delta") == 1


async def test_stream_footer_merges_with_disclaimer(gate_on):
    """正文以换行结尾时页脚不再加前导换行 — 与 AI 免责声明的 "> " 行
    紧邻, markdown 渲染合并进同一引用块 (用户要求: 提示并入免责那块)。"""
    pool = _pool("涨幅 5%")

    async def _src():
        yield json.dumps({"type": "delta", "content": "编造 999 万\n"}, ensure_ascii=False)
        yield json.dumps({"type": "delta", "content": "> ⚠️ 本内容由 AI 生成, 入市需谨慎。\n"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), pool)
    body = "".join(_delta_content(ln) for ln in out if _evt_type(ln) == "delta")
    assert "入市需谨慎。\n> ※ 另有 1 处" in body  # 无空行隔断 → 同一引用块
    assert "\n\n>" not in body


async def test_stream_none_pool_fail_closed(gate_on):
    """pool=None → fail-closed 全打码 (接线遗漏时安全侧兜底)。"""
    async def _src():
        yield json.dumps({"type": "delta", "content": "收盘 10.5 元\n"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), None)
    body = "".join(_delta_content(ln) for ln in out if _evt_type(ln) == "delta")
    assert "10.5" not in MARKED_RE.sub("", body)


async def test_stream_callable_pool(gate_on):
    """callable 池: 助手链路白名单随工具轮次增长, 每行重解析。"""
    calls = {"n": 0}
    pool1 = _pool("无关 9.9")
    pool2 = _pool("收盘 10.5 元")

    def factory():
        calls["n"] += 1
        return pool1 if calls["n"] == 1 else pool2

    async def _src():
        yield json.dumps({"type": "delta", "content": "第一行 10.5 元\n第二行 10.5 元\n"},
                         ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), factory)
    body = "".join(_delta_content(ln) for ln in out if _evt_type(ln) == "delta")
    lines = body.split("\n")
    assert MARK_OPEN in lines[0]      # 第一行时池里还没有 10.5
    assert MARK_OPEN not in lines[1]  # 第二行池已更新 → 放行


async def test_stream_news_section_relaxed(gate_on):
    """消息面/新闻章节: 只记录不打码 (联网内容不在本轮数据里, 属预期)。"""
    pool = _pool("涨幅 5%")

    async def _src():
        yield json.dumps({"type": "delta", "content": "## 消息面\n"}, ensure_ascii=False)
        yield json.dumps({"type": "delta", "content": "传闻营收 8888 万\n"}, ensure_ascii=False)
        yield json.dumps({"type": "delta", "content": "## 技术面\n"}, ensure_ascii=False)
        yield json.dumps({"type": "delta", "content": "目标价 8888 万\n"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), pool)
    body = "".join(_delta_content(ln) for ln in out if _evt_type(ln) == "delta")
    assert "传闻营收 8888 万" in body     # 消息面章节放行
    assert "## 技术面" in body
    tech = body.split("## 技术面")[1]
    assert wrap_marked("8888 万") in tech   # 回到普通章节恢复标记
    assert "8888" not in MARKED_RE.sub("", tech)
    done = json.loads(out[-1])
    assert done["numbers_masked"] == 1   # 宽松章节不计入打码数


async def test_stream_non_delta_events_passthrough(gate_on):
    """meta/notice/error 等非 delta 事件原样透传, 且触发余量冲刷。"""
    pool = _pool("涨幅 5%")

    async def _src():
        yield json.dumps({"type": "delta", "content": "半行 999.9"}, ensure_ascii=False)
        yield json.dumps({"type": "notice", "message": "提示"}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    out = await _arun(_src(), pool)
    types = [_evt_type(ln) for ln in out]
    assert types == ["delta", "notice", "delta", "done"]  # 余量在 notice 前冲刷 + 页脚
    assert MARK_OPEN in _delta_content(out[0])


async def test_stream_source_exception_propagates(gate_on):
    """源流异常原样透传 — 由各 analyzer 外层 try/except 收敛成 error 事件。"""
    pool = _pool("涨幅 5%")

    async def _src():
        yield json.dumps({"type": "delta", "content": "开头\n"}, ensure_ascii=False)
        raise RuntimeError("upstream boom")

    with pytest.raises(RuntimeError, match="upstream boom"):
        await _arun(_src(), pool)


# ---------------------------------------------------------------------------
# 白名单采集契约 (ai_number_whitelist)
# ---------------------------------------------------------------------------

def test_whitelist_empty_fail_closed():
    pool = collect_whitelist()
    assert pool.empty_reason == "no_numbers_collected"
    assert pool.all == ()


def test_whitelist_rank_sentinel_skipped():
    """轮动哨兵 rank=999 (缺数据) 不得进池 — 否则"排名第 999"被放行。"""
    pool = collect_whitelist(({"rank": 999, "pct": 3.2},))
    assert 999.0 not in pool.all
    assert 3.2 in pool.all


def test_whitelist_bool_skipped():
    pool = collect_whitelist(({"flag": True, "other": False, "v": 2.5},))
    assert 1.0 not in pool.all
    assert 0.0 not in pool.all
    assert 2.5 in pool.all


def test_whitelist_amount_yi_form():
    """金额字段 >=1e6 自动补"亿"展示形态 (财务数据常以元存、以亿展示)。"""
    pool = collect_whitelist(({"revenue": 2.5e8},))
    assert gate_text("营收 2.5 亿元", pool).masked == 0


def test_whitelist_strings_skipped():
    """字符串值不进结构化池 (防止把代码/名称里的数字洗白)。"""
    pool = collect_whitelist(({"name": "600519", "v": 1.5},))
    assert 600519.0 not in pool.all
    assert 1.5 in pool.all


def test_scan_text_numbers_symmetric():
    """文本扫描与闸门同管线: 豁免形态 (日期) 不进池, 数据形态进池。"""
    got = scan_text_numbers("2024-10-08 涨 5.2%, 收盘 10.50 元")
    assert (5.2, "%") in got
    assert any(v == 10.5 for v, _ in got)
    assert not any(v == 2024 for v, _ in got)


# ---------------------------------------------------------------------------
# 提示词规则
# ---------------------------------------------------------------------------

def test_rule_prompt_contains_no_digits():
    """规则文本刻意零数字 — 系统提示词也进白名单, 示例数字会留假放行口子。"""
    assert not any(c.isdigit() for c in NUMBER_RULE_PROMPT)
    assert MARK_OPEN in NUMBER_RULE_PROMPT  # 规则里告知标记形式


# ---------------------------------------------------------------------------
# 接线不被拆断言 (Vibe number_gate.test.ts:254-259 源码正则先例)
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parent.parent

@pytest.mark.parametrize("relpath", [
    "app/services/stock_analyzer.py",
    "app/services/market_recap.py",
    "app/services/financial_analyzer.py",
    "app/services/concept_rotation_analyzer.py",
    "app/custom/assistant/chat_service.py",
])
def test_number_gate_wired_into_all_streams(relpath):
    src = (_BACKEND / relpath).read_text(encoding="utf-8")
    assert "gate_ndjson_stream(" in src, f"{relpath} 的闸门接线被拆"


@pytest.mark.parametrize("relpath", [
    "app/services/stock_analyzer.py",
    "app/services/market_recap.py",
    "app/services/financial_analyzer.py",
    "app/services/concept_rotation_analyzer.py",
    "app/custom/assistant/prompt.py",
])
def test_number_rule_prompt_appended(relpath):
    src = (_BACKEND / relpath).read_text(encoding="utf-8")
    assert "NUMBER_RULE_PROMPT" in src, f"{relpath} 未追加数字出处纪律提示词"


# ---------------------------------------------------------------------------
# 偏好开关
# ---------------------------------------------------------------------------

def test_preference_default_off(monkeypatch):
    """默认关 (灰度): 未配置时 get 返回 False, 流式包装纯透传。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    assert preferences.get_ai_number_gate_enabled() is False


def test_pop_gate_footer():
    """IM 组装用: 从正文末尾摘出页脚引用行, 无页脚时原样返回。"""
    from app.services.ai_number_gate import pop_gate_footer

    body = "分析正文。\n\n> ⚠️ 本内容由 AI 生成, 入市需谨慎。"
    footer = "\n> ※ 另有 2 处数字在本轮提供的数据里找不到出处, 已标为「〖…〗」。"
    kept, popped = pop_gate_footer(body + footer)
    assert popped is not None and popped.startswith("> ※ 另有 2 处")
    assert kept == body  # 正文尾部空白收敛, 页脚完整摘出

    kept2, popped2 = pop_gate_footer(body)
    assert popped2 is None and kept2 == body


def test_settings_api_roundtrip(tmp_path, monkeypatch):
    """PUT /preferences/ai-number-gate 落盘 + GET /preferences 回显。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import router

    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.put("/api/settings/preferences/ai-number-gate", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json() == {"ai_number_gate_enabled": True}
        assert preferences.get_ai_number_gate_enabled() is True
        assert client.get("/api/settings/preferences").json()["ai_number_gate_enabled"] is True

        resp = client.put("/api/settings/preferences/ai-number-gate", json={"enabled": False})
        assert resp.json() == {"ai_number_gate_enabled": False}
        assert preferences.get_ai_number_gate_enabled() is False
    finally:
        preferences._invalidate_cache()
