"""AI 研判记分卡 (P0-7 → P1-10 完整版) 测试 — 多周期结算 / 台账 / 统计 / 提示词注入 / API。

覆盖 ai_scorecard 的核心契约:
  - T+1/3/5/10 每周期独立结算: 看多/偏多涨了对, 看空/偏空跌了对,
    中性 |涨跌幅| ≤ 该周期容差带对 (±1/1.5/2/3%)
  - 数据够一个周期结一个: 不足 2 行 → pending 不入账; 部分结算后数据增长可补齐
  - 超 90 天仍凑不齐 → expired 终态, 已结算周期保留并计入统计
  - alpha: 有沪深300 序列算 bench_pct/alpha_pct; 缺基准只降级指标不阻塞结算
  - 旧台账 (P0-7 单周期格式) 读取时自动迁移进 horizons["5"]
  - 无答题卡 / ok=false 的报告不入账; 终态条目不再复查 K 线 (幂等)
  - 每周期独立 MIN_SAMPLE=20 护栏; 顶层兼容字段 = T+5 口径
  - prompt_line: 空台账不注入; 分周期命中率/样本不足两种文案
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
    """看多: T+h > T0 记对; T+h < T0 记错。6 行数据结清 T+1/3/5, T+10 待补。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_up", "600001.SH", "看多"), _report("sar_dn", "600002.SH", "看多")],
        _Repo({"600001.SH": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0],   # T+5 +10% → 对
               "600002.SH": [10.0, 9.8, 9.6, 9.4, 9.2, 9.0]}, start),  # T+5 -10% → 错
    )
    assert ai_scorecard.settle_due(repo) == 2
    ledger = ai_scorecard._load_ledger()
    h5 = ledger["sar_up"]["horizons"]["5"]
    assert h5["hit"] is True
    assert h5["pct"] == 10.0
    assert h5["t_close"] == 11.0
    assert ledger["sar_up"]["t0_close"] == 10.0
    # 6 行只够 T+1/3/5, T+10 尚未结算 (非终态, 后续可补)
    assert set(ledger["sar_up"]["horizons"]) == {"1", "3", "5"}
    assert ledger["sar_dn"]["horizons"]["5"]["hit"] is False


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
    assert ledger["sar_bear"]["horizons"]["5"]["hit"] is True
    assert ledger["sar_lean"]["horizons"]["5"]["hit"] is True


def test_neutral_band(env):
    """中性: T+5 |涨跌幅| ≤ 2% 记对, 超出容差带记错 (无论涨跌方向)。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_n_ok", "600005.SH", "中性"), _report("sar_n_bad", "600006.SH", "中性")],
        _Repo({"600005.SH": [10.0, 10.0, 10.05, 10.1, 10.1, 10.15],  # T+5 +1.5% → 对
               "600006.SH": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0]}, start),  # T+5 +10% → 错
    )
    ai_scorecard.settle_due(repo)
    ledger = ai_scorecard._load_ledger()
    assert ledger["sar_n_ok"]["horizons"]["5"]["hit"] is True
    assert ledger["sar_n_bad"]["horizons"]["5"]["hit"] is False


def test_neutral_bands_scale_per_horizon(env):
    """中性容差带按周期缩放: 同一条 +1.5% 在 T+1 (±1%) 判错, T+3/5 (±1.5/2%) 判对。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_band", "600016.SH", "中性")],
        _Repo({"600016.SH": [10.0, 10.15, 10.15, 10.15, 10.15, 10.15]}, start),
    )
    ai_scorecard.settle_due(repo)
    hz = ai_scorecard._load_ledger()["sar_band"]["horizons"]
    assert hz["1"]["hit"] is False   # +1.5% 超出 T+1 的 ±1%
    assert hz["3"]["hit"] is True    # 恰好在 T+3 的 ±1.5% 边界内
    assert hz["5"]["hit"] is True


def test_insufficient_rows_stays_pending(env):
    """不足 2 行 (连 T+1 都结不了: 新研判/停牌/数据滞后) → 不入账, 下次触发可重试。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_new", "600007.SH", "看多", days_ago=2)],
        _Repo({"600007.SH": [10.0]}, start),  # 只有 1 行
    )
    assert ai_scorecard.settle_due(repo) == 0
    assert ai_scorecard._load_ledger() == {}
    s = ai_scorecard.stats()
    assert s["pending"] == 1
    assert s["settled"] == 0


def test_partial_settlement_backfills_longer_horizons(env):
    """数据够一个周期结一个: 3 行先结 T+1, 补到 6 行结 T+3/5, 11 行结清 T+10 达终态。"""
    start = date.today() - timedelta(days=20)
    reports = [_report("sar_part", "600017.SH", "看多")]
    series = {"600017.SH": [10.0, 10.1, 10.2]}
    repo = env(reports, _Repo(series, start))
    assert ai_scorecard.settle_due(repo) == 1
    assert set(ai_scorecard._load_ledger()["sar_part"]["horizons"]) == {"1"}
    # 部分结算 ≠ 终态: 仍算 pending, 等更长周期
    assert ai_scorecard.stats()["pending"] == 1

    series["600017.SH"] = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]
    assert ai_scorecard.settle_due(repo) == 1  # 有新进展 → 计入更新数
    assert set(ai_scorecard._load_ledger()["sar_part"]["horizons"]) == {"1", "3", "5"}

    series["600017.SH"] = [10.0 + 0.1 * i for i in range(11)]
    assert ai_scorecard.settle_due(repo) == 1
    entry = ai_scorecard._load_ledger()["sar_part"]
    assert set(entry["horizons"]) == {"1", "3", "5", "10"}
    # 已结算周期只增不改: T+1 仍是当初的 +1%
    assert entry["horizons"]["1"]["pct"] == 1.0

    # 终态: 不再复查 K 线
    repo2 = env(reports, _Repo(series, start))
    assert ai_scorecard.settle_due(repo2) == 0
    assert repo2.calls == []


def test_expired_after_90_days(env):
    """研判日超 90 天仍凑不齐 → expired 终态; 已结算周期保留并计入统计。"""
    start = date.today() - timedelta(days=200)
    repo = env(
        [_report("sar_old", "600008.SH", "看多", days_ago=120)],
        _Repo({"600008.SH": [10.0, 10.1]}, start),  # 长期停牌只有 2 行 → 只够 T+1
    )
    assert ai_scorecard.settle_due(repo) == 1
    entry = ai_scorecard._load_ledger()["sar_old"]
    assert entry["expired"] is True
    assert entry["horizons"]["1"]["hit"] is True   # 已结周期保留
    assert "5" not in entry["horizons"]            # 凑不齐的周期不再等
    s = ai_scorecard.stats()
    assert s["settled"] == 0               # T+5 口径: expired 未结不算已结算
    assert s["horizons"]["1"]["settled"] == 1  # T+1 口径: 保留的周期计入
    assert s["pending"] == 0               # 已入台账终态, 不再算 pending


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
    """四周期结清的终态条目不重复结算 (已结周期只增不改)。"""
    start = date.today() - timedelta(days=20)
    reports = [_report("sar_once", "600010.SH", "看多")]
    full = [10.0 + 0.1 * i for i in range(11)]  # 11 行 → 一次结清 T+1/3/5/10
    repo = env(reports, _Repo({"600010.SH": full}, start))
    assert ai_scorecard.settle_due(repo) == 1
    first = ai_scorecard._load_ledger()["sar_once"]
    assert set(first["horizons"]) == {"1", "3", "5", "10"}
    # 第二次: 报告还在, 但台账已终态 → 不再查 K 线也不再写
    repo2 = env(reports, _Repo({"600010.SH": [10.0] * 11}, start))
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
    assert ledger["sar_fine"]["horizons"]["5"]["hit"] is True


# ================================================================
# alpha (对沪深300超额) 与旧台账迁移
# ================================================================

def test_alpha_vs_benchmark(env):
    """有沪深300 序列 → 按交易日对齐算 bench_pct/alpha_pct, 并进周期聚合。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_a", "600018.SH", "看多")],
        _Repo({"600018.SH": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0],   # T+5 +10%
               "000300.SH": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]}, start),  # T+5 +5%
    )
    ai_scorecard.settle_due(repo)
    h5 = ai_scorecard._load_ledger()["sar_a"]["horizons"]["5"]
    assert h5["pct"] == 10.0
    assert h5["bench_pct"] == 5.0
    assert h5["alpha_pct"] == 5.0
    s = ai_scorecard.stats()
    assert s["horizons"]["5"]["avg_alpha_pct"] == 5.0
    assert s["horizons"]["5"]["alpha_samples"] == 1
    assert s["benchmark"] == {"symbol": "000300.SH", "name": "沪深300"}


def test_alpha_missing_benchmark_does_not_block(env):
    """基准序列缺失 → alpha 记 None, 结算照常 (不学 astock 北交所票永久 pending)。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_nb", "600019.SH", "看多")],
        _Repo({"600019.SH": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0]}, start),  # 无 000300.SH
    )
    assert ai_scorecard.settle_due(repo) == 1
    h5 = ai_scorecard._load_ledger()["sar_nb"]["horizons"]["5"]
    assert h5["hit"] is True
    assert h5["bench_pct"] is None and h5["alpha_pct"] is None
    assert ai_scorecard.stats()["horizons"]["5"]["avg_alpha_pct"] is None


def test_old_ledger_format_migrated(env):
    """P0-7 旧格式台账读取时自动迁移: 已结算 → horizons["5"], expired 保留。"""
    env([])
    old_hit = {"report_id": "o1", "symbol": "600000.SH", "name": "x", "direction": "看多",
               "analysis_date": "2026-01-05", "t0_close": 10.0, "t5_close": 10.5,
               "pct": 5.0, "hit": True, "horizon": 5, "settled_at": "2026-01-12T10:00:00"}
    old_exp = dict(old_hit, report_id="o2", hit=None, expired=True)
    ai_scorecard._save_ledger({"o1": old_hit, "o2": old_exp})
    ledger = ai_scorecard._load_ledger()
    assert ledger["o1"]["horizons"]["5"]["hit"] is True
    assert ledger["o1"]["horizons"]["5"]["pct"] == 5.0
    assert ledger["o2"]["expired"] is True and ledger["o2"]["horizons"] == {}
    # 旧战绩计入 T+5 口径统计
    s = ai_scorecard.stats()
    assert s["settled"] == 1 and s["hits"] == 1


# ================================================================
# 统计与样本阈值
# ================================================================

def _seed_ledger(entries: dict) -> None:
    ai_scorecard._save_ledger(entries)


def _entry(rid: str, direction: str, hit: bool) -> dict:
    """旧格式 (P0-7) 条目 — 统计路径经自动迁移后与新旧台账无差别。"""
    return {"report_id": rid, "symbol": "600000.SH", "name": "", "direction": direction,
            "analysis_date": "2026-01-05", "t0_close": 10.0, "t5_close": 10.5,
            "pct": 5.0, "hit": hit, "horizon": 5, "settled_at": "2026-01-12T10:00:00"}


def _entry_h(rid: str, direction: str, horizons: dict) -> dict:
    """新格式 (P1-10) 条目; horizons key 为 "1"/"3"/"5"/"10"。"""
    return {"report_id": rid, "symbol": "600000.SH", "name": "", "direction": direction,
            "analysis_date": "2026-01-05", "t0_close": 10.0, "horizons": horizons,
            "settled_at": "2026-01-12T10:00:00"}


def _hz(pct: float, hit: bool, alpha: float | None = None) -> dict:
    return {"t_close": 10.0 * (1 + pct / 100), "pct": pct, "hit": hit,
            "bench_pct": None if alpha is None else round(pct - alpha, 2),
            "alpha_pct": alpha, "settled_at": "2026-01-12T10:00:00"}


def test_stats_insufficient_sample(env):
    """T+5 结算样本 <20 → hit_rate_pct 为 None, insufficient=True (不给误导数字)。"""
    env([])
    _seed_ledger({f"sar_{i}": _entry(f"sar_{i}", "看多", i % 2 == 0) for i in range(19)})
    s = ai_scorecard.stats()
    assert s["settled"] == 19
    assert s["insufficient"] is True
    assert s["hit_rate_pct"] is None
    assert s["min_sample"] == 20


def test_stats_hit_rate_and_by_direction(env):
    """≥20 条 → 命中率与分方向统计正确 (顶层兼容字段 = T+5 口径)。"""
    env([])
    entries = {}
    # 看多 20 条: 14 对 6 错; 中性 8 条: 4 对 4 错 → 总 28 条 18 对 = 64.3%
    for i in range(20):
        entries[f"b{i}"] = _entry(f"b{i}", "看多", i < 14)
    for i in range(8):
        entries[f"n{i}"] = _entry(f"n{i}", "中性", i < 4)
    _seed_ledger(entries)
    s = ai_scorecard.stats()
    assert s["settled"] == 28
    assert s["hits"] == 18
    assert s["hit_rate_pct"] == 64.3
    assert s["insufficient"] is False
    assert s["by_direction"]["看多"] == {"settled": 20, "hits": 14}
    assert s["by_direction"]["中性"] == {"settled": 8, "hits": 4}
    assert s["horizons"]["5"]["avg_pct"] == 5.0  # 迁移条目的 pct 保留
    assert len(s["recent"]) == 10  # 最近明细截断到 10 条
    assert "不是回测" in s["disclaimer"]


def test_stats_min_sample_per_horizon(env):
    """MIN_SAMPLE 护栏按周期独立: T+1 满 20 出命中率, T+5 只有 5 条仍样本不足。"""
    env([])
    entries = {}
    for i in range(20):
        entries[f"h1_{i}"] = _entry_h(f"h1_{i}", "看多", {"1": _hz(1.0, True)})
    for i in range(5):
        entries[f"h5_{i}"] = _entry_h(f"h5_{i}", "看多", {"5": _hz(2.0, i < 3)})
    _seed_ledger(entries)
    s = ai_scorecard.stats()
    assert s["horizons"]["1"]["settled"] == 20
    assert s["horizons"]["1"]["hit_rate_pct"] == 100.0
    assert s["horizons"]["1"]["insufficient"] is False
    assert s["horizons"]["5"]["settled"] == 5
    assert s["horizons"]["5"]["hit_rate_pct"] is None
    assert s["horizons"]["5"]["insufficient"] is True
    assert s["horizons"]["1"]["neutral_band_pct"] == 1.0
    assert s["horizons"]["10"]["neutral_band_pct"] == 3.0
    # 未结算过的周期结构完整但全零
    assert s["horizons"]["3"]["settled"] == 0
    assert s["horizons"]["3"]["avg_pct"] is None


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
    assert "样本不足 (3/20)" in line
    assert "100" not in line  # 3/3 也不能显示 100% 命中率


def test_prompt_line_with_hit_rate(env):
    """样本充足文案: 含结算数/判对数/命中率, 并要求按战绩校准置信度。"""
    env([])
    entries = {f"sar_{i}": _entry(f"sar_{i}", "看多", i < 10) for i in range(20)}
    _seed_ledger(entries)
    line = ai_scorecard.prompt_line()
    assert "20 次结算判对 10 次" in line
    assert "50.0%" in line
    assert "置信度校准" in line
    assert "而非回测" in line  # 表头自带免责声明


def test_prompt_line_multi_horizon(env):
    """分周期各一行: 充足的给命中率+平均涨跌+超额, 不足的明说不足。"""
    env([])
    entries = {}
    for i in range(20):
        entries[f"m1_{i}"] = _entry_h(f"m1_{i}", "看多",
                                      {"1": _hz(1.0, i % 2 == 0, alpha=0.5)})
    for i in range(3):
        entries[f"m5_{i}"] = _entry_h(f"m5_{i}", "看多", {"5": _hz(2.0, True)})
    _seed_ledger(entries)
    line = ai_scorecard.prompt_line()
    assert "- T+1: 命中率 50.0% (20 次结算判对 10 次), 平均涨跌 1.0%, 对沪深300平均超额 0.5%" in line
    assert "- T+5: 样本不足 (3/20)" in line
    assert "- T+3" not in line and "- T+10" not in line  # 无样本周期不占行


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
    # P1-10: 分周期结构 + 免责声明随响应下发
    assert resp["scorecard"]["horizons"]["1"]["settled"] == 1
    assert resp["scorecard"]["horizons"]["10"]["settled"] == 0
    assert "不是回测" in resp["scorecard"]["disclaimer"]


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
    """台账落盘为合法 JSON (原子写), 结构为 {report_id: entry(多周期)}。"""
    start = date.today() - timedelta(days=20)
    repo = env(
        [_report("sar_json", "600015.SH", "偏空")],
        _Repo({"600015.SH": [10.0, 9.9, 9.8, 9.7, 9.6, 9.4]}, start),
    )
    ai_scorecard.settle_due(repo)
    raw = json.loads(ai_scorecard._ledger_path().read_text(encoding="utf-8"))
    assert raw["sar_json"]["direction"] == "偏空"
    assert raw["sar_json"]["horizons"]["5"]["hit"] is True
    assert raw["sar_json"]["horizons"]["5"]["pct"] == -6.0
