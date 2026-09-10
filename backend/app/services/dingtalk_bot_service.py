"""钉钉智能机器人服务 — 基于官方 dingtalk-stream SDK。

与「群推送 Webhook」(webhook_adapter, 单向 POST) 并存的第二条钉钉通道:
智能机器人开「Stream 模式」后, 通过 WebSocket 双向通信, 支持 @机器人
交互、流式回复、AI Card。

架构:
  - 使用官方 dingtalk-stream SDK, 自动处理注册凭证/CONNECT 帧/心跳/重连
  - daemon 线程内跑 SDK 的 start_forever()
  - _running 线程存活标志 / _enabled 功能开关(持久化)
  - 失败静默降级: 连接失败/凭证错误只记 WARNING, 不阻断应用启动

凭证来源: preferences.dingtalk_bot_id (ClientId) / dingtalk_bot_secret (ClientSecret)
限制: 每机器人仅 1 条长连接(新连接踢旧连接), 故配置变更需 stop→start
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

import dingtalk_stream

logger = logging.getLogger(__name__)

# 触发关键词 → 显示名称映射 (实际使用系统当前配置的 AI 模型)
_TRIGGER_KEYWORDS = {
    "deepseek": "DeepSeek",
    "千问": "通义千问",
    "qwen": "通义千问",
    "通义": "通义千问",
    "智谱": "智谱清言",
    "glm": "智谱清言",
    "kimi": "Kimi",
}


def _parse_stock_analysis_msg(content: str) -> tuple[str, str] | None:
    """解析个股分析消息: "@机器人 股票名称 触发词" 或 "股票名称  触发词"。

    返回 (stock_name, trigger_display) 或 None(格式不匹配)。
    触发词仅用于显示, 实际使用系统当前配置的 AI 模型。
    """
    text = content.strip()
    if not text:
        return None

    # 1. 去掉 @机器人 前缀 (钉钉 @机器人 时消息会带上 @机器人名称)
    text = re.sub(r"^@\S+\s*", "", text).strip()
    if not text:
        return None

    # 2. 分割股票名称和触发词 (用单个空格分割, 取最后部分)
    parts = text.split()
    if len(parts) < 2:
        return None

    # 最后一部分是触发词, 前面所有部分合并为股票名称
    trigger_input = parts[-1].strip().lower()
    stock_name = " ".join(parts[:-1]).strip()

    # 3. 映射触发词 → 显示名称
    display_name = _TRIGGER_KEYWORDS.get(trigger_input)
    if not display_name:
        return None

    if not stock_name:
        return None

    return stock_name, display_name


def _resolve_symbol(repo, stock_name: str) -> str | None:
    """根据股票名称查找 symbol (代码.交易所)。"""
    try:
        # 合并所有 instruments 查找
        for df_name in ("get_instruments", "get_etf_instruments"):
            df = getattr(repo, df_name)()
            if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
                continue
            for symbol, name in df.select(["symbol", "name"]).iter_rows():
                if name == stock_name or stock_name in name:
                    return symbol

        # 指数
        df = repo.get_instruments_asset("index")
        if not df.is_empty() and "symbol" in df.columns and "name" in df.columns:
            for symbol, name in df.select(["symbol", "name"]).iter_rows():
                if name == stock_name or stock_name in name:
                    return symbol
    except Exception as e:
        logger.warning("解析 symbol 失败: %s", e)
    return None


def _build_analysis_report(
    content: str,
    stock_name: str,
    symbol: str,
    model_name: str,
    elapsed: float,
) -> str:
    """组装个股分析报告 Markdown 文本。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elapsed_str = f"{elapsed:.1f}" if elapsed < 60 else f"{int(elapsed // 60)}分{int(elapsed % 60)}秒"

    lines = [
        "📊 **AI 个股分析报告**",
        "",
        f"📌 **标的**: {stock_name} ({symbol})",
        f"🤖 **分析模型**: {model_name}",
        f"⏰ **分析时间**: {now}",
        f"⏱️ **分析用时**: {elapsed_str}",
        "",
        "---",
        "",
        content,
    ]
    return "\n".join(lines)


class _ChatbotHandler(dingtalk_stream.ChatbotHandler):
    """钉钉机器人消息处理器 — 个股分析。"""

    def __init__(self, service: "DingtalkBotService") -> None:
        super().__init__()
        self._service = service

    async def process(self, message):
        """SDK 回调: 处理机器人消息。

        message: CallbackMessage, 其 data 字段包含 ChatbotMessage 的原始 dict。
        返回 (status, message) — dingtalk_stream.AckMessage.STATUS_OK 表示成功。
        """
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(message.data)
            content = incoming_message.text.content.strip() if incoming_message.text else ""
            sender_nick = incoming_message.sender_nick or "?"
            msg_id = incoming_message.message_id or ""

            logger.info("钉钉智能机器人收到用户消息 [%s] msg=%s: %s", sender_nick, msg_id, str(content)[:100])

            parsed = _parse_stock_analysis_msg(content)
            if not parsed:
                help_text = (
                    " 个股分析机器人\n\n"
                    "请使用以下格式发送分析请求:\n"
                    "```\n股票名称  模型名称\n```\n\n"
                    "示例:\n"
                    "```\n贵州茅台  DeepSeek\n```\n\n"
                    "支持的模型: DeepSeek、千问(qwen)、智谱(glm)"
                )
                await self._reply_markdown("使用帮助", help_text, incoming_message)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            stock_name, trigger_display = parsed

            repo = self._service._app_state.repo if self._service._app_state else None
            if not repo:
                await self._reply_markdown("系统提示", "⚠️ 系统未就绪, 请稍后重试", incoming_message)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            symbol = _resolve_symbol(repo, stock_name)
            if not symbol:
                await self._reply_markdown("提示", "股票名称错误或模型暂不可用", incoming_message)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            start_time = time.time()
            try:
                analysis_content = await self._run_analysis(repo, symbol)
                elapsed = time.time() - start_time
                report = _build_analysis_report(analysis_content, stock_name, symbol, trigger_display, elapsed)
                await self._reply_markdown("AI 个股分析报告", report, incoming_message)
            except Exception as e:
                logger.exception("钉钉智能机器人个股分析失败: %s", e)
                await self._reply_markdown("分析失败", f"⚠️ 分析失败: {e}", incoming_message)

            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.exception("钉钉机器人消息处理异常: %s", e)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    async def _run_analysis(self, repo, symbol: str) -> str:
        """执行个股分析, 返回完整分析文本 (使用系统当前配置的 AI 模型)。"""
        from app.services import ai_provider
        from app.services.stock_analyzer import (
            _KLINE_KEEP_COLS,
            _SYSTEM_PROMPT,
            _build_user_prompt,
            _clean_rows,
            _load_financials,
            _load_kline,
            compute_levels,
        )

        df = _load_kline(repo, symbol)
        if df.is_empty():
            return f"标的 {symbol} 暂无日 K 数据, 请先同步"

        levels = compute_levels(df)
        close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
        fins = _load_financials(repo.store.data_dir, symbol)

        kline_tail = _clean_rows(df, _KLINE_KEEP_COLS)
        user_prompt = _build_user_prompt(
            kline_tail,
            fins,
            levels,
            close,
            symbol,
            "",
            asset_type=repo.resolve_asset_type(symbol),
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        full_content = ""
        async for delta in ai_provider.stream_ai_text(
            messages,
            temperature=0.5,
            max_tokens=None,
            timeout=180.0,
        ):
            full_content += delta
        return full_content if full_content else "AI 未返回有效内容, 请重试"

    async def _reply_markdown(self, title: str, content: str, incoming_message) -> None:
        """通过 SDK 回复 Markdown 消息。"""
        msg_id = incoming_message.message_id or ""

        max_len = 20000
        if len(content.encode("utf-8")) > max_len:
            content = content[:max_len] + "\n\n... (内容过长已截断)"

        try:
            self.reply_markdown(title, content, incoming_message)
            logger.info("钉钉智能机器人已回复 Markdown 消息 (msgId=%s, 长度=%d)", msg_id, len(content))
        except Exception as e:
            logger.exception("钉钉回复 Markdown 失败: %s", e)


class DingtalkBotService:
    """钉钉智能机器人 Stream 长连接管理器 — 单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._connected = False
        self._last_error: str = ""
        self._app_state = None
        self._sdk_client = None

    def set_app_state(self, app_state) -> None:
        """注入 FastAPI app.state (供消息处理时访问 repo)。"""
        self._app_state = app_state

    def start(self) -> bool:
        """启动长连接线程。凭证不齐或已运行则跳过。返回是否真正启动。"""
        from app.services import preferences

        bot_id = preferences.get_dingtalk_bot_id()
        secret = preferences.get_dingtalk_bot_secret()
        if not bot_id or not secret:
            logger.info("钉钉智能机器人未启动: 缺少 ClientId 或 ClientSecret")
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = ""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("钉钉智能机器人长连接服务已启动 (bot_id=%s)", bot_id)
        return True

    def stop(self) -> None:
        """停止长连接线程。"""
        with self._lock:
            self._running = False
            client = self._sdk_client
        if client:
            try:
                client.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        with self._lock:
            self._sdk_client = None
            self._connected = False
        logger.info("钉钉智能机器人长连接服务已停止")

    def boot_check(self) -> None:
        """启动时检查 preferences, 凭证齐全且 enabled 则自动连接。"""
        from app.services import preferences

        try:
            if preferences.get_dingtalk_bot_enabled():
                self.start()
        except Exception as e:
            logger.warning("钉钉智能机器人 boot_check 失败: %s", e)

    def apply_credential_change(self) -> None:
        """配置变更后重建连接。单连接限制: 必须 stop 再 start。"""
        was_running = self._running
        if was_running:
            self.stop()
        from app.services import preferences

        if preferences.get_dingtalk_bot_enabled() and self.start():
            logger.info("钉钉智能机器人凭证已更新, 重新连接")
        elif was_running:
            logger.info("钉钉智能机器人凭证已更新, 但当前未启用或凭证不齐, 停止连接")

    def status(self) -> dict:
        """返回连接状态(供 UI 展示)。"""
        from app.services import preferences

        return {
            "enabled": preferences.get_dingtalk_bot_enabled(),
            "running": self._running,
            "connected": self._connected,
            "bot_id_configured": bool(preferences.get_dingtalk_bot_id()),
            "secret_configured": bool(preferences.get_dingtalk_bot_secret()),
            "last_error": self._last_error,
        }

    def _run_loop(self) -> None:
        """daemon 线程入口: 创建并运行 SDK 客户端。"""
        try:
            from app.services import preferences

            bot_id = preferences.get_dingtalk_bot_id()
            secret = preferences.get_dingtalk_bot_secret()
            if not bot_id or not secret:
                self._last_error = "缺少 ClientId 或 ClientSecret"
                return

            credential = dingtalk_stream.Credential(bot_id, secret)
            client = dingtalk_stream.DingTalkStreamClient(credential)

            handler = _ChatbotHandler(self)
            client.register_callback_handler(
                dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                handler,
            )

            with self._lock:
                if not self._running:
                    return
                self._sdk_client = client
                self._connected = True

            logger.info("钉钉智能机器人已连接 (bot_id=%s)", bot_id)
            self._last_error = ""
            client.start_forever()

        except Exception as e:
            logger.warning("钉钉智能机器人连接异常: %s", e)
            self._last_error = str(e)
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self._sdk_client = None