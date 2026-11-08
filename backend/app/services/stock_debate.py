"""多空辩论 (P1-9) — 让几个 AI 开会辩论一只股票。

对标 jcp / TradingAgents-AShare / Vibe-Research 的多智能体辩论, 但刻意做成
轻量按需版 (QuantMind 曾做成常驻重型服务, 维护成本过高已于 2026-10 拆掉):
无常驻进程、无调度器, 用户点一次跑一次, 全程在一次 SSE 请求生命周期内。

## 流程

  ① 备料: 复用 stock_analyzer 的数据包 (日K+指标 / 关键价位 / 财务摘要),
     三方拿同一份材料, 数字闸门白名单也从这份材料采集。
  ② 多方 & 空方 **并行** 独立分析 (asyncio 并发, 首轮互相看不到对方输出 —
     两个独立 LLM 会话, 不存在共享上下文)。系统提示词强制立场。
     每方文末附机读主张清单 CLAIMS (三至五条最强论点) → 汇成主张账本,
     清单内嵌在辩手输出里, 零额外 LLM 调用。
  ③ 风控主管审阅材料 + 双方全文 + 主张账本, 逐条核对主张后输出机读裁决块
     DEBATE_RULING: 通过(pass) 或 打回(rewrite, 指定打回哪一方 + 理由) + 答题卡研判。
  ④ 打回 → 被批评方带主管意见 + **对方主张清单** 重写一轮 (须逐条正面回应
     对方主张) → 主管终审 (终审必拍板)。
     硬上限 5 次 LLM 调用: 多、空、裁决、重写×1、终审。

## 角色模型分配 (调研报告特别提示)

  "全用一家的模型, 它容易自己同意自己" — 三个角色可各自绑定一个已配置的
  模型档案 (preferences.debate_role_profiles), 调用时套 profile_override
  (contextvar, 并行任务互不串扰, 不落盘不改激活档案)。未绑定 → 激活档案。

## 协议 (NDJSON, 事件带 role 字段路由到前端双栏)

  {"type":"meta","symbol","name","summary","levels","close","roles":{...}}
  {"type":"role_delta","role":"bull|bear|judge","content":"..."}
  {"type":"claims","role":"bull|bear","claims":["..."]}  主张账本 (解析失败不发, 流程不断)
  {"type":"role_done","role":"...","content":"该角色全文(干净)","rewritten":bool}
  {"type":"rewrite","role":"...","reason":"主管打回理由"}
  {"type":"role_error","role":"bull|bear","message":"..."}  单方辩手失败 (非致命, 降级继续)
  {"type":"ruling","ok":true,"decision":"pass|rewrite","verdict":{"direction","reason"},"final":bool}
  {"type":"ruling","ok":false}          裁决块解析失败 (fail-honest, 不猜)
  {"type":"error","message":"..."} / {"type":"done"}

## 单方失败降级 (2026-11)

  一方辩手调用失败不再阻断整场: 已流出的部分论点保留 (role_done 收尾 + 主管
  提示词注明截断), 完全无输出的一方以缺席占位进入主管裁决; role_error 事件
  供前端挂警示横幅 (非致命)。仅当双方都无可用论点时才 error 终止。

## fail-honest 纪律 (与 ai_verdict 一致)

  裁决块缺失/JSON 损坏/枚举外 → ruling ok=false, 前端明示"未解析出裁决",
  绝不从主管正文里猜结论。打回解析不出目标角色 → 视为通过 (宁可少重写,
  不可错重写)。主张清单缺失/损坏 → 该方不进账本 (claims 事件不发), 辩论
  照常 — 账本是碰撞增强件, 不是流程门控。
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
# 用汉字而非阿拉伯数字表述字数上限 — 系统提示词会进数字闸门白名单, 阿拉伯数字会留假放行口子。

# ================================================================
# 系统提示词 (刻意不含阿拉伯数字示例 — 会进数字闸门白名单, 留假放行口子;
# 长度上限用汉字表述, 同 VERDICT_PROMPT 纪律)
# ================================================================

_BULL_SYSTEM = """你是一场多空辩论中的【多方辩手】。你的唯一任务是论证看多该标的。

纪律:
一、只能使用用户提供的材料中的数据, 禁止编造任何数字、新闻或公告。
二、立场鲜明: 只讲看多的理由 (上涨逻辑、支撑因素、催化剂、估值优势), 不允许骑墙、不允许"但也存在风险"式的和稀泥 — 风险提示是风控主管的事。
三、每个论点都要落到材料里的具体数据 (价位、指标、财务), 空喊口号视为无效论证。
四、全文控制在一千二百字以内, 分条陈述, 结尾用一句话给出多方核心结论。
五、不输出具体买卖操作指令 (点位目标可以谈, "建议买入/卖出"不可以)。
六、核心结论之后, 在全文最末另起一行输出机读主张清单, 格式固定:
<!-- CLAIMS: ["主张一", "主张二", "主张三"] -->
   - 三至五条, 每条一句话且必须落到材料中的具体数据, 选你最有底气、最希望空方正面回应的论点
   - 该清单是给程序读的, 不要在正文里解释、引用或复述它"""

_BEAR_SYSTEM = """你是一场多空辩论中的【空方辩手】。你的唯一任务是论证看空该标的。

纪律:
一、只能使用用户提供的材料中的数据, 禁止编造任何数字、新闻或公告。
二、立场鲜明: 只讲看空的理由 (下跌风险、压力因素、利空信号、估值泡沫), 不允许骑墙、不允许"但也有积极因素"式的和稀泥 — 亮点陈述是多方辩手的事。
三、每个论点都要落到材料里的具体数据 (价位、指标、财务), 空喊口号视为无效论证。
四、全文控制在一千二百字以内, 分条陈述, 结尾用一句话给出空方核心结论。
五、不输出具体买卖操作指令 (点位风险可以谈, "建议买入/卖出"不可以)。
六、核心结论之后, 在全文最末另起一行输出机读主张清单, 格式固定:
<!-- CLAIMS: ["主张一", "主张二", "主张三"] -->
   - 三至五条, 每条一句话且必须落到材料中的具体数据, 选你最有底气、最希望多方正面回应的论点
   - 该清单是给程序读的, 不要在正文里解释、引用或复述它"""

_JUDGE_SYSTEM = """你是一场多空辩论的【风控主管】。双方辩手已就同一份材料各自陈述, 你负责审阅并拍板。

你的输出分三部分, 依次进行:
一、逐方点评: 用户消息附有双方文末登记的主张账本 (机读清单) 时, 对照账本逐条
   核对 — 哪些主张有材料数据支撑、哪些被对方数据反驳、哪些空喊口号无据; 并指出
   论证中的其他硬伤 — 引用材料数据错误、逻辑跳跃、以偏概全。哪一方论证质量高就
   说高, 不各打五十大板。打回时 rewrite_reason 尽量点名具体主张。
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

# 辩手主张清单标记 (主张账本): JSON 字符串数组, 解析纪律同裁决块 — fail-honest
CLAIMS_RE = re.compile(r"<!--\s*CLAIMS:\s*(\[.*?\])\s*-->", re.DOTALL)

# 机读裁决块的标题行是普通正文 (标记行本身已被剥离), 剥完后主管栏末尾会
# 悬一空标题 → 展示/落库前裁掉这类行尾标题 (容忍尾部空行)。
_DANGLING_RULING_HEADING_RE = re.compile(
    r"\n*[一二三四五六七八九十0-9]+、[^\n]*机读裁决块[^\n]*\s*$")

_REASON_MAX = 60          # 研判理由展示截断 (同 ai_verdict)
_REWRITE_REASON_MAX = 120  # 打回理由截断 (要转给重写方, 放宽些)
_CLAIM_MAX = 80           # 单条主张展示截断 (账本注入主管/对方重写提示词, 防刷屏)
_CLAIMS_MAX_N = 6          # 每方主张条数上限 (提示词要求三至五, 放宽一格兜底)


def parse_claims(text: str) -> list[str] | None:
    """从辩手全文解析主张清单 → 合法 list[str] 或 None (缺失/损坏一律 None)。

    多处标记取最后一处 (模型复述示例时以末尾为准, 同 parse_ruling)。
    """
    last: re.Match | None = None
    for m in CLAIMS_RE.finditer(text):
        last = m
    if last is None:
        return None
    return _claims_payload_to_list(last.group(1))


def _claims_payload_to_list(payload: str) -> list[str] | None:
    """CLAIMS 标记内的 JSON 数组原文 → 清洗后的主张列表 (空/损坏 → None)。"""
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[debate] 主张清单 JSON 损坏: %s", payload[:120])
        return None
    if not isinstance(obj, list):
        return None
    claims: list[str] = []
    for item in obj:
        if not isinstance(item, str):
            continue  # 混入非字符串项只丢该项, 不废整份清单
        s = item.strip()
        if not s:
            continue
        if len(s) > _CLAIM_MAX:
            s = s[:_CLAIM_MAX] + "…"
        claims.append(s)
    return claims[:_CLAIMS_MAX_N] or None


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


async def _strip_marker_deltas(chunks: AsyncIterator[str], captured: list[str],
                               pattern: re.Pattern) -> AsyncIterator[str]:
    """流式剥离机读标记 (裁决块/主张清单): 行缓冲, 残行不下发 (标记可能跨 chunk 被切开)。

    与 ai_verdict.verdict_ndjson_stream 同款纪律: 客户端永远看不到标记残片。
    捕获的标记内 JSON 原文追加进 captured, 供流结束后 parse。
    """
    buf = ""

    def _strip(segment: str) -> str:
        for m in pattern.finditer(segment):
            captured.append(m.group(1))
        return pattern.sub("", segment)

    try:
        async for chunk in chunks:
            buf += chunk
            # 只发完整行, 残行留在 buf
            while (nl := buf.find("\n")) >= 0:
                line = buf[: nl + 1]
                buf = buf[nl + 1 :]
                had_marker = bool(pattern.search(line))
                line = _strip(line)
                if had_marker and not line.strip():
                    continue  # 标记独占一行 → 整行丢弃, 不留尾部空行
                if line:
                    yield line
    except (GeneratorExit, asyncio.CancelledError):
        raise  # 关闭/取消路径不得再产出
    except BaseException:
        # 中途异常 (如输出长度上限截断): 先冲刷余量再抛 —
        # 编排器降级要靠这段残文续辩, 正文尾巴不能吞
        if buf:
            out = _strip(buf)
            if out:
                yield out
        raise
    # 流结束: 冲刷余量 (标记缺失时正文尾巴不能吞)
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


def _format_ledger(ledger: dict[str, list[str]]) -> str:
    """主张账本 → 主管用户消息附段。双方清单都缺失时返回空串 (不注入空段)。"""
    sections = []
    for role, label in ((ROLE_BULL, "多方"), (ROLE_BEAR, "空方")):
        claims = ledger.get(role)
        if claims:
            lines = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1))
            sections.append(f"{label}主张:\n{lines}")
    if not sections:
        return ""
    return "\n\n===== 主张账本 (辩手文末登记的机读清单) =====\n" + "\n\n".join(sections)


def _judge_user_prompt(materials_text: str, bull_text: str, bear_text: str,
                       final: bool = False, ledger: dict[str, list[str]] | None = None) -> str:
    """主管用户消息: 材料 + 双方全文 + 主张账本 (+ 终审标记)。"""
    parts = [
        materials_text,
        "\n\n===== 多方辩手陈述 =====\n" + bull_text,
        "\n\n===== 空方辩手陈述 =====\n" + bear_text,
    ]
    ledger_text = _format_ledger(ledger or {})
    if ledger_text:
        parts.append(ledger_text)
    parts.append("\n请按系统提示词要求: 先逐方点评, 再综合裁决, 最后输出机读裁决块。")
    if final:
        parts.append(_FINAL_NOTE)
    return "\n".join(parts)


def _rewrite_user_prompt(materials_text: str, focus: str, role: str, critique: str,
                         opponent_claims: list[str] | None = None) -> str:
    """重写方用户消息: 原材料 + 主管打回意见 + 对方主张清单 (立场不变, 只修论证质量)。

    对方主张注入是碰撞的关键一环: 首轮多空并行互相看不到对方, 重写轮是被打回方
    唯一能正面回应对方论点的机会 — 要求逐条回应, 不得回避。
    """
    base = _role_user_prompt(materials_text, focus, role)
    extra = (
        f"\n\n【打回重写】风控主管对你的上一版陈述不满意, 打回理由如下:\n{critique}\n"
        "请针对打回理由重写你的完整陈述 (立场不变, 仍是"
        + ("看多论证" if role == ROLE_BULL else "看空论证")
        + "), 不要提及打回过程本身。"
    )
    if opponent_claims:
        label = "空方" if role == ROLE_BULL else "多方"
        lines = "\n".join(f"{i}. {c}" for i, c in enumerate(opponent_claims, 1))
        extra += (
            f"\n\n【对方主张清单】以下是{label}辩手登记的核心主张, 重写时须逐条正面回应"
            f" — 用材料数据反驳, 或指出其不成立之处; 回应不了的不得假装没看见:\n{lines}"
        )
    return base + extra


# ================================================================
# 编排器
# ================================================================

async def _run_role_llm(role: str, system_prompt: str, user_prompt: str,
                        profile: dict | None, pool, temperature: float,
                        out_q: asyncio.Queue) -> None:
    """跑一个辩手角色: 流式产出剥主张标记、经数字闸门过滤后推入队列。

    profile_override 在任务协程内生效 (contextvar 按任务隔离, 并行不串扰)。
    主张清单标记经 _strip_marker_deltas 行缓冲剥离 (客户端看不到残片),
    剥离层在闸门内侧 — 先剥标记再过闸门, 否则闸门会把清单 JSON 里的数字
    包进〖〗破坏 JSON (同 _judge_phase 裁决块纪律)。
    正常结束推 ("done", role, 干净全文, claims|None);
    异常推 ("error", role, message, 已流出部分论点), 由编排器降级收敛 (单方失败不阻断)。
    """
    from app.services.ai_number_gate import gate_ndjson_stream
    from app.services.ai_provider import profile_override, stream_ai_text

    captured: list[str] = []
    full = ""  # 先绑定: except 分支要带部分论点, 任何位置抛异常都不能 NameError

    async def _ndjson_deltas() -> AsyncIterator[str]:
        async for clean in _strip_marker_deltas(
            stream_ai_text(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=temperature,
                # 不限制输出: 字数上限提高后 3000 token 钳制不够 (推理模型思考
                # token 也计入预算); 服务端自有上限的模型(如免费额度)由降级兜底。
                max_tokens=None,
            ),
            captured, CLAIMS_RE,
        ):
            yield json.dumps({"type": "delta", "content": clean}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    try:
        with profile_override(profile):
            async for line in gate_ndjson_stream(_ndjson_deltas(), pool):
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if ev.get("type") == "delta":
                    content = ev.get("content", "")
                    full += content
                    await out_q.put(("delta", role, content))
        # 多处标记取最后一处 (同 parse_claims 语义); 解析失败 → None, 不进账本
        claims = _claims_payload_to_list(captured[-1]) if captured else None
        await out_q.put(("done", role, full, claims))
    except Exception as exc:  # noqa: BLE001 — 单角色失败交给编排器收敛
        logger.warning("[debate] 角色 %s 调用失败: %s", role, exc)
        # 带上已流出的部分论点 (可能为空): 编排器据此降级续辩而非整场阻断
        await out_q.put(("error", role, str(exc), full))


async def _judge_phase(role_profiles: dict, pool, materials_text: str,
                       bull_text: str, bear_text: str, final: bool,
                       out_q: asyncio.Queue,
                       ledger: dict[str, list[str]] | None = None) -> None:
    """主管阶段 (队列模式, 与辩手一致 — 增量实时转发, 不缓冲)。

    向 out_q 推送:
      ("ev", event_dict)      — role_delta / role_done 事件, 编排器直接 yield
      ("result", full, ruling) — 正常结束: 干净全文 + 裁决 (可能 None)
      ("error", msg)          — 调用失败, 编排器收敛为 error 事件

    裁决标记经 _strip_marker_deltas 行缓冲剥离, 客户端看不到标记残片;
    数字闸门包在剥离层外侧 (先剥标记再过闸门, 同 ai_verdict 纪律 —
    否则闸门会把裁决 JSON 里的数字包进〖〗破坏 JSON)。
    """
    from app.services.ai_number_gate import gate_ndjson_stream
    from app.services.ai_provider import profile_override, stream_ai_text

    user_prompt = _judge_user_prompt(materials_text, bull_text, bear_text,
                                     final=final, ledger=ledger)
    captured: list[str] = []

    async def _ndjson_deltas() -> AsyncIterator[str]:
        async for clean in _strip_marker_deltas(
            stream_ai_text(
                [{"role": "system", "content": _JUDGE_SYSTEM},
                 {"role": "user", "content": user_prompt}],
                temperature=0.3,  # 裁判要稳, 不要发挥
                # 同辩手: 主管要引用双方全文, 输出预算不钳制 (服务端上限兜底)
                max_tokens=None,
            ),
            captured, RULING_RE,
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

        # 机读块标记已剥离, 标题行悬在末尾 → 裁掉 (流式增量已发的部分由
        # 前端 role_done 全文覆盖收尾; 落库报告同享干净文本)
        full = _DANGLING_RULING_HEADING_RE.sub("", full)

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
    # P2-23 不可篡改账本: 辩论三方看同一份材料, 登记一次即可 (指纹+快照)
    from app.services import ai_audit
    audit_id = ai_audit.begin_run(data_dir, "debate", symbol)
    kline_rows = materials.get("kline_rows") or []
    audit_inputs = [ai_audit.add_input(
        data_dir, audit_id, "kline", kline_rows,
        as_of=str(kline_rows[-1]["date"]) if kline_rows else "")]
    fins = materials.get("fins") or {}
    if any(fins.values()):
        audit_inputs.append(ai_audit.add_input(data_dir, audit_id, "financials", fins))
    if materials.get("quality"):
        audit_inputs.append(ai_audit.add_input(data_dir, audit_id, "quality", materials["quality"]))

    # P1-15: roles 已带各角色模型显示名; 顶层再补当前激活配置归属快照,
    # 前端保存辩论记录时随报告落盘 (未配角色档案时角色用的就是这套配置)。
    from app.services.ai_provider import current_attribution
    attr = current_attribution()
    yield json.dumps({
        "type": "meta", "symbol": symbol, "name": name,
        "summary": summarize_levels(materials["levels"], materials["close"]),
        "levels": materials["levels"], "close": materials["close"],
        "quality": materials.get("quality"),  # P1-11 数据体检徽章
        "roles": {r: {"model": role_model_display(role_profiles.get(r)),
                      "custom_profile": role_profiles.get(r) is not None}
                  for r in ROLES},
        **attr,
        # P2-23: 账本 run id (前端保存辩论记录时带回绑定 report_id)
        "audit_id": audit_id,
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
    ledger: dict[str, list[str]] = {}  # 主张账本: role → 机读主张清单 (解析失败方缺席)
    role_errors: dict[str, str] = {}
    done_count = 0
    while done_count < 2:
        msg = await q.get()
        kind, role = msg[0], msg[1]
        if kind == "delta":
            yield json.dumps({"type": "role_delta", "role": role, "content": msg[2]},
                             ensure_ascii=False)
        elif kind == "done":
            arg_texts[role] = msg[2]
            if msg[3]:
                ledger[role] = msg[3]
                yield json.dumps({"type": "claims", "role": role, "claims": msg[3]},
                                 ensure_ascii=False)
            done_count += 1
            yield json.dumps({"type": "role_done", "role": role, "content": msg[2],
                              "rewritten": False}, ensure_ascii=False)
        else:  # error: 单方失败降级续辩 (role_error 非致命), 不再整场阻断
            done_count += 1
            role_errors[role] = msg[2]
            partial = msg[3] if len(msg) > 3 else ""
            label = "多方" if role == ROLE_BULL else "空方"
            if partial:
                # 已流出的内容不浪费: role_done 收尾卡片, 主管提示词注明截断
                arg_texts[role] = partial + "\n\n(上方论点因调用异常被截断, 内容可能不完整。)"
                yield json.dumps({"type": "role_done", "role": role, "content": partial,
                                  "rewritten": False}, ensure_ascii=False)
                note = f"{label}辩手输出异常: {msg[2]}; 已用截断前内容继续辩论"
            else:
                note = f"{label}辩手调用失败: {msg[2]}; 该方论点缺席, 辩论降级继续"
            yield json.dumps({"type": "role_error", "role": role, "message": note},
                             ensure_ascii=False)
    for t in tasks:
        t.cancel()
    missing = [r for r in (ROLE_BULL, ROLE_BEAR) if r not in arg_texts]
    if len(missing) == 2:  # 双方都无可用论点才终止
        yield json.dumps({"type": "error", "message": "多空双方辩手均调用失败: " +
                          "; ".join(f"{r}: {role_errors.get(r)}" for r in missing)},
                         ensure_ascii=False)
        return
    for r in missing:
        arg_texts[r] = (f"(本方辩手本轮调用失败: {role_errors.get(r, '未知错误')}; "
                        "未提出论点。请主管基于单方论点与材料裁决, 并在点评中说明缺席情况。)")

    # ---- ③ 风控主管初审 (队列模式: 增量实时转发, 前端能看到主管边想边写) ----
    ruling = None
    judge_failed = False
    judge_text = ""
    jq: asyncio.Queue = asyncio.Queue()
    jt = asyncio.create_task(_judge_phase(
        role_profiles, pool, materials["text"],
        arg_texts[ROLE_BULL], arg_texts[ROLE_BEAR], final=False,
        out_q=jq, ledger=ledger))
    while True:
        msg = await jq.get()
        if msg[0] == "ev":
            yield json.dumps(msg[1], ensure_ascii=False)
        elif msg[0] == "result":
            judge_text = msg[1]
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
        opponent = ROLE_BEAR if target == ROLE_BULL else ROLE_BULL
        yield json.dumps({"type": "rewrite", "role": target,
                          "reason": ruling["rewrite_reason"]}, ensure_ascii=False)
        rq: asyncio.Queue = asyncio.Queue()
        rt = asyncio.create_task(_run_role_llm(
            target,
            _BULL_SYSTEM if target == ROLE_BULL else _BEAR_SYSTEM,
            _rewrite_user_prompt(materials["text"], focus, target, ruling["rewrite_reason"],
                                 opponent_claims=ledger.get(opponent)),
            role_profiles.get(target), pool, 0.7, rq))
        rewrite_failed = False
        while True:
            msg = await rq.get()
            kind, role = msg[0], msg[1]
            if kind == "delta":
                yield json.dumps({"type": "role_delta", "role": role, "content": msg[2]},
                                 ensure_ascii=False)
            elif kind == "done":
                arg_texts[role] = msg[2]
                if msg[3]:
                    ledger[role] = msg[3]  # 重写版主张替换旧账 (终审看最新清单)
                    yield json.dumps({"type": "claims", "role": role, "claims": msg[3]},
                                     ensure_ascii=False)
                yield json.dumps({"type": "role_done", "role": role, "content": msg[2],
                                  "rewritten": True}, ensure_ascii=False)
                break
            else:
                rewrite_failed = True
                yield json.dumps({"type": "error",
                                  "message": f"重写调用失败: {msg[2]}"}, ensure_ascii=False)
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
            arg_texts[ROLE_BULL], arg_texts[ROLE_BEAR], final=True,
            out_q=fq, ledger=ledger))
        while True:
            msg = await fq.get()
            if msg[0] == "ev":
                yield json.dumps(msg[1], ensure_ascii=False)
            elif msg[0] == "result":
                judge_text = msg[1]
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

    # P2-23: 产出哈希 = 三方最终全文 (前端落库口径同源), prompt 哈希含三系统提示+材料
    ai_audit.finish_run(
        data_dir, audit_id=audit_id, kind="debate", symbol=symbol,
        inputs=audit_inputs,
        prompt_text="\n\n".join([_BULL_SYSTEM, _BEAR_SYSTEM, _JUDGE_SYSTEM,
                                 materials["text"], focus]),
        output_text="\n\n".join([arg_texts.get(ROLE_BULL, ""), arg_texts.get(ROLE_BEAR, ""),
                                 judge_text]),
        model=attr.get("model", ""), provider=attr.get("provider", ""),
        profile_id=attr.get("profile_id", ""),
    )
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
