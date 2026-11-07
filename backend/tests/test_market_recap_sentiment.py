"""复盘提示词 情绪细粒度统计块 (P1-19): 构建 / fail-open / regime 行读取。"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services import regime_builder
from app.services.market_recap import (
    _build_sentiment_detail_block,
    _build_user_prompt,
    _load_regime_row,
)

_REGIME_ROW = {
    "promo_t1_pool": 20, "promo_t1_ok": 8,
    "promo_t2_pool": 5, "promo_t2_ok": 2,
    "promo_t3_pool": 0, "promo_t3_ok": 0,
    "promo_t4p_pool": 2, "promo_t4p_ok": 1,
    "premium_pool": 27, "premium_avg_pct": 0.0234, "premium_median_pct": 0.0112,
    "broken_pool": 6, "broken_avg_pct": -0.0315,
}

_OVERVIEW_WITH_IND = {
    "as_of": "2026-01-05",
    "limit_industry": {
        "total": 40, "mapped_total": 36,
        "top5": [
            {"name": "半导体", "count": 8, "pct": 22.2},
            {"name": "软件服务", "count": 5, "pct": 13.9},
            {"name": "汽车零部件", "count": 4, "pct": 11.1},
            {"name": "化学制药", "count": 3, "pct": 8.3},
            {"name": "军工电子", "count": 3, "pct": 8.3},
        ],
        "top5_share": 0.6389, "label": "极集中",
    },
}


# ───────────────────────── 块构建 ─────────────────────────

def test_block_full():
    block = _build_sentiment_detail_block(_OVERVIEW_WITH_IND, _REGIME_ROW)
    assert "分档晋级率" in block
    assert "首板→2板 8/20(40%)" in block
    assert "2板→3板 2/5(40%)" in block
    assert "4板+→次日 1/2(50%)" in block
    assert "3板→4板" not in block  # 池 0 的档不展示
    assert "昨日连板池 27 家今日平均 +2.34% / 中位 +1.12%" in block
    assert "昨日炸板 6 家今日平均 -3.15%" in block
    assert "涨停行业集中度: 极集中(top5 行业占 64%,已映射 36/40 家)" in block
    assert "半导体×8" in block


def test_block_empty_when_no_data():
    assert _build_sentiment_detail_block({"as_of": "2026-01-05"}, None) == ""
    # limit_industry 空壳(top5 空)也不出行业段
    assert _build_sentiment_detail_block(
        {"limit_industry": {"total": 0, "mapped_total": 0, "top5": [], "top5_share": None, "label": None}},
        None,
    ) == ""


def test_block_industry_only_without_regime_row():
    """regime 行缺失(未重算/被禁用)时 fail-open: 只保留行业集中度段。"""
    block = _build_sentiment_detail_block(_OVERVIEW_WITH_IND, None)
    assert "涨停行业集中度" in block
    assert "分档晋级率" not in block and "赚钱效应" not in block


def test_block_partial_regime_row():
    """旧数据缺新列(全 None/缺失): 不崩、对应段跳过。"""
    block = _build_sentiment_detail_block({}, {"promo_t1_pool": None, "premium_pool": 0})
    assert block == ""


# ───────────────────────── 用户提示词集成 ─────────────────────────

def test_user_prompt_includes_section_only_with_data():
    prompt = _build_user_prompt(_OVERVIEW_WITH_IND, [], "", regime_row=_REGIME_ROW)
    assert "## 情绪细粒度统计" in prompt
    assert "首板→2板 8/20(40%)" in prompt

    plain = _build_user_prompt({"as_of": "2026-01-05"}, [], "")
    assert "## 情绪细粒度统计" not in plain


# ───────────────────────── regime 行读取 ─────────────────────────

def test_load_regime_row(monkeypatch):
    df = pl.DataFrame({
        "date": [date(2026, 1, 4), date(2026, 1, 5)],
        "premium_avg_pct": [0.01, 0.02],
    })
    monkeypatch.setattr(regime_builder, "load_regime_history", lambda data_dir: df)

    row = _load_regime_row(None, "2026-01-05")
    assert row is not None and row["premium_avg_pct"] == 0.02
    assert _load_regime_row(None, "2026-01-09") is None  # 缺日
    assert _load_regime_row(None, "bad-date") is None  # 非法日期
    monkeypatch.setattr(regime_builder, "load_regime_history",
                        lambda data_dir: pl.DataFrame({"date": [], "premium_avg_pct": []}))
    assert _load_regime_row(None, "2026-01-05") is None  # 空表
