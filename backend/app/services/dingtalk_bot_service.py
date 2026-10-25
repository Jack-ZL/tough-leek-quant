"""钉钉智能机器人 Stream 长连接服务 — 个股分析交互。

与「群推送 Webhook」(webhook_adapter, 单向 POST) 并存的第二条钉钉通道:
智能机器人开「Stream 模式」后, 通过 dingtalk-stream SDK 双向通信, 支持 @机器人
交互、个股分析回复。消息处理复用 stock_analyzer.analyze_stock_text 的完整
分析流程(与前端个股分析一致), 并按钉钉回复字符上限截断。

架构(对齐 wecom_bot_service):
  - daemon 线程内跑 SDK 的 start_forever() (内部自建事件循环 + 自动重连)
  - _running 线程存活标志 / _enabled 功能开关(持久化)
  - 失败静默降级: 凭证错误/连接失败只记 WARNING, 不阻断应用启动

凭证来源: preferences.dingtalk_bot_id / dingtalk_bot_secret (ClientId / ClientSecret)
回复方式: SDK ChatbotHandler.reply_markdown (POST 到消息会话 Webhook), 按钉钉
        智能机器人回复上限 4000 字符截断(按字符, 非字节)。
限制: SDK 的 start_forever 无对外 close 接口, 配置变更需 stop 后起新线程重建。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from datetime import datetime

import dingtalk_stream
import polars as pl

logger = logging.getLogger(__name__)

# 钉钉智能机器人回复上限: 最长 4000 字符 (按字符截断, 非字节)
_REPLY_MAX_CHARS = 4000
_REPLY_TRUNCATE_SUFFIX = "\n\n... (内容过长已截断)"
# AI 正文字数预算: 报告头 + 免责也占字符, 给正文留 3500 字预算。
_AI_BODY_CHAR_BUDGET = 3500
_LENGTH_HINT = (
    f"\n\n【输出要求】本报告将通过钉钉智能机器人发送, 平台消息上限 {_REPLY_MAX_CHARS} 字符, "
    f"请将分析正文全文控制在 {_AI_BODY_CHAR_BUDGET} 字以内。"
    "注意: 正文末尾请勿输出任何免责声明或风险提示, 免责声明由系统统一追加, 不计入正文字数。"
    "以上要求优先于系统提示中的字数范围与免责声明要求; 宁可压缩内容, 也不要被平台截断。"
)

# 报告末尾免责声明 (由机器人统一追加, 与 stock_analyzer 系统提示词措辞一致)
_REPORT_DISCLAIMER = (
    "> ⚠️ 本内容由 AI 基于公开行情与财务数据生成,仅客观陈述技术状态,"
    "不构成任何投资建议或买卖指令。交易有风险,入市需谨慎。"
)

_HELP_TEXT = (
    "📊 **AI 个股分析机器人**\n\n"
    "请使用以下格式发送分析请求:\n"
    "```\n@机器人 [模型名称] 股票名称\n```\n\n"
    "示例:\n"
    "```\n@机器人 贵州茅台\n@机器人 deepseek 贵州茅台\n@机器人 千问 贵州茅台\n```\n\n"
    "模型名称可选: 支持档案名/模型名, 也支持厂商简称 (如 千问/qwen、deepseek、kimi、智谱/glm, "
    "不区分大小写); 命中已配置的模型档案时本次分析改用该模型, 未命中则使用当前生效模型。"
)

# 服务端超时未收到响应可能按同一 msgId 重推消息, 按 msgId 去重的 TTL
_MSG_DEDUP_TTL = 600.0


def _truncate_reply_by_chars(content: str, max_chars: int = _REPLY_MAX_CHARS) -> str:
    """按字符数安全截断回复正文, 为后缀预留字符。"""
    if len(content) <= max_chars:
        return content
    return content[: max_chars - len(_REPLY_TRUNCATE_SUFFIX)] + _REPLY_TRUNCATE_SUFFIX


def _parse_stock_analysis_msg(content: str) -> str | None:
    """解析个股分析消息: "@机器人 股票名称"。

    去掉开头的 @提及 后, 取第一个词作为股票名称 (A 股简称不含空格)。
    返回股票名称; 无法解析返回 None。
    """
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^@\S+\s*", "", text).strip()
    if not text:
        return None
    return text.split()[0]


def _resolve_symbol(repo, stock_name: str) -> str | None:
    """根据股票名称查找 symbol (代码.交易所), 支持精确 + 模糊匹配。"""
    try:
        for df_name in ("get_instruments", "get_etf_instruments"):
            df = getattr(repo, df_name)()
            if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
                continue
            hit = df.filter(pl.col("name") == stock_name).head(1)
            if not hit.is_empty():
                return hit["symbol"][0]
            hit = df.filter(pl.col("name").str.contains(stock_name)).head(1)
            if not hit.is_empty():
                return hit["symbol"][0]

        df = repo.get_instruments_asset("index")
        if not df.is_empty() and "symbol" in df.columns and "name" in df.columns:
            hit = df.filter(pl.col("name") == stock_name).head(1)
            if not hit.is_empty():
                return hit["symbol"][0]
            hit = df.filter(pl.col("name").str.contains(stock_name)).head(1)
            if not hit.is_empty():
                return hit["symbol"][0]
    except Exception:  # noqa: BLE001 — 标的查询失败走"未找到"提示
        pass
    return None


def _current_model_label() -> str:
    """返回报告头部标注用的模型名; 模型缺失时回退到 provider, 均空返回空串。"""
    from app.services import ai_provider

    model = ai_provider.current_ai_model()
    if not model:
        model = ai_provider.current_ai_provider()
    return str(model or "").strip()


def _build_analysis_report(
    content: str, stock_name: str, symbol: str, elapsed: float, model: str = ""
) -> str:
    """组装个股分析报告 Markdown 文本。"""
    from app.services.ai_number_gate import emphasize_marked_dingtalk, pop_gate_footer

    # 数字忠实度闸门: 页脚从正文末尾摘下, 挪到免责声明之后 (同为 "> " 引用行,
    # 钉钉渲染时合并进同一引用块); 〖…〗标记换成钉钉支持的加粗+⚠️ 醒目形式。
    content, gate_footer = pop_gate_footer(content)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elapsed_str = f"{elapsed:.1f}" if elapsed < 60 else f"{int(elapsed // 60)}分{int(elapsed % 60)}秒"

    lines = [
        "📊 **AI 个股分析报告**",
        "",
        f"📌 **标的**: {stock_name} ({symbol})",
        f"⏰ **分析时间**: {now}",
        f"⏱️ **分析用时**: {elapsed_str}",
    ]
    if model:
        lines.append(f"🤖 **分析模型**: {model}")
    lines += [
        "",
        "---",
        "",
        content,
    ]
    # 机器人 length_hint 已要求模型不要输出免责声明(由系统统一追加); 此处兜底追加,
    # 若模型仍输出则去重, 保证报告末尾有且仅有一条免责声明。
    if "不构成任何投资建议" not in content:
        lines += ["", _REPORT_DISCLAIMER]
    if gate_footer:
        lines.append(gate_footer)
    return emphasize_marked_dingtalk("\n".join(lines))


class _ChatbotHandler(dingtalk_stream.ChatbotHandler):
    """钉钉机器人消息处理器 — 个股分析。"""

    def __init__(self, service: DingtalkBotService) -> None:
        super().__init__()
        self._service = service

    async def process(self, message):
        """SDK 回调: 立即 ACK, 解析与分析交给后台任务。

        SDK 的 raw_process 会 await 本协程返回后才向钉钉服务端发送 ACK, 而服务端
        要求约 10s 内 ACK, 否则按同一 msgId 重推消息; AI 分析常需数分钟, 因此
        必须立即返回 ACK 再后台分析, 并按 msgId 去重防止重推导致重复回复。
        """
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(message.data)
            content = incoming_message.text.content.strip() if incoming_message.text else ""
            sender_nick = incoming_message.sender_nick or "?"
            msg_id = incoming_message.message_id or ""

            logger.info(
                "钉钉智能机器人收到用户消息 [%s] msg=%s: %s",
                sender_nick, msg_id, str(content)[:100],
            )

            if self._service.is_duplicate_msg(msg_id):
                logger.info("钉钉智能机器人忽略重复重推消息 msg=%s", msg_id)
            else:
                self._service.spawn_bg(self._handle_message(incoming_message))
        except Exception:  # 解析失败不影响 ACK
            logger.exception("钉钉机器人消息处理异常")
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    async def _handle_message(self, incoming_message) -> None:
        """后台执行: 解析 → 个股分析 → 回复 (不阻塞 ACK)。"""
        try:
            if incoming_message.message_type != "text":
                return

            content = incoming_message.text.content.strip() if incoming_message.text else ""

            from app.services import ai_provider

            profile, cleaned = ai_provider.match_profile_in_text(content)
            stock_name = _parse_stock_analysis_msg(cleaned or content)
            if not stock_name:
                await self._reply_markdown("使用帮助", _HELP_TEXT, incoming_message)
                return

            repo = self._service._app_state.repo if self._service._app_state else None
            if not repo:
                await self._reply_markdown("系统提示", "⚠️ 系统未就绪, 请稍后重试", incoming_message)
                return

            symbol = _resolve_symbol(repo, stock_name)
            if not symbol:
                await self._reply_markdown(
                    "提示", f"未找到股票「{stock_name}」, 请检查名称是否正确", incoming_message
                )
                return

            start_time = time.time()
            try:
                await self._reply_markdown(
                    "分析中", f"🔍 正在分析 **{stock_name}**({symbol}), 请稍候...", incoming_message
                )
                with ai_provider.profile_override(profile):
                    analysis_content = await self._run_analysis(repo, symbol)
                    elapsed = time.time() - start_time
                    model = _current_model_label()
                report = _build_analysis_report(analysis_content, stock_name, symbol, elapsed, model)
                await self._reply_markdown("AI 个股分析报告", report, incoming_message)
            except Exception as e:  # 分析失败需回复用户
                logger.exception("钉钉智能机器人个股分析失败: %s", e)
                await self._reply_markdown("分析失败", f"⚠️ 分析失败: {e}", incoming_message)
        except Exception:
            logger.exception("钉钉机器人后台任务异常")

    async def _run_analysis(self, repo, symbol: str) -> str:
        """执行个股分析, 复用 stock_analyzer.analyze_stock_text 的完整流程。"""
        from app.services.stock_analyzer import analyze_stock_text

        return await analyze_stock_text(
            repo,
            repo.store.data_dir,
            symbol,
            focus="",
            length_hint=_LENGTH_HINT,
        )

    async def _reply_markdown(self, title: str, content: str, incoming_message) -> None:
        """通过 SDK 回复 Markdown 消息 (POST 到消息会话 Webhook)。"""
        content = _truncate_reply_by_chars(content)

        # SDK 的 reply_markdown 是同步 requests.post, 移出事件循环避免阻塞收帧/心跳
        await asyncio.to_thread(self.reply_markdown, title, content, incoming_message)
        logger.info(
            "钉钉智能机器人已回复 Markdown 消息 (msgId=%s, 长度=%d)",
            incoming_message.message_id, len(content),
        )


class DingtalkBotService:
    """钉钉智能机器人 Stream 长连接管理器 — 单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False          # 连接线程存活标志
        self._thread: threading.Thread | None = None
        self._connected = False        # SDK 是否已启动并进入收发循环
        self._last_error: str = ""
        self._app_state = None         # 延迟注入, 避免循环导入
        self._sdk_client = None        # 当前 SDK 客户端 (stop 时尽力关闭)
        self._seen_msg_ids: dict[str, float] = {}  # msgId 去重 (TTL 见 _MSG_DEDUP_TTL)
        self._bg_tasks: set[asyncio.Task] = set()  # 后台分析任务强引用

    # ================================================================
    # 生命周期
    # ================================================================

    def set_app_state(self, app_state) -> None:
        """注入 FastAPI app.state (供消息处理时访问 repo / datastore)。"""
        self._app_state = app_state

    def is_duplicate_msg(self, msg_id: str) -> bool:
        """msgId 去重 (带 TTL): 首次出现返回 False 并登记, 重复重推返回 True。"""
        if not msg_id:
            return False
        now = time.time()
        with self._lock:
            expired = [k for k, ts in self._seen_msg_ids.items() if now - ts > _MSG_DEDUP_TTL]
            for key in expired:
                del self._seen_msg_ids[key]
            if msg_id in self._seen_msg_ids:
                return True
            self._seen_msg_ids[msg_id] = now
            return False

    def spawn_bg(self, coro) -> None:
        """在 SDK 事件循环上提交后台任务, 持强引用防止被 GC 提前回收。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("钉钉机器人后台任务失败: %s", task.exception())

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
        """停止长连接线程。

        SDK 的 start_forever 无对外 close 接口, 仅能尽力关闭底层 websocket 促使
        SDK 退出收发循环; 线程为 daemon, 不阻塞进程退出。
        """
        with self._lock:
            self._running = False
            client = self._sdk_client
        if client is not None:
            ws = getattr(client, "websocket", None)
            if ws is not None and hasattr(ws, "close"):
                with contextlib.suppress(Exception):
                    ws.close()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            self._sdk_client = None
            self._connected = False
        logger.info("钉钉智能机器人长连接服务已停止")

    def boot_check(self) -> None:
        """启动时检查 preferences, 凭证齐全且 enabled 则自动连接。

        失败静默降级(记 WARNING), 不阻断应用启动。
        """
        from app.services import preferences

        try:
            if preferences.get_dingtalk_bot_enabled():
                self.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("钉钉智能机器人 boot_check 失败: %s", e)

    def apply_credential_change(self) -> None:
        """配置变更后重建连接。凭证变更需 stop 后起新线程重读凭证。"""
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

    # ================================================================
    # 连接线程
    # ================================================================

    def _run_loop(self) -> None:
        """daemon 线程入口: 创建 SDK 客户端并阻塞运行 start_forever()。"""
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

            self._last_error = ""
            logger.info("钉钉智能机器人已连接 (bot_id=%s)", bot_id)
            # 阻塞: SDK 内部自建事件循环并自动重连
            client.start_forever()

        except Exception as e:  # noqa: BLE001
            logger.warning("钉钉智能机器人连接异常: %s", e)
            self._last_error = str(e)
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self._sdk_client = None
