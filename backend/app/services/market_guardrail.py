"""市场口径护栏 —— 非 A 股市场的 AI 提示词纠偏 (docs/hk-market-plan.md §9.1)。

为什么需要
----------
AI 链路 (个股分析 / 多空辩论) 的系统提示词是**按 A 股写的**: 人设「15 年 A 股
一线研究经验」、消息面示例「涨停/连板/炸板 → 可能存在利好」、财务缺失文案
「当前为 Free 模式或尚未同步财务报表」。港股代码走进同一条链时, 模型会
**用 A 股常识脑补**, 最典型的一种错法:

    数据里 ``consecutive_limit_ups`` 是 null / 0
    → 模型读成「该股没涨停、连板情绪缺席」
    → 真相是「港股根本没有涨跌停这个语义」

这是 AI 版的 CONTRIBUTING §3「看起来对但错」: 输出通顺、结论自信、前提错误。
`services/kline_sync_hk.py` 明确写了港股 ``limit_up_price`` 恒 null、
``consecutive_limit_ups`` 恒 0「**这是正确结果不是缺数据**」—— 护栏就是把这句话
翻译成模型能遵守的禁令。

设计口径
--------
* **仅非 A 股市场注入**: A 股返回空串, 提示词逐字不变 (既有 A 股测试零改动)。
* **数值一律读 ``rules_for(market)``**, 不写死 15 / 100 / HKD: 延时分钟、币种、
  默认基准、T+0/T+1、有无涨跌停、每手股数全部来自 ``app/market.py`` 的规则表 ——
  那是市场差异的**唯一**住所 (见 ``tests/test_market_architecture_guard.py``)。
  写死一次, 规则表改了提示词就静默说谎。
* **按 market 注册表分派**, 不写 ``if market == Market.HK``: 新增市场 = 注册一行。
* 🔴 **护栏文本不得含阿拉伯数字**。系统提示词会进数字闸门白名单
  (``stock_analyzer.py`` 的 ``collect_whitelist(texts=(system_prompt, ...))``、
  ``stock_debate.py`` 的 ``texts=(_BULL_SYSTEM, ..., materials["text"])``),
  白名单里的数字等于给模型**发通行证**: 护栏写「涨跌停 ±10%/±20%」, 模型编一句
  「今日涨幅 10%」就能过闸。``ai_number_gate.NUMBER_RULE_PROMPT`` 上方那条
  「刻意不含任何示例数字——系统提示词也会进白名单, 示例数字会留假放行口子」
  说的就是这个。所以规则表里的数值一律经 ``_cn_num`` 转成中文数字再落文案,
  并由 ``test_guardrail_leaks_no_number_into_whitelist`` 兜底 (含 monkeypatch
  成 ``quote_delay_minutes=30`` / ``lot_size=400`` 的**会泄漏值**反向验证)。
* 结构照抄 QuantMind ``skill_templates/hk_market_guardrail.md``, 但**两处刻意不同**:
  ① 本项目 symbol 是 **5 位** + ``.HK`` (``00700.HK``), QuantMind 是 4 位;
  ② QuantMind 的 board lot「按 1 股简化」, 本项目**有 ``lot_size`` 真值**
  (``financial_sync_hk.hk_lot_size_map``), 所以要求模型不要假设 100 股。
"""

from __future__ import annotations

from collections.abc import Callable

from app.market import Market, rules_for

__all__ = [
    "financial_missing_note",
    "guardrail_prompt",
    "prompt_market_label",
]


_CN_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")
_CN_UNITS = ("", "十", "百", "千")


def _cn_four(n: int) -> str:
    """``0 < n < 10000`` → 中文数字 (内部零头补「零」, 如 1005 → 一千零五)。"""
    out: list[str] = []
    zero_pending = False
    for i in (3, 2, 1, 0):
        d = (n // 10**i) % 10
        if d == 0:
            if out:
                zero_pending = True
            continue
        if zero_pending:
            out.append(_CN_DIGITS[0])
            zero_pending = False
        out.append(_CN_DIGITS[d] + _CN_UNITS[i])
    return "".join(out)


def _cn_num(n: int) -> str:
    """整数 → 中文数字 (``0..99999``), 供提示词文案使用。

    为什么需要: 护栏里的数值来自 ``rules_for`` (延时分钟、每手股数), 直接插值会
    把阿拉伯数字带进系统提示词 → 进数字闸门白名单 → 给编造数字发通行证。
    转成中文数字既保住「规则表是唯一来源」, 又不泄漏白名单。

    超范围**抛错而不是退回 ``str(n)``**: 静默退回等于静默重新打开泄漏口子。
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError(f"提示词中文数字只接受 int, 收到 {type(n).__name__}: {n!r}")
    if not 0 <= n <= 99999:
        raise ValueError(f"提示词中文数字超出支持范围 (0..99999): {n}")
    if n == 0:
        return _CN_DIGITS[0]
    wan, ge = divmod(n, 10000)
    parts: list[str] = []
    if wan:
        parts.append(_cn_four(wan) + "万")
    if ge:
        if wan and ge < 1000:
            parts.append(_CN_DIGITS[0])
        parts.append(_cn_four(ge))
    text = "".join(parts)
    # 中文习惯: 15 读「十五」不读「一十五」
    return text[1:] if text.startswith("一十") else text


#: 提示词人设里紧跟「15 年」之后的市场称谓, **含所需的前导空格**。
#:
#: 🔴 两个坑:
#: ① 不直接用 ``rules_for(market).label``: 规则表里是 ``"A股"`` (无空格), 而既有
#:    A 股系统提示词写的是 ``"15 年 A 股一线研究经验"`` (有空格)。用 label 会让
#:    A 股提示词**逐字变化** —— 那是既有行为, 按 CONTRIBUTING §9 必须原样保留。
#: ② 空格放在**值里**而不是模板里: 中英混排 (``" A 股"``) 需要空格, 纯中文
#:    (``"港股"``) 不需要 —— 写死在模板里会让港股渲染成「15 年 港股一线」。
_PROMPT_LABEL: dict[Market, str] = {
    Market.CN: " A 股",
    Market.HK: "港股",
}


def prompt_market_label(market: Market | str | None = Market.CN) -> str:
    """提示词人设里用的市场称谓 (``" A 股"`` / ``"港股"``, 含前导空格规则)。"""
    return _PROMPT_LABEL[rules_for(market).market]


def _no_guardrail() -> str:
    """A 股: 系统提示词本就是按 A 股写的, 无需纠偏, 注入空串保持逐字不变。"""
    return ""


def _hk_guardrail() -> str:
    """港股护栏 6 条。所有市场事实读 ``rules_for(Market.HK)``, 不写死。

    🔴 全文**不得出现阿拉伯数字** (``沪深300`` / ``T+0`` 这类专有名词与
    ``scan_text_numbers`` 实测不收的形态除外): 系统提示词会进数字闸门白名单,
    数字进去就等于给模型编造同值数字发通行证。规则表里的数值经 ``_cn_num``
    转中文数字。详见模块 docstring 与 ``test_guardrail_leaks_no_number_into_whitelist``。
    """
    rules = rules_for(Market.HK)
    # T+0 / T+1 与有无涨跌停从规则表推导, 而不是在文案里写死「T+0」「无涨跌停」:
    # 规则表是单一来源, 文案跟着它走才不会哪天规则改了提示词还在说谎。
    turnover = "T+1 (当日买入次日才能卖)" if rules.t_plus_1 else "T+0 (当日可卖, T+2 交收不影响交易)"
    price_limit = (
        "有涨跌停" if rules.has_price_limit else "**无涨跌停**, 单日振幅不受限制"
    )
    lot = (
        f"固定 {_cn_num(rules.lot_size)} 股"
        if rules.lot_size
        else "**按个股 board lot 各不相同** (数据里 ``lot_size`` 为空表示需查个股真值)"
    )
    delay = _cn_num(rules.quote_delay_minutes)
    return f"""

## 🇭🇰 港股市场口径护栏

**本次分析标的是港股。以下六条约束的优先级高于上文任何章节**; 上文若出现
「涨停 / 连板 / 炸板 / ST / 过户费」等 A 股措辞, 对港股一律按「无此语义」处理。

1. **禁止套用 A 股规则**: T+1 回转交易、一百股整手、涨跌停板幅度限制、ST/*ST
   过滤、过户费、印花税双边征收 —— 这些**全部不适用于港股**, 不要据此推理或措辞。
2. **symbol 口径**: 港股代码是 **五位数字 + ``.HK``**; 创业板 (八开头) 同样保留
   五位。不要补零成六位, 也不要截成四位。
3. **交易机制**: 港股 **{turnover}**; {price_limit}; 每手股数{lot},
   **不要假设一手一百股**。
4. **费用 / 货币 / 基准**: 佣金约万分之三 (最低三 {rules.currency})、
   印花税千分之一 (**卖出单边**)、无过户费; 计价货币是 **{rules.currency}**
   (不是 CNY); 默认对比基准是 **{rules.default_benchmark}** (恒生指数),
   不是沪深300 / 上证指数。
5. **数据口径**: 行情为 **{delay} 分钟延时**, 不是实时报价,
   涉及「当前价 / 盘中」的表述要体现延时; 财报货币可能与股价货币不一致
   (见 ``CURRENCY`` 字段), PE / PB 等估值比率需注意分子分母的货币口径。
6. 🔴 **空值不等于「没有」**: 数据里 ``consecutive_limit_ups`` /
   ``signal_limit_up`` / ``signal_broken_limit_up`` 等 A 股专属列为空或为零,
   含义是「**港股无此语义**」, **不是「该股没涨停」**。禁止据此推断「未涨停」
   「无连板情绪」「打板资金缺席」「情绪到位」等任何结论。
"""


#: 按 market 的护栏文本工厂。
#:
#: 🔴 值是**函数**而不是算好的字符串: 护栏文案要在调用时读 ``rules_for``, 这样
#: 规则表被 monkeypatch / 未来改动后文案跟着变; 若在 import 时算好并存字符串,
#: 补丁会静默失效 (与 ``stock_analyzer._TODAY_BY_MARKET``、
#: ``ai_data_quality._CALENDAR_SOURCES`` 同一个 import 期绑定陷阱)。
_GUARDRAILS: dict[Market, Callable[[], str]] = {
    Market.CN: _no_guardrail,
    Market.HK: _hk_guardrail,
}


def guardrail_prompt(market: Market | str | None = Market.CN) -> str:
    """该市场的提示词护栏文本; A 股返回空串 (提示词逐字不变)。

    未注册的市场当场 ``ValueError``, 不静默回退 A 股 —— 回退会让新市场拿到一份
    「看起来对」的 A 股护栏, 比没有护栏更危险。
    """
    key = rules_for(market).market
    builder = _GUARDRAILS.get(key)
    if builder is None:
        raise ValueError(f"市场 {key} 未注册 AI 提示词护栏")
    return builder()


#: 「无财务数据」时写进用户提示词的说明。
#:
#: 🔴 触发短语 ``该标的暂无财务数据`` **必须逐字保留**: 系统提示词第 4 节靠这句
#: 原文匹配来决定输出「财务面接入中」段落, 改了措辞第 4 节就不会触发。
_FINANCIAL_MISSING_NOTE: dict[Market, str] = {
    Market.CN: (
        "(该标的暂无财务数据:当前为 Free 模式或尚未同步财务报表。"
        '请按系统提示词第 4 节的说明,在基本面/财务面维度给出"接入中"的友好提示,不要编造数据。)'
    ),
    Market.HK: (
        "(该标的暂无财务数据:该标的为**港股**,港股财报与 A 股**分开存储** —— "
        "需要在「财务概况」页单独同步港股财务表, 同步 A 股财务表不会带上港股, "
        "**不是 Free 模式限制**, 不要写成「Free 模式或尚未同步」。"
        '请按系统提示词第 4 节的说明,在基本面/财务面维度给出"接入中"的友好提示,'
        "不要编造 ROE / 营收等数字。)"
    ),
}


def financial_missing_note(market: Market | str | None = Market.CN) -> str:
    """无财务数据时注入用户提示词的说明文案 (按市场)。"""
    key = rules_for(market).market
    note = _FINANCIAL_MISSING_NOTE.get(key)
    if note is None:
        raise ValueError(f"市场 {key} 未注册财务缺失文案")
    return note
