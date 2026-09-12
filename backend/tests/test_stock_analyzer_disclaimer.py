"""免责声明改为代码追加: 不再让模型输出(省 token), 由系统统一附在报告末尾。"""
import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.services import stock_analyzer
from app.services.stock_analyzer import _DISCLAIMER, _SYSTEM_PROMPT


def test_system_prompt_no_longer_asks_model_for_disclaimer():
    assert "重要免责" not in _SYSTEM_PROMPT
    assert _DISCLAIMER not in _SYSTEM_PROMPT
    assert "不要输出" in _SYSTEM_PROMPT  # 明确告知模型免责声明由系统追加


def test_build_analysis_report_appends_disclaimer():
    from app.services import dingtalk_bot_service as dingtalk
    from app.services import wecom_bot_service as wecom

    for builder in (dingtalk._build_analysis_report, wecom._build_analysis_report):
        report = builder("正文内容", "国晟科技", "603778.SH", "deepseek-v4-pro", 1.2)
        assert "正文内容" in report
        assert report.rstrip().endswith(_DISCLAIMER)


async def test_stream_appends_disclaimer_delta(monkeypatch):
    from app.services import ai_provider

    async def fake_stream(messages, **kwargs):
        yield "正文"

    monkeypatch.setattr(ai_provider, "stream_ai_text", fake_stream)
    monkeypatch.setattr(
        stock_analyzer, "_load_kline",
        lambda repo, symbol: pl.DataFrame({"date": ["2026-01-01"], "close": [10.0]}),
    )
    monkeypatch.setattr(stock_analyzer, "compute_levels", lambda df: {})
    monkeypatch.setattr(stock_analyzer, "_load_financials", lambda data_dir, symbol: {"metrics": [], "income": []})
    monkeypatch.setattr(stock_analyzer, "_clean_rows", lambda df, cols: [])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=Path("/tmp")),
        resolve_asset_type=lambda symbol: "stock",
    )

    events = [json.loads(line) async for line in stock_analyzer.analyze_stock_stream(repo, Path("/tmp"), "603778.SH")]
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    assert events[-1]["type"] == "done"
    assert "".join(deltas).endswith(_DISCLAIMER)
    assert "正文" in "".join(deltas)
