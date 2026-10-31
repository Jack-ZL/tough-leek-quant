"""多空辩论 (P1-9) 测试。

覆盖:
- parse_ruling: 机读裁决块解析 (fail-honest + 打回降级规则)
- _strip_ruling_deltas: 流式剥离裁决标记 (跨 chunk 切开也不漏)
- preferences.get/set_debate_role_profiles: 角色档案分配存取清洗
- resolve_role_profiles / role_model_display: 失效档案静默回退
- debate_reports: 辩论记录 CRUD
- debate_stream: 全流程编排 (直通 / 打回重写 / 裁决解析失败 / 角色失败 / 无数据)
- API: role-profiles GET/PUT, reports CRUD
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import stock_debate
from app.services.stock_debate import parse_ruling, strip_ruling


# ================================================================
# parse_ruling
# ================================================================

def _marker(payload: dict) -> str:
    return f"<!-- DEBATE_RULING: {json.dumps(payload, ensure_ascii=False)} -->"


def test_parse_ruling_pass():
    text = "点评正文\n" + _marker({
        "decision": "pass", "rewrite_role": "", "rewrite_reason": "",
        "direction": "偏多", "reason": "多方论证更扎实"})
    r = parse_ruling(text)
    assert r == {"decision": "pass", "rewrite_role": "", "rewrite_reason": "",
                 "direction": "偏多", "reason": "多方论证更扎实"}


def test_parse_ruling_rewrite():
    text = _marker({"decision": "rewrite", "rewrite_role": "bear",
                    "rewrite_reason": "空喊口号无数据", "direction": "中性", "reason": "r"})
    r = parse_ruling(text)
    assert r["decision"] == "rewrite" and r["rewrite_role"] == "bear"


def test_parse_ruling_no_marker_returns_none():
    assert parse_ruling("完全没有标记的文本") is None


def test_parse_ruling_bad_json_returns_none():
    assert parse_ruling("<!-- DEBATE_RULING: {decision: pass} -->") is None


def test_parse_ruling_invalid_decision_returns_none():
    r = parse_ruling(_marker({"decision": "maybe", "rewrite_role": "",
                              "rewrite_reason": "", "direction": "看多", "reason": "r"}))
    assert r is None


def test_parse_ruling_invalid_direction_returns_none():
    r = parse_ruling(_marker({"decision": "pass", "rewrite_role": "",
                              "rewrite_reason": "", "direction": "暴涨", "reason": "r"}))
    assert r is None


def test_parse_ruling_rewrite_without_role_downgrades_to_pass():
    """打回但未指明角色 → 无法执行, 降级为通过 (fail-honest 不猜)。"""
    r = parse_ruling(_marker({"decision": "rewrite", "rewrite_role": "",
                              "rewrite_reason": "不行", "direction": "看空", "reason": "r"}))
    assert r["decision"] == "pass"


def test_parse_ruling_rewrite_with_invalid_role_downgrades():
    r = parse_ruling(_marker({"decision": "rewrite", "rewrite_role": "judge",
                              "rewrite_reason": "x", "direction": "看多", "reason": "r"}))
    assert r["decision"] == "pass"


def test_parse_ruling_last_marker_wins():
    text = (_marker({"decision": "rewrite", "rewrite_role": "bull", "rewrite_reason": "a",
                     "direction": "看多", "reason": "1"}) + "\n"
            + _marker({"decision": "pass", "rewrite_role": "", "rewrite_reason": "",
                       "direction": "看空", "reason": "2"}))
    r = parse_ruling(text)
    assert r["decision"] == "pass" and r["direction"] == "看空"


def test_parse_ruling_truncates_long_reason():
    r = parse_ruling(_marker({"decision": "pass", "rewrite_role": "", "rewrite_reason": "",
                              "direction": "中性", "reason": "长" * 200}))
    assert len(r["reason"]) <= 61 and r["reason"].endswith("…")


def test_strip_ruling_removes_marker():
    text = "正文\n" + _marker({"decision": "pass", "rewrite_role": "", "rewrite_reason": "",
                                "direction": "中性", "reason": "r"})
    assert "DEBATE_RULING" not in strip_ruling(text)
    assert "正文" in strip_ruling(text)


# ================================================================
# _strip_ruling_deltas (流式剥离)
# ================================================================

async def _aiter(items):
    for it in items:
        yield it


async def test_strip_deltas_marker_split_across_chunks():
    """标记被切成多段 chunk 也不能漏给客户端。"""
    full = "结论正文\n" + _marker({"decision": "pass", "rewrite_role": "", "rewrite_reason": "",
                                    "direction": "偏多", "reason": "r"})
    # 逐字符切碎, 最恶劣的切法
    captured: list[str] = []
    out = [c async for c in stock_debate._strip_ruling_deltas(_aiter(list(full)), captured)]
    joined = "".join(out)
    assert "DEBATE_RULING" not in joined
    assert "结论正文" in joined
    assert len(captured) == 1
    assert json.loads(captured[0])["direction"] == "偏多"


async def test_strip_deltas_plain_text_passthrough():
    captured: list[str] = []
    out = [c async for c in stock_debate._strip_ruling_deltas(
        _aiter(["第一段\n", "第二段"]), captured)]
    assert "".join(out) == "第一段\n第二段"
    assert captured == []


# ================================================================
# preferences: 角色档案分配
# ================================================================

@pytest.fixture()
def prefs(tmp_path, monkeypatch):
    from app.services import preferences
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    yield preferences
    preferences._invalidate_cache()


def test_prefs_default_empty(prefs):
    assert prefs.get_debate_role_profiles() == {}


def test_prefs_roundtrip(prefs):
    out = prefs.set_debate_role_profiles({"bull": "p1", "bear": "p2", "judge": ""})
    assert out == {"bull": "p1", "bear": "p2", "judge": ""}
    assert prefs.get_debate_role_profiles() == {"bull": "p1", "bear": "p2", "judge": ""}


def test_prefs_sanitizes_unknown_keys_and_types(prefs):
    out = prefs.set_debate_role_profiles(
        {"bull": "p1", "judge": 123, "hacker": "x", "bear": None})
    assert out == {"bull": "p1", "bear": "", "judge": ""}
    assert "hacker" not in prefs.load()["debate_role_profiles"]


def test_prefs_rejects_non_dict(prefs):
    with pytest.raises(ValueError):
        prefs.set_debate_role_profiles(["bull"])


def test_prefs_dirty_value_ignored(prefs):
    prefs.save({"debate_role_profiles": {"bull": 42, "bear": "ok"}})
    got = prefs.get_debate_role_profiles()
    assert got == {"bear": "ok"}  # 非字符串值被丢弃


# ================================================================
# resolve_role_profiles / role_model_display
# ================================================================

def test_resolve_role_profiles_roundrobin(prefs, monkeypatch):
    """未手动指定: 多→空→主管 依次取档案列表 0/1/2 位, 取完循环 (2 档案: A B A)。"""
    from app import secrets_store
    monkeypatch.setattr(secrets_store, "list_ai_profiles",
                        lambda: [{"id": "p1", "name": "A", "model": "m1"},
                                 {"id": "p2", "name": "B", "model": "m2"}])
    got = stock_debate.resolve_role_profiles()
    assert got["bull"]["id"] == "p1"
    assert got["bear"]["id"] == "p2"
    assert got["judge"]["id"] == "p1"


def test_resolve_role_profiles_single_profile_all_same(prefs, monkeypatch):
    """只有一个档案: 没得选, 全员用它 (不硬凑不同模型)。"""
    from app import secrets_store
    monkeypatch.setattr(secrets_store, "list_ai_profiles",
                        lambda: [{"id": "p1", "name": "A", "model": "m1"}])
    got = stock_debate.resolve_role_profiles()
    assert got == {"bull": {"id": "p1", "name": "A", "model": "m1"},
                   "bear": {"id": "p1", "name": "A", "model": "m1"},
                   "judge": {"id": "p1", "name": "A", "model": "m1"}}


def test_resolve_role_profiles_no_profiles_falls_back_active(prefs, monkeypatch):
    from app import secrets_store
    monkeypatch.setattr(secrets_store, "list_ai_profiles", lambda: [])
    assert stock_debate.resolve_role_profiles() == {"bull": None, "bear": None, "judge": None}


def test_resolve_role_profiles_manual_override_and_stale(prefs, monkeypatch):
    """手动指定优先; 失效 id 回退该角色的轮转位 (不是激活档案)。"""
    from app import secrets_store
    monkeypatch.setattr(secrets_store, "list_ai_profiles",
                        lambda: [{"id": "p1", "name": "A", "model": "m1"},
                                 {"id": "p2", "name": "B", "model": "m2"}])
    prefs.set_debate_role_profiles({"bull": "p2", "bear": "deleted-pid", "judge": ""})
    got = stock_debate.resolve_role_profiles()
    assert got["bull"]["id"] == "p2"   # 手动
    assert got["bear"]["id"] == "p2"   # 失效 → 轮转位 1
    assert got["judge"]["id"] == "p1"  # 轮转位 2 % 2 = 0


def test_resolve_role_profiles_secrets_error_fail_open(prefs, monkeypatch):
    from app import secrets_store

    def boom():
        raise RuntimeError("secrets 损坏")
    monkeypatch.setattr(secrets_store, "list_ai_profiles", boom)
    prefs.set_debate_role_profiles({"bull": "p1", "bear": "", "judge": ""})
    got = stock_debate.resolve_role_profiles()
    assert got == {"bull": None, "bear": None, "judge": None}


def test_role_model_display(monkeypatch):
    assert stock_debate.role_model_display({"model": "deepseek-v3", "name": "DS"}) == "deepseek-v3"
    assert stock_debate.role_model_display({"name": "DS"}) == "DS"
    monkeypatch.setattr("app.services.ai_provider.current_ai_model", lambda: "qwen-max")
    assert stock_debate.role_model_display(None) == "qwen-max"


# ================================================================
# debate_reports 存储
# ================================================================

@pytest.fixture()
def reports_store(tmp_path, monkeypatch):
    from app.services import debate_reports
    monkeypatch.setattr(debate_reports._store, "_path",
                        lambda: tmp_path / "ai_debate_reports.json")
    return debate_reports


def test_reports_crud(reports_store):
    r = reports_store.save_report({"symbol": "600519.SH", "name": "贵州茅台",
                                   "bull_text": "b", "bear_text": "s", "judge_text": "j"})
    assert r["id"].startswith("deb")
    assert reports_store.list_reports()[0]["id"] == r["id"]
    assert reports_store.delete_report(r["id"]) is True
    assert reports_store.list_reports() == []
    assert reports_store.delete_report("deb_nope") is False


def test_reports_cap_at_max(reports_store):
    for i in range(reports_store.MAX_REPORTS + 5):
        reports_store.save_report({"symbol": f"S{i}"})
    assert len(reports_store.list_reports()) == reports_store.MAX_REPORTS


# ================================================================
# debate_stream 编排 (fake LLM)
# ================================================================

_FAKE_MATERIALS = {
    "kline_rows": [], "levels": {}, "close": 10.0, "fins": None,
    "asset_type": "stock",
    "text": "材料: 收盘 10.0",
}


def _fake_llm(scripts: dict[str, list[str] | Exception], calls: list[str]):
    """按系统提示词关键字路由的假 stream_ai_text。"""
    async def _fake(messages, **kwargs):
        system = messages[0]["content"]
        # 用带括号的角色名路由 — 空方提示词里也出现"多方辩手"字样 (分工说明), 裸词会撞车
        if "【多方辩手】" in system:
            key = "bull"
        elif "【空方辩手】" in system:
            key = "bear"
        else:
            key = "judge"
        # 重写轮: 用户消息带打回理由 → 用 bull2/bear2/judge2 剧本
        user = messages[-1]["content"]
        if "打回" in user or "重写" in user:
            alt = f"{key}2"
            if alt in scripts:
                key = alt
        calls.append(key)
        script = scripts[key]
        if isinstance(script, Exception):
            raise script
        for chunk in script:
            yield chunk
    return _fake


@pytest.fixture()
def debate_env(monkeypatch):
    """隔离编排依赖: 材料/档案/白名单/闸门全部打桩, 只测流程。"""
    monkeypatch.setattr(stock_debate, "build_materials",
                        lambda *a, **k: dict(_FAKE_MATERIALS))
    monkeypatch.setattr(stock_debate, "resolve_role_profiles",
                        lambda: {"bull": None, "bear": None, "judge": None})
    monkeypatch.setattr("app.services.ai_number_whitelist.collect_whitelist",
                        lambda *a, **k: None)

    async def _passthrough(lines, pool):
        async for ln in lines:
            yield ln
    monkeypatch.setattr("app.services.ai_number_gate.gate_ndjson_stream", _passthrough)
    monkeypatch.setattr("app.services.ai_provider.current_ai_model", lambda: "fake-model")
    return monkeypatch


def _install_llm(monkeypatch, scripts, calls):
    monkeypatch.setattr("app.services.ai_provider.stream_ai_text",
                        _fake_llm(scripts, calls))


async def _collect(agen):
    return [json.loads(ln) async for ln in agen]


_JUDGE_PASS = ["点评。\n", _marker({"decision": "pass", "rewrite_role": "", "rewrite_reason": "",
                                    "direction": "偏多", "reason": "多方更扎实"})]
_JUDGE_REWRITE_BEAR = ["点评。\n", _marker({
    "decision": "rewrite", "rewrite_role": "bear", "rewrite_reason": "空喊口号",
    "direction": "中性", "reason": "待重写"})]
_JUDGE_FINAL_PASS = ["终审点评。\n", _marker({
    "decision": "pass", "rewrite_role": "", "rewrite_reason": "",
    "direction": "看多", "reason": "重写后合格"})]


async def test_debate_happy_path(debate_env):
    calls: list[str] = []
    _install_llm(debate_env, {
        "bull": ["多方论点一。", "多方论点二。"],
        "bear": ["空方论点一。"],
        "judge": _JUDGE_PASS,
    }, calls)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH", name="贵州茅台"))

    types = [e["type"] for e in events]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "error" not in types
    # 双方各一次 role_done, 主管一次
    done_roles = sorted(e["role"] for e in events if e["type"] == "role_done")
    assert done_roles == ["bear", "bull", "judge"]
    # 裁决: 直通只有一条 ruling, final=false, 带 verdict
    rulings = [e for e in events if e["type"] == "ruling"]
    assert len(rulings) == 1
    assert rulings[0]["ok"] is True and rulings[0]["decision"] == "pass"
    assert rulings[0]["verdict"] == {"direction": "偏多", "reason": "多方更扎实"}
    # 主管输出已剥离标记
    judge_done = next(e for e in events if e["type"] == "role_done" and e["role"] == "judge")
    assert "DEBATE_RULING" not in judge_done["content"]
    assert sorted(calls) == ["bear", "bull", "judge"]
    # meta 带角色模型展示
    assert events[0]["roles"]["bull"]["model"] == "fake-model"


async def test_debate_rewrite_round(debate_env):
    calls: list[str] = []
    _install_llm(debate_env, {
        "bull": ["多方论点。"],
        "bear": ["空方糊弄。"],
        "judge": _JUDGE_REWRITE_BEAR,
        "bear2": ["空方重写论点。"],
        "judge2": _JUDGE_FINAL_PASS,
    }, calls)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH"))

    types = [e["type"] for e in events]
    assert types[-1] == "done" and "error" not in types
    # rewrite 事件指明 bear
    rw = next(e for e in events if e["type"] == "rewrite")
    assert rw["role"] == "bear" and rw["reason"] == "空喊口号"
    # bear 的第二次 role_done 带 rewritten=true
    bear_dones = [e for e in events if e["type"] == "role_done" and e["role"] == "bear"]
    assert len(bear_dones) == 2
    assert bear_dones[0]["rewritten"] is False and bear_dones[1]["rewritten"] is True
    assert bear_dones[1]["content"] == "空方重写论点。"
    # 两条 ruling: 初审 rewrite + 终审 pass(final)
    rulings = [e for e in events if e["type"] == "ruling"]
    assert len(rulings) == 2
    assert rulings[0]["decision"] == "rewrite" and rulings[0]["final"] is False
    assert rulings[1]["decision"] == "pass" and rulings[1]["final"] is True
    assert rulings[1]["verdict"]["direction"] == "看多"
    # 硬上限 5 次调用: bull + bear + judge + bear2 + judge2
    assert len(calls) == 5


async def test_debate_ruling_parse_fail_is_honest(debate_env):
    calls: list[str] = []
    _install_llm(debate_env, {
        "bull": ["多方。"], "bear": ["空方。"],
        "judge": ["主管没按格式交裁决"],
    }, calls)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH"))
    rulings = [e for e in events if e["type"] == "ruling"]
    assert len(rulings) == 1 and rulings[0]["ok"] is False
    assert [e["type"] for e in events][-1] == "done"  # 不因此中断


async def test_debate_role_failure_emits_error(debate_env):
    calls: list[str] = []
    _install_llm(debate_env, {
        "bull": RuntimeError("boom"),
        "bear": ["空方。"], "judge": _JUDGE_PASS,
    }, calls)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH"))
    types = [e["type"] for e in events]
    assert "error" in types
    assert "多方辩手调用失败" in next(e for e in events if e["type"] == "error")["message"]
    assert types[-1] != "done"  # 失败不发 done
    assert "judge" not in calls  # 主管没被调


async def test_debate_judge_failure_emits_error(debate_env):
    calls: list[str] = []
    _install_llm(debate_env, {
        "bull": ["多方。"], "bear": ["空方。"],
        "judge": RuntimeError("judge boom"),
    }, calls)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH"))
    err = next(e for e in events if e["type"] == "error")
    assert "风控主管调用失败" in err["message"]


async def test_debate_no_materials(debate_env, monkeypatch):
    monkeypatch.setattr(stock_debate, "build_materials", lambda *a, **k: None)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH"))
    assert events[0]["type"] == "error"
    assert "暂无日 K 数据" in events[0]["message"]


async def test_debate_materials_exception(debate_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("K线读取炸了")
    monkeypatch.setattr(stock_debate, "build_materials", boom)
    events = await _collect(stock_debate.debate_stream(
        repo=None, data_dir=None, symbol="600519.SH"))
    assert events[0]["type"] == "error" and "数据准备失败" in events[0]["message"]


# ================================================================
# API
# ================================================================

@pytest.fixture()
def client(prefs, reports_store, monkeypatch):
    from app import secrets_store
    from app.api.stock_debate import router
    monkeypatch.setattr(secrets_store, "list_ai_profiles",
                        lambda: [{"id": "p1", "name": "DS", "model": "deepseek-v3"},
                                 {"id": "p2", "name": "QW", "model": "qwen-max"}])
    monkeypatch.setattr(secrets_store, "get_active_ai_profile_id", lambda: "p1")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_role_profiles_get_default(client):
    res = client.get("/api/stock-debate/role-profiles")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["assignments"] == {}
    assert {p["id"] for p in body["profiles"]} == {"p1", "p2"}
    assert body["active_profile_id"] == "p1"


def test_api_role_profiles_put_roundtrip(client):
    res = client.put("/api/stock-debate/role-profiles",
                     json={"bull": "p1", "bear": "p2", "judge": ""})
    assert res.json()["assignments"] == {"bull": "p1", "bear": "p2", "judge": ""}
    got = client.get("/api/stock-debate/role-profiles").json()
    assert got["assignments"] == {"bull": "p1", "bear": "p2", "judge": ""}


def test_api_reports_crud(client):
    res = client.post("/api/stock-debate/reports", json={
        "symbol": "600519.SH", "name": "贵州茅台",
        "bull_text": "b", "bear_text": "s", "judge_text": "j",
        "verdict": {"direction": "偏多", "reason": "r"}, "rewritten": True,
        "roles": {"bull": "deepseek-v3", "bear": "qwen-max", "judge": "deepseek-v3"},
    })
    assert res.status_code == 200
    rid = res.json()["report"]["id"]
    listing = client.get("/api/stock-debate/reports").json()["reports"]
    assert listing[0]["id"] == rid and listing[0]["rewritten"] is True
    assert client.delete(f"/api/stock-debate/reports/{rid}").json()["ok"] is True
    assert client.get("/api/stock-debate/reports").json()["reports"] == []


def test_api_analyze_rejects_empty_symbol(client):
    res = client.post("/api/stock-debate/analyze", json={"symbol": ""})
    assert res.status_code == 400
