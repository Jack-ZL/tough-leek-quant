"""企业微信智能机器人长连接服务 — WebSocket 保活 + 个股分析交互。

与「群推送 Webhook」(webhook_adapter, 单向 POST) 并存的第二条企业微信通道:
智能机器人开「API 模式 / 长连接」后, 通过 WebSocket 双向通信, 支持 @机器人
交互、流式回复、模板卡片。

架构(对齐 depth_service):
  - daemon 线程内跑 asyncio 事件循环, 用已安装的 websockets(v16) 库
  - _running 线程存活标志 / _enabled 功能开关(持久化)
  - 指数退避重连 min(base * 2^(n-1), 60s), 与 useQuoteStream / _post_feishu 一致
  - 失败静默降级: 连接失败/凭证错误只记 WARNING, 不阻断应用启动

凭证来源: preferences.wecom_bot_id / wecom_bot_secret
连接地址: wss://openws.work.weixin.qq.com (官方固定)
协议帧(官方文档 path/101463):
  订阅 aibot_subscribe → 收 errcode=0 → 每 30s ping → 收消息/事件回调
限制: 每机器人仅 1 条长连接(新连接踢旧连接), 故配置变更需 stop→start
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime

import polars as pl

logger = logging.getLogger(__name__)

_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
_HEARTBEAT_INTERVAL = 30.0
_RECONNECT_BASE_DELAY = 5.0
_RECONNECT_CAP = 60.0
_RECONNECT_MAX_ATTEMPTS = 10

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
    """解析个股分析消息: "@机器人 股票名称 触发词" 或 "股票名称  触发词"。"""
    text = content.strip()
    if not text:
        return None

    text = re.sub(r"^@\S+\s*", "", text).strip()
    if not text:
        return None

    parts = text.split()
    if len(parts) < 2:
        return None

    trigger_input = parts[-1].strip().lower()
    stock_name = " ".join(parts[:-1]).strip()

    display_name = _TRIGGER_KEYWORDS.get(trigger_input)
    if not display_name or not stock_name:
        return None
    return stock_name, display_name


def _resolve_symbol(repo, stock_name: str) -> str | None:
    """根据股票名称查找 symbol (代码.交易所)。"""
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
    except Exception:
        pass
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


class WecomBotService:
    """企业微信智能机器人 WebSocket 长连接管理器 — 单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._last_error: str = ""
        self._app_state = None
        self._ws: object | None = None

    def set_app_state(self, app_state) -> None:
        """注入 FastAPI app.state (供消息处理时访问 repo)。"""
        self._app_state = app_state

    def start(self) -> bool:
        """启动长连接线程。凭证不齐或已运行则跳过。返回是否真正启动。"""
        from app.services import preferences

        bot_id = preferences.get_wecom_bot_id()
        secret = preferences.get_wecom_bot_secret()
        if not bot_id or not secret:
            logger.info("智能机器人未启动: 缺少 BotID 或 Secret")
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = ""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("智能机器人长连接服务已启动 (bot_id=%s)", bot_id)
        return True

    def stop(self) -> None:
        """停止长连接线程。"""
        with self._lock:
            self._running = False
            loop = self._ws_loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        with self._lock:
            self._ws_loop = None
            self._connected = False
            self._ws = None
        logger.info("智能机器人长连接服务已停止")

    def boot_check(self) -> None:
        """启动时检查 preferences, 凭证齐全且 enabled 则自动连接。"""
        from app.services import preferences

        try:
            if preferences.get_wecom_bot_enabled():
                self.start()
        except Exception as e:
            logger.warning("智能机器人 boot_check 失败: %s", e)

    def apply_credential_change(self) -> None:
        """配置变更后重建连接。单连接限制: 必须 stop 再 start。"""
        was_running = self._running
        if was_running:
            self.stop()
        from app.services import preferences

        if preferences.get_wecom_bot_enabled() and self.start():
            logger.info("智能机器人凭证已更新, 重新连接")
        elif was_running:
            logger.info("智能机器人凭证已更新, 但当前未启用或凭证不齐, 停止连接")

    def status(self) -> dict:
        """返回连接状态(供 UI 展示)。"""
        from app.services import preferences

        return {
            "enabled": preferences.get_wecom_bot_enabled(),
            "running": self._running,
            "connected": self._connected,
            "bot_id_configured": bool(preferences.get_wecom_bot_id()),
            "secret_configured": bool(preferences.get_wecom_bot_secret()),
            "last_error": self._last_error,
        }

    def _run_loop(self) -> None:
        """daemon 线程入口: 创建 asyncio 事件循环并运行连接主循环。"""
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            with self._lock:
                self._ws_loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._connect_loop())
        except Exception as e:
            logger.warning("智能机器人连接线程异常: %s", e)
            self._last_error = str(e)
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self._ws_loop = None
                self._ws = None
            if loop is not None:
                try:
                    loop.close()
                except Exception:
                    pass

    async def _connect_loop(self) -> None:
        """主连接循环: 连接 → 鉴权 → 心跳保活 → 断开 → 退避重连。"""
        import websockets
        from app.services import preferences

        attempt = 0
        while self._running:
            bot_id = preferences.get_wecom_bot_id()
            secret = preferences.get_wecom_bot_secret()
            if not bot_id or not secret:
                self._last_error = "缺少 BotID 或 Secret"
                await self._sleep_interruptible(5.0)
                continue

            try:
                async with websockets.connect(
                    _WECOM_WS_URL,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    req_id = str(uuid.uuid4())
                    subscribe_frame = {
                        "cmd": "aibot_subscribe",
                        "headers": {"req_id": req_id},
                        "body": {"bot_id": bot_id, "secret": secret},
                    }
                    await ws.send(json.dumps(subscribe_frame))

                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    resp = json.loads(resp_raw)
                    errcode = resp.get("errcode", resp.get("body", {}).get("errcode", -1))
                    if errcode != 0:
                        errmsg = resp.get("errmsg", resp.get("body", {}).get("errmsg", "未知错误"))
                        self._last_error = f"鉴权失败(errcode={errcode}): {errmsg}"
                        logger.warning("智能机器人鉴权失败: %s", self._last_error)
                        await self._sleep_interruptible(30)
                        continue

                    with self._lock:
                        self._connected = True
                        self._ws = ws
                    self._last_error = ""
                    attempt = 0
                    logger.info("智能机器人已连接 (bot_id=%s)", bot_id)
                    await self._maintain_connection(ws)

            except asyncio.TimeoutError:
                self._last_error = "鉴权响应超时"
                logger.warning("智能机器人鉴权超时")
            except Exception as e:
                self._last_error = str(e)
                logger.warning("智能机器人连接异常: %s", e)
            finally:
                with self._lock:
                    self._connected = False
                    self._ws = None

            if not self._running:
                break
            attempt += 1
            delay = min(_RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), _RECONNECT_CAP)
            logger.info("智能机器人 %ds 后重连(第 %d 次)", delay, attempt)
            await self._sleep_interruptible(delay)

    async def _maintain_connection(self, ws) -> None:
        """连接保持阶段: 每 30s 发 ping, 同时接收服务端推送。"""
        while self._running:
            try:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_INTERVAL)
                    await self._handle_incoming(ws, raw)
                    continue
                except asyncio.TimeoutError:
                    pass
                await ws.send(json.dumps({"cmd": "ping"}))
            except Exception as e:
                raise

    async def _handle_incoming(self, ws, raw) -> None:
        """解析收到的消息帧并处理。"""
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.info("智能机器人收到非 JSON 消息: %s", str(raw)[:200])
            return

        cmd = frame.get("cmd", "?")
        body = frame.get("body", {})

        if cmd == "aibot_msg_callback":
            await self._handle_user_message(ws, frame, body)
        elif cmd == "aibot_event_callback":
            eventtype = body.get("event", {}).get("eventtype", "?")
            logger.info("智能机器人收到事件回调: %s", eventtype)
        else:
            logger.info("智能机器人收到帧 cmd=%s: %s", cmd, str(raw)[:200])

    async def _handle_user_message(self, ws, frame, body) -> None:
        """处理用户消息: 解析 → 个股分析 → 回复。"""
        userid = body.get("from", {}).get("userid", "?")
        chattype = body.get("chattype", "?")
        msgtype = body.get("msgtype", "?")
        content = body.get("text", {}).get("content") or body.get("content", "")

        logger.info("智能机器人收到用户消息 [%s/%s] %s: %s", chattype, msgtype, userid, str(content)[:100])

        if msgtype != "text":
            return

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
            await self._reply_markdown(ws, frame, help_text)
            return

        stock_name, trigger_display = parsed
        repo = self._app_state.repo if self._app_state else None
        if not repo:
            await self._reply_markdown(ws, frame, "⚠️ 系统未就绪, 请稍后重试")
            return

        symbol = _resolve_symbol(repo, stock_name)
        if not symbol:
            await self._reply_markdown(ws, frame, "股票名称错误或模型暂不可用")
            return

        start_time = time.time()
        try:
            analysis_content = await self._run_analysis(repo, symbol)
            elapsed = time.time() - start_time
            report = _build_analysis_report(analysis_content, stock_name, symbol, trigger_display, elapsed)
            await self._reply_markdown(ws, frame, report)
        except Exception as e:
            logger.exception("智能机器人个股分析失败: %s", e)
            await self._reply_markdown(ws, frame, f"⚠️ 分析失败: {e}")

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

    async def _reply_markdown(self, ws, frame, content: str) -> None:
        """通过 WebSocket 回复 Markdown 消息。"""
        req_id = frame.get("headers", {}).get("req_id", str(uuid.uuid4()))
        body = frame.get("body", {})
        msgid = body.get("msgid", "")

        max_len = 20000
        if len(content.encode("utf-8")) > max_len:
            content = content[:max_len] + "\n\n... (内容过长已截断)"

        respond_frame = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgid": msgid,
                "msgtype": "markdown",
                "markdown": {
                    "content": content,
                },
            },
        }
        await ws.send(json.dumps(respond_frame, ensure_ascii=False))
        logger.info("智能机器人已回复 Markdown 消息 (msgid=%s, 长度=%d)", msgid, len(content))

    async def _sleep_interruptible(self, seconds: float) -> None:
        """可被 stop() 中断的 sleep(通过检查 _running)。"""
        waited = 0.0
        while self._running and waited < seconds:
            await asyncio.sleep(min(0.5, seconds - waited))
            waited += 0.5
