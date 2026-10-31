"""AI 个股分析服务 — 技术面 / 基本面 / 财务面 / 消息面 四维综合分析。

职责:
  组合一只股票的 K 线(含已算好的技术指标)+ 财务表 + 关键价位 →
  拼装客观技术分析系统提示词 → 流式调用 LLM → 逐 chunk 吐给前端。

与 financial_analyzer.py 的区别(刻意区分,非复用):
  - 角色:客观技术分析师(非 CFA 财务分析师)
  - 数据源:K 线 + 技术指标为主,财务表为辅(财务分析以财务表为主)
  - 输出框架:技术面→基本面→财务面→消息面(四维),落点是客观技术状态与风险提示
    (财务分析的落点是财务质量评级)。注意:本服务不输出买卖建议、操作指令。

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import polars as pl

from app.indicators.levels import compute_levels, summarize_levels
from app.market_time import cn_today
from app.services.financial_sync import get_financial_df

logger = logging.getLogger(__name__)

# 注入最近多少根日 K(技术面分析样本)
_KLINE_WINDOW = 90
# 注入财务表的最近期数
_MAX_PERIODS = 4


# ================================================================
# 数据加载
# ================================================================

def _load_kline(repo, symbol: str) -> pl.DataFrame:
    """读取该标的最近 N 根日 K(已含技术指标 / 信号)。

    repo: KlineRepository;走内存缓存,性能可控。
    """
    end = cn_today()
    start = end - timedelta(days=_KLINE_WINDOW * 2)  # 多取一些保证交易日够
    # 按资产类型分流: ETF/指数走独立 enriched 存储 (无财务数据, 提示词已有兜底)
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return df
    return df.tail(_KLINE_WINDOW)


def _clean_rows(df: pl.DataFrame, keep_cols: list[str]) -> list[dict]:
    """把 DataFrame 转成 JSON 安全的 dict 列表(只保留关键列 + 清洗 NaN/Inf + date→字符串)。

    polars 的 date 列会变成 Python datetime.date,json.dumps 无法直接序列化,
    必须转成 ISO 字符串,否则 json.dumps 抛 TypeError 让整个流静默失败。
    """
    import datetime
    import math
    cols = [c for c in keep_cols if c in df.columns]
    sub = df.select(cols)
    rows = []
    for rec in sub.to_dicts():
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float):
                clean[k] = None if not math.isfinite(v) else round(v, 4)
            elif isinstance(v, (datetime.date, datetime.datetime)):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        rows.append(clean)
    return rows


def _load_financials(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """读取该标的核心财务指标 + 利润表(只取最有信息量的两张表)。

    财务面只需要关键指标(ROE / 增速 / 毛利率 等),不需要把 4 张表全塞进上下文
    (那是 financial_analyzer 的职责)。这里取轻量,留给技术面更多 token。
    """
    out: dict[str, list[dict]] = {}
    for table in ("metrics", "income"):
        df = get_financial_df(data_dir, table)
        if df.is_empty():
            out[table] = []
            continue
        df = df.filter(pl.col("symbol") == symbol)
        if df.is_empty():
            out[table] = []
            continue
        if "period_end" in df.columns:
            df = df.sort("period_end", descending=True).head(2)  # 只取最近 2 期
        import math
        rows = []
        for rec in df.to_dicts():
            clean = {}
            for k, v in rec.items():
                if k == "symbol":
                    continue
                if isinstance(v, float):
                    clean[k] = None if not math.isfinite(v) else v
                else:
                    clean[k] = v
            rows.append(clean)
        out[table] = rows
    return out


# ================================================================
# 系统提示词 —— 客观技术分析四维框架(与财务分析明确区分)
# 重要原则:只描述"指标/价位处于什么客观状态",不输出"应该怎么操作"。
# 本报告定位为客观行情分析,不提供任何买卖建议、仓位建议或交易指令。
# ================================================================

_SYSTEM_PROMPT_HEAD = """你是一位拥有 15 年 A 股一线研究经验的技术分析分析师,擅长从 K 线、量价、关键价位与基本面交叉验证中客观解读个股的技术状态。你的任务是:基于提供的个股数据,产出一份**客观、中立、不包含任何买卖或操作建议**的技术分析报告。

## 核心红线(务必遵守)

- **绝对不输出**"买入/卖出/加仓/减仓/观望/轻仓/重仓/止损/止盈/建议买入区间/操作建议"等任何交易指令或倾向性措辞
- **绝对不**按"激进型/稳健型/保守型"给用户分别的操作建议
- 你的角色是**客观陈述**该股当前的技术状态、价位结构、量价特征与潜在风险,让读者自行判断
- 换成"一个中立财经记者能不能写出来"——能写就保留,不能写就删除

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构。不要输出任何 JSON 或代码块,直接输出 Markdown 正文。

### 1. 🎯 一句话定调(1-2 句)
用一句话概括该股当前的**技术状态**(如"近期高位放量滞涨,量能持续性存疑"/"价格在 60 日均线上方运行,均线呈多头排列")。结尾用【当前状态:企稳 / 反弹 / 震荡 / 调整 / 走弱】客观描述技术形态,**不评价好坏、不下操作结论**。

### 2. 📈 技术面分析(核心维度)
这是你的主战场,务必深入,只陈述客观事实:
- **趋势判断**:均线多头/空头排列、20/60 日均线方向、价格在均线之上/下
- **形态结构**:近期是否出现突破/破位/双底/双顶/旗形等关键形态
- **指标信号**:MACD 金叉/死叉/背离、KDJ 超买超卖、RSI 强弱、布林通道位置
- **量价配合**:放量上涨/缩量回调/量价背离/换手率异动
每条结论必须引用具体数值(如"MACD 在 6/12 出现金叉,DIF 0.32 上穿 DEA 0.18"),客观陈述,不下买卖定性。

### 3. 💰 关键价位(客观价位结构)
基于提供的关键价位数据,客观列出价位结构:
- **上方压力位**(逐档列出,标注来源):第一压力、第二压力
- **下方支撑位**(逐档列出,标注来源):第一支撑、第二支撑
- 用数据说话,引用提供的压力/支撑(成交密集区)/枢轴点数值
- **注意:只客观列出价位及其技术含义(如"此处为前期成交密集区"),不输出"建议买入区间""止损位""止盈位"等操作指令**

### 4. 🏭 基本面与财务面(辅助验证)
简要点评(2-4 句,不展开长篇):
- 盈利质量(ROE / 毛利率水平)、成长性(营收/利润增速)的客观水平
- 与技术面的**客观对照**:好公司 + 技术面走坏 → 客观陈述两者背离;差公司 + 技术面强势 → 客观提示炒作可能性
- 不下"逢低吸纳""规避"等结论

**当用户消息中标注了"该标的暂无财务数据"时**,本节请输出:
> 📌 财务面分析能力正在接入中。当前未同步该标的的财务报表,基本面维度暂无法评估。
> 技术面分析不依赖财务数据,以下结论依然有效;待财务数据同步后可补充本维度。

**当用户消息中标注了该标的为 ETF 时**,本节请输出:
> 📌 该标的为场内 ETF,无上市公司财务报表,基本面/财务面不适用公司盈利质量框架。
> 请仅基于价量与关键价位做技术分析,不要编造 ROE/营收等数字,也不要写成「财务接入中」。

**绝对不要**在无数据时编造 ROE / 增速等数字。

"""

# 第 5 节双变体: 联网可用时要求「来源+日期+[消息]/[推断] 双标签+价量交叉验证」,
# 不可用时维持价量异动推断。由 ai_provider.web_search_supported() 在调用时选择,
# 避免模型拿到与实际能力矛盾的指令 (如声称有搜索却检索不到)。
_NEWS_SECTION_SEARCH = """### 5. 📰 消息面(联网搜索)
你已启用联网搜索,可检索该股相关新闻/公告。规则:
- **检索范围**:围绕该股搜索以下类别——①经营/订单(业绩预告、中标大单、并购重组、扩产投产) ②风险事件(诉讼仲裁、处罚/问询函/立案、业绩暴雷) ③股东行为(增减持、股权质押、回购、股权激励) ④资本运作(定增、可转债、分拆) ⑤与公司主营直接相关的行业动态(产业政策、产品涨价/竞品停产、上下游价格传导)。宏观消息仅在直接影响该股时才写,不泛泛而谈
- **只陈述事实**:写明消息内容、来源与日期(如"据 9/12 公司公告…");未检索到相关消息时直说"未检索到相关消息",绝不编造
- **区分标注**:确证消息标 [消息],价量异动推断标 [推断],两者不得混写
- **交叉验证**:将消息与近期 K 线对照(如某日放量涨停与某利好互相印证);消息偏正面但股价走弱时,客观指出背离,不站队
- **时效性**:优先采用近 30 天内消息;更早的注明时间,提示影响可能已消化
- 若无明显异动且未检索到相关消息,直说"近期价量平稳,无明显异动信号;未检索到相关消息"
- 核心红线不变:只陈述消息及其客观含义,不输出"利好兑现/逢低吸纳"等定性

"""

_NEWS_SECTION_OFFLINE = """### 5. 📰 消息面(价量异动推断)
**注意:当前模型未启用联网搜索,无直接新闻数据输入。** 请基于 K 线的**异动信号**进行客观推断(如:
- 涨停/连板/炸板 → 可能存在利好或资金关注
- 放量暴跌 → 可能存在未公开扰动
- 突破放量 → 可能存在催化剂
明确标注"[推断]",告诉读者这是基于价量的客观推测。若无明显异动,直说"近期价量平稳,无明显异动信号"。

"""

_SYSTEM_PROMPT_TAIL = """### 6. ⚖️ 综合研判与风险提示
2-3 段,只做客观描述,不下操作结论:
- 客观描述该股当前所处的技术阶段(底部企稳 / 上升途中 / 高位震荡 / 下跌趋势)
- 客观评估当前价位的"空间不对称性"(距上方压力位与下方支撑位的距离),不评价好坏
- 客观列出后续值得关注的量价信号(如量能能否维持、某均线得失、是否放量突破压力位),**不附任何操作结论**

## 分析准则(务必遵守)

1. **技术面优先**:技术面和量价是主要分析对象,基本面是交叉验证手段,主次分明
2. **数据说话**:每个判断引用具体数值,严禁空泛套话("走势良好"必须改成"连续 3 日站稳 20 日均线且放量")
3. **客观中立**:看多就客观陈述多头特征,看空就客观陈述空头特征,不下"该买/该卖"结论;数据不支持时直言无法判断
4. **价位精确**:压力位/支撑位必须落到具体价格,基于提供的关键价位数据陈述
5. **不输出操作指令**:不写"买入/卖出/止损/加仓/减仓/仓位建议/操作建议"等任何交易指令;提示潜在风险但不下操作结论
6. **简明客观**:用读者能扫读的密度输出,总字数 1000-1800 字,重在客观信息密度

## 重要免责
报告末尾附一行:"> ⚠️ 本内容由 AI 基于公开行情与财务数据生成,仅客观陈述技术状态,不构成任何投资建议或买卖指令。交易有风险,入市需谨慎。"

现在请基于下方数据进行分析。"""


def _build_system_prompt(web_search: bool) -> str:
    """按是否启用联网搜索拼装系统提示词(第 5 节双变体)。

    web_search 取值须与实际请求参数一致(ai_provider.web_search_supported),
    否则提示词声称的能力与模型真实能力矛盾, 会诱发编造"搜索结果"。
    """
    section = _NEWS_SECTION_SEARCH if web_search else _NEWS_SECTION_OFFLINE
    # 数字出处纪律 (ai_number_gate 配套提示词): 让模型主动写出算式, 显著降低打码率
    # 研判答题卡 (ai_verdict 配套提示词): 结论以机读标记输出, 程序直接读、不靠猜
    from app.services.ai_number_gate import NUMBER_RULE_PROMPT
    from app.services.ai_verdict import VERDICT_PROMPT
    return _SYSTEM_PROMPT_HEAD + section + _SYSTEM_PROMPT_TAIL + NUMBER_RULE_PROMPT + VERDICT_PROMPT


# 兼容旧引用/测试: 默认无联网变体
_SYSTEM_PROMPT = _build_system_prompt(False)


# ================================================================
# 用户消息构建
# ================================================================

def _paper_position_part(positions: list[dict], close: float | None, raw_close: float | None) -> str:
    """虚拟持仓上下文 (V3): 模拟盘持有该标的时注入提示词, 让 AI 结合仓位给建议。

    positions 为多账户列表 [{account_id, qty, avg_cost}, ...] (V2 账户隔离),
    只收 qty>0 的账户, 逐账户一行; 全空则返回空串 (不注入"空仓"之类误导性表述)。
    浮盈用 raw 口径: 持仓 avg_cost 是不复权 raw 成本, 与前复权 close 混算会在
    除权后产生虚假盈亏, 故优先用 raw_close (缺列时退化用 close 并接受口径误差)。
    """
    lines: list[str] = []
    for item in positions or []:
        qty = item.get("qty") or 0
        avg_cost = item.get("avg_cost") or 0
        if qty <= 0 or avg_cost <= 0:
            continue
        line = f"账户 {item.get('account_id') or 'default'}: 持仓 {qty} 股, 平均成本 {avg_cost:.3f} 元 (不复权)"
        ref = raw_close if (raw_close is not None and raw_close > 0) else close
        if ref is not None and ref > 0:
            line += f", 现价 {ref:.2f} 元, 浮动盈亏 {(ref / avg_cost - 1) * 100:+.2f}%"
        lines.append(line)
    if not lines:
        return ""
    return (
        "\n以下是该标的在模拟盘 (虚拟账户) 的当前持仓:\n"
        + "\n".join(lines)
        + "\n请在结论中结合该虚拟持仓状态给出持有/减仓/加仓的可执行参考,"
        "并明确这是基于模拟盘虚拟持仓的建议。\n"
    )


def _build_user_prompt(
    kline_tail: list[dict],
    fins: dict[str, list[dict]],
    levels: dict[str, list[dict]],
    close: float | None,
    symbol: str,
    focus: str,
    asset_type: str = "stock",
    paper_positions: list[dict] | None = None,
    raw_close: float | None = None,
    web_search: bool = False,
    scorecard_context: str = "",
    quality_context: str = "",
) -> str:
    """构建用户消息:标的 + 价位摘要 + 技术指标 JSON + 财务摘要 + 关注点 (+ 虚拟持仓)。

    asset_type 用于区分无财务数据时的文案:指数/ETF 无财务是常态,不走 Free 文案。
    web_search 与系统提示词第 5 节变体保持一致(联网时消息面提示改为结合搜索结果)。
    scorecard_context (P0-7): AI 历史战绩块, 空串不注入; 放用户提示词的原因同
    market_recap._build_user_prompt (自定义系统提示词快照会覆盖注入)。
    quality_context (P1-11): 数据体检块, 空串不注入; 同上放用户提示词。
    """
    news_hint = (
        "消息面维度可结合联网检索到的相关新闻/公告与价量异动交叉验证。"
        if web_search
        else "消息面维度基于价量异动推断即可。"
    )
    parts: list[str] = [
        f"标的标准代码: {symbol}",
        f"关键价位概览: {summarize_levels(levels, close)}",
        "",
        "以下是该标的最近日 K 数据(JSON,含 OHLCV 与已计算的技术指标。"
        f"最近 {_KLINE_WINDOW} 个交易日,升序):",
        "```json",
        json.dumps(kline_tail, ensure_ascii=False),
        "```",
    ]

    has_fin = any(fins.values())
    if has_fin:
        parts.extend([
            "",
            "以下是该标的最新财务数据(JSON,核心指标 + 利润表,金额单位为元):",
            "```json",
            json.dumps(fins, ensure_ascii=False),
            "```",
        ])
    elif asset_type == "index":
        parts.extend([
            "",
            "(该标的为指数: 无财务、股本与涨跌停数据。请按系统提示词第 4 节的说明,"
            "在基本面/财务面维度给出\"接入中\"的友好提示,不要编造数据;"
            f"{news_hint})",
        ])
    elif asset_type == "etf":
        parts.extend([
            "",
            "(该标的为 ETF: 场内基金无上市公司财务报表、股本与涨跌停口径。"
            "请按系统提示词第 4 节的说明,在基本面/财务面维度明确这是 ETF 常态,"
            "不要编造公司财务数字,也不要写成「财务接入中」;"
            f"{news_hint})",
        ])
    else:
        parts.extend([
            "",
            "(该标的暂无财务数据:当前为 Free 模式或尚未同步财务报表。"
            "请按系统提示词第 4 节的说明,在基本面/财务面维度给出\"接入中\"的友好提示,不要编造数据。)",
        ])

    from app.services.ai_provider import build_focus_instruction
    focus_instruction = build_focus_instruction(focus, report_name="个股分析报告")
    if focus_instruction:
        parts.extend(["", focus_instruction])
    paper_part = _paper_position_part(paper_positions or [], close, raw_close)
    if paper_part:
        parts.extend(["", paper_part])
    # P0-7 历史战绩块 (自带 "## 你的历史战绩" 标题与前导空行, 空串则整段不出现)
    if scorecard_context:
        parts.append(scorecard_context)
    # P1-11 数据体检块 (自带 "## 数据体检" 标题与前导空行, 无问题则空串不出现)
    if quality_context:
        parts.append(quality_context)
    return "\n".join(parts)


# ================================================================
# 关键列筛选(控制上下文体积)
# ================================================================

_KLINE_KEEP_COLS = [
    "date", "open", "high", "low", "close", "volume", "change_pct",
    "ma5", "ma10", "ma20", "ma60",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_14", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower",
    "atr_14", "vol_ratio_5d", "turnover_rate",
    "consecutive_limit_ups",
    # 信号类(布尔)——只挑对消息面推断有用的几个
    "signal_limit_up", "signal_broken_limit_up", "signal_macd_golden",
    "signal_macd_dead", "signal_ma_golden_5_20", "signal_volume_surge",
    "signal_boll_breakout_upper", "signal_boll_breakdown_lower",
]


# ================================================================
# 流式分析入口
# ================================================================

async def analyze_stock_stream(
    repo,
    data_dir: Path,
    symbol: str,
    focus: str = "",
    length_hint: str = "",
) -> AsyncIterator[str]:
    """流式个股分析:yield 出每个 NDJSON 事件。

    协议(与 financial_analyzer 一致,前端解析无差异):
      {"type":"meta","symbol","summary","levels"}  数据 + 价位摘要
      {"type":"delta","content":"..."}             逐 chunk 文本
      {"type":"error","message":"..."}
      {"type":"done"}

    length_hint: 追加到用户提示词末尾的附加要求 (如企业微信机器人回复的
    平台长度限制), 默认空字符串表示不追加 (前端调用不受影响)。
    """
    # 1. 加载 K 线
    df = _load_kline(repo, symbol)
    if df.is_empty():
        yield json.dumps({
            "type": "error",
            "message": f"标的 {symbol} 暂无日 K 数据,请先同步",
        }, ensure_ascii=False)
        return

    # 2. 价位计算(基于 K 线)
    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None

    # 3. 财务(辅助)。ETF/指数无公司报表, 跳过扫描, 由提示词走对应常态文案。
    asset_type = repo.resolve_asset_type(symbol)
    fins = {} if asset_type in {"etf", "index"} else _load_financials(data_dir, symbol)

    # 3.5 数据体检 (P1-11): 对即将喂给 AI 的同一份 df/fins 打分。
    # fail-open — 体检自身任何异常都不影响分析主流程 (quality=None 不注入)。
    from app.services import ai_data_quality
    try:
        quality = ai_data_quality.score_symbol(data_dir, symbol, df, fins, asset_type)
    except Exception:  # noqa: BLE001
        logger.warning("[P1-11] 数据体检失败 (跳过注入): %s", symbol, exc_info=True)
        quality = None
    # 门控默认关闭 (只警示); 开启后 D 级硬伤在此拒绝, 不消耗 AI token
    if ai_data_quality.should_block(quality):
        yield json.dumps({
            "type": "error",
            "message": ai_data_quality.block_message(quality),
        }, ensure_ascii=False)
        return

    # 4. meta
    yield json.dumps({
        "type": "meta",
        "symbol": symbol,
        "summary": summarize_levels(levels, close),
        "levels": levels,
        "close": close,
        # P1-11: 体检结果随 meta 下发, 前端显示等级徽章并随报告落库
        "quality": quality,
    }, ensure_ascii=False)

    # 5+6. 构建提示词 + 流式调用 LLM(整体 try-except,任何异常都 yield error,避免前端卡死)
    try:
        from app.services.ai_provider import stream_ai_text, web_search_supported

        # 联网搜索判定须与请求参数同源: 提示词第 5 节变体与 enable_search 保持一致
        search_on = web_search_supported()

        # 虚拟持仓上下文 (V3): 逐账户 (V2 多账户) 收集持有该标的的持仓注入
        # (任何异常静默跳过, 不影响分析)。
        paper_positions: list[dict] = []
        try:
            from app.strategy import paper as paper_trading
            for acc_id in paper_trading.list_account_ids(data_dir):
                pos = paper_trading.load_positions(data_dir, account_id=acc_id).get(symbol)
                if pos and (pos.get("qty") or 0) > 0:
                    paper_positions.append({"account_id": acc_id, **pos})
        except Exception as e:  # noqa: BLE001
            logger.warning("读取模拟盘持仓失败 (跳过注入): %s", e)
        # 浮盈口径用 raw: 持仓成本不复权, 与前复权 close 混算会在除权后失真
        raw_close = float(df.tail(1)["raw_close"][0]) if "raw_close" in df.columns else None

        kline_tail = _clean_rows(df, _KLINE_KEEP_COLS)
        system_prompt = _build_system_prompt(search_on)
        # P0-7 AI 历史战绩: 纯读台账不触发结算, 失败给空串 — 注入不了不影响分析主流程
        from app.services import ai_scorecard
        try:
            scorecard_ctx = ai_scorecard.prompt_line()
        except Exception:  # noqa: BLE001
            scorecard_ctx = ""
        user_prompt = _build_user_prompt(kline_tail, fins, levels, close, symbol, focus,
                                         asset_type=asset_type, paper_positions=paper_positions,
                                         raw_close=raw_close, web_search=search_on,
                                         scorecard_context=scorecard_ctx,
                                         # P1-11 数据体检块 (无问题时空串不注入)
                                         quality_context=ai_data_quality.prompt_block(quality))
        # 机器人等平台可追加长度限制提示 (默认空, 前端调用行为不变)
        user_content = user_prompt + length_hint

        # 数字忠实度闸门 (docs/ai-number-fidelity-plan.md): 白名单 = 拼 prompt 前的
        # 结构化数据 (形态族双口径) + prompt 成品文本 (展示形态)。闸门包在 service
        # 层 delta 出口 — IM 机器人 (analyze_stock_text) 直接消费本流, 一并覆盖。
        from app.services.ai_number_gate import gate_ndjson_stream
        from app.services.ai_number_whitelist import collect_whitelist

        pool = collect_whitelist(
            (kline_tail, fins, levels, close, raw_close, paper_positions),
            texts=(system_prompt, user_content),
        )

        async def _delta_lines() -> AsyncIterator[str]:
            async for delta in stream_ai_text(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.5,
                # 不限制输出: 推理模型(deepseek reasoner 系)思考 token 计入 max_tokens
                # 预算, 固定上限会把正文挤光(实测 4500 全被推理吃掉 → 正文 0 字)。
                max_tokens=None,
                prefer_final_answer=True,
                # 联网搜索实测可达 200s+ (冷门个股检索慢), 启用时放宽超时,
                # 否则模型在默认 180s 预算内放弃搜索 → "未检索到相关消息"。
                timeout=420.0 if search_on else 180.0,
            ):
                yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

        got_content = False
        # 研判卡剥离在闸门内侧 (先剥标记再核对数字): VERDICT JSON 里的数字
        # 若先过闸门会被包进〖〗破坏 JSON; verdict 事件作为非 delta 事件由闸门透传。
        from app.services.ai_verdict import verdict_ndjson_stream
        async for line in gate_ndjson_stream(verdict_ndjson_stream(_delta_lines()), pool):
            # 只有 delta 才算"有正文" — verdict/页脚事件不计, 避免空输出误判成功
            try:
                if json.loads(line).get("type") == "delta":
                    got_content = True
            except (json.JSONDecodeError, TypeError):
                pass
            yield line

    except Exception as e:
        logger.exception("AI stock analysis failed for %s: %s", symbol, e)
        yield json.dumps({"type": "error", "message": f"AI 分析失败: {e}"}, ensure_ascii=False)
        return

    if not got_content:
        # 流正常结束但一个正文块都没有(典型: 输出上限被思考吃光后静默截断)
        logger.warning("AI stock analysis ended with empty content for %s", symbol)
        yield json.dumps({"type": "error", "message": "AI 未返回正文(输出被截断), 请重试"}, ensure_ascii=False)
        return
    yield json.dumps({"type": "done"}, ensure_ascii=False)


async def analyze_stock_text(
    repo,
    data_dir: Path,
    symbol: str,
    focus: str = "",
    length_hint: str = "",
) -> str:
    """非流式个股分析:复用 analyze_stock_stream 的完整流程, 拼接出纯正文文本。

    供企业微信智能机器人等无 NDJSON 消费方的场景使用: 机器人拿到的是完整
    Markdown 正文 (不含报告头 / 免责声明, 由调用方自行组装)。

    length_hint: 追加到用户提示词末尾的附加要求 (平台长度限制提示)。
    返回正文文本; 分析失败或 K 线缺失时返回错误描述文本 (非空)。

    研判卡 (ai_verdict): 流里的 verdict 事件转成一行可读文本插到正文末尾
    (闸门页脚之前 — IM 组装时 pop_gate_footer 依赖页脚是最后一行);
    解析失败也明说"未解析出研判结论", 绝不从正文猜 (fail-honest)。
    """
    full = ""
    verdict: dict | None = None
    async for event in analyze_stock_stream(repo, data_dir, symbol, focus, length_hint=length_hint):
        try:
            data = json.loads(event)
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("type") == "delta":
            full += data.get("content", "")
        elif data.get("type") == "verdict":
            verdict = data
        elif data.get("type") == "error":
            return data.get("message", "AI 分析失败")
    if not full:
        return "AI 未返回正文, 请重试"
    if verdict is not None:
        if verdict.get("ok"):
            vline = f"🧭 AI 研判: {verdict.get('direction', '')} —— {verdict.get('reason', '')}".rstrip(" —")
        else:
            vline = "🧭 AI 研判: 本次未解析出研判结论"
        # 插到闸门页脚之前, 保持页脚仍是最后一行 (机器人 pop_gate_footer 依赖)
        from app.services.ai_number_gate import pop_gate_footer
        body, footer = pop_gate_footer(full)
        full = body + "\n\n" + vline + ("\n" + footer if footer else "")
    return full
