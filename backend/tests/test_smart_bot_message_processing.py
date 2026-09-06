from __future__ import annotations

import polars as pl
import pytest

pytest.importorskip("dingtalk_stream")

from app.services import dingtalk_bot_service as dingtalk
from app.services import wecom_bot_service as wecom


class _DummyRepo:
    def __init__(self) -> None:
        self._stocks = pl.DataFrame(
            {
                "symbol": ["600519.SH", "000001.SZ"],
                "name": ["贵州茅台", "平安银行"],
            }
        )
        self._etfs = pl.DataFrame(
            {
                "symbol": ["510300.SH"],
                "name": ["沪深300ETF"],
            }
        )
        self._indexes = pl.DataFrame(
            {
                "symbol": ["000001.SH"],
                "name": ["上证指数"],
            }
        )

    def get_instruments(self) -> pl.DataFrame:
        return self._stocks

    def get_etf_instruments(self) -> pl.DataFrame:
        return self._etfs

    def get_instruments_asset(self, _asset_type: str) -> pl.DataFrame:
        return self._indexes


@pytest.mark.parametrize(
    ("parser", "text", "expected"),
    [
        (dingtalk._parse_stock_analysis_msg, "@机器人 贵州茅台 DeepSeek", ("贵州茅台", "DeepSeek")),
        (wecom._parse_stock_analysis_msg, "贵州茅台 qwen", ("贵州茅台", "通义千问")),
        (dingtalk._parse_stock_analysis_msg, "贵州茅台 unknown-model", None),
        (wecom._parse_stock_analysis_msg, "仅有股票名", None),
    ],
)
def test_parse_stock_analysis_message(parser, text: str, expected):
    assert parser(text) == expected


def test_dingtalk_resolve_symbol_across_stock_etf_and_index():
    repo = _DummyRepo()
    assert dingtalk._resolve_symbol(repo, "贵州茅台") == "600519.SH"
    assert dingtalk._resolve_symbol(repo, "沪深300") == "510300.SH"
    assert dingtalk._resolve_symbol(repo, "上证指数") == "000001.SH"


def test_wecom_resolve_symbol_across_stock_etf_and_index():
    repo = _DummyRepo()
    assert wecom._resolve_symbol(repo, "平安银行") == "000001.SZ"
    assert wecom._resolve_symbol(repo, "沪深300") == "510300.SH"
    assert wecom._resolve_symbol(repo, "上证指数") == "000001.SH"


def test_analysis_report_contains_header_and_meta():
    report = dingtalk._build_analysis_report(
        content="这是分析正文",
        stock_name="贵州茅台",
        symbol="600519.SH",
        model_name="DeepSeek",
        elapsed=12.3,
    )
    assert "AI 个股分析报告" in report
    assert "贵州茅台 (600519.SH)" in report
    assert "DeepSeek" in report
