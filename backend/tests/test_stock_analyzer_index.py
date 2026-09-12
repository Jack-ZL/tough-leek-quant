"""AI 分析 prompt 指数文案测试。"""
from app.services.stock_analyzer import _build_user_prompt


def test_user_prompt_index_no_financials():
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 3000.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=3000.0, symbol="000001.SH", focus="",
        asset_type="index",
    )
    assert "指数" in prompt
    assert "Free 模式" not in prompt  # 指数无财务是常态, 不走 Free 文案


def test_user_prompt_hk_note():
    """港股标的提示词应含港股语义说明 (无涨跌停/港币口径)。"""
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 88.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=88.0, symbol="00700.HK", focus="",
        asset_type="stock",
    )
    assert "港股" in prompt
    assert "港币" in prompt
    assert "涨跌停" in prompt


def test_user_prompt_ah_premium_note():
    """A 股存在 H 股配对时应附 AH 溢价率比价参考。"""
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 46.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=46.0, symbol="600036.SH", focus="",
        asset_type="stock",
        ah_premium={
            "a_symbol": "600036.SH",
            "h_symbol": "03968.HK",
            "a_close": 46.0,
            "h_close": 50.0,
            "fx": 0.92,
            "premium": 0.0,
        },
    )
    assert "AH" in prompt
    assert "03968.HK" in prompt
    assert "0.0%" in prompt


def test_user_prompt_ah_premium_omitted_when_none():
    """无 AH 配对时不应出现比价文案。"""
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 46.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=46.0, symbol="600519.SH", focus="",
        asset_type="stock",
        ah_premium=None,
    )
    assert "AH 比价" not in prompt
