"""多空辩论 (P1-9) — 让几个 AI 开会辩论一只股票。

对标 jcp / TradingAgents-AShare / Vibe-Research 的多智能体辩论, 但刻意做成
轻量按需版 (QuantMind 曾做成常驻重型服务, 维护成本过高已于 2026-10 拆掉):
无常驻进程、无调度器, 用户点一次跑一次, 全程在一次 SSE 请求生命周期内。

## 流程

  ① 备料: 复用 stock_analyzer 的数据包 (日K+指标 / 关键价位 / 财务摘要),
     三方拿同一份材料, 数字闸门白名单也从这份材料采集。
  ② 多方 & 空方 **并行** 独立分析 (asyncio 并发, 互相看不到对方输出 —
     两个独立 LLM 会话, 不存在共享上下文)。系统提示词强制立场。
  ③ 风控主管审阅材料 + 双方全文, 输出机读裁决块 DEBATE_RULING:
     通过(pass) 或 打回(rewrite, 指定打回哪一方 + 理由) + 答题卡研判。
  ④ 打回 → 被批评方带意见重写 **一轮** → 主管终审 (终审必拍板)。
     硬上限 5 次 LLM 调用: 多、空、裁决、重写×1、终审。

## 角色模型分配 (调研报告特别提示)

  "全用一家的模型, 它容易自己同意自己" — 三个角色可各自绑定一个已配置的
  模型档案 (preferences.debate_role_profiles), 调用时套 profile_override
  (contextvar, 并行任务互不串扰, 不落盘不改激活档案)。未绑定 → 激活档案。

## 协议 (NDJSON, 事件带 role 字段路由到前端双栏)

  {"type":"meta","symbol","name","summary","levels","close","roles":{...}}
  {"type":"role_delta","role":"bull|bear|judge","content":"..."}
  {"type":"role_done","role":"...","content":"该角色全文(干净)","rewritten":bool}
  {"type":"rewrite","role":"...","reason":"主管打回理由"}
  {"type":"ruling","ok":true,"decision":"pass|rewrite","verdict":{"direction","reason"},"final":bool}
  {"type":"ruling","ok":false}          裁决块解析失败 (fail-honest, 不猜)
  {"type":"error","message":"..."} / {"type":"done"}

## fail-honest 纪律 (与 ai_verdict 一致)

  裁决块缺失/JSON 损坏/枚举外 → ruling ok=false, 前端明示"未解析出裁决",
  绝不从主管正文里猜结论。打回解析不出目标角色 → 视为通过 (宁可少重写,
  不可错重写)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

from app.indicators.levels import compute_levels, summarize_levels
from app.services.ai_verdict import VERDICT_DIRECTIONS

logger = logging.getLogger(__name__)

# 角色常量 (协议字段值, 前端按此路由双栏)
ROLE_BULL = "bull"
ROLE_BEAR = "bear"
ROLE_JUDGE = "judge"
ROLES = (ROLE_BULL, ROLE_BEAR, ROLE_JUDGE)

# 每方论点长度上限提示 (控制成本与阅读负担; 主管裁决不限, 它要引用双方)。
# 用汉字"八百"而非数字 — 系统提示词会进数字闸门白名单, 阿拉伯数字会留假放行口子。

# ================================================================
# 系统提示词 (刻意不含阿拉伯数字示例 — 会进数字闸门白名单, 留假放行口子;
# 长度上限用汉字表述, 同 VERDICT_PROMPT 纪律)
# ================================================================

_BULL_SYSTEM = """你是一场多空辩论中的【多方辩手】。你的唯一任务是论证看多该标的。

纪律:
一、只能使用用户提供的材料中的数据, 禁止编造任何数字、新闻或公告。
二、立场鲜明: 只讲看多的理由 (上涨逻辑、支撑因素、催化剂、估值优势), 不允许骑墙、不允许"但也存在风险"式的和稀泥 — 风险提示是风控主管的事。
三、每个论点都要落到材料里的具体数据 (价位、指标、财务), 空喊口号视为无效论证。
四、全文控制在八百字以内, 分条陈述, 结尾用一句话给出多方核心结论。
五、不输出具体买卖操作指令 (点位目标可以谈, "建议买入/卖出"不可以)。"""

_BEAR_SYSTEM = """你是一场多空辩论中的【空方辩手】。你的唯一任务是论证看空该标的。

纪律:
一、只能使用用户提供的材料中的数据, 禁止编造任何数字、新闻或公告。
二、立场鲜明: 只讲看空的理由 (下跌风险、压力因素、利空信号、估值泡沫), 不允许骑墙、不允许"但也有积极因素"式的和稀泥 — 亮点陈述是多方辩手的事。
三、每个论点都要落到材料里的具体数据 (价位、指标、财务), 空喊口号视为无效论证。
四、全文控制在八百字以内, 分条陈述, 结尾用一句话给出空方核心结论。
五、不输出具体买卖操作指令 (点位风险可以谈, "建议买入/卖出"不可以)。"""

_JUDGE_SYSTEM = """你是一场多空辩论的【风控主管】。双方辩手已就同一份材料各自陈述, 你负责审阅并拍板。

你的输出分三部分, 依次进行:
一、逐方点评: 分别指出多方与空方论证中的硬伤 — 引用材料数据错误、逻辑跳跃、
   以偏概全、空喊口号无数据支撑。哪一方论证质量高就说高, 不各打五十大板。
二、综合裁决: 基于材料与双方论证, 给出你自己的独立研判结论 (不是简单站队,
   双方都错时你要给出第三种判断)。
三、机读裁决块 (必须输出, 格式固定, 键名不可改动): 在全文最末另起一行输出
<!-- DEBATE_RULING: {"decision": "pass", "rewrite_role": "", "rewrite_reason": "", "direction": "看多", "reason": "一句话核心结论"} -->
   - decision 只可填 pass (双方论证合格, 裁决生效) 或 rewrite (某一方分析糊弄, 打回重写)
   - decision 为 rewrite 时: rewrite_role 填被打回的一方 (bull 或 bear, 只能选一方, 挑问题更严重的), rewrite_reason 用中文写明打回理由与重写要求 (不超过五十字); decision 为 pass 时这两个键留空字符串
   - direction 只可填: 看多 / 偏多 / 中性 / 偏空 / 看空 (这是你本人的研判, 数据有倾向必须选偏多或偏空, 确实无法判断才选中性)
   - reason 不超过二十个字, 中文, 内部不得出现英文双引号和换行
   - 该标记全文只出现一次且必须在最后一行; 它是给程序读的, 不要在正文里解释或复述它

打回权只在初审有效: 若用户消息注明【终审】, 无论质量如何 decision 必须填 pass, 直接拍板。
纪律: 只能引用材料中出现过的数据; 不输出具体买卖操作指令。"""

# 终审时追加到主管用户消息的强制拍板提示
_FINAL_NOTE = "\n\n【终审】本轮为终审, 重写机会已用完, decision 必须填 pass, 请直接给出最终裁决。"

# ================================================================
# 机读裁决块解析 (模式与 ai_verdict 一致: 正则捕获 + fail-honest)
# ================================================================

# DOTALL: 容忍模型违规把字符串写出换行, 整体捕获交给 json.loads 裁决
RULING_RE = re.compile(r"<!--\s*DEBATE_RULING:\s*(\{.*?\})\s*-->", re.DOTALL)

_REASON_MAX = 60          # 研判理由展示截断 (同 ai_verdict)
_REWRITE_REASON_MAX = 120  # 打回理由截断 (要转给重写方, 放宽些)


def parse_ruling(text: str) -> dict | None:
    """从主管全文解析裁决块 → 合法 dict 或 None (缺失/损坏/枚举外一律 None)。

    返回 {"decision": "pass"|"rewrite", "rewrite_role": "bull"|"bear"|"",
          "rewrite_reason": str, "direction": str, "reason": str}
    多处标记取最后一处 (模型复述示例时以末尾为准, 同 parse_verdict)。
    """
    last: re.Match | None = None
    for m in RULING_RE.finditer(text):
        last = m
    if last is None:
        return None
    try:
        obj = json.loads(last.group(1))
    except (json.JSONDecodeError, ValueError):
        logger.warning("[debate] 裁决块 JSON 损坏: %s", last.group(1)[:120])
        return None
    if not isinstance(obj, dict):
        return None

    decision = str(obj.get("decision", "")).strip()
    if decision not in ("pass", "rewrite"):
        return None
    direction = str(obj.get("direction", "")).strip()
    if direction not in VERDICT_DIRECTIONS:
        return None

    rewrite_role = str(obj.get("rewrite_role", "")).strip()
    if rewrite_role not in (ROLE_BULL, ROLE_BEAR):
        rewrite_role = ""
    # 打回但没指明角色 → 无法执行打回, 降级为通过 (宁可少重写, 不可错重写)
    if decision == "rewrite" and not rewrite_role:
        decision = "pass"
    rewrite_reason = str(obj.get("rewrite_reason", "")).strip()[:_REWRITE_REASON_MAX]
    reason = str(obj.get("reason", "")).strip()
    if len(reason) > _REASON_MAX:
        reason = reason[:_REASON_MAX] + "…"
    return {
        "decision": decision,
        "rewrite_role": rewrite_role,
        "rewrite_reason": rewrite_reason,
        "direction": direction,
        "reason": reason,
    }


def strip_ruling(text: str) -> str:
    """剥掉全文中的裁决标记 (展示/落库用干净文本)。"""
    return RULING_RE.sub("", text)


async def _strip_ruling_deltas(chunks: AsyncIterator[str], captured: list[str]) -> AsyncIterator[str]:
    """流式剥离裁决标记: 行缓冲, 残行不下发 (标记可能跨 chunk 被切开)。

    与 ai_verdict.verdict_ndjson_stream 同款纪律: 客户端永远看不到标记残片。
    捕获的标记原文追加进 captured, 供流结束后 parse。
    """
    buf = ""

    def _strip(segment: str) -> str:
        for m in RULING_RE.finditer(segment):
            captured.append(m.group(1))
        return RULING_RE.sub("", segment)

    async for chunk in chunks:
        buf += chunk
        # 只发完整行, 残行留在 buf
        while (nl := buf.find("\n")) >= 0:
            line = buf[: nl + 1]
            buf = buf[nl + 1 :]
            had_marker = bool(RULING_RE.search(line))
            line = _strip(line)
            if had_marker and not line.strip():
                continue  # 标记独占一行 → 整行丢弃, 不留尾部空行
            if line:
                yield line
    # 流结束: 冲刷余量 (裁决块缺失时正文尾巴不能吞)
    if buf:
        out = _strip(buf)
        if out:
            yield out


# ================================================================
# 角色模型档案解析
# ================================================================

def resolve_role_profiles() -> dict[str, dict | None]:
    """角色 → 模型档案 dict (None = 用当前激活档案)。

    未手动指定的角色自动轮转分配: 按 多→空→主管 的角色顺序, 依次取档案列表
    第 0/1/2… 位, 取完从头循环 (2 个档案时: 多=A 空=B 主管=A; 3 个时各一个)。
    这样多空双方天然落在不同模型上, 避免同源偏见互相附和; 只有一个档案
    (或档案列表为空) 时全员回退激活档案 — 没得选就不硬凑。
    手动指定的档案 id 失效 (被删除) 时同样回退轮转位 — 辩论不能因设置过期跑不了。
    """
    from app import secrets_store
    from app.services import preferences

    try:
        assignments = preferences.get_debate_role_profiles()
    except Exception:  # noqa: BLE001 — 偏好损坏不阻塞辩论
        assignments = {}
    try:
        profile_list = list(secrets_store.list_ai_profiles())
    except Exception:  # noqa: BLE001
        profile_list = []
    by_id = {str(p.get("id")): p for p in profile_list}

    result: dict[str, dict | None] = {}
    for idx, role in enumerate(ROLES):
        pid = str(assignments.get(role) or "")
        if pid and pid in by_id:
            result[role] = by_id[pid]
        elif profile_list:
            result[role] = profile_list[idx % len(profile_list)]
        else:
            result[role] = None
    return result


def role_model_display(profile: dict | None) -> str:
    """meta 事件里展示的角色模型名 (未绑定档案 → 当前激活模型名)。"""
    if profile:
        return str(profile.get("model") or profile.get("name") or "")
    from app.services.ai_provider import current_ai_model
    try:
        return current_ai_model()
    except Exception:  # noqa: BLE001
        return ""


# ================================================================
# 材料包 (三方共享的唯一事实来源)
# ================================================================

def build_materials(repo, data_dir: Path, symbol: str) -> dict | None:
    """备料: 返回 {kline_rows, levels, close, fins, asset_type, text} 或 None (无K线)。

    复用 stock_analyzer 的私有工具 — 辩论与个股分析必须看同一份材料,
    数据口径 (窗口/指标/清洗规则) 不允许出现第二套。
    """
    from app.services import stock_analyzer

    df = stock_analyzer._load_kline(repo, symbol)
    if df.is_empty():
        return None
    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    asset_type = repo.resolve_asset_type(symbol)
    fins = {} if asset_type in {"etf", "index"} else stock_analyzer._load_financials(data_dir, symbol)
    kline_rows = stock_analyzer._clean_rows(df, stock_analyzer._KLINE_KEEP_COLS)

    text = "\n".join([
        f"标的标准代码: {symbol}",
        f"关键价位概览: {summarize_levels(levels, close)}",
        "",
        "以下是该标的最近日 K 数据(JSON,含 OHLCV 与已计算的技术指标,升序):",
        "```json",
        json.dumps(kline_rows, ensure_ascii=False),
        "```",
    ])
    if any(fins.values()):
        text += "\n\n以下是该标的最新财务数据(JSON,金额单位为元):\n```json\n" \
                + json.dumps(fins, ensure_ascii=False) + "\n```"
    else:
        text += "\n\n(该标的暂无财务数据, 请勿编造财务数字。)"

    # 数据体检 (P1-11): 与个股分析同一套打分, 体检块直接拼进材料文本 —
    # 多方/空方/风控主管拿到的材料逐字节一致, 对数据局限的认知也一致。
    # fail-open: 体检异常只跳过注入, 不影响辩论备料。
    from app.services import ai_data_quality
    try:
        quality = ai_data_quality.score_symbol(data_dir, symbol, df, fins, asset_type)
    except Exception:  # noqa: BLE001
        logger.warning("[P1-11] 辩论备料数据体检失败 (跳过注入): %s", symbol, exc_info=True)
        quality = None
    quality_text = ai_data_quality.prompt_block(quality)
    if quality_text:
        text += "\n" + quality_text

    return {
        "kline_rows": kline_rows, "levels": levels, "close": close,
        "fins": fins, "asset_type": asset_type, "text": text,
        "quality": quality,  # P1-11: meta 事件透传给前端徽章
    }


def _role_user_prompt(materials_text: str, focus: str, role: str) -> str:
    """辩手用户消息: 材料 + 关注点。多空拿到的材料逐字节一致 (公平辩论)。"""
    parts = [materials_text]
    if focus:
        from app.services.ai_provider import sanitize_focus
        cleaned = sanitize_focus(focus)
        if cleaned:
            parts.append(f"\n用户特别关注: {cleaned}")
    if role == ROLE_BULL:
        parts.append("\n请陈述你的看多论证。")
    else:
        parts.append("\n请陈述你的看空论证。")
    return "\n".join(parts)


def _judge_user_prompt(materials_text: str, bull_text: str, bear_text: str,
                       final: bool = False) -> str:
    """主管用户消息: 材料 + 双方全文 (+ 终审标记)。"""
    parts = [
        materials_text,
        "\n\n===== 多方辩手陈述 =====\n" + bull_text,
        "\n\n===== 空方辩手陈述 =====\n" + bear_text,
        "\n请按系统提示词要求: 先逐方点评, 再综合裁决, 最后输出机读裁决块。",
    ]
    if final:
        parts.append(_FINAL_NOTE)
    return "\n".join(parts)


def _rewrite_user_prompt(materials_text: str, focus: str, role: str, critique: str) -> str:
    """重写方用户消息: 原材料 + 主管打回意见 (立场不变, 只修论证质量)。"""
    base = _role_user_prompt(materials_text, focus, role)
    return base + (
        f"\n\n【打回重写】风控主管对你的上一版陈述不满意, 打回理由如下:\n{critique}\n"
        "请针对打回理由重写你的完整陈述 (立场不变, 仍是"
        + ("看多论证" if role == ROLE_BULL else "看空论证")
        + "), 不要提及打回过程本身。"
    )


# ================================================================
# 编排器
# ================================================================

async def _run_role_llm(role: str, system_prompt: str, user_prompt: str,
                        profile: dict | None, pool, temperature: float,
                        out_q: asyncio.Queue) -> None:
    """跑一个辩手角色: 流式产出经数字闸门过滤后推入队列。

    profile_override 在任务协程内生效 (contextvar 按任务隔离, 并行不串扰)。
    异常推 ("error", role, message), 由编排器统一收敛。
    """
    from app.services.ai_number_gate import gate_ndjson_stream
    from app.services.ai_provider import profile_override, stream_ai_text

    async def _ndjson_deltas() -> AsyncIterator[str]:
        async for chunk in stream_ai_text(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=temperature,
        ):
            yield json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    try:
        with profile_override(profile):
            full = ""
            async for line in gate_ndjson_stream(_ndjson_deltas(), pool):
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if ev.get("type") == "delta":
                    content = ev.get("content", "")
                    full += content
                    await out_q.put(("delta", role, content))
        await out_q.put(("done", role, full))
    except Exception as exc:  # noqa: BLE001 — 单角色失败交给编排器收敛
        logger.warning("[debate] 角色 %s 调用失败: %s", role, exc)
        await out_q.put(("error", role, str(exc)))


async def _judge_phase(role_profiles: dict, pool, materials_text: str,
                       bull_text: str, bear_text: str, final: bool,
                       out_q: asyncio.Queue) -> None:
    """主管阶段 (队列模式, 与辩手一致 — 增量实时转发, 不缓冲)。

    向 out_q 推送:
      ("ev", event_dict)      — role_delta / role_done 事件, 编排器直接 yield
      ("result", full, ruling) — 正常结束: 干净全文 + 裁决 (可能 None)
      ("error", msg)          — 调用失败, 编排器收敛为 error 事件

    裁决标记经 _strip_ruling_deltas 行缓冲剥离, 客户端看不到标记残片;
    数字闸门包在剥离层外侧 (先剥标记再过闸门, 同 ai_verdict 纪律 —
    否则闸门会把裁决 JSON 里的数字包进〖〗破坏 JSON)。
    """
    from app.services.ai_number_gate import gate_ndjson_stream
    from app.services.ai_provider import profile_override, stream_ai_text

    user_prompt = _judge_user_prompt(materials_text, bull_text, bear_text, final=final)
    captured: list[str] = []

    async def _ndjson_deltas() -> AsyncIterator[str]:
        async for clean in _strip_ruling_deltas(
            stream_ai_text(
                [{"role": "system", "content": _JUDGE_SYSTEM},
                 {"role": "user", "content": user_prompt}],
                temperature=0.3,  # 裁判要稳, 不要发挥
            ),
            captured,
        ):
            yield json.dumps({"type": "delta", "content": clean}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    try:
        full = ""
        with profile_override(role_profiles.get(ROLE_JUDGE)):
            async for line in gate_ndjson_stream(_ndjson_deltas(), pool):
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if ev.get("type") == "delta":
                    content = ev.get("content", "")
                    full += content
                    await out_q.put(("ev", {"type": "role_delta", "role": ROLE_JUDGE,
                                            "content": content}))

        # 解析裁决: 标记原文 (captured) 优先, 全文兜底 (剥离层已捕获, 双保险)
        ruling = None
        if captured:
            try:
                ruling = parse_ruling(
                    "\n".join(f"<!-- DEBATE_RULING: {c} -->" for c in captured))
            except Exception:  # noqa: BLE001
                ruling = None
        await out_q.put(("ev", {"type": "role_done", "role": ROLE_JUDGE,
                                "content": full, "rewritten": False, "final": final}))
        await out_q.put(("result", full, ruling))
    except Exception as exc:  # noqa: BLE001 — 失败交给编排器收敛
        logger.warning("[debate] 风控主管调用失败 (final=%s): %s", final, exc)
        await out_q.put(("error", str(exc)))


async def debate_stream(
    repo,
    data_dir: Path,
    symbol: str,
    name: str = "",
    focus: str = "",
) -> AsyncIterator[str]:
    """多空辩论主编排: yield NDJSON 事件行 (协议见模块 docstring)。

    任何阶段异常都收敛为 error 事件, 绝不让前端卡死 (同 analyze_stock_stream)。
    """
    from app.services.ai_number_whitelist import collect_whitelist

    # ---- ① 备料 ----
    try:
        materials = build_materials(repo, data_dir, symbol)
    except Exception as exc:  # noqa: BLE001
        yield json.dumps({"type": "error", "message": f"数据准备失败: {exc}"}, ensure_ascii=False)
        return
    if materials is None:
        yield json.dumps({"type": "error",
                          "message": f"标的 {symbol} 暂无日 K 数据,请先同步"}, ensure_ascii=False)
        return

    # P1-11 门控 (默认关闭只警示): 开启后 D 级硬伤拒绝辩论, 不消耗 AI token
    from app.services import ai_data_quality
    if ai_data_quality.should_block(materials.get("quality")):
        yield json.dumps({"type": "error",
                          "message": ai_data_quality.block_message(materials["quality"])},
                         ensure_ascii=False)
        return

    role_profiles = resolve_role_profiles()
    yield json.dumps({
        "type": "meta", "symbol": symbol, "name": name,
        "summary": summarize_levels(materials["levels"], materials["close"]),
        "levels": materials["levels"], "close": materials["close"],
        "quality": materials.get("quality"),  # P1-11 数据体检徽章
        "roles": {r: {"model": role_model_display(role_profiles.get(r)),
                      "custom_profile": role_profiles.get(r) is not None}
                  for r in ROLES},
    }, ensure_ascii=False)

    # 数字闸门白名单: 材料结构化数据 + 全部系统提示词 + 材料成品文本
    pool = collect_whitelist(
        (materials["kline_rows"], materials["levels"], materials["fins"]),
        texts=(_BULL_SYSTEM, _BEAR_SYSTEM, _JUDGE_SYSTEM, materials["text"]),
    )

    # ---- ② 多空并行独立分析 ----
    q: asyncio.Queue = asyncio.Queue()
    tasks = [
        asyncio.create_task(_run_role_llm(
            ROLE_BULL, _BULL_SYSTEM,
            _role_user_prompt(materials["text"], focus, ROLE_BULL),
            role_profiles.get(ROLE_BULL), pool, 0.7, q)),
        asyncio.create_task(_run_role_llm(
            ROLE_BEAR, _BEAR_SYSTEM,
            _role_user_prompt(materials["text"], focus, ROLE_BEAR),
            role_profiles.get(ROLE_BEAR), pool, 0.7, q)),
    ]
    arg_texts: dict[str, str] = {}
    failed = False
    done_count = 0
    while done_count < 2:
        kind, role, payload = await q.get()
        if kind == "delta":
            yield json.dumps({"type": "role_delta", "role": role, "content": payload},
                             ensure_ascii=False)
        elif kind == "done":
            arg_texts[role] = payload
            done_count += 1
            yield json.dumps({"type": "role_done", "role": role, "content": payload,
                              "rewritten": False}, ensure_ascii=False)
        else:  # error
            failed = True
            yield json.dumps({"type": "error",
                              "message": f"{'多方' if role == ROLE_BULL else '空方'}辩手调用失败: {payload}"},
                             ensure_ascii=False)
            break
    for t in tasks:
        t.cancel()
    if failed:
        return

    # ---- ③ 风控主管初审 (队列模式: 增量实时转发, 前端能看到主管边想边写) ----
    ruling = None
    judge_failed = False
    jq: asyncio.Queue = asyncio.Queue()
    jt = asyncio.create_task(_judge_phase(
        role_profiles, pool, materials["text"],
        arg_texts[ROLE_BULL], arg_texts[ROLE_BEAR], final=False, out_q=jq))
    while True:
        msg = await jq.get()
        if msg[0] == "ev":
            yield json.dumps(msg[1], ensure_ascii=False)
        elif msg[0] == "result":
            ruling = msg[2]
            break
        else:  # error
            judge_failed = True
            yield json.dumps({"type": "error",
                              "message": f"风控主管调用失败: {msg[1]}"}, ensure_ascii=False)
            break
    jt.cancel()
    if judge_failed:
        return
    yield _ruling_event(ruling, final=False)

    # ---- ④ 打回重写 (至多一轮) + 终审 ----
    if ruling and ruling["decision"] == "rewrite":
        target = ruling["rewrite_role"]
        yield json.dumps({"type": "rewrite", "role": target,
                          "reason": ruling["rewrite_reason"]}, ensure_ascii=False)
        rq: asyncio.Queue = asyncio.Queue()
        rt = asyncio.create_task(_run_role_llm(
            target,
            _BULL_SYSTEM if target == ROLE_BULL else _BEAR_SYSTEM,
            _rewrite_user_prompt(materials["text"], focus, target, ruling["rewrite_reason"]),
            role_profiles.get(target), pool, 0.7, rq))
        rewrite_failed = False
        while True:
            kind, role, payload = await rq.get()
            if kind == "delta":
                yield json.dumps({"type": "role_delta", "role": role, "content": payload},
                                 ensure_ascii=False)
            elif kind == "done":
                arg_texts[role] = payload
                yield json.dumps({"type": "role_done", "role": role, "content": payload,
                                  "rewritten": True}, ensure_ascii=False)
                break
            else:
                rewrite_failed = True
                yield json.dumps({"type": "error",
                                  "message": f"重写调用失败: {payload}"}, ensure_ascii=False)
                break
        rt.cancel()
        if rewrite_failed:
            return
        # 终审: 必拍板 (提示词强制 pass; 就算模型仍给 rewrite 也按生效裁决展示)
        final_ruling = None
        final_failed = False
        fq: asyncio.Queue = asyncio.Queue()
        ft = asyncio.create_task(_judge_phase(
            role_profiles, pool, materials["text"],
            arg_texts[ROLE_BULL], arg_texts[ROLE_BEAR], final=True, out_q=fq))
        while True:
            msg = await fq.get()
            if msg[0] == "ev":
                yield json.dumps(msg[1], ensure_ascii=False)
            elif msg[0] == "result":
                final_ruling = msg[2]
                break
            else:  # error
                final_failed = True
                yield json.dumps({"type": "error",
                                  "message": f"终审调用失败: {msg[1]}"}, ensure_ascii=False)
                break
        ft.cancel()
        if final_failed:
            return
        yield _ruling_event(final_ruling, final=True)

    yield json.dumps({"type": "done"}, ensure_ascii=False)


def _ruling_event(ruling: dict | None, *, final: bool) -> str:
    """裁决 NDJSON 事件。解析失败 → ok=false (fail-honest, 前端明示不猜)。"""
    if ruling is None:
        return json.dumps({"type": "ruling", "ok": False, "final": final}, ensure_ascii=False)
    return json.dumps({
        "type": "ruling", "ok": True, "final": final,
        "decision": ruling["decision"],
        "verdict": {"direction": ruling["direction"], "reason": ruling["reason"]},
    }, ensure_ascii=False)
