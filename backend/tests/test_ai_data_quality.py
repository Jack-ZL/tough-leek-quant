# P1-11 数据体检打分 — 规则/评分/缓存/门控/注入 全覆盖。
# 日历统一 monkeypatch 为 None (工作日近似), 保证停更天数计算与真实交易日历解耦。
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import ai_data_quality as adq


# ===== 工具 =====

def make_df(n: int, end: date = date(2026, 2, 4), vol: int = 1000,
            ma20: float | None = 10.0) -> pl.DataFrame:
    """构造 n 根工作日日K (最后一天 = end)。"""
    dates: list[date] = []
    d = end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()
    return pl.DataFrame({
        "date": dates,
        "close": [10.0] * n,
        "volume": [vol] * n,
        "ma20": [ma20] * n,
    })


FRESH_FINS = {"metrics": [{"period_end": "2025-12-31", "roe": 0.1}], "income": []}
# 2026-02-04 是周三 20:00 — 已过 15:35 定版线, 期望最后交易日 = 当天
NOW = datetime(2026, 2, 4, 20, 0)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例: 清结果缓存 + 工作日日历 (不依赖 fuyao 年份表)。"""
    adq._cache.clear()
    monkeypatch.setattr(adq, "_trading_calendar", lambda: None)
    yield
    adq._cache.clear()


def score(tmp_path: Path, df=None, fins=None, asset_type="stock", now=NOW, **kw):
    return adq.score_symbol(tmp_path, "600000", df if df is not None else make_df(100),
                            fins if fins is not None else FRESH_FINS,
                            asset_type, now=now, **kw)


def codes(quality: dict) -> set[str]:
    return {i["code"] for i in quality["issues"]}


# ===== 评分规则 =====

def test_clean_data_grade_a(tmp_path):
    q = score(tmp_path)
    assert q["score"] == 100 and q["grade"] == "A" and q["hard"] is False
    # 快照缺失只是 info 背景说明, 不扣分
    assert codes(q) <= {"minute_snapshot_stale"}
    assert q["version"] == 1 and q["symbol"] == "600000"


def test_rows_low_is_hard(tmp_path):
    q = score(tmp_path, df=make_df(20))
    assert "kline_rows_low" in codes(q)
    assert q["hard"] is True
    # 单项 high 扣 25 → 75 分 B 级 (硬伤但未到 D, 门控开启也不拦)
    assert q["score"] == 75 and q["grade"] == "B"


def test_rows_medium_band(tmp_path):
    q = score(tmp_path, df=make_df(40))
    issue = next(i for i in q["issues"] if i["code"] == "kline_rows_low")
    assert issue["severity"] == "medium" and q["hard"] is False


def test_stale_levels(tmp_path):
    # 停更 7 个交易日 (1/26 周一 → 2/4 周三) → high
    q = score(tmp_path, df=make_df(100, end=date(2026, 1, 26)))
    assert next(i for i in q["issues"] if i["code"] == "kline_stale")["severity"] == "high"
    # 停更 3 个交易日 (1/30 周五 → 2/4) → medium
    q = score(tmp_path, df=make_df(100, end=date(2026, 1, 30)))
    assert next(i for i in q["issues"] if i["code"] == "kline_stale")["severity"] == "medium"
    # 停更 1 个交易日 (2/3 周二 → 2/4) → low
    q = score(tmp_path, df=make_df(100, end=date(2026, 2, 3)))
    assert next(i for i in q["issues"] if i["code"] == "kline_stale")["severity"] == "low"


def test_cutoff_before_1535_expects_previous_day(tmp_path):
    # 当天 10:00 (未过定版线): 数据到昨天不算停更
    q = score(tmp_path, df=make_df(100, end=date(2026, 2, 3)),
              now=datetime(2026, 2, 4, 10, 0))
    assert "kline_stale" not in codes(q)


def test_zero_volume_tail(tmp_path):
    df = make_df(100)
    df = df.with_columns(pl.when(pl.int_range(pl.len()) >= 97)
                         .then(pl.lit(0)).otherwise(pl.col("volume")).alias("volume"))
    q = score(tmp_path, df=df)
    assert "zero_volume_tail" in codes(q)


def test_indicator_missing(tmp_path):
    df = make_df(100).with_columns(
        pl.when(pl.int_range(pl.len()) == 99).then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("ma20")).alias("ma20"))
    q = score(tmp_path, df=df)
    assert "indicator_missing" in codes(q)


def test_date_disorder(tmp_path):
    df = make_df(100)
    dates = df["date"].to_list()
    dates[50], dates[51] = dates[51], dates[50]  # 制造乱序
    q = score(tmp_path, df=df.with_columns(pl.Series("date", dates)))
    assert "date_disorder" in codes(q)


def test_financials_missing_and_stale(tmp_path):
    # 缺失 → info 不扣分
    q = score(tmp_path, fins={"metrics": [], "income": []})
    assert next(i for i in q["issues"] if i["code"] == "fin_missing")["severity"] == "info"
    # 报告期超 120 天 → low
    q = score(tmp_path, fins={"metrics": [{"period_end": "2025-06-30"}], "income": []})
    assert "fin_stale" in codes(q)


def test_etf_skips_financial_and_minute_checks(tmp_path):
    q = score(tmp_path, fins=None, asset_type="etf")
    assert "fin_missing" not in codes(q) and "minute_snapshot_stale" not in codes(q)
    assert q["score"] == 100


# ===== 分钟缺口快照 =====

def test_minute_snapshot_roundtrip(tmp_path):
    scan = {"status": "issues", "scanned_dates": 3,
            "gaps": [{"symbol": "600000", "date": "2026-02-03", "missing_bars": 40,
                      "extra": "dropped"},
                     {"symbol": "000001", "date": "2026-02-03", "missing_bars": 10}]}
    adq.save_minute_snapshot(tmp_path, scan)
    snap = json.loads(adq.minute_snapshot_path(tmp_path).read_text(encoding="utf-8"))
    # 只留三列, 其余字段裁剪
    assert snap["gaps"][0] == {"symbol": "600000", "date": "2026-02-03", "missing_bars": 40}

    q = score(tmp_path)
    issue = next(i for i in q["issues"] if i["code"] == "minute_gaps")
    assert issue["severity"] == "medium" and "40" in issue["message"]
    # 未命中标的不产生缺口问题
    q2 = adq.score_symbol(tmp_path, "000002", make_df(100), FRESH_FINS, now=NOW)
    assert "minute_gaps" not in codes(q2)


def test_minute_snapshot_stale_ignored(tmp_path, monkeypatch):
    adq.save_minute_snapshot(tmp_path, {"status": "ok", "gaps": []})
    # 快照超龄 (49h) → 视为不存在, 降级 info
    real_now = adq.cn_now
    monkeypatch.setattr(adq, "cn_now",
                        lambda: real_now() + timedelta(hours=49))
    assert adq._read_minute_snapshot(tmp_path) is None


# ===== 全市场体检报告 =====

def _write_integrity(tmp_path: Path, report: dict):
    p = tmp_path / "user_data" / "integrity_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def test_market_report_flagged_and_global(tmp_path):
    _write_integrity(tmp_path, {
        "version": 1, "status": "issues",
        "checked_at": adq.cn_now().isoformat(),
        "repairable_count": 2,
        "issues": [{"date": "2026-02-03", "kind": "missing", "kind_label": "日K缺失",
                    "symbols": ["600000"], "symbol_count": 1}],
    })
    q = score(tmp_path)
    assert next(i for i in q["issues"] if i["code"] == "integrity_flagged")["severity"] == "medium"
    # 未被点名 → 只给全局 info, 不误报安全
    q2 = adq.score_symbol(tmp_path, "000002", make_df(100), FRESH_FINS, now=NOW)
    assert "integrity_global" in codes(q2) and "integrity_flagged" not in codes(q2)


def test_market_block(tmp_path):
    # 无报告 → 空串
    assert adq.market_block(tmp_path) == ""
    _write_integrity(tmp_path, {
        "version": 1, "status": "issues",
        "checked_at": adq.cn_now().isoformat(),
        "repairable_count": 1,
        "issues": [{"kind_label": "日K缺失"}, {"kind_label": "分钟缺口"}],
    })
    block = adq.market_block(tmp_path)
    assert "## 数据体检 (全市场)" in block and "2 处问题" in block
    # clean 状态不打扰
    _write_integrity(tmp_path, {"version": 1, "status": "clean",
                                "checked_at": adq.cn_now().isoformat(), "issues": []})
    assert adq.market_block(tmp_path) == ""


# ===== 等级 / 缓存 / 门控 / prompt 注入 =====

def test_grade_boundaries():
    assert adq._grade_of(100) == "A" and adq._grade_of(90) == "A"
    assert adq._grade_of(89) == "B" and adq._grade_of(75) == "B"
    assert adq._grade_of(74) == "C" and adq._grade_of(60) == "C"
    assert adq._grade_of(59) == "D" and adq._grade_of(0) == "D"


def test_multiple_hard_issues_grade_d(tmp_path):
    # 行数硬伤(25) + 停更 17 个交易日硬伤(25) + 零成交(10) → 40 分 D 级
    df = make_df(10, end=date(2026, 1, 12), vol=0)
    q = score(tmp_path, df=df)
    assert q["score"] == 40 and q["grade"] == "D" and q["hard"] is True


def test_cache_fingerprint(tmp_path):
    df = make_df(100)
    q1 = score(tmp_path, df=df)
    q2 = score(tmp_path, df=df)
    assert q1 is q2  # 指纹相同 → 直接复用同一对象
    q3 = score(tmp_path, df=make_df(99))
    assert q3 is not q1  # 数据变了 → 重算
    q4 = score(tmp_path, df=df, use_cache=False)
    assert q4 is not q1 and q4["score"] == q1["score"]


def test_should_block_default_off(tmp_path, monkeypatch):
    q = score(tmp_path, df=make_df(10, end=date(2026, 1, 12)))
    assert q["grade"] == "D" and q["hard"] is True
    # 默认 (偏好关闭) → 只警示不拦截
    monkeypatch.setattr("app.services.preferences.get_ai_quality_block_enabled", lambda: False)
    assert adq.should_block(q) is False
    # 开启后 D 级硬伤才拦
    monkeypatch.setattr("app.services.preferences.get_ai_quality_block_enabled", lambda: True)
    assert adq.should_block(q) is True
    # 非 D / 非硬伤 / None 都不拦
    assert adq.should_block(score(tmp_path, df=make_df(20))) is False  # B 级硬伤
    assert adq.should_block(None) is False


def test_block_message(tmp_path):
    q = score(tmp_path, df=make_df(10, end=date(2026, 1, 12)))
    msg = adq.block_message(q)
    assert "硬伤" in msg and "数据体检" in msg


def test_prompt_block(tmp_path):
    # 干净 (或仅 info) → 空串不注入
    assert adq.prompt_block(None) == ""
    assert adq.prompt_block(score(tmp_path)) == ""
    # 有问题 → 标题 + 清单 + 分档指令
    q = score(tmp_path, df=make_df(40))
    block = adq.prompt_block(q)
    assert "## 数据体检" in block and "评级" in block and "[medium]" in block
    # D 级给最强指令
    qd = score(tmp_path, df=make_df(10, end=date(2026, 1, 12)))
    assert "不要基于上述受损数据" in adq.prompt_block(qd)
    # info 级不进块 (与前端徽章口径一致)
    qi = score(tmp_path, fins={"metrics": [], "income": []})
    assert adq.prompt_block(qi) == ""


def test_analyzer_prompt_injection(tmp_path):
    """_build_user_prompt 的 quality_context 参数: 空串不出现, 非空追加在末尾。"""
    from app.services import stock_analyzer
    base = dict(asset_type="stock", paper_positions=None, raw_close=None,
                web_search=False)
    args = ([{"date": "2026-02-04", "close": 10.0}], {}, {}, 10.0, "600000", "")
    out = stock_analyzer._build_user_prompt(*args, **base)
    assert "数据体检" not in out
    out = stock_analyzer._build_user_prompt(*args, quality_context="\n## 数据体检 (评级 C, 80/100)", **base)
    assert "## 数据体检 (评级 C, 80/100)" in out
    # 体检块在战绩块之后 (追加顺序)
    out = stock_analyzer._build_user_prompt(
        *args, scorecard_context="## 你的历史战绩", quality_context="## 数据体检", **base)
    assert out.index("历史战绩") < out.index("数据体检")
