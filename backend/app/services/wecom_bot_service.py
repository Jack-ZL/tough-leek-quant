"""企业微信智能机器人长连接服务 — WebSocket 保活 + 个股分析交互。

与「群推送 Webhook」(webhook_adapter, 单向 POST) 并存的第二条企业微信通道:
智能机器人开「API 模式 / 长连接」后, 通过 WebSocket 双向通信, 支持 @机器人
交互、个股分析回复。消息处理复用 stock_analyzer.analyze_stock_text 的完整
分析流程(与前端个股分析一致), 并按企业微信回复字节上限截断。

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
import contextlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime

import polars as pl

logger = logging.getLogger(__name__)

# 企业微信智能机器人 WebSocket 固定连接地址
_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
# 心跳间隔(官方要求 ≤30s, 否则服务端断开)
_HEARTBEAT_INTERVAL = 30.0
# 重连退避: base * 2^(n-1), 上限 60s (与 useQuoteStream.ts 一致)
_RECONNECT_BASE_DELAY = 5.0
_RECONNECT_CAP = 60.0
_RECONNECT_MAX_ATTEMPTS = 10  # 连续失败到此次数后仍继续重连, 仅放慢节奏
# 服务端超时未收到响应可能按同一 msgid 重推消息, 按 msgid 去重的 TTL
_MSG_DEDUP_TTL = 600.0

# 企业微信智能机器人回复上限: 最长 20480 字节 (UTF-8, 中文约 3 字节/字), 不能按字符数截取
_REPLY_MAX_BYTES = 20480
_REPLY_TRUNCATE_SUFFIX = "\n\n... (内容过长已截断)"
# AI 正文字数预算: 与群推送不同, 智能机器人单条上限更大, 但报告头+免责也占字节,
# 给正文留 6000 字预算 (全中文约 18000 字节), 为头部与混合编码留余量。
_AI_BODY_CHAR_BUDGET = 6000
_LENGTH_HINT = (
	f"\n\n【输出要求】本报告将通过企业微信智能机器人发送, 平台消息上限 {_REPLY_MAX_BYTES} 字节(UTF-8 编码), "
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


def _truncate_reply_by_bytes(content: str, max_bytes: int = _REPLY_MAX_BYTES) -> str:
	"""按 UTF-8 字节数安全截断回复正文, 不切断多字节字符, 为后缀预留字节。"""
	encoded = content.encode("utf-8")
	if len(encoded) <= max_bytes:
		return content
	suffix_bytes = len(_REPLY_TRUNCATE_SUFFIX.encode("utf-8"))
	cut = encoded[: max_bytes - suffix_bytes]
	return cut.decode("utf-8", errors="ignore") + _REPLY_TRUNCATE_SUFFIX


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


def _profile_switch_note(profile: dict | None) -> str:
	"""P1-15: 消息命中模型档案时的回显文案 (未命中返回空串, 不打扰)。

	临时覆盖只对本次回复生效、不改激活档案, 所以措辞是"本次用"而非"已切换"。
	"""
	if not profile:
		return ""
	name = profile.get("name") or profile.get("id") or "未命名"
	model = profile.get("model") or profile.get("codex_model") or _current_model_label()
	return f"\n🔀 本次用档案「{name}」(模型 {model})"


def _build_analysis_report(
		content: str, stock_name: str, symbol: str, elapsed: float, model: str = "",
) -> str:
	"""组装个股分析报告 Markdown 文本。"""
	from app.services.ai_number_gate import emphasize_marked_wecom, pop_gate_footer
	
	# 数字忠实度闸门: 页脚从正文末尾摘下, 挪到免责声明之后 (同为 "> " 引用行,
	# 合并进同一引用块); 〖…〗标记换成企微 markdown 支持的 warning 字体色。
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
	return emphasize_marked_wecom("\n".join(lines))


class WecomBotService:
	"""企业微信智能机器人 WebSocket 长连接管理器 — 单例。"""
	
	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._running = False  # 连接线程存活标志
		self._thread: threading.Thread | None = None
		self._ws_loop: asyncio.AbstractEventLoop | None = None
		self._connected = False  # WebSocket 是否已连接并通过鉴权
		self._last_error: str = ""
		self._app_state = None  # 延迟注入, 避免循环导入
		self._ws: object | None = None  # 当前 WebSocket 连接 (消息回复用)
		self._seen_msgids: dict[str, float] = {}  # msgid 去重 (TTL 见 _MSG_DEDUP_TTL)
		self._bg_tasks: set[asyncio.Task] = set()  # 后台分析任务强引用
	
	# ================================================================
	# 生命周期
	# ================================================================
	
	def set_app_state(self, app_state) -> None:
		"""注入 FastAPI app.state (供消息处理时访问 repo / datastore)。"""
		self._app_state = app_state
	
	def _is_duplicate_msg(self, msgid: str) -> bool:
		"""msgid 去重 (带 TTL): 首次出现返回 False 并登记, 重复重推返回 True。"""
		if not msgid:
			return False
		now = time.time()
		with self._lock:
			expired = [k for k, ts in self._seen_msgids.items() if now - ts > _MSG_DEDUP_TTL]
			for key in expired:
				del self._seen_msgids[key]
			if msgid in self._seen_msgids:
				return True
			self._seen_msgids[msgid] = now
			return False
	
	def _spawn_bg(self, coro) -> None:
		"""在长连接事件循环上提交后台任务, 持强引用防止被 GC 提前回收。"""
		task = asyncio.create_task(coro)
		self._bg_tasks.add(task)
		task.add_done_callback(self._task_done)
	
	def _task_done(self, task: asyncio.Task) -> None:
		self._bg_tasks.discard(task)
		if not task.cancelled() and task.exception() is not None:
			logger.error("智能机器人后台任务失败: %s", task.exception())
	
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
		# 唤醒可能在 recv/退避 sleep 中的事件循环, 促使其退出
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
		"""启动时检查 preferences, 凭证齐全且 enabled 则自动连接。

		失败静默降级(记 WARNING), 不阻断应用启动。
		"""
		from app.services import preferences
		
		try:
			if preferences.get_wecom_bot_enabled():
				self.start()
		except Exception as e:  # noqa: BLE001
			logger.warning("智能机器人 boot_check 失败: %s", e)
	
	def apply_credential_change(self) -> None:
		"""配置变更后重建连接。单连接限制: 必须 stop 再 start。

		保存新凭证 → stop 旧连接 → 若 enabled 且凭证齐全则 start 新连接。
		"""
		was_running = self._running
		if was_running:
			self.stop()
		# 重新读取最新凭证判断是否应启动
		from app.services import preferences
		if preferences.get_wecom_bot_enabled() and self.start():
			logger.info("智能机器人凭证已更新, 重新连接")
		elif was_running:
			logger.info("智能机器人凭证已更新, 但当前未启用或凭证不齐, 停止连接")
	
	def status(self) -> dict:
		"""返回连接状态(供 UI 展示)。"""
		from app.services import preferences
		return {
			"enabled":           preferences.get_wecom_bot_enabled(),
			"running":           self._running,
			"connected":         self._connected,
			"bot_id_configured": bool(preferences.get_wecom_bot_id()),
			"secret_configured": bool(preferences.get_wecom_bot_secret()),
			"last_error":        self._last_error,
		}
	
	# ================================================================
	# 连接线程
	# ================================================================
	
	def _run_loop(self) -> None:
		"""daemon 线程入口: 创建 asyncio 事件循环并运行连接主循环。"""
		try:
			loop = asyncio.new_event_loop()
			with self._lock:
				self._ws_loop = loop
			asyncio.set_event_loop(loop)
			loop.run_until_complete(self._connect_loop())
		except Exception as e:  # noqa: BLE001
			# stop() 的 loop.stop() 会以 "Event loop stopped before Future
			# completed" 打断 run_until_complete — 属预期停止路径, 不是故障。
			if not isinstance(e, RuntimeError) or "Event loop stopped" not in str(e):
				logger.warning("智能机器人连接线程异常: %s", e)
				self._last_error = str(e)
		finally:
			with self._lock:
				self._connected = False
				self._ws_loop = None
			try:
				# 取消残留任务并等其收尾 (websocket 上下文随之优雅关闭), 再关
				# 循环 — 否则连接任务被销毁时仍 pending, 日志噪音 + 连接靠
				# 服务端超时回收。loop.stop() 打断后循环未关闭, 可再次 run。
				pending = asyncio.all_tasks(loop)
				for task in pending:
					task.cancel()
				if pending:
					# 收尾设 5s 上限: 个别 task 吞取消/慢关 websocket 时不让 daemon
					# 线程无限滞留 (join(10) 超时后仅剩日志误导)
					loop.run_until_complete(
						asyncio.wait_for(
							asyncio.gather(*pending, return_exceptions=True), timeout=5.0,
						),
					)
			except Exception:  # noqa: BLE001
				pass
			try:
				loop.close()
			except Exception:  # noqa: BLE001
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
				# 凭证被清空, 等待重新配置
				self._last_error = "缺少 BotID 或 Secret"
				await self._sleep_interruptible(5.0)
				continue
			
			try:
				async with websockets.connect(
						_WECOM_WS_URL,
						ping_interval=None,  # 用业务层 ping, 不用协议层
						close_timeout=5,
				) as ws:
					# 1. 发送订阅鉴权帧
					req_id = str(uuid.uuid4())
					subscribe_frame = {
						"cmd":     "aibot_subscribe",
						"headers": {"req_id": req_id},
						"body":    {"bot_id": bot_id, "secret": secret},
					}
					await ws.send(json.dumps(subscribe_frame))
					
					# 2. 等待鉴权响应(errcode=0 表示成功)
					resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
					resp = json.loads(resp_raw)
					errcode = resp.get("errcode", resp.get("body", {}).get("errcode", -1))
					if errcode != 0:
						errmsg = resp.get("errmsg", resp.get("body", {}).get("errmsg", "未知错误"))
						self._last_error = f"鉴权失败(errcode={errcode}): {errmsg}"
						logger.warning("智能机器人鉴权失败: %s", self._last_error)
						# 鉴权失败是凭证问题, 重连无益, 等待用户修正
						await self._sleep_interruptible(30)
						continue
					
					# 连接成功
					with self._lock:
						self._connected = True
						self._ws = ws
					self._last_error = ""
					attempt = 0
					logger.info("智能机器人已连接 (bot_id=%s)", bot_id)
					
					# 3. 心跳保活 + 接收循环
					await self._maintain_connection(ws)
			
			except TimeoutError:
				self._last_error = "鉴权响应超时"
				logger.warning("智能机器人鉴权超时")
			except Exception as e:  # noqa: BLE001 — 网络/断开, 可重连
				self._last_error = str(e)
				logger.warning("智能机器人连接异常: %s", e)
			finally:
				with self._lock:
					self._connected = False
					self._ws = None
			
			# 4. 指数退避重连
			if not self._running:
				break
			attempt += 1
			delay = min(_RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), _RECONNECT_CAP)
			logger.info("智能机器人 %ds 后重连(第 %d 次)", delay, attempt)
			await self._sleep_interruptible(delay)
	
	async def _maintain_connection(self, ws) -> None:
		"""连接保持阶段: 每 30s 发 ping, 同时接收服务端推送并分发处理。"""
		while self._running:
			try:
				# 用 wait_for 同时实现"心跳定时"和"接收消息", 哪个先到都行
				try:
					raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_INTERVAL)
					await self._handle_incoming(ws, raw)
					continue
				except TimeoutError:
					pass  # 接收超时 → 到了心跳时间
				# 发送业务层 ping 保活
				await ws.send(json.dumps({"cmd": "ping"}))
			except Exception:
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
		
		# 用户消息: aibot_msg_callback (用户 @机器人 / 单聊发消息)
		if cmd == "aibot_msg_callback":
			# 不能阻塞收帧循环: 心跳 ping 依赖本循环, 慢分析会导致掉线重连
			self._spawn_bg(self._handle_user_message(ws, frame, body))
		# 事件回调: 进入会话 / 卡片点击 / 连接被踢等
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
		
		msgid = body.get("msgid", "")
		if self._is_duplicate_msg(msgid):
			logger.info("智能机器人忽略重复重推消息 msgid=%s", msgid)
			return
		
		logger.info("智能机器人收到用户消息 [%s/%s] %s: %s",
		            chattype, msgtype, userid, str(content)[:100])
		
		if msgtype != "text":
			return
		
		from app.services import ai_provider
		
		profile, cleaned = ai_provider.match_profile_in_text(content)
		stock_name = _parse_stock_analysis_msg(cleaned or content)
		if not stock_name:
			await self._reply_markdown(ws, frame, _HELP_TEXT)
			return
		
		repo = self._app_state.repo if self._app_state else None
		if not repo:
			await self._reply_markdown(ws, frame, "⚠️ 系统未就绪, 请稍后重试")
			return
		
		symbol = _resolve_symbol(repo, stock_name)
		if not symbol:
			await self._reply_markdown(ws, frame, f"未找到股票「{stock_name}」, 请检查名称是否正确")
			return
		
		start_time = time.time()
		try:
			# P1-15: 命中模型档案不再静默切换 — "正在分析"回执里明说本次用哪套配置
			switch_note = _profile_switch_note(profile)
			await self._reply_markdown(
				ws, frame, f"🔍 正在分析 **{stock_name}**({symbol}), 请稍候...{switch_note}",
			)
			with ai_provider.profile_override(profile):
				analysis_content = await self._run_analysis(repo, symbol)
				elapsed = time.time() - start_time
				model = _current_model_label()
			report = _build_analysis_report(analysis_content, stock_name, symbol, elapsed, model)
			await self._reply_markdown(ws, frame, report)
		except Exception as e:
			logger.exception("智能机器人个股分析失败: %s", e)
			await self._reply_markdown(ws, frame, f"⚠️ 分析失败: {e}")
	
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
	
	async def _reply_markdown(self, ws, frame, content: str) -> None:
		"""通过 WebSocket 回复 Markdown 消息 (aibot_respond_msg 帧)。"""
		req_id = frame.get("headers", {}).get("req_id", str(uuid.uuid4()))
		body = frame.get("body", {})
		msgid = body.get("msgid", "")
		
		content = _truncate_reply_by_bytes(content)
		
		respond_frame = {
			"cmd":     "aibot_respond_msg",
			"headers": {"req_id": req_id},
			"body":    {
				"msgid":    msgid,
				"msgtype":  "markdown",
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
