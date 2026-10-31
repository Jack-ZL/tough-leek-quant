"""AI 研判记分卡 (P0-7) 测试 — 结算规则 / 台账 / 统计 / 提示词注入 / API。

覆盖 ai_scorecard 的核心契约:
  - T+5 结算: 看多/偏多涨了对, 看空/偏空跌了对, 中性 |涨跌幅|≤2% 对
  - 数据不足 6 行 → pending 不入账 (fail-open, 绝不用残缺数据结算)
  - 超 90 天仍凑不齐 → expired 终态 (hit=None, 不计入统计)
  - 无答题卡 / ok=false 的报告不入账
  - 台账只结算一次 (幂等)
  - 样本 <10 不给命中率 (insufficient), ≥10 才算 hit_rate_pct
  - prompt_line: 空台账不注入; 样本不足/充足两种文案
  - 台账损坏 fail-open; 单条 K 线查询异常不影响其他条目
  - API: GET /scorecard 先结算再统计, 结算抛错仍返回旧台账
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import ai_scorecard, stock_reports


class _Repo:
    """假日线仓库: 按 symbol 返回预置的 (date, close) 序列。"""

    def __init__(self, series: dict[str, list[float]], start: date) -> None:
        self.series = series
        self.start = start
        self.calls: list[str] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        self.calls.append(symbol)
        closes = self.series.get(symbol)
        if closes is None:
            return pl.DataFrame()
        return pl.DataFrame({
            "date": [self.start + timedelta(days=i) for i in range(len(closes))],
            "close": closes,
        })


def _report(rid: str, symbol: str, direction: str | None, days_ago: int = 10,
            ok: bool = True) -> dict:
    """构造一条带答题卡的个股报告 (created_at 为 days_ago 天前)。"""
    created = (date.today() - timedelta(days=days_ago)).isoformat() + "T15:00:00"
    verdict = None
    if direction is not None:
        verdict = {"ok": ok, "direction": direction, "reason": "测试"}
    return {"id": rid, "symbol": symbol, "name": "测试股", "content": "正文",
            "created_at": created, "verdict": verdict}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离台账文件与报告存储, 返回挂载辅助函数。"""
    monkeypatch.setattr(ai_scorecard, "_ledger_path",
                        lambda: tmp_path / "ai_scorecard.json")

    def mount(reports: list[dict], repo: _Repo | None = None):
        monkeypatch.setattr(stock_reports, "list_reports", lambda: reports)
        return repo or _Repo({}, date.today() - timedelta(days=20))

    return mount


# ================================================================
# 结算规则
# ================================================================

def test_bull_hit_and_miss(env):
    """看多: T5 > T0 记对; T5 < T0 记错。T0=第一行收盘, T5=第 6 行收盘。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_up", "600001.SH", "看多"), _report("sar_dn", "600002.SH", "看多")],
        _Repo({"600001.SH": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0],   # +10% → 对
               "600002.SH": [10.0, 9.8, 9.6, 9.4, 9.2, 9.0]}, start),  # -10% → 错
    )
    assert ai_scorecard.settle_due(repo) == 2
    ledger = ai_scorecard._load_ledger()
    assert ledger["sar_up"]["hit"] is True
    assert ledger["sar_up"]["pct"] == 10.0
    assert ledger["sar_up"]["t0_close"] == 10.0
    assert ledger["sar_up"]["t5_close"] == 11.0
    assert ledger["sar_up"]["horizon"] == 5
    assert ledger["sar_dn"]["hit"] is False


def test_bear_and_lean_directions(env):
    """看空/偏空: 跌了记对; 偏多与看多同规则 (方向族一致)。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_bear", "600003.SH", "看空"), _report("sar_lean", "600004.SH", "偏多")],
        _Repo({"600003.SH": [10.0, 9.9, 9.8, 9.7, 9.6, 9.5],   # 跌 → 看空对
               "600004.SH": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]}, start),  # 涨 → 偏多对
    )
    ai_scorecard.settle_due(repo)
    ledger = ai_scorecard._load_ledger()
    assert ledger["sar_bear"]["hit"] is True
    assert ledger["sar_lean"]["hit"] is True


def test_neutral_band(env):
    """中性: |涨跌幅| ≤ 2% 记对, 超出容差带记错 (无论涨跌方向)。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_n_ok", "600005.SH", "中性"), _report("sar_n_bad", "600006.SH", "中性")],
        _Repo({"600005.SH": [10.0, 10.0, 10.05, 10.1, 10.1, 10.15],  # +1.5% → 对
               "600006.SH": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0]}, start),  # +10% → 错
    )
    ai_scorecard.settle_due(repo)
    ledger = ai_scorecard._load_ledger()
    assert ledger["sar_n_ok"]["hit"] is True
    assert ledger["sar_n_bad"]["hit"] is False


def test_insufficient_rows_stays_pending(env):
    """不足 6 行 (新研判/停牌/数据滞后) → 不入账, 下次触发可重试。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_new", "600007.SH", "看多", days_ago=2)],
        _Repo({"600007.SH": [10.0, 10.1, 10.2]}, start),  # 只有 3 行
    )
    assert ai_scorecard.settle_due(repo) == 0
    assert ai_scorecard._load_ledger() == {}
    s = ai_scorecard.stats()
    assert s["pending"] == 1
    assert s["settled"] == 0


def test_expired_after_90_days(env):
    """研判日超 90 天仍凑不齐数据 → expired 终态 (hit=None, 不计入统计)。"""
    start = date.today() - timedelta(days=200)
    repo = env(
        [_report("sar_old", "600008.SH", "看多", days_ago=120)],
        _Repo({"600008.SH": [10.0, 10.1]}, start),  # 长期停牌只有 2 行
    )
    assert ai_scorecard.settle_due(repo) == 1
    entry = ai_scorecard._load_ledger()["sar_old"]
    assert entry["expired"] is True
    assert entry["hit"] is None
    s = ai_scorecard.stats()
    assert s["settled"] == 0  # expired 不算已结算
    assert s["pending"] == 0  # 已入台账, 不再算 pending


def test_skip_invalid_verdict(env):
    """无答题卡 / ok=false / 方向不在枚举 → 不入账 (绝不从正文猜方向)。"""
    start = date.today() - timedelta(days=20)
    good = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]
    repo = env(
        [_report("sar_none", "600009.SH", None),
         _report("sar_fail", "600009.SH", "看多", ok=False),
         {"id": "sar_bad", "symbol": "600009.SH", "created_at": "2026-01-01T10:00:00",
          "verdict": {"ok": True, "direction": "暴涨", "reason": "非法方向"}}],
        _Repo({"600009.SH": good}, start),
    )
    assert ai_scorecard.settle_due(repo) == 0
    assert ai_scorecard._load_ledger() == {}


def test_settle_is_idempotent(env):
    """已入台账的条目不重复结算 (台账只增不改)。"""
    start = date.today() - timedelta(days=20)
    reports = [_report("sar_once", "600010.SH", "看多")]
    repo = env(reports, _Repo({"600010.SH": [10.0, 10.1, 10.2, 10.3, 10.4, 11.0]}, start))
    assert ai_scorecard.settle_due(repo) == 1
    first = ai_scorecard._load_ledger()["sar_once"]
    # 第二次: 报告还在, 但台账已有 → 不再查 K 线也不再写
    repo2 = env(reports, _Repo({"600010.SH": [10.0] * 6}, start))
    assert ai_scorecard.settle_due(repo2) == 0
    assert ai_scorecard._load_ledger()["sar_once"] == first
    assert repo2.calls == []


def test_kline_error_does_not_block_others(env, monkeypatch):
    """单条 K 线查询抛异常 → 跳过该条, 其余照常结算 (fail-open)。"""
    start = date.today() - timedelta(days=20)

    class _BoomRepo(_Repo):
        def get_daily_asset(self, asset_type, symbol, s, e, columns=None):
            if symbol == "600011.SH":
                raise RuntimeError("上游炸了")
            return super().get_daily_asset(asset_type, symbol, s, e, columns)

    repo = env(
        [_report("sar_boom", "600011.SH", "看多"), _report("sar_fine", "600012.SH", "看多")],
        _BoomRepo({"600012.SH": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]}, start),
    )
    assert ai_scorecard.settle_due(repo) == 1
    ledger = ai_scorecard._load_ledger()
    assert "sar_boom" not in ledger
    assert ledger["sar_fine"]["hit"] is True


# ================================================================
# 统计与样本阈值
# ================================================================

def _seed_ledger(entries: dict) -> None:
    ai_scorecard._save_ledger(entries)


def _entry(rid: str, direction: str, hit: bool) -> dict:
    return {"report_id": rid, "symbol": "600000.SH", "name": "", "direction": direction,
            "analysis_date": "2026-01-05", "t0_close": 10.0, "t5_close": 10.5,
            "pct": 5.0, "hit": hit, "horizon": 5, "settled_at": "2026-01-12T10:00:00"}


def test_stats_insufficient_sample(env):
    """结算样本 <10 → hit_rate_pct 为 None, insufficient=True (不给误导数字)。"""
    env([])
    _seed_ledger({f"sar_{i}": _entry(f"sar_{i}", "看多", i % 2 == 0) for i in range(9)})
    s = ai_scorecard.stats()
    assert s["settled"] == 9
    assert s["insufficient"] is True
    assert s["hit_rate_pct"] is None
    assert s["min_sample"] == 10


def test_stats_hit_rate_and_by_direction(env):
    """≥10 条 → 命中率与分方向统计正确。"""
    env([])
    entries = {}
    # 看多 8 条: 6 对 2 错; 中性 4 条: 2 对 2 错 → 总 12 条 8 对 = 66.7%
    for i in range(8):
        entries[f"b{i}"] = _entry(f"b{i}", "看多", i < 6)
    for i in range(4):
        entries[f"n{i}"] = _entry(f"n{i}", "中性", i < 2)
    _seed_ledger(entries)
    s = ai_scorecard.stats()
    assert s["settled"] == 12
    assert s["hits"] == 8
    assert s["hit_rate_pct"] == 66.7
    assert s["insufficient"] is False
    assert s["by_direction"]["看多"] == {"settled": 8, "hits": 6}
    assert s["by_direction"]["中性"] == {"settled": 4, "hits": 2}
    assert len(s["recent"]) == 10  # 最近明细截断到 10 条


def test_corrupt_ledger_fail_open(env):
    """台账 JSON 损坏 → 按空台账处理, 不抛异常。"""
    env([])
    ai_scorecard._ledger_path().write_text("{半截JSON", encoding="utf-8")
    s = ai_scorecard.stats()
    assert s["settled"] == 0
    assert ai_scorecard.prompt_line() == ""


# ================================================================
# 提示词注入
# ================================================================

def test_prompt_line_empty_when_nothing_settled(env):
    """无已结算样本 → 空串 (不往提示词里塞噪音)。"""
    env([])
    assert ai_scorecard.prompt_line() == ""


def test_prompt_line_insufficient_sample(env):
    """样本不足文案: 报结算次数, 明说样本不足, 不给命中率数字。"""
    env([])
    _seed_ledger({f"sar_{i}": _entry(f"sar_{i}", "看多", True) for i in range(3)})
    line = ai_scorecard.prompt_line()
    assert "## 你的历史战绩" in line
    assert "3 次" in line
    assert "样本不足" in line
    assert "100" not in line  # 3/3 也不能显示 100% 命中率


def test_prompt_line_with_hit_rate(env):
    """样本充足文案: 含结算数/判对数/命中率, 并要求按战绩校准置信度。"""
    env([])
    entries = {f"sar_{i}": _entry(f"sar_{i}", "看多", i < 6) for i in range(12)}
    _seed_ledger(entries)
    line = ai_scorecard.prompt_line()
    assert "12 次" in line
    assert "判对 6 次" in line
    assert "50.0%" in line
    assert "置信度校准" in line


def test_prompt_injection_into_user_prompts(env):
    """两条注入路径: 复盘与个股分析的用户提示词都带战绩块, 空战绩不带。"""
    from app.services import market_recap, stock_analyzer

    env([])
    _seed_ledger({f"sar_{i}": _entry(f"sar_{i}", "看多", i < 6) for i in range(12)})
    ctx = ai_scorecard.prompt_line()

    recap_prompt = market_recap._build_user_prompt(
        {"as_of": "2026-01-09"}, [], "", scorecard_context=ctx)
    assert "## 你的历史战绩" in recap_prompt

    stock_prompt = stock_analyzer._build_user_prompt(
        [], {}, {}, 10.0, "600000.SH", "", scorecard_context=ctx)
    assert "## 你的历史战绩" in stock_prompt

    # 空串 → 整段不出现
    recap_plain = market_recap._build_user_prompt({"as_of": "2026-01-09"}, [], "")
    assert "历史战绩" not in recap_plain
    stock_plain = stock_analyzer._build_user_prompt([], {}, {}, 10.0, "600000.SH", "")
    assert "历史战绩" not in stock_plain


# ================================================================
# API 端点
# ================================================================

def test_scorecard_endpoint_settles_then_stats(env):
    """GET /scorecard: 先惰性结算再返回统计。"""
    from app.api import stock_analysis as api

    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_api", "600013.SH", "看多")],
        _Repo({"600013.SH": [10.0, 10.1, 10.2, 10.3, 10.4, 10.8]}, start),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    resp = api.get_scorecard(request)
    assert resp["ok"] is True
    assert resp["scorecard"]["settled"] == 1
    assert resp["scorecard"]["hits"] == 1


def test_scorecard_endpoint_survives_settle_error(env, monkeypatch):
    """结算抛异常 → 端点仍返回现有台账统计 (fail-open, 前端不出错)。"""
    from app.api import stock_analysis as api

    env([])
    _seed_ledger({"sar_x": _entry("sar_x", "看多", True)})

    def _boom(repo):
        raise RuntimeError("DuckDB 炸了")

    monkeypatch.setattr(ai_scorecard, "settle_due", _boom)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))
    resp = api.get_scorecard(request)
    assert resp["ok"] is True
    assert resp["scorecard"]["settled"] == 1


def test_save_report_triggers_lazy_settle(env, monkeypatch):
    """POST /reports 落库带方向的研判后触发惰性结算; 结算失败不影响保存。"""
    from app.api import stock_analysis as api

    calls: list = []
    monkeypatch.setattr(ai_scorecard, "settle_due", lambda repo: calls.append(repo) or 1)
    monkeypatch.setattr(stock_reports, "save_report", lambda d: d)

    req = api.SaveReportRequest(symbol="600014.SH", content="正文",
                                verdict={"ok": True, "direction": "看多", "reason": "r"})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo="REPO")))
    resp = api.save_report(request, req)
    assert resp["ok"] is True
    assert calls == ["REPO"]

    # ok=false 的答题卡不触发结算 (没有方向可记)
    calls.clear()
    req2 = api.SaveReportRequest(symbol="600014.SH", content="正文", verdict={"ok": False})
    api.save_report(request, req2)
    assert calls == []

    # 结算抛异常 → 保存仍然成功
    def _boom(repo):
        raise RuntimeError("炸")

    monkeypatch.setattr(ai_scorecard, "settle_due", _boom)
    resp3 = api.save_report(request, req)
    assert resp3["ok"] is True


def test_ledger_file_is_valid_json_after_settle(env):
    """台账落盘为合法 JSON (原子写), 结构为 {report_id: entry}。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_json", "600015.SH", "偏空")],
        _Repo({"600015.SH": [10.0, 9.9, 9.8, 9.7, 9.6, 9.4]}, start),
    )
    ai_scorecard.settle_due(repo)
    raw = json.loads(ai_scorecard._ledger_path().read_text(encoding="utf-8"))
    assert raw["sar_json"]["direction"] == "偏空"
    assert raw["sar_json"]["hit"] is True
