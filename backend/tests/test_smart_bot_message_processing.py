from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

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


# ================================================================
# ACK 时机与消息去重 (钉钉 Stream 超时重推 → 重复回复)
# ================================================================


class _AppState:
    def __init__(self, repo) -> None:
        self.repo = repo


async def _wait_until(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def _dingtalk_callback_msg(text: str, msg_id: str):
    """构造 SDK 回调消息 (process 只访问 .data)。"""
    return SimpleNamespace(
        data={
            "msgId": msg_id,
            "senderNick": "tester",
            "senderStaffId": "u1",
            "sessionWebhook": "https://example.invalid/webhook",
            "msgtype": "text",
            "text": {"content": text},
        }
    )


@pytest.fixture
def dingtalk_handler():
    service = dingtalk.DingtalkBotService()
    service.set_app_state(_AppState(_DummyRepo()))
    handler = dingtalk._ChatbotHandler(service)

    analysis_calls: list[str] = []
    replies: list[str] = []

    async def fake_run_analysis(repo, symbol):
        analysis_calls.append(symbol)
        await asyncio.sleep(0.5)  # 模拟 LLM 慢分析 (远超钉钉 ~10s ACK 上限的量级)
        return "分析内容"

    async def fake_reply(title, content, incoming_message):
        replies.append(title)

    handler._run_analysis = fake_run_analysis
    handler._reply_markdown = fake_reply
    return handler, service, analysis_calls, replies


async def test_dingtalk_process_acks_before_slow_analysis(dingtalk_handler):
    """ACK 必须先于分析完成返回, 否则钉钉服务端超时重推同一消息。"""
    handler, _service, analysis_calls, replies = dingtalk_handler

    start = time.monotonic()
    _status, _ = await handler.process(_dingtalk_callback_msg("贵州茅台 DeepSeek", "m-ack"))
    ack_elapsed = time.monotonic() - start

    assert ack_elapsed < 0.2, f"process 耗时 {ack_elapsed:.2f}s, ACK 被慢分析阻塞"
    assert analysis_calls == [], "ACK 返回时分析不应已完成 (应在后台执行)"

    # 后台任务先回「正在分析」占位, 分析完成后回完整报告, 共两条
    assert await _wait_until(lambda: replies == ["正在分析"])
    assert await _wait_until(
        lambda: replies == ["正在分析", "AI 个股分析报告"]
    )
    assert analysis_calls == ["600519.SH"]


async def test_dingtalk_dedup_redelivered_message(dingtalk_handler):
    """同一 msgId 被重推时只分析和回复一次。"""
    handler, _service, analysis_calls, replies = dingtalk_handler

    await handler.process(_dingtalk_callback_msg("贵州茅台 DeepSeek", "m-dup"))
    await handler.process(_dingtalk_callback_msg("贵州茅台 DeepSeek", "m-dup"))

    assert await _wait_until(lambda: len(replies) == 2)
    await asyncio.sleep(0.1)  # 留出第二次(错误的)分析入队的时间
    assert analysis_calls == ["600519.SH"], "重复重推的消息触发了第二次分析"
    assert replies == ["正在分析", "AI 个股分析报告"], "重复重推的消息导致了重复回复"


@pytest.fixture
def wecom_service():
    service = wecom.WecomBotService()
    service.set_app_state(_AppState(_DummyRepo()))

    analysis_calls: list[str] = []
    sent: list[dict] = []

    async def fake_run_analysis(repo, symbol):
        analysis_calls.append(symbol)
        await asyncio.sleep(0.5)
        return "分析内容"

    service._run_analysis = fake_run_analysis

    class _FakeWs:
        async def send(self, raw):
            sent.append(json.loads(raw))

    return service, _FakeWs(), analysis_calls, sent


def _wecom_frame(msgid: str) -> str:
    return json.dumps(
        {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r1"},
            "body": {
                "msgid": msgid,
                "from": {"userid": "u1"},
                "chattype": "group",
                "msgtype": "text",
                "text": {"content": "贵州茅台 DeepSeek"},
            },
        },
        ensure_ascii=False,
    )


async def test_wecom_handle_incoming_not_blocked_by_analysis(wecom_service):
    """收帧循环不能被慢分析阻塞 (否则心跳停发导致掉线), 分析放后台。"""
    service, ws, analysis_calls, sent = wecom_service

    start = time.monotonic()
    await service._handle_incoming(ws, _wecom_frame("w-ack"))
    elapsed = time.monotonic() - start

    assert elapsed < 0.2, f"_handle_incoming 耗时 {elapsed:.2f}s, 心跳循环被分析阻塞"
    assert analysis_calls == []

    assert await _wait_until(
        lambda: [f["cmd"] for f in sent] == ["aibot_respond_msg", "aibot_respond_msg"]
    )
    assert "正在分析" in sent[0]["body"]["markdown"]["content"]
    assert "AI 个股分析报告" in sent[1]["body"]["markdown"]["content"]
    assert analysis_calls == ["600519.SH"]


async def test_wecom_dedup_redelivered_message(wecom_service):
    """同一 msgid 被重推时只分析和回复一次。"""
    service, ws, analysis_calls, sent = wecom_service

    await service._handle_incoming(ws, _wecom_frame("w-dup"))
    await service._handle_incoming(ws, _wecom_frame("w-dup"))

    assert await _wait_until(lambda: len(sent) == 2)
    await asyncio.sleep(0.1)
    assert analysis_calls == ["600519.SH"], "重复重推的消息触发了第二次分析"
    assert [f["cmd"] for f in sent] == ["aibot_respond_msg"] * 2, "重复重推导致重复回复"
async def test_dingtalk_reply_truncates_to_char_limit():
    """钉钉智能机器人回复上限 4000 字符: 超限按字符数截断。"""
    service = dingtalk.DingtalkBotService()
    handler = dingtalk._ChatbotHandler(service)
    sent: list[str] = []
    handler.reply_markdown = lambda title, content, msg: sent.append(content)

    big = "股" * 8000
    await handler._reply_markdown("t", big, SimpleNamespace(message_id="m1"))

    out = sent[0]
    assert len(out) <= 4000, "截断后仍超钉钉 4000 字符上限"
    assert "内容过长已截断" in out


async def test_wecom_reply_truncates_by_utf8_bytes(wecom_service):
    """企微智能机器人上限 20480 字节(UTF-8): 超限按字节安全截断。"""
    service, ws, _calls, sent = wecom_service
    big = "股" * 8000  # 24000 字节 > 20480
    frame = {"headers": {"req_id": "r1"}, "body": {"msgid": "w-trunc"}}
    await service._reply_markdown(ws, frame, big)

    out = sent[0]["body"]["markdown"]["content"]
    assert len(out.encode("utf-8")) <= 20480, "截断后字节数仍超企微上限"
    assert "内容过长已截断" in out
    assert "\ufffd" not in out, "截断切在多字节字符中间"

    # 20480 字节以内不应截断 (旧上限 20000 会误截)
    sent.clear()
    fits = "股" * 6800  # 20400 字节 <= 20480
    await service._reply_markdown(ws, frame, fits)
    assert sent[0]["body"]["markdown"]["content"] == fits


class _FakeDf:
    columns = ("close",)

    def is_empty(self):
        return False

    def tail(self, n):
        return {"close": [10.0]}


async def _capture_analysis_messages(monkeypatch, run_analysis):
    """打桩 stock_analyzer/ai_provider, 捕获 _run_analysis 实际发送的 messages。"""
    from app.services import ai_provider, stock_analyzer

    captured: list = []
    monkeypatch.setattr(stock_analyzer, "_load_kline", lambda repo, symbol: _FakeDf())
    monkeypatch.setattr(stock_analyzer, "_load_financials", lambda *a, **k: {})
    monkeypatch.setattr(stock_analyzer, "compute_levels", lambda df: {})
    monkeypatch.setattr(stock_analyzer, "_clean_rows", lambda df, cols: [])
    monkeypatch.setattr(stock_analyzer, "_build_user_prompt", lambda *a, **k: "KLINE_DATA")

    async def fake_stream(messages, **kwargs):
        captured.append(messages)
        yield "分析结论"

    monkeypatch.setattr(ai_provider, "stream_ai_text", fake_stream)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir="/tmp"),
        resolve_asset_type=lambda symbol: "stock",
    )
    content = await run_analysis(repo, "600519.SH")
    assert content == "分析结论"
    return captured[0]


async def test_dingtalk_analysis_prompt_states_platform_char_limit(monkeypatch):
    """钉钉来源的分析请求需告知 AI 平台字符上限, 让模型主动控制篇幅。"""
    service = dingtalk.DingtalkBotService()
    handler = dingtalk._ChatbotHandler(service)
    messages = await _capture_analysis_messages(monkeypatch, handler._run_analysis)
    user_content = messages[1]["content"]
    assert "钉钉" in user_content
    assert "4000" in user_content


async def test_wecom_analysis_prompt_states_platform_byte_limit(monkeypatch):
    """企微来源的分析请求需告知 AI 平台字节上限。"""
    service = wecom.WecomBotService()
    messages = await _capture_analysis_messages(monkeypatch, service._run_analysis)
    user_content = messages[1]["content"]
    assert "企业微信" in user_content
    assert "20480" in user_content
