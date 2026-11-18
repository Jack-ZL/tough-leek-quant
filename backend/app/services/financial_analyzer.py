"""AI 财务分析服务 — 读取个股财务数据 → 构建专业提示词 → 流式调用 LLM。

职责: 拉取单只标的的财务报表与股本表 → 转成紧凑 JSON → 拼装 CFA 分析师级系统提示词
       → 流式调用 OpenAI 兼容 API → 逐 chunk 吐给前端。

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import polars as pl

from app.market import Market, infer_market, rules_for
from app.services.financial_sync import FINANCIAL_TABLES, get_financial_df
from app.services.market_guardrail import guardrail_prompt, prompt_market_label

logger = logging.getLogger(__name__)

# 最多注入的报告期数(最新 N 期),避免上下文爆炸 / token 浪费
_MAX_PERIODS = 4


def _clean_rows(df: pl.DataFrame) -> list[dict]:
    """DataFrame → JSON 安全的 dict 列表(NaN/Inf → null,去掉重复回传的 symbol)。"""
    rows = []
    for rec in df.to_dicts():
        clean = {}
        for k, v in rec.items():
            if k == "symbol":
                continue  # 不需要重复回传 symbol
            if isinstance(v, float):
                clean[k] = None if not math.isfinite(v) else v
            else:
                clean[k] = v
        rows.append(clean)
    return rows


def _load_financials_cn(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """A 股: 五张财务表逐张读 ``financials/<table>/part.parquet``。"""
    result: dict[str, list[dict]] = {}
    for table in FINANCIAL_TABLES:
        df = get_financial_df(data_dir, table)
        if df.is_empty():
            result[table] = []
            continue
        df = df.filter(pl.col("symbol") == symbol)
        if df.is_empty():
            result[table] = []
            continue
        # 按 period_end 降序,截取最新 N 期
        if "period_end" in df.columns:
            df = df.sort("period_end", descending=True).head(_MAX_PERIODS)
        result[table] = _clean_rows(df)
    return result


def _load_financials_hk(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """港股: 读 ``financials_hk`` 规范化表(PR2b 落库),**不碰 A 股财务宽表**。

    🔴 与 ``stock_analyzer._load_financials_hk`` 曾经的做法(直接返回 ``{}``)不同:
    这里必须真读。港股代码在 A 股宽表里一行都没有, 沿用 ``get_financial_df``
    会白扫全表拿到空帧, 再据此输出「请先同步财务表」—— 用户照做也修不好,
    因为 A 股同步根本不带港股。

    返回的键是 ``metrics`` / ``income`` / ``balance_sheet`` / ``cash_flow`` 四张,
    **没有 A 股的 ``shares``**(港股宽表不提供股本表), 列集是 A 股的子集 +
    九个港股专有列(``org_type`` / ``pricing_currency`` / ``lot_size`` 等)。
    口径差异由 :data:`_HK_FINANCIAL_NOTE` 写进系统提示词, 不在这里补 null 冒充。
    """
    from app.services.financial_sync_hk import hk_latest_financials

    frames = hk_latest_financials(data_dir, symbol, limit=_MAX_PERIODS)
    return {table: _clean_rows(df) for table, df in frames.items()}


#: 按 market 的财务读取器。港股注册成「真读港股表」而不是「不注册」:
#: 不注册会走 fail-closed 抛错, 而港股财务是**已落库可用**的数据(PR2b),
#: 不该让整个分析流程报错。新市场忘了注册才该抛错。
_FINANCIAL_LOADERS: dict[Market, Callable[[Path, str], dict[str, list[dict]]]] = {
    Market.CN: _load_financials_cn,
    Market.HK: _load_financials_hk,
}


def _load_stock_financials(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """按 symbol 所属市场读取财务数据,返回 {table: [records...]}(最新 N 期,降序)。

    签名**刻意保持两参数不变**(与 ``stock_analyzer._load_financials`` 同理):
    市场由 symbol 唯一决定(``infer_market``), 无需外部传入; 加参数会让既有
    调用方与打桩静默失配。
    """
    market = infer_market(symbol)
    loader = _FINANCIAL_LOADERS.get(market)
    if loader is None:  # 新市场忘了注册: 当场报错, 不静默按 A 股读
        raise ValueError(f"市场 {market} 未注册 AI 财务读取器")
    return loader(data_dir, symbol)


def _summarize(fins: dict[str, list[dict]]) -> str:
    """生成一行业务摘要,便于 LLM 快速把握数据全貌(行数/期数)。

    🔴 遍历 ``fins`` 的键而不是 ``FINANCIAL_TABLES``: 港股只有四张表, 按 A 股
    常量遍历会多出一行 ``shares: 无数据`` —— 港股**根本没有股本表**, 写成「无数据」
    等于告诉模型「这里本该有但缺了」, 诱导它去推断股本。A 股侧读取器恒置满五个键
    (空表也写 ``[]``), 故遍历 ``fins`` 对 A 股输出逐字不变。
    """
    parts = []
    for table, rows in fins.items():
        if rows:
            periods = [r.get("period_end") for r in rows if r.get("period_end")]
            parts.append(f"{table}: {len(rows)}期 ({', '.join(str(p) for p in periods[:3])})")
        else:
            parts.append(f"{table}: 无数据")
    return " · ".join(parts)


# ================================================================
# 系统提示词 —— CFA 分析师级,九维分析框架
# ================================================================

#: 系统提示词模板。``{MARKET_LABEL}`` / ``{MARKET_NORM_RULE}`` 是**仅有**的两个
#: 市场占位符, 由 :func:`_build_system_prompt` 填充。用 ``str.replace`` 而不是
#: ``str.format``: 正文里有大量 Markdown 花括号风险, replace 不需要转义
#: (与 ``stock_analyzer._SYSTEM_PROMPT_HEAD_TMPL`` 同一套做法)。
_SYSTEM_PROMPT_TMPL = """你是一位拥有 15 年{MARKET_LABEL}投研经验的资深财务分析师(CFA + CPA),以机构研究的严谨标准开展工作。你的任务是:基于提供的上市公司财务数据,产出一份**客观、中立、不包含任何投资建议或买卖指令**的财务分析报告。

## 核心红线(务必遵守)

- **绝对不输出**"投资参考""建议买入/卖出""值得投资""逢低吸纳"等任何投资建议或买卖指令
- 你的角色是**客观陈述**该公司的财务特征画像、亮点、风险,供读者自行判断
- 财务质量分类(如高质量蓝筹/高风险等)可作为客观特征描述,但不与"该不该投资"挂钩

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构。不要输出任何 JSON 或代码块,直接输出 Markdown 正文。

### 1. 📌 核心摘要(1-2 句)
用一句话概括该公司的财务画像:盈利质量、成长动能、财务健康度的最关键判断。结尾用【综合评级:★★★☆☆】给出 1-5 星评级(仅反映财务质量客观水平,不构成投资建议)。

### 2. ✅ 亮点(2-3 条)
列出最值得关注的**积极信号**,每条用加粗短语领起,配数据支撑。例如盈利高增、ROE 持续提升、现金流充沛等。

### 3. ⚠️ 风险提示(2-3 条)
客观指出**潜在风险或值得警惕的信号**,例如应收激增、存货堆积、经营现金流与净利润背离、债务攀升等。宁可保守,不要回避。

### 4. 📊 分项诊断
用**表格**呈现各维度的诊断结论,列为「维度 / 关键指标 / 判断」。维度包括:
- **盈利能力**:ROE / ROA / 毛利率 / 净利率
- **成长性**:营收同比 / 净利润同比
- **偿债能力**:资产负债率 / 流动比率(用资产/负债估算)
- **现金流**:经营现金流净额 / 与净利润的匹配度
- **营运效率**:存货周转率等(有数据时)

每个判断给「优秀 / 良好 / 一般 / 偏弱 / 警惕」之一,并一句话说明依据。

### 5. 🎯 综合评估与财务特征画像
2-3 段总结:该公司当前的财务状态(优秀/稳健/承压/恶化)、核心驱动力、未来需重点跟踪的指标。**结尾客观描述该公司的财务特征画像**(如"高 ROE + 低负债 + 现金流稳定 + 营收增速放缓"),作为财务层面的客观归纳,**不下"该不该投资""属于哪类投资标的"的结论**。

## 分析准则(务必遵守)

1. **数据说话**:每个判断必须引用具体数值(如"营收同比 +28.5%"),严禁空泛套话
2. **纵向对比**:利用多期数据看趋势(改善/恶化),而非只看单期
3. **交叉验证**:经营现金流 vs 净利润(是否造血)、毛利率 vs 费用率(盈利结构)、负债 vs 资产(杠杆)
4. **行业常识**:{MARKET_NORM_RULE}
5. **客观中立**:数据不支持时直言"数据不足,无法判断",绝不编造或过度演绎
6. **不输出投资建议**:不写"建议买入/卖出/投资参考/值得投资"等;财务质量评级仅反映客观财务水平,不与投资行为挂钩
7. **简明有力**:避免冗长,用专业读者能扫读的密度输出,总字数 800-1500 字

## 重要免责
报告末尾附一行:"> ⚠️ 本内容由 AI 基于公开财务数据生成,仅客观陈述财务特征,不构成任何投资建议或买卖指令。"

现在请基于下方数据进行分析。"""


#: 分析准则第四条的「对照口径」整句(含阈值示例)。
#:
#: 🔴 港股**沿用同一组阈值数字**而不是改写成中文数字: 系统提示词会进数字闸门
#: 白名单, 而阈值是模型下判断时要**引用**的标准(如「ROE 高于 15% 属优秀」)。
#: 换成中文数字 → 阈值不入池 → 模型写出的阿拉伯数字阈值被判为「无出处」而打码。
#: A 股侧本就把这些数字入了池, 港股照抄即**不新增**任何白名单数字
#: (钉子: ``test_hk_prompt_leaks_no_extra_numbers``)。
_MARKET_NORM_RULE: dict[Market, str] = {
    Market.CN: "对照 A 股常识判断水平(如 ROE>15% 优秀,资产负债率>70% 偏高,毛利率<20% 偏低)",
    Market.HK: "对照港股与 IFRS 口径判断水平(如 ROE>15% 优秀,资产负债率>70% 偏高,毛利率<20% 偏低)",
}

#: 财报口径附注。A 股是**空串**(提示词逐字不变), 港股是六条口径纠偏。
#:
#: 放在 ``financial_analyzer`` 而不是 ``market_guardrail``: 护栏是**跨分析器共用**
#: 的市场事实(T+0 / 无涨跌停 / 延时 / 代码位数), 而这里是**财务分析专属**的
#: 表结构与币种口径 —— 塞进护栏会泄漏到个股综合分析、多空辩论等不需要它的提示词里。
#:
#: 🔴 全文不得出现 A 股提示词里没有的阿拉伯数字(理由同 ``_MARKET_NORM_RULE``)。
_MARKET_FIN_NOTE: dict[Market, str] = {
    Market.CN: "",
    Market.HK: """

## 🇭🇰 港股财报口径(本次标的为港股, 优先级高于上文通用准则)

1. **表数量不同**: 港股只有核心指标 / 利润表 / 资产负债表 / 现金流量表四类,
   **没有 A 股的股本表**。JSON 里不出现 ``shares`` 键是**数据源口径**, 不是缺失,
   不要写成「股本数据未披露」, 也不要据此推断总股本或流通股本。
2. **列集是 A 股的子集**: 港股宽表不提供周转率与现金比率类指标(存货周转率、
   应收账款周转率、速动比率等), 这些键**根本不会出现**在 JSON 里。
   「营运效率」维度据此直接写「港股宽表未提供该口径」, **不要用其它列推算或编造**。
3. 🔴 **币种是混合的, 且不必是人民币**: 金额列(营收 / 净利润 / 资产 / 负债 /
   现金流)是**编报币种**, 港股公司常见为 CNY 或 HKD, 同一市场不同公司可能不同;
   ``pricing_currency`` 是**计价币种**(股价口径), 与编报币种**不必相同**;
   估值列 ``pe_ttm`` / ``pb_ttm`` / ``total_market_cap`` 是**港币**口径。
   提到金额时必须交代币种口径或引用 ``pricing_currency``,
   **禁止**默认按人民币表述, **禁止**自行做汇率换算或折算后跨标的比较。
4. **无公告日**: 港股宽表没有公告日, ``announce_date`` 恒为空 —— 这是数据源口径,
   不是「未披露」, 不要据此推断信息披露是否及时。
5. **金融股字段族**: ``org_type`` 决定哪些科目有值(一般企业 / 银行 / 证券 / 保险)。
   实测银行与证券类公司**有**营业收入 / 净利润 / 资产负债, 但 ``gross_margin``
   (毛利率)恒为空、现金流量表三项也常整表为空 —— 这是**行业常态与数据源口径**,
   不是「数据异常」或「经营恶化」, 不要写成缺失或据此下调评级;
   毛利率、存货周转、经营现金流匹配度等维度对金融股直接标注「不适用」。
   同理, 资产负债率对银行天然极高(实测九成上下), **不要套用一般企业的
   「高于七成偏高」阈值**, 偿债能力维度对金融股改用权益乘数与监管口径描述。
6. **报告期口径**: ``period_name`` 是港股财报期名(年报 / 中报 / 一季报 / 三季报)。
   港股**中报即半年度正式财报**(经审阅), 不是 A 股意义上的季度快报;
   年报的报告期月份**不固定在年末**(港股财年多样), 不要按 A 股「年报即年末」推断,
   也**不要把中报数据年化**后再与年报比较。""",
}

#: 完全读不到财务数据时, 回给前端的错误提示后半句。
#:
#: 🔴 必须是**用户照做能修好**的建议。港股沿用 A 股那句「请先同步财务表」是
#: 错误建议: 财务概况页的 A 股同步不会带上港股, 用户点完仍然为空
#: (与 f1 / f3 / levels 同一类缺陷 —— 提示词把用户导向修不好的操作)。
_MARKET_EMPTY_HINT: dict[Market, str] = {
    Market.CN: "请先同步财务表",
    Market.HK: "请先在「财务概况」页同步港股财务表(港股财报与 A 股分开存储, 同步 A 股财务表不会带上港股)",
}


def _market_key(market: Market | str | None) -> Market:
    """把 ``"hk"`` / ``"HK"`` / ``Market.HK`` 归一到枚举, 并校验三张表都已注册。

    未注册当场 ``ValueError``, 不静默回退 A 股 —— 回退会让新市场拿到一份
    「看起来对」的 A 股财务口径, 比报错危险得多(与 ``guardrail_prompt`` 同理)。
    """
    key = rules_for(market).market
    if key not in _MARKET_NORM_RULE or key not in _MARKET_FIN_NOTE or key not in _MARKET_EMPTY_HINT:
        raise ValueError(f"市场 {key} 未注册 AI 财务提示词口径")
    return key


def _build_system_prompt(market: Market | str | None = Market.CN) -> str:
    """按市场拼装系统提示词(人设称谓 + 对照口径 + 财报附注 + 市场护栏)。

    market 决定四处: ① 人设里的市场称谓; ② 分析准则第四条的对照口径;
    ③ 是否追加港股财报口径附注; ④ 是否追加 ``market_guardrail`` 护栏。
    **A 股四处全部渲染成引入 market 维之前的原文, 逐字不变** ——
    既有 A 股测试与 :data:`_SYSTEM_PROMPT` 兼容常量都依赖这一点。

    附注插在模板正文之后、护栏之前: 附注是**财务领域口径**、护栏是**市场事实口径**,
    两者同层; ``NUMBER_RULE_PROMPT`` 属输出契约, 由调用方追加在最贴近用户消息的
    位置(与 ``stock_analyzer._build_system_prompt`` 同一套层次)。
    """
    key = _market_key(market)
    body = (
        _SYSTEM_PROMPT_TMPL
        .replace("{MARKET_LABEL}", prompt_market_label(key))
        .replace("{MARKET_NORM_RULE}", _MARKET_NORM_RULE[key])
    )
    return body + _MARKET_FIN_NOTE[key] + guardrail_prompt(key)


# 兼容旧引用/测试: A 股渲染结果, 逐字等于引入 market 维之前的 _SYSTEM_PROMPT。
# 称谓走 prompt_market_label 单一来源(值里自带前导空格), 不在这里手写 "A 股"。
_SYSTEM_PROMPT = _build_system_prompt(Market.CN)


#: 用户消息里那句数据说明(金额单位口径按市场不同)。
_MARKET_DATA_LINE: dict[Market, str] = {
    Market.CN: "以下是该标的最新财务数据(JSON 格式,金额单位为元,比率类指标为百分点):",
    Market.HK: (
        "以下是该标的最新财务数据(JSON 格式,金额是**编报币种**原值、"
        "并非统一的人民币元, 比率类指标为百分点;币种与表结构口径见系统提示词"
        "「港股财报口径」一节, 不要自行换算汇率):"
    ),
}


def _build_user_prompt(
    fins: dict[str, list[dict]],
    symbol: str,
    focus: str,
    market: Market | str | None = Market.CN,
) -> str:
    """构建用户消息:标的代码 + 数据 JSON + 可选关注点。

    ``market`` **加在参数表末尾**并默认 A 股, 以免动到既有位置参数调用方
    (``tests/test_ai_analysis_focus.py`` 以三参数调用)。
    """
    key = _market_key(market)
    data_json = json.dumps(fins, ensure_ascii=False, indent=2)
    lines = [
        f"标的标准代码: {symbol}",
        f"数据概览: {_summarize(fins)}",
        "",
        _MARKET_DATA_LINE[key],
        "```json",
        data_json,
        "```",
    ]
    from app.services.ai_provider import build_focus_instruction
    focus_instruction = build_focus_instruction(focus, report_name="财务分析报告")
    if focus_instruction:
        lines.extend(["", focus_instruction])
    return "\n".join(lines)


async def analyze_financials_stream(
    data_dir: Path,
    symbol: str,
    focus: str = "",
) -> AsyncIterator[str]:
    """流式分析:yield 出每个文本 chunk。

    - 启动时先 yield 一条 {"type":"meta",...} 让前端显示数据摘要
    - 之后逐 chunk yield {"type":"delta","content":"..."}
    - 出错时 yield {"type":"error","message":"..."}
    - 结束 yield {"type":"done"}
    """
    # 1. 加载数据(按 symbol 所属市场分派: A 股读 financials/, 港股读 financials_hk/)
    #    _market_key 顺带校验三张口径表都已注册 —— 新市场漏注册当场报错,
    #    不静默按 A 股口径出一份「看起来对」的报告。
    market = _market_key(infer_market(symbol))
    fins = _load_stock_financials(data_dir, symbol)
    total_rows = sum(len(v) for v in fins.values())
    if total_rows == 0:
        yield json.dumps({
            "type": "error",
            "message": f"标的 {symbol} 暂无任何财务数据,{_MARKET_EMPTY_HINT[market]}",
        }, ensure_ascii=False)
        return

    # 2. meta (P1-15: 带归属快照, 前端保存报告时原样带回落盘)
    from app.services import ai_provider as _ai_prov
    attr = _ai_prov.current_attribution()
    # P2-23 不可篡改账本: 登记喂给模型的财务数据 (指纹+快照)
    from app.services import ai_audit
    audit_id = ai_audit.begin_run(data_dir, "financial", symbol)
    audit_inputs = [ai_audit.add_input(data_dir, audit_id, "financials", fins)]
    yield json.dumps({
        "type": "meta",
        "symbol": symbol,
        "summary": _summarize(fins),
        "periods": total_rows,
        **attr,
        # P2-23: 账本 run id (前端保存报告时带回绑定 report_id)
        "audit_id": audit_id,
    }, ensure_ascii=False)

    # 3. 调用 LLM 流式
    try:
        from app.services.ai_provider import stream_ai_text

        user_prompt = _build_user_prompt(fins, symbol, focus, market)

        # 数字忠实度闸门 (docs/ai-number-fidelity-plan.md): 白名单 = 结构化财务数据
        # (revenue/profit 等金额字段自动补"亿"展示形态) + prompt 成品文本。
        from app.services.ai_number_gate import NUMBER_RULE_PROMPT, gate_ndjson_stream
        from app.services.ai_number_whitelist import collect_whitelist

        system_prompt = _build_system_prompt(market) + NUMBER_RULE_PROMPT
        pool = collect_whitelist((fins,), texts=(system_prompt, user_prompt))

        async def _delta_lines() -> AsyncIterator[str]:
            async for delta in stream_ai_text(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                # 不限制输出(推理模型思考 token 计入预算, 见 ai_provider.stream_ai_text)
                max_tokens=None,
                prefer_final_answer=True,
            ):
                yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

        got_content = False
        out_parts: list[str] = []
        async for line in gate_ndjson_stream(_delta_lines(), pool):
            try:
                _ev = json.loads(line)
                if _ev.get("type") == "delta":
                    out_parts.append(_ev.get("content", ""))
            except (json.JSONDecodeError, TypeError):
                pass
            got_content = True
            yield line

    except Exception as e:  # noqa: BLE001
        logger.exception("AI financial analysis failed for %s: %s", symbol, e)
        yield json.dumps({"type": "error", "message": f"AI 分析失败: {e}"}, ensure_ascii=False)
        return

    if not got_content:
        logger.warning("AI financial analysis ended with empty content for %s", symbol)
        yield json.dumps({"type": "error", "message": "AI 未返回正文(输出被截断), 请重试"}, ensure_ascii=False)
        return
    # P2-23: 产出哈希由后端在流结束算
    ai_audit.finish_run(
        data_dir, audit_id=audit_id, kind="financial", symbol=symbol,
        inputs=audit_inputs, prompt_text=system_prompt + "\n\n" + user_prompt,
        output_text="".join(out_parts),
        model=attr.get("model", ""), provider=attr.get("provider", ""),
        profile_id=attr.get("profile_id", ""),
    )
    yield json.dumps({"type": "done"}, ensure_ascii=False)
