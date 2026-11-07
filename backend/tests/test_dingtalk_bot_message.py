"""钉钉智能机器人消息解析 / 回复组装 / 处理路径测试。

覆盖不依赖真实 LLM 与 SDK 网络的纯逻辑: 消息解析、超长分段发送、报告组装、
标的解析、msgId 去重, 以及消息处理的 help / 未找到 / 非文本分支。
"""
import asyncio
from types import SimpleNamespace

import polars as pl

from app.services import dingtalk_bot_service as d

# ===== 消息解析 =====

def test_parse_stock_name_strips_mention():
    assert d._parse_stock_analysis_msg("@机器人 贵州茅台") == "贵州茅台"
    assert d._parse_stock_analysis_msg("  @某bot   宁德时代") == "宁德时代"
    assert d._parse_stock_analysis_msg("贵州茅台 帮我看下") == "贵州茅台"


def test_parse_stock_name_empty():
    assert d._parse_stock_analysis_msg("@机器人") is None
    assert d._parse_stock_analysis_msg("") is None
    assert d._parse_stock_analysis_msg("   ") is None


# ===== 超长分段发送 =====

def test_reply_markdown_short_single_send():
    svc = d.DingtalkBotService()
    handler = d._ChatbotHandler(svc)
    sent = []
    handler.reply_markdown = lambda title, content, _msg: sent.append((title, content))
    asyncio.run(handler._reply_markdown("提示", "短文本", _incoming()))
    assert sent == [("提示", "短文本")]


def test_reply_markdown_splits_long_content():
    """超过 4000 字符上限时分段发送, 每段不超限且内容不丢 (不截断)。"""
    svc = d.DingtalkBotService()
    handler = d._ChatbotHandler(svc)
    sent = []
    handler.reply_markdown = lambda title, content, _msg: sent.append((title, content))
    body = "\n\n".join(f"## 第{i}节\n\n" + "汉" * 400 for i in range(20))
    asyncio.run(handler._reply_markdown("报告", body, _incoming()))
    assert len(sent) > 1
    for _, content in sent:
        assert len(content) <= d._REPLY_MAX_CHARS
    joined = "".join(c for _, c in sent)
    for i in range(20):
        assert f"第{i}节" in joined  # 尾部内容未被丢弃
    assert f"(1/{len(sent)})" in sent[0][1]


# ===== 报告组装 =====

def test_build_analysis_report():
    rpt = d._build_analysis_report("正文内容", "贵州茅台", "600519.SH", 12.3)
    assert "贵州茅台" in rpt
    assert "600519.SH" in rpt
    assert "正文内容" in rpt
    assert "入市需谨慎" in rpt  # 免责声明兜底


def test_build_report_dedups_disclaimer():
    """正文已含免责声明时不再追加, 避免出现两条重复免责。"""
    body = "分析正文。\n\n> ⚠️ 本内容由 AI 基于公开行情与财务数据生成,不构成任何投资建议。"
    rpt = d._build_analysis_report(body, "贵州茅台", "600519.SH", 1.0)
    assert rpt.count("不构成任何投资建议") == 1


def test_build_report_includes_model():
    rpt = d._build_analysis_report("正文", "贵州茅台", "600519.SH", 1.0, model="deepseek-chat")
    assert "🤖 **分析模型**" in rpt
    assert "deepseek-chat" in rpt


def test_build_report_gate_footer_and_mark():
    """数字忠实度闸门: 问题数字保留可见并加粗+⚠️ 突出; 页脚挪到免责声明后合并引用块。"""
    from app.services.ai_number_gate import MARKED_RE, wrap_marked

    body = (
        f"目标价 {wrap_marked('88.8 元')}。\n\n"
        "> ⚠️ 本内容由 AI 基于公开行情与财务数据生成,不构成任何投资建议或买卖指令。交易有风险,入市需谨慎。\n"
        "> ※ 另有 1 处数字在本轮提供的数据里找不到出处, 也不是回答中算式算出来的, 已标为「〖…〗」。"
    )
    rpt = d._build_analysis_report(body, "贵州茅台", "600519.SH", 1.0)
    # 问题数字原样可见, 换成钉钉支持的醒目形式 (加粗+⚠️), 〖〗 括号不残留
    assert "**⚠️88.8 元**" in rpt
    assert not MARKED_RE.search(rpt)  # 未转换的 〖…※〗 标记不残留 (页脚文案里的「〖…〗」字样除外)
    # 页脚紧跟免责声明 (同为 "> " 行, 中间无空行 → 合并同一引用块)
    assert "入市需谨慎。\n> ※ 另有 1 处" in rpt
    # 正文里的免责声明被识别 → 不再重复追加系统免责
    assert rpt.count("不构成任何投资建议") == 1


def test_build_report_omits_empty_model():
    rpt = d._build_analysis_report("正文", "贵州茅台", "600519.SH", 1.0, model="")
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
    assert d._resolve_symbol(_repo_with_stock(), "贵州茅台") == "600519.SH"
    assert d._resolve_symbol(_repo_with_stock(), "茅台") == "600519.SH"


def test_resolve_symbol_not_found():
    assert d._resolve_symbol(_repo_with_stock(), "不存在") is None


def test_resolve_symbol_falls_back_to_etf_and_index():
    repo = _Repo(
        etfs=pl.DataFrame({"symbol": ["510300.SH"], "name": ["沪深300ETF"]}),
        index=pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]}),
    )
    assert d._resolve_symbol(repo, "沪深300ETF") == "510300.SH"
    assert d._resolve_symbol(repo, "上证指数") == "000001.SH"


# ===== msgId 去重 =====

def test_is_duplicate_msg_ttl():
    svc = d.DingtalkBotService()
    assert svc.is_duplicate_msg("m1") is False
    assert svc.is_duplicate_msg("m1") is True
    assert svc.is_duplicate_msg("m2") is False
    assert svc.is_duplicate_msg("") is False  # 空 msgId 不去重


# ===== 消息处理路径 (不触发真实 LLM / SDK 网络) =====

def _incoming(content="", msgtype="text", msgid="m1"):
    return SimpleNamespace(
        message_type=msgtype,
        message_id=msgid,
        sender_nick="测试用户",
        text=SimpleNamespace(content=content),
        session_webhook="https://example.invalid/webhook",
    )


def _handler(repo=None):
    svc = d.DingtalkBotService()
    svc.set_app_state(SimpleNamespace(repo=repo))
    handler = d._ChatbotHandler(svc)
    replies = []
    async def fake_reply(title, content, _incoming):
        replies.append((title, content))
    handler._reply_markdown = fake_reply
    return handler, replies


def test_handle_message_help_on_unparseable():
    handler, replies = _handler(repo=_repo_with_stock())
    asyncio.run(handler._handle_message(_incoming("@机器人")))
    assert "AI 个股分析机器人" in replies[-1][1]


def test_handle_message_symbol_not_found():
    handler, replies = _handler(repo=_repo_with_stock())
    asyncio.run(handler._handle_message(_incoming("@机器人 不存在股")))
    assert "未找到股票" in replies[-1][1]


def test_handle_message_ignores_non_text():
    handler, replies = _handler(repo=_repo_with_stock())
    asyncio.run(handler._handle_message(_incoming(msgtype="image")))
    assert not replies
