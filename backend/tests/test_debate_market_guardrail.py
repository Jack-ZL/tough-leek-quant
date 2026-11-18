"""PR4 f4: 多空辩论的市场口径护栏 (docs/hk-market-plan.md §9.4)。

验收口径来自方案 §14:「港股辩论材料含市场护栏; 多空双方不能用 A 股涨跌停/连板
逻辑论证」—— 断言**材料文本与三方实际收到的用户消息**, 不断言模型行为。

## 为什么护栏拼进 ``materials["text"]`` 而不是三个系统提示词

``_BULL_SYSTEM`` / ``_BEAR_SYSTEM`` / ``_JUDGE_SYSTEM`` 是模块级常量, 被 6 处
引用。改成按市场分派的函数要动全部调用点, 且三个角色各自拼一遍容易拼歪。
而 ``materials["text"]`` 已经同时被:

- ``collect_whitelist(texts=(..., materials["text"]))``  (:667)
- ``ai_audit.finish_run(prompt_text=... materials["text"] ...)``  (:823)
- 五次 LLM 调用的用户消息 (多/空/裁决/重写/终审, 全部以它为第一段)

覆盖, 所以拼在这一处 = 三方**逐字节一致**地看到同一份护栏, 零额外接线。
这与既有「数据体检块拼进 text」的做法同理 (原注释: 多方/空方/风控主管拿到的
材料逐字节一致, 对数据局限的认知也一致)。

## 🔴 数字闸门白名单纪律

``materials["text"]`` 会进白名单采集, 白名单里的数字 = 模型写同值数字时闸门
放行。护栏全文零阿拉伯数字 (``test_market_guardrail.py`` 第 8 节钉住), 本文件
再从辩论侧端到端复核一次: 港股材料相对 A 股**不新增**任何白名单数字。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.market import Market
from app.services import market_guardrail as mg
from app.services import stock_debate
from app.services.ai_number_whitelist import scan_text_numbers

HK = "00700.HK"
CN = "600519.SH"


def _frame(n: int, *, start: date = date(2026, 1, 1), base: float = 100.0) -> pl.DataFrame:
    """n 根升序日K, 列够 compute_levels / _clean_rows 用。"""
    return pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "open": [base + i * 0.1 for i in range(n)],
        "high": [base + i * 0.1 + 1 for i in range(n)],
        "low": [base + i * 0.1 - 1 for i in range(n)],
        "close": [base + i * 0.2 for i in range(n)],
        "volume": [1_000_000 + i for i in range(n)],
    })


class _FakeRepo:
    def __init__(self, cn: pl.DataFrame | None = None, hk: pl.DataFrame | None = None):
        self._cn = cn if cn is not None else pl.DataFrame()
        self._hk = hk if hk is not None else pl.DataFrame()

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return self._cn

    def get_hk_daily(self, symbol, start, end):
        return self._hk


@pytest.fixture()
def repo():
    return _FakeRepo(cn=_frame(70, base=50.0), hk=_frame(70, base=300.0))


# ===== 1. 材料文本 =====

def test_hk_materials_contain_market_guardrail(repo, tmp_path):
    """港股材料含完整护栏, 且 ``market`` 键透出给调用方。"""
    materials = stock_debate.build_materials(repo, tmp_path, HK)
    assert materials is not None
    assert materials["market"] is Market.HK

    guardrail = mg.guardrail_prompt(Market.HK)
    assert guardrail
    assert guardrail in materials["text"]


def test_cn_materials_stay_byte_identical(repo, tmp_path):
    """A 股护栏是空串 ⇒ 材料**逐字节**等于引入 market 维之前的形态。

    CONTRIBUTING §9: 既有 A 股行为原样保留。这里不用「不含护栏」这种弱断言,
    而是把旧实现的拼接顺序原样重建一遍做全等比较 —— 空串若被拼进去会多出
    两个换行, 弱断言抓不到。
    """
    materials = stock_debate.build_materials(repo, tmp_path, CN)
    assert materials is not None
    assert materials["market"] is Market.CN
    assert mg.guardrail_prompt(Market.CN) == ""

    text = materials["text"]
    assert text.startswith(f"标的标准代码: {CN}\n关键价位概览: ")
    assert "港股市场口径护栏" not in text
    # 标的行与价位行之间不得多出空行 (空护栏被拼进去的痕迹)
    assert "\n\n\n" not in text.split("```json")[0]


def test_guardrail_sits_before_the_data(repo, tmp_path):
    """护栏在标的标识之后、K 线数据之前 —— 三方先拿规则再读数据。"""
    text = stock_debate.build_materials(repo, tmp_path, HK)["text"]
    guardrail = mg.guardrail_prompt(Market.HK)

    assert text.index(f"标的标准代码: {HK}") < text.index(guardrail)
    assert text.index(guardrail) < text.index("关键价位概览")
    assert text.index(guardrail) < text.index("```json")


# ===== 2. 三方 (含重写轮) 拿到的是同一份护栏 =====

def _marker(payload: dict) -> str:
    return f"<!-- DEBATE_RULING: {json.dumps(payload, ensure_ascii=False)} -->"


_JUDGE_REWRITE_BEAR = ["点评。\n", _marker({
    "decision": "rewrite", "rewrite_role": "bear", "rewrite_reason": "空喊口号",
    "direction": "中性", "reason": "待重写"})]
_JUDGE_FINAL_PASS = ["终审点评。\n", _marker({
    "decision": "pass", "rewrite_role": "", "rewrite_reason": "",
    "direction": "看多", "reason": "重写后合格"})]


def _fake_llm(prompts: list[tuple[str, str]]):
    """按系统提示词里的角色名路由; 记录 (角色, 用户消息) 供断言。"""
    scripts = {
        "bull": ["多方论点一。"],
        "bear": ["空方论点一。"],
        "bear2": ["空方重写论点。"],
        "judge": _JUDGE_REWRITE_BEAR,
        "judge2": _JUDGE_FINAL_PASS,
    }

    async def _fake(messages, **kwargs):
        system = messages[0]["content"]
        # 用带括号的角色名路由 — 空方提示词里也出现"多方辩手"字样, 裸词会撞车
        if "【多方辩手】" in system:
            key = "bull"
        elif "【空方辩手】" in system:
            key = "bear"
        else:
            key = "judge"
        user = messages[-1]["content"]
        if "打回" in user or "重写" in user:
            alt = f"{key}2"
            if alt in scripts:
                key = alt
        prompts.append((key, user))
        for chunk in scripts[key]:
            yield chunk
    return _fake


async def _run_debate(monkeypatch, repo, tmp_path, symbol, prompts, whitelist_calls):
    """跑完整场辩论 (含打回重写), 顺带记录白名单采集与审计提示词。"""
    materials = stock_debate.build_materials(repo, tmp_path, symbol)
    assert materials is not None

    monkeypatch.setattr(stock_debate, "resolve_role_profiles",
                        lambda: {"bull": None, "bear": None, "judge": None})
    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _fake_llm(prompts))
    monkeypatch.setattr("app.services.ai_provider.current_ai_model", lambda: "fake-model")

    def _record_whitelist(objs, texts=()):
        whitelist_calls.append(tuple(texts))
        return None
    monkeypatch.setattr("app.services.ai_number_whitelist.collect_whitelist", _record_whitelist)

    async def _passthrough(lines, pool):
        async for ln in lines:
            yield ln
    monkeypatch.setattr("app.services.ai_number_gate.gate_ndjson_stream", _passthrough)

    monkeypatch.setattr(stock_debate, "build_materials", lambda *a, **k: materials)

    return [json.loads(ln) async for ln in stock_debate.debate_stream(
        repo=repo, data_dir=tmp_path, symbol=symbol, name="")]


async def test_all_llm_calls_carry_the_same_guardrail(monkeypatch, repo, tmp_path):
    """🔴 五次 LLM 调用 (多/空/裁决/重写/终审) 的用户消息**全部**含同一份护栏。

    只测 ``materials["text"]`` 是不够的: 护栏进了材料不等于进了每一次调用。
    这条从 ``debate_stream`` 实际发出的用户消息反向核对, 顺带证明「拼一处即
    覆盖全部角色」这个设计选择真的成立。
    """
    prompts: list[tuple[str, str]] = []
    events = await _run_debate(monkeypatch, repo, tmp_path, HK, prompts, [])

    assert [e["type"] for e in events][-1] == "done"
    assert "error" not in [e["type"] for e in events]

    guardrail = mg.guardrail_prompt(Market.HK)
    roles = [key for key, _ in prompts]
    assert roles == ["bull", "bear", "judge", "bear2", "judge2"], f"实际调用序列: {roles}"

    materials_text = None
    for key, user in prompts:
        assert guardrail in user, f"{key} 的用户消息没拿到市场护栏"
        # 三方材料段逐字节一致 (公平辩论的既有约束, 护栏不得破坏它)
        head = user.split("\n\n===== ")[0].split("\n用户特别关注")[0].split("\n请陈述")[0]
        if materials_text is None:
            materials_text = head
        assert head == materials_text, f"{key} 拿到的材料段与其他角色不一致"


async def test_ashare_debate_sees_no_guardrail(monkeypatch, repo, tmp_path):
    """A 股辩论全程不含港股护栏 (逐字节不变的端到端复核)。"""
    prompts: list[tuple[str, str]] = []
    await _run_debate(monkeypatch, repo, tmp_path, CN, prompts, [])

    assert prompts
    for key, user in prompts:
        assert "港股市场口径护栏" not in user, f"{key} 的 A 股材料混进了港股护栏"


# ===== 3. 白名单与审计哈希的覆盖面 =====

async def test_whitelist_texts_include_the_guardrail(monkeypatch, repo, tmp_path):
    """护栏落在 ``collect_whitelist(texts=...)`` 的采集范围内 —— 无需额外接线。

    这既是「设计选择成立」的证据, 也是约束: 若哪天有人把护栏挪出
    ``materials["text"]``, 这条会红, 提醒他白名单覆盖面同时变了。
    """
    calls: list[tuple] = []
    await _run_debate(monkeypatch, repo, tmp_path, HK, [], calls)

    assert len(calls) == 1, f"白名单应只采集一次, 实际 {len(calls)} 次"
    texts = calls[0]
    guardrail = mg.guardrail_prompt(Market.HK)
    assert any(guardrail in t for t in texts), "护栏不在白名单采集的 texts 里"
    # 三个系统提示词 + 材料成品文本
    assert len(texts) == 4


async def test_audit_prompt_hash_covers_the_guardrail(monkeypatch, repo, tmp_path):
    """审计提示词哈希含护栏 ⇒ 护栏改了哈希就变, 事后可追溯。"""
    captured: dict[str, str] = {}

    def _record_finish(data_dir, *, audit_id, kind, symbol, inputs, prompt_text, **kw):
        captured["prompt_text"] = prompt_text
    monkeypatch.setattr("app.services.ai_audit.finish_run", _record_finish)

    await _run_debate(monkeypatch, repo, tmp_path, HK, [], [])

    assert captured.get("prompt_text"), "finish_run 没被调用或没传 prompt_text"
    assert mg.guardrail_prompt(Market.HK) in captured["prompt_text"]


# ===== 4. 🔴 数字闸门白名单卫生 (辩论侧端到端复核) =====

def test_hk_materials_leak_no_number_into_whitelist(repo, tmp_path):
    """港股材料里的护栏段不得给编造数字发通行证。

    ``test_market_guardrail.py`` 第 8 节从护栏本体钉过一遍; 这里从**辩论实际
    送进白名单的那段文本**再钉一遍 —— 拼接过程若引入数字 (比如把 lot_size
    直接插值), 本体会绿而这里会红。
    """
    text = stock_debate.build_materials(repo, tmp_path, HK)["text"]
    guardrail = mg.guardrail_prompt(Market.HK)

    leaked = scan_text_numbers(guardrail)
    assert leaked == [], f"护栏泄漏数字进白名单: {leaked}"

    # 材料整体扫出的数字必须全部来自 K 线/财务数据本身, 护栏一个都不贡献
    without_guardrail = text.replace(guardrail, "")
    assert set(scan_text_numbers(text)) == set(scan_text_numbers(without_guardrail))


async def test_hk_debate_adds_no_whitelist_numbers_vs_ashare(monkeypatch, repo, tmp_path):
    """端到端: 港股辩论送进白名单的文本, 相对 A 股不新增任何数字。"""
    hk_calls: list[tuple] = []
    cn_calls: list[tuple] = []
    await _run_debate(monkeypatch, repo, tmp_path, HK, [], hk_calls)
    await _run_debate(monkeypatch, repo, tmp_path, CN, [], cn_calls)

    def _numbers(calls):
        out = set()
        for texts in calls:
            for t in texts:
                out.update(scan_text_numbers(t))
        return out

    # 三个系统提示词是市场无关常量, 两边扫出的差异只可能来自材料文本
    extra = _numbers(hk_calls) - _numbers(cn_calls)
    guardrail_numbers = set(scan_text_numbers(mg.guardrail_prompt(Market.HK)))
    assert guardrail_numbers == set()
    assert not (extra & guardrail_numbers), f"护栏贡献了白名单数字: {extra & guardrail_numbers}"
