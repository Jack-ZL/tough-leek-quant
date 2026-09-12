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

_BOTH_PARSERS = [dingtalk._parse_stock_analysis_msg, wecom._parse_stock_analysis_msg]

# 模拟 secrets.json 中用户手工加了 id 的 ai_profiles (智能机器人按触发词匹配)
_FAKE_PROFILES = {
    "ai_profiles": [
        {
            "id": "qwen",
            "name": "qwen3.8-max",
            "provider": "openai_compat",
            "base_url": "https://example.invalid/qwen/v1",
            "api_key": "sk-q",
            "model": "qwen3.8-max",
        },
        {
            "id": "deepseek",
            "name": "deepseek-v4-pro",
            "provider": "openai_compat",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-d",
            "model": "deepseek-v4-pro",
        },
        {
            "id": "gpt5",
            "name": "gpt-5.5",
            "provider": "openai_compat",
            "base_url": "https://example.invalid/gpt/v1",
            "api_key": "sk-g",
            "model": "gpt-5.5",
        },
    ]
}


@pytest.fixture(autouse=True)
def _isolated_secrets(monkeypatch):
    """默认空 secrets.json, 避免读到开发机上真实的 ai_profiles/密钥。"""
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "load", lambda: {})


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
        (dingtalk._parse_stock_analysis_msg, "@机器人 贵州茅台 DeepSeek", ("贵州茅台", "DeepSeek", None)),
        (wecom._parse_stock_analysis_msg, "贵州茅台 qwen", ("贵州茅台", "通义千问", None)),
        (dingtalk._parse_stock_analysis_msg, "贵州茅台 unknown-model", None),
        (wecom._parse_stock_analysis_msg, "仅有股票名", None),
        # 触发词大小写不一致也要能匹配
        (dingtalk._parse_stock_analysis_msg, "@机器人 国晟科技 deepSEEK", ("国晟科技", "DeepSeek", None)),
        (wecom._parse_stock_analysis_msg, "国晟科技 DEEPSEEK", ("国晟科技", "DeepSeek", None)),
        (dingtalk._parse_stock_analysis_msg, "贵州茅台 QWen", ("贵州茅台", "通义千问", None)),
    ],
)
def test_parse_stock_analysis_message(parser, text: str, expected):
    assert parser(text) == expected


@pytest.mark.parametrize("parser", _BOTH_PARSERS)
@pytest.mark.parametrize("trigger", ["千问", "qwen", "QWEN", "qWeN", "通义"])
def test_parse_trigger_matches_profile_case_insensitive(parser, trigger, monkeypatch):
    """触发词无论大小写, 都要经别名映射匹配到 ai_profiles 中 id=qwen 的配置。"""
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "load", lambda: dict(_FAKE_PROFILES))
    parsed = parser(f"国晟科技 {trigger}")
    assert parsed is not None
    stock_name, display, profile = parsed
    assert stock_name == "国晟科技"
    assert display == "通义千问"
    assert profile["id"] == "qwen"
    assert profile["model"] == "qwen3.8-max"


@pytest.mark.parametrize("parser", _BOTH_PARSERS)
def test_parse_trigger_direct_profile_id(parser, monkeypatch):
    """别名表以外的触发词, 只要与某个 profile id 匹配(忽略大小写)也可用。"""
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "load", lambda: dict(_FAKE_PROFILES))
    stock_name, display, profile = parser("贵州茅台 GPT5")
    assert stock_name == "贵州茅台"
    assert profile["id"] == "gpt5"
    assert display == "gpt-5.5"  # 无内置显示名时用 profile 名称


@pytest.mark.parametrize("mod", [dingtalk, wecom])
def test_resolve_trigger_profile_alias_then_direct(mod, monkeypatch):
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "load", lambda: dict(_FAKE_PROFILES))
    assert mod._resolve_trigger_profile("千问")["id"] == "qwen"
    assert mod._resolve_trigger_profile("DEEPSEEK")["id"] == "deepseek"
    assert mod._resolve_trigger_profile("gpt5")["id"] == "gpt5"
    assert mod._resolve_trigger_profile("unknown") is None
    assert mod._resolve_trigger_profile("") is None


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
def dingtalk_handler(monkeypatch):
    from app import secrets_store

    # 提供假 ai_profiles, 使 "DeepSeek" 触发词能解析到 profile
    monkeypatch.setattr(secrets_store, "load", lambda: dict(_FAKE_PROFILES))
    service = dingtalk.DingtalkBotService()
    service.set_app_state(_AppState(_DummyRepo()))
    handler = dingtalk._ChatbotHandler(service)

    analysis_calls: list[str] = []
    replies: list[str] = []

    async def fake_run_analysis(repo, symbol, profile=None):
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
def wecom_service(monkeypatch):
    from app import secrets_store

    # 提供假 ai_profiles, 使 "DeepSeek" 触发词能解析到 profile
    monkeypatch.setattr(secrets_store, "load", lambda: dict(_FAKE_PROFILES))
    service = wecom.WecomBotService()
    service.set_app_state(_AppState(_DummyRepo()))

    analysis_calls: list[str] = []
    sent: list[dict] = []

    async def fake_run_analysis(repo, symbol, profile=None):
        analysis_calls.append(symbol)
        await asyncio.sleep(0.5)
        return "分析内容"

    service._run_analysis = fake_run_analysis

    class _FakeWs:
        async def send(self, raw):
            sent.append(json.loads(raw))

    return service, _FakeWs(), analysis_calls, sent


def _wecom_frame(msgid: str, content: str = "贵州茅台 DeepSeek") -> str:
    return json.dumps(
        {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r1"},
            "body": {
                "msgid": msgid,
                "from": {"userid": "u1"},
                "chattype": "group",
                "msgtype": "text",
                "text": {"content": content},
            },
        },
        ensure_ascii=False,
    )


async def test_dingtalk_rejects_unconfigured_model(dingtalk_handler):
    """触发词可识别但 ai_profiles 未配置对应模型时, 提示不支持, 不回退默认模型分析。"""
    handler, _service, analysis_calls, replies = dingtalk_handler
    contents: list[str] = []

    async def fake_reply(title, content, incoming_message):
        replies.append(title)
        contents.append(content)

    handler._reply_markdown = fake_reply
    await handler.process(_dingtalk_callback_msg("贵州茅台 Kimi", "m-nomodel"))

    assert await _wait_until(lambda: len(contents) == 1)
    await asyncio.sleep(0.1)  # 留出(错误的)分析入队时间
    assert "未配置" in contents[0]
    assert len(contents) == 1, "不应继续发送分析报告"
    assert analysis_calls == [], "未配置的模型不应触发分析"


async def test_wecom_rejects_unconfigured_model(wecom_service):
    """企微侧同样: 触发词未配置对应 profile 时只回一条不支持提示。"""
    service, ws, analysis_calls, sent = wecom_service

    await service._handle_incoming(ws, _wecom_frame("w-nomodel", content="贵州茅台 Kimi"))

    assert await _wait_until(lambda: len(sent) == 1)
    await asyncio.sleep(0.1)
    assert "未配置" in sent[0]["body"]["markdown"]["content"]
    assert len(sent) == 1, "不应继续发送分析报告"
    assert analysis_calls == [], "未配置的模型不应触发分析"


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


async def _capture_analysis_messages(monkeypatch, run_analysis, **run_kwargs):
    """打桩 stock_analyzer/ai_provider, 捕获 _run_analysis 实际发送的 messages 与 kwargs。"""
    from app.services import ai_provider, stock_analyzer

    captured: list = []
    captured_kwargs: list = []
    monkeypatch.setattr(stock_analyzer, "_load_kline", lambda repo, symbol: _FakeDf())
    monkeypatch.setattr(stock_analyzer, "_load_financials", lambda *a, **k: {})
    monkeypatch.setattr(stock_analyzer, "compute_levels", lambda df: {})
    monkeypatch.setattr(stock_analyzer, "_clean_rows", lambda df, cols: [])
    monkeypatch.setattr(stock_analyzer, "_build_user_prompt", lambda *a, **k: "KLINE_DATA")

    async def fake_stream(messages, **kwargs):
        captured.append(messages)
        captured_kwargs.append(kwargs)
        yield "分析结论"

    monkeypatch.setattr(ai_provider, "stream_ai_text", fake_stream)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir="/tmp"),
        resolve_asset_type=lambda symbol: "stock",
    )
    content = await run_analysis(repo, "600519.SH", **run_kwargs)
    assert content == "分析结论"
    return captured[0], captured_kwargs[0]


async def test_dingtalk_analysis_prompt_states_platform_char_limit(monkeypatch):
    """钉钉来源的分析请求需告知 AI 平台字符上限, 让模型主动控制篇幅。"""
    service = dingtalk.DingtalkBotService()
    handler = dingtalk._ChatbotHandler(service)
    messages, _kwargs = await _capture_analysis_messages(monkeypatch, handler._run_analysis)
    user_content = messages[1]["content"]
    assert "钉钉" in user_content
    assert "4000" in user_content


async def test_wecom_analysis_prompt_states_platform_byte_limit(monkeypatch):
    """企微来源的分析请求需告知 AI 平台字节上限。"""
    service = wecom.WecomBotService()
    messages, _kwargs = await _capture_analysis_messages(monkeypatch, service._run_analysis)
    user_content = messages[1]["content"]
    assert "企业微信" in user_content
    assert "20480" in user_content


async def test_dingtalk_analysis_passes_profile(monkeypatch):
    """触发词匹配到的 ai_profiles 配置需原样传给 stream_ai_text。"""
    profile = _FAKE_PROFILES["ai_profiles"][0]
    service = dingtalk.DingtalkBotService()
    handler = dingtalk._ChatbotHandler(service)
    _messages, kwargs = await _capture_analysis_messages(
        monkeypatch, handler._run_analysis, profile=profile
    )
    assert kwargs["profile"] is profile


async def test_wecom_analysis_passes_profile(monkeypatch):
    profile = _FAKE_PROFILES["ai_profiles"][1]
    service = wecom.WecomBotService()
    _messages, kwargs = await _capture_analysis_messages(
        monkeypatch, service._run_analysis, profile=profile
    )
    assert kwargs["profile"] is profile


async def test_analysis_without_profile_keeps_configured_model(monkeypatch):
    """未匹配到 profile 时 profile=None, 由 ai_provider 回退系统当前配置的模型。"""
    service = dingtalk.DingtalkBotService()
    handler = dingtalk._ChatbotHandler(service)
    _messages, kwargs = await _capture_analysis_messages(monkeypatch, handler._run_analysis)
    assert kwargs["profile"] is None


async def test_stream_ai_text_forwards_profile(monkeypatch):
    """ai_provider.stream_ai_text 需把 profile 透传给 _stream_openai。"""
    from app.services import ai_provider

    captured: dict = {}
    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(ai_provider, "_check_input_budget", lambda *a, **k: None)

    async def fake_stream_openai(messages, **kwargs):
        captured.update(kwargs)
        yield "ok"

    monkeypatch.setattr(ai_provider, "_stream_openai", fake_stream_openai)
    profile = _FAKE_PROFILES["ai_profiles"][0]
    out = [
        chunk
        async for chunk in ai_provider.stream_ai_text(
            [{"role": "user", "content": "hi"}], profile=profile
        )
    ]
    assert out == ["ok"]
    assert captured["profile"] is profile


async def test_stream_ai_text_codex_ignores_profile(monkeypatch):
    """Codex CLI 路径不支持 profile 覆盖: 忽略并走默认流程, 不抛异常。"""
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda: True)
    monkeypatch.setattr(ai_provider, "_check_input_budget", lambda *a, **k: None)

    async def fake_codex(messages, **kwargs):
        return "done"

    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_codex)
    out = [
        chunk
        async for chunk in ai_provider.stream_ai_text(
            [{"role": "user", "content": "hi"}],
            profile=_FAKE_PROFILES["ai_profiles"][0],
        )
    ]
    assert out == ["done"]


async def test_stream_openai_uses_profile_config(monkeypatch):
    """_stream_openai 需用 profile 的 api_key/base_url/model 覆盖全局配置。"""
    from app.services import ai_provider

    captured_client: dict = {}
    captured_create: dict = {}

    def fake_openai_client(api_key, timeout, **kwargs):
        captured_client.update(api_key=api_key, **kwargs)

        async def create(**create_kwargs):
            captured_create.update(create_kwargs)
            return object()

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    async def fake_iter_text(stream):
        yield "ok"

    monkeypatch.setattr(ai_provider, "_openai_client", fake_openai_client)
    monkeypatch.setattr(ai_provider, "_iter_openai_text", fake_iter_text)

    profile = _FAKE_PROFILES["ai_profiles"][0]  # qwen: sk-q @ example.invalid/qwen/v1
    out = [
        chunk
        async for chunk in ai_provider._stream_openai(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
            max_tokens=None,
            timeout=5.0,
            prefer_final_answer=False,
            profile=profile,
        )
    ]
    assert out == ["ok"]
    assert captured_client["api_key"] == "sk-q"
    assert captured_client["base_url"] == "https://example.invalid/qwen/v1"
    assert captured_create["model"] == "qwen3.8-max"
