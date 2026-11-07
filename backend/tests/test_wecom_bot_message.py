"""企业微信智能机器人消息解析 / 回复组装 / 处理路径测试。

覆盖不依赖真实 LLM 的纯逻辑: 消息解析、超长分段发送、报告组装、标的解析,
以及 @机器人 消息处理的 help / 去重 / 未找到 / 非文本分支。
"""
import asyncio
import json
from types import SimpleNamespace

import polars as pl

from app.services import wecom_bot_service as w

# ===== 消息解析 =====

def test_parse_stock_name_strips_mention():
    assert w._parse_stock_analysis_msg("@机器人 贵州茅台") == "贵州茅台"
    assert w._parse_stock_analysis_msg("  @某bot   宁德时代") == "宁德时代"
    assert w._parse_stock_analysis_msg("贵州茅台 帮我看下") == "贵州茅台"


def test_parse_stock_name_empty():
    assert w._parse_stock_analysis_msg("@机器人") is None
    assert w._parse_stock_analysis_msg("") is None
    assert w._parse_stock_analysis_msg("   ") is None


# ===== 超长分段发送 =====

def test_reply_markdown_short_single_send():
    svc = w.WecomBotService()
    ws = _WS()
    asyncio.run(svc._reply_markdown(ws, _text_frame("m0", "x"), "短文本"))
    assert len(ws.sent) == 1
    assert ws.sent[0]["body"]["markdown"]["content"] == "短文本"


def test_reply_markdown_splits_long_content():
    """超过 20480 字节上限时分段发送, 每段不超限且内容不丢 (不截断)。"""
    svc = w.WecomBotService()
    ws = _WS()
    body = "\n\n".join(f"## 第{i}节\n\n" + "汉" * 2000 for i in range(15))
    asyncio.run(svc._reply_markdown(ws, _text_frame("m9", "x"), body))
    assert len(ws.sent) > 1
    for frame in ws.sent:
        content = frame["body"]["markdown"]["content"]
        assert len(content.encode("utf-8")) <= w._REPLY_MAX_BYTES
    joined = "".join(f["body"]["markdown"]["content"] for f in ws.sent)
    for i in range(15):
        assert f"第{i}节" in joined  # 尾部内容未被丢弃
    assert f"(1/{len(ws.sent)})" in ws.sent[0]["body"]["markdown"]["content"]


# ===== 报告组装 =====

def test_build_analysis_report():
    rpt = w._build_analysis_report("正文内容", "贵州茅台", "600519.SH", 12.3)
    assert "贵州茅台" in rpt
    assert "600519.SH" in rpt
    assert "正文内容" in rpt
    assert "入市需谨慎" in rpt  # 免责声明兜底


def test_build_report_dedups_disclaimer():
    """正文已含免责声明时不再追加, 避免出现两条重复免责。"""
    body = "分析正文。\n\n> ⚠️ 本内容由 AI 基于公开行情与财务数据生成,不构成任何投资建议。"
    rpt = w._build_analysis_report(body, "贵州茅台", "600519.SH", 1.0)
    assert rpt.count("不构成任何投资建议") == 1


def test_build_report_includes_model():
    rpt = w._build_analysis_report("正文", "贵州茅台", "600519.SH", 1.0, model="deepseek-chat")
    assert "🤖 **分析模型**" in rpt
    assert "deepseek-chat" in rpt


def test_build_report_gate_footer_and_mark():
    """数字忠实度闸门: 问题数字保留可见并换企微 warning 字体色; 页脚挪到免责声明后合并引用块。"""
    from app.services.ai_number_gate import MARKED_RE, wrap_marked

    body = (
        f"目标价 {wrap_marked('88.8 元')}。\n"
        "> ※ 另有 1 处数字在本轮提供的数据里找不到出处, 也不是回答中算式算出来的, 已标为「〖…〗」。"
    )
    rpt = w._build_analysis_report(body, "贵州茅台", "600519.SH", 1.0)
    assert '<font color="warning">⚠️88.8 元</font>' in rpt
    assert not MARKED_RE.search(rpt)  # 未转换的 〖…※〗 标记不残留 (页脚文案里的「〖…〗」字样除外)
    # 页脚被摘出并挪到系统追加的免责声明之后, 紧邻无空行 → 同一引用块
    assert "入市需谨慎。\n> ※ 另有 1 处" in rpt


def test_build_report_omits_empty_model():
    rpt = w._build_analysis_report("正文", "贵州茅台", "600519.SH", 1.0, model="")
    assert "分析模型" not in rpt


# ===== 标的解析 =====

class _Repo:
    def __init__(self, stocks=None, etfs=None, index=None):
        self._stocks = stocks if stocks is not None else pl.DataFrame()
        self._etfs = etfs if etfs is not None else pl.DataFrame()
        self._index = index if index is not None else pl.DataFrame()

    def get_instruments(self):
        return self._stocks

    def get_etf_instruments(self):
        return self._etfs

    def get_instruments_asset(self, _asset_type):
        return self._index


def _repo_with_stock():
    return _Repo(stocks=pl.DataFrame({"symbol": ["600519.SH"], "name": ["贵州茅台"]}))


def test_resolve_symbol_exact_and_fuzzy():
    assert w._resolve_symbol(_repo_with_stock(), "贵州茅台") == "600519.SH"
    assert w._resolve_symbol(_repo_with_stock(), "茅台") == "600519.SH"


def test_resolve_symbol_not_found():
    assert w._resolve_symbol(_repo_with_stock(), "不存在") is None


def test_resolve_symbol_falls_back_to_etf_and_index():
    repo = _Repo(
        etfs=pl.DataFrame({"symbol": ["510300.SH"], "name": ["沪深300ETF"]}),
        index=pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]}),
    )
    assert w._resolve_symbol(repo, "沪深300ETF") == "510300.SH"
    assert w._resolve_symbol(repo, "上证指数") == "000001.SH"


# ===== 消息处理路径 (不触发真实 LLM) =====

class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _frame(body):
    return {"headers": {"req_id": "req-1"}, "body": body}


def _text_frame(msgid, content):
    return _frame({"msgid": msgid, "msgtype": "text", "text": {"content": content}})


def test_handle_user_message_help_on_unparseable():
    svc = w.WecomBotService()
    svc.set_app_state(SimpleNamespace(repo=_repo_with_stock()))
    ws = _WS()
    frame = _text_frame("m1", "@机器人")
    asyncio.run(svc._handle_user_message(ws, frame, frame["body"]))
    assert "AI 个股分析机器人" in ws.sent[-1]["body"]["markdown"]["content"]


def test_handle_user_message_dedup_msgid():
    svc = w.WecomBotService()
    svc.set_app_state(SimpleNamespace(repo=_repo_with_stock()))
    ws = _WS()
    frame = _text_frame("m1", "@机器人")
    asyncio.run(svc._handle_user_message(ws, frame, frame["body"]))
    before = len(ws.sent)
    asyncio.run(svc._handle_user_message(ws, frame, frame["body"]))
    assert len(ws.sent) == before


def test_handle_user_message_symbol_not_found():
    svc = w.WecomBotService()
    svc.set_app_state(SimpleNamespace(repo=_repo_with_stock()))
    ws = _WS()
    frame = _text_frame("m2", "@机器人 不存在股")
    asyncio.run(svc._handle_user_message(ws, frame, frame["body"]))
    assert "未找到股票" in ws.sent[-1]["body"]["markdown"]["content"]


def test_handle_user_message_ignores_non_text():
    svc = w.WecomBotService()
    svc.set_app_state(SimpleNamespace(repo=_repo_with_stock()))
    ws = _WS()
    frame = _frame({"msgid": "m3", "msgtype": "image"})
    asyncio.run(svc._handle_user_message(ws, frame, frame["body"]))
    assert not ws.sent
