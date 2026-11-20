"""市场规则模型 — 全项目唯一的市场规则真源 (single source of truth)。

为什么需要这一层
----------------
A 股的时段、涨跌停、整手股数、交易日历原先散落在三个模块里, 且都以
「模块级常量 + symbol 前缀」的形式硬编码:

- ``market_time.py``: ``_MORNING_START`` / ``_AFTERNOON_END`` / ``_TRADING_TOTAL_MINUTES = 240``
- ``price_limits.py``: 靠 ``300/301/302/688/689/.BJ`` 前缀判板块涨跌幅
- ``services/trading_day.py``: A 股专属探针篮子 ``_BASKET``

这套写法对 A 股是自洽的, 但接入第二个市场时会**静默产出错误结果**而非报错。
实测: ``board_limit_pct("00700.HK")`` 走到主板分支返回 ``0.10`` —— 港股被
悄悄套上 10% 涨跌停。这正是 CONTRIBUTING §3 反复警告的「不报错但结果错」。

因此所有市场差异收敛到本文件的规则表, 三个模块改为按 market 查表分派,
``market="cn"`` 为默认值 → 既有 51 个 market_time 消费方、8 个 trading_day
消费方、16 个 price_limit_pct 调用方**零改动**。

设计约束
--------
1. **能力用「布尔 + 参数」声明, 不用分支**。消费方读 ``rules.has_price_limit``,
   不写 ``if market == "hk"``。这条由 AST 守卫强制 (不是 grep —— grep 会命中
   本文件里引用该反模式的 docstring): ``tests/test_market_architecture_guard.py``
   扫 ``app/`` 全量源码, 出现 market 与市场字面量的比较即失败。
2. **过滤场景与推断场景分开兜底**。``parse_market`` 未知返回 ``None``;
   ``infer_market`` 未知兜底 CN。混用会让「不传 market」静默变成「只看 A 股」。
3. **symbol 判据一套两种载体**。Python 正则与 SQL 正则同源生成, 避免两处漂移。
4. **降级方向必须安全**。半日市表未覆盖时退化为全日市 (宁可漏标不可误标,
   见 ``_HK_HALF_DAYS`` 注释)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from datetime import time as dt_time
from enum import StrEnum

__all__ = [
    "CN_RULES",
    "HK_RULES",
    "Market",
    "MarketTradingRules",
    "Session",
    "SymbolMarketMismatchError",
    "assert_symbol_market",
    "clear_half_days",
    "cn_exchange_of",
    "currency_symbol",
    "half_day_close",
    "infer_market",
    "is_a_share_symbol",
    "is_hk_symbol",
    "is_valid_symbol",
    "market_symbol_pattern",
    "market_symbol_sql_regex",
    "parse_market",
    "register_half_days",
    "rules_for",
    "sessions_for",
    "trading_total_minutes",
]


class Market(StrEnum):
    """支持的市场。值为大写字符串, 可直接作 API query 参数与缓存 key 的一部分。

    用 :class:`enum.StrEnum` 而不是 ``(str, Enum)``: 后者 ``str(Market.CN)`` 与
    f-string 插值都得到 ``"Market.CN"`` 而不是 ``"CN"``, 于是
    ``f"{market}" == "CN"`` 恒假、``cache[f"{market}"]`` 与 ``cache["CN"]``
    是两个键。这类错误不抛异常, 只让分支静默失效 (QuantMind 就踩过:
    ``str(OrderSide.SELL) == "OrderSide.SELL"`` 永不成立 → 印花税静默归零)。
    ``StrEnum`` 的 ``str()`` / f-string 都返回 ``"CN"``, 与 ``.value`` 一致。
    """

    CN = "CN"
    HK = "HK"


class SymbolMarketMismatchError(ValueError):
    """symbol 与声明的 market 不匹配。

    护栏异常: 港股代码进 A 股处理层时**当场报错**, 而不是静默套用 A 股规则
    产出一个看起来合理的错误数字。

    继承 ``ValueError``: 既有 ``except ValueError`` 的调用方不会漏接。
    名字以 ``Error`` 结尾, 与本仓其余自定义异常一致。
    """


@dataclass(frozen=True)
class Session:
    """一个连续竞价时段 (市场本地墙钟时间)。"""

    name: str
    start: dt_time
    end: dt_time

    @property
    def minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)


# 时区: 沪深与港交所同为 UTC+8 且均无夏令时。
#
# 这里刻意用固定偏移而非 zoneinfo.ZoneInfo("Asia/Shanghai"/"Asia/Hong_Kong"):
# market_time.py 的原始注释已说明「服务器/容器本地时区不可靠 (python:slim 镜像
# 默认 UTC)」, 而 ZoneInfo 还额外依赖镜像内存在 tzdata —— 引入一个本可避免的
# 失败模式。iana_timezone 字段仅作展示与文档用途。
_UTC8 = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class MarketTradingRules:
    """一个市场的交易规则。frozen: 规则表是只读契约, 不允许运行时改写。"""

    market: Market
    label: str
    currency: str
    iana_timezone: str
    tz: timezone
    sessions: tuple[Session, ...]
    t_plus_1: bool
    #: 固定整手股数; ``None`` = 按标的元数据 (港股 board lot 逐股不同,
    #: 见 ``instruments_hk.lot_size``)。消费方拿到 None 时**必须**去查标的,
    #: 不得假设为 100。
    lot_size: int | None
    has_price_limit: bool
    #: 行情延时 (分钟)。港股免费源为 15 分钟延时, 必须在 UI 与 AI prompt 中明示。
    quote_delay_minutes: int
    default_benchmark: str
    #: 交易日探针的开盘缓冲窗: 此时刻之前行情戳仍停在上一交易日属正常
    #: (集合竞价未翻新), 不据此判休市。
    stale_buffer_until: dt_time
    #: 是否有**全市场分钟K**落盘 (``data/kline_minute/date=X/part.parquet``)。
    #: 分钟级板块轮动/盘中资金流的唯一数据源; False 的市场相关接口必须 fail-closed
    #: 返回 ``no_minute_source``, 不得用日K插值出「看起来像分钟」的曲线。
    has_minute_bars: bool
    #: 是否有**大盘背景**数据源 (涨跌家数/两市成交额/涨停统计/北向), 即
    #: ``services.market_overview_builder.build_market_overview`` 能否对该市场成立。
    #: False 的市场, 依赖大盘背景的 AI 分析必须 fail-closed —— 拿 A 股背景去解释
    #: 另一个市场的板块轮动, 会生成「通顺但事实错误」的结论 (CONTRIBUTING §4)。
    has_market_overview: bool
    #: 交易日探针的一篮流动性票 (OR 语义: 任一戳为今日即交易日)。
    probe_basket: tuple[str, ...] = field(default=())

    @property
    def trading_total_minutes(self) -> int:
        """全日市的当日总交易分钟数。半日市请用 :func:`trading_total_minutes`。"""
        return sum(s.minutes for s in self.sessions)


CN_RULES = MarketTradingRules(
    market=Market.CN,
    label="A股",
    currency="CNY",
    iana_timezone="Asia/Shanghai",
    tz=_UTC8,
    sessions=(
        Session("AM", dt_time(9, 30), dt_time(11, 30)),
        Session("PM", dt_time(13, 0), dt_time(15, 0)),
    ),
    t_plus_1=True,
    lot_size=100,
    has_price_limit=True,
    quote_delay_minutes=0,
    default_benchmark="000001.SH",
    stale_buffer_until=dt_time(9, 40),
    # 全量分钟能力 (MinuteRefreshService) 盘中持续写 data/kline_minute/;
    # 大盘背景 (涨跌家数/两市成交额/涨停统计/北向) 走 A 股行情与扩展表。
    has_minute_bars=True,
    has_market_overview=True,
    # 大盘蓝筹: 探 max(timestamp), 任一戳为今日即交易日。
    # 同日全部停牌 = 市场性事件, 与休市同处理无碍。
    probe_basket=("000001.SZ", "600519.SH", "600036.SH", "601318.SH", "000651.SZ"),
)

HK_RULES = MarketTradingRules(
    market=Market.HK,
    label="港股",
    currency="HKD",
    iana_timezone="Asia/Hong_Kong",
    tz=_UTC8,
    # 港股早盘 9:30-12:00 (150 分钟) + 午盘 13:00-16:00 (180 分钟) = 330 分钟。
    # 注意与 A 股不同: 午休 12:00-13:00 (A 股 11:30-13:00), 收盘 16:00 (A 股 15:00)。
    sessions=(
        Session("AM", dt_time(9, 30), dt_time(12, 0)),
        Session("PM", dt_time(13, 0), dt_time(16, 0)),
    ),
    t_plus_1=False,  # T+0 当日可卖; T+2 交收不影响交易
    # 港股每手股数 (board lot) 逐股不同 (腾讯 100 股/手, 汇丰 400 股/手),
    # 无固定值 → None, 消费方必须查 instruments_hk.lot_size。
    lot_size=None,
    has_price_limit=False,
    quote_delay_minutes=15,
    default_benchmark="HSI",
    stale_buffer_until=dt_time(9, 40),
    # 🔴 两项都是 False, 且**不是「暂时没接」**:
    #  - 分钟K: 港股只有日K同步 (kline_sync_hk), repository._DIRNAMES 里没有任何
    #    分钟目录, data/kline_minute/ 是 A 股全量分钟能力的专属落盘。
    #  - 大盘背景: build_market_overview 根本没有 market 参数, 内部走
    #    ScreenerService 的 A 股 enriched 缓存, 且要算涨停统计 (港股无涨跌停)
    #    与概念/行业榜 (A 股扩展表)。给它喂港股等于串市场拼接。
    has_minute_bars=False,
    has_market_overview=False,
    probe_basket=("00700.HK", "00005.HK", "01398.HK", "00388.HK", "09988.HK"),
)

RULES_BY_MARKET: dict[Market, MarketTradingRules] = {
    Market.CN: CN_RULES,
    Market.HK: HK_RULES,
}

#: 前端/CLI 惯用名 → Market。少了这层别名, ``rules_for("hk")`` 会抛错。
_MARKET_ALIASES: dict[str, Market] = {
    "CN": Market.CN,
    "HK": Market.HK,
    "A": Market.CN,
    "A_SHARE": Market.CN,
    "ASHARE": Market.CN,
    "SSE": Market.CN,
    "SZSE": Market.CN,
    "HKEX": Market.HK,
    "HK_STOCK": Market.HK,
    "港股": Market.HK,
    "A股": Market.CN,
}


def parse_market(value: object) -> Market | None:
    """**过滤场景**用: 解析 market 参数, 无法识别返回 ``None``。

    ⚠️ 不要在这里兜底 CN。「不传 market / 传了非法 market」若静默变成「只看
    A 股」, 用户会以为看到的是全市场结果 —— 这是最难发现的一类错误。
    需要兜底的推断场景请用 :func:`infer_market`。
    """
    if value is None:
        return None
    if isinstance(value, Market):
        return value
    if isinstance(value, MarketTradingRules):
        return value.market
    key = str(value).strip().upper()
    if not key:
        return None
    return _MARKET_ALIASES.get(key)


def rules_for(market: object = Market.CN) -> MarketTradingRules:
    """取市场规则表。``market`` 接受 Market 枚举或其别名串 (见 :data:`_MARKET_ALIASES`)。

    与 :func:`parse_market` 的区别: 本函数是**取规则**, 调用方已经决定了要
    哪个市场 (默认 CN), 因此无法识别时抛错而不是返回 None —— 静默兜底会让
    拼错的 market 参数变成「悄悄按 A 股算」。

    ⚠️ ``None`` 同样抛错, **不**兜底成 CN。「market 尚未确定」和「market 是
    A 股」是两件事: 前者兜底成后者, 就会拿 A 股规则去算港股 (涨跌停 ±10%、
    T+1、100 股整手), 且全程无声。调用方若真的允许缺省, 应显式写
    ``rules_for(market or Market.CN)``, 让这个决定留在调用侧、可被 grep 到。
    """
    parsed = parse_market(market)
    if parsed is None:
        raise ValueError(f"未知市场: {market!r} (支持: {', '.join(m.value for m in Market)})")
    return RULES_BY_MARKET[parsed]


# ================================================================
# symbol 判据 — 一套定义, Python 与 SQL 两种载体
#
# 两种载体必须同源: 若 Python 侧与 SQL 侧各写一份正则, 迟早漂移
# (一侧放行另一侧拒绝), 表现为「列表页有这只票, 详情页 404」。
# ================================================================

#: A 股合法交易所后缀。**排序固定** → 由它生成的正则字符串跨进程稳定
#: (可复现、可写进测试断言)。实测全量 instruments (5892 行) 与 kline 分区
#: 只出现这三种后缀。
_CN_EXCHANGES: tuple[str, ...] = ("BJ", "SH", "SZ")
_CN_EXCHANGE_SUFFIXES = frozenset(_CN_EXCHANGES)

#: 各市场 symbol 的正则**片段** (不含锚点)。SQL 载体由同一份片段生成。
_SYMBOL_PATTERN_BODY: dict[Market, str] = {
    # A 股: 6 位数字 + 显式交易所白名单。
    #
    # ⚠️ 不能写成宽松的 `[A-Z]{2}`: 那样 `007000.HK` (6 位 + .HK) 会被判成
    # 合法 A 股并流进 A 股链路。白名单由 _CN_EXCHANGES 单一来源生成。
    Market.CN: r"[0-9]{6}\.(?:" + "|".join(_CN_EXCHANGES) + r")",
    # 港股: 5 位数字 + .HK (东财/akshare 原生格式, 如 00700.HK)。
    # 全市场统一 5 位, 创业板 (8 开头) 同样 5 位, 无需特例。
    Market.HK: r"[0-9]{5}\.HK",
}

_SYMBOL_PATTERNS: dict[Market, re.Pattern[str]] = {
    market: re.compile(rf"^({body})$") for market, body in _SYMBOL_PATTERN_BODY.items()
}


def market_symbol_pattern(market: object = Market.CN) -> re.Pattern[str]:
    """该市场 symbol 的**已锚定** Python 正则 (严格区分大小写, 只匹配规范形式)。

    与 :func:`market_symbol_sql_regex` 构成**平价对**: 同一份
    ``_SYMBOL_PATTERN_BODY`` 片段生成, 两侧对任意输入的判定必须一致。
    该平价由 ``tests/test_market.py`` 钉住 —— 改一侧不改另一侧会立刻红。

    ⚠️ 调用方必须用 ``fullmatch`` 而不是 ``match``: Python 的 ``$`` 允许匹配
    末尾换行符之前的位置, ``match`` 会放行 ``"600519.SH\\n"`` 而 DuckDB 不会。
    """
    return _SYMBOL_PATTERNS[rules_for(market).market]


def market_symbol_sql_regex(market: object = Market.CN) -> str:
    """该市场 symbol 的**已锚定** SQL 正则 (DuckDB ``regexp_matches`` 用)。

    与 :func:`market_symbol_pattern` **同源生成**, 保证两侧判定一致。

    ⚠️ DuckDB ``regexp_matches`` **区分大小写**, 与 Python 侧的严格模式一致。
    这要求落库的 symbol 已是规范形式 (后缀大写) —— 规范化是插件层职责
    (CONTRIBUTING §4)。若某处 SQL 需要容忍非规范数据, 应显式写
    ``regexp_matches(upper(symbol), ?)`` 而不是把正则改成大小写不敏感:
    后者会让非规范 symbol 通过校验入库, 与规范形式并存成两行。
    """
    body = _SYMBOL_PATTERN_BODY[rules_for(market).market]
    return f"^({body})$"


def is_valid_symbol(symbol: object, market: object | None = None) -> bool:
    """symbol 是否为**规范形式** (canonical)。

    - 给了 ``market``: 必须匹配该市场的判据。
    - 未给 ``market``: 匹配**任一**已知市场即算合法 (用于泛化校验入口)。

    ⚠️ **严格区分大小写, 不做规范化**。规范形式是交易所后缀大写
    (``600519.SH`` / ``00700.HK``)。这里刻意**不** ``.upper()``:

    若校验放行小写 ``00700.hk``, 它会以非规范形式入库, 与 ``00700.HK``
    变成两行、join 不上、去重失效 —— 比直接拒绝更难查。

    把供应商标识符转成规范形式是**插件层的职责** (CONTRIBUTING §4:
    providers convert vendor fields/units/dates/symbols), 不是校验层的。
    需要宽松路由的场景用 :func:`infer_market` / :func:`assert_symbol_market`,
    它们显式 ``.strip().upper()`` 并在文档里说明原因。

    ⚠️ 同理**不** ``.strip()``: 放行 ``"600519.SH "`` 与放行小写是同一类错误
    (非规范形式入库、与规范形式并存成两行)。而且 SQL 载体不会 strip,
    两侧会判定不一致。

    ⚠️ 用 ``fullmatch`` 而不是 ``match``: Python 的 ``$`` 锚点**允许**匹配
    字符串末尾换行符之前的位置, 于是 ``match(r"^(...)$", "600519.SH\\n")``
    为真, 而 DuckDB 的 ``$`` 只匹配文本末尾 → 双载体漂移。``fullmatch``
    要求整串被消费, 尾换行自然被拒。
    """
    text = str(symbol or "")
    if not text:
        return False
    targets = (
        [rules_for(market).market]
        if market is not None
        else list(Market)
    )
    return any(_SYMBOL_PATTERNS[m].fullmatch(text) for m in targets)


def infer_market(symbol: object) -> Market:
    """**推断场景**用: 从 symbol 猜市场, 猜不出兜底 CN。

    仅用于「拿到一个 symbol, 需要决定走哪条链路」的场景 (如目录路由、
    asset_type 解析)。过滤/查询场景请用 :func:`parse_market`, 那里未知
    必须返回 None 而不是兜底。

    与 :func:`is_valid_symbol` 不同, 这里**先 ``.upper()`` 再匹配**:
    路由失败 (把 ``00700.hk`` 兜底成 CN) 的后果是港股数据流进 A 股链路,
    比「路由到一个大小写非规范的港股」严重得多。规范化仍应由插件层完成,
    但路由层不能因为大小写就把标的送错市场。

    兜底 CN 是存量兼容: 项目历史上所有 symbol 都是 A 股。
    """
    text = str(symbol or "").strip().upper()
    for market in Market:
        if _SYMBOL_PATTERNS[market].fullmatch(text):
            return market
    return Market.CN


def assert_symbol_market(symbol: object, market: object = Market.CN) -> None:
    """护栏: symbol **明确属于别的市场**时当场报错。

    用于 A 股专属处理层的入口。不这么做的话, 港股代码会静默套用 A 股规则
    并产出一个看起来合理的错误数字 (实测 ``board_limit_pct("00700.HK")``
    返回 0.10, 即「港股有 10% 涨跌停」)。

    ⚠️ 只拦「确实匹配到别的市场」, **不拦「匹配不到任何市场」**。
    仓内大量内部路径用裸 6 位代码 (``"000001"`` / ``"300001"``, 无交易所后缀),
    且与带后缀代码在同一列里混用 (见 ``tests/test_st_limit_and_sharpe.py``);
    ``board_limit_pct`` 靠前缀判板块, 本就支持裸码。在这里要求规范形会把
    存量路径全部打断。格式校验是 :func:`is_valid_symbol` 在存储 / API 边界上
    的职责, 规范化是插件层的职责 (CONTRIBUTING §4) —— 三者分工不同, 不能互相
    顶替。

    已知盲区: **裸的**他市场代码 (如 ``"00700"`` 不带 ``.HK``) 拦不住。可接受:
    港股数据一律经插件层规范化成 ``00700.HK`` 之后才进内部路径。

    大小写宽松 (先 ``.upper()``): 本护栏拦的是**市场错配**, 不是大小写
    非规范。在这里因大小写抛错只会制造噪音, 掩盖真正要拦的错误。
    """
    expected = rules_for(market).market
    text = str(symbol or "").strip().upper()
    actual = next(
        (m for m in Market if m is not expected and _SYMBOL_PATTERNS[m].fullmatch(text)),
        None,
    )
    if actual is None:
        return
    raise SymbolMarketMismatchError(
        f"symbol {symbol!r} 属于 {actual.value} 市场, 不能进 {expected.value} 专属处理层。"
        f"{expected.value} 规范判据: {market_symbol_pattern(expected).pattern}"
    )


def is_a_share_symbol(symbol: object) -> bool:
    """是否 A 股 symbol (护栏的宽松版: 只判归属, 不抛错)。"""
    return bool(_SYMBOL_PATTERNS[Market.CN].fullmatch(str(symbol or "").strip().upper()))


def is_hk_symbol(symbol: object) -> bool:
    """是否港股 symbol。"""
    return bool(_SYMBOL_PATTERNS[Market.HK].fullmatch(str(symbol or "").strip().upper()))


def cn_exchange_of(symbol: object) -> str | None:
    """A 股 symbol 的交易所后缀 (SH/SZ/BJ); 非 A 股返回 None。"""
    text = str(symbol or "").strip().upper()
    if not _SYMBOL_PATTERNS[Market.CN].fullmatch(text):
        return None
    suffix = text.rsplit(".", 1)[-1]
    return suffix if suffix in _CN_EXCHANGE_SUFFIXES else None


# ================================================================
# 成交量单位 (volume unit)
#
# 内部 ``volume`` 字段的单位**随市场而变**: A 股是「手」(1 手 = 100 股),
# 港股是「股」(东财/腾讯原生单位)。实测依据与两条独立交叉验证见
# docs/hk-market-plan.md §3.1.2。
#
# 曾经这个单位是隐式的 —— 换算系数 ``* 100`` 硬编码散落在换手率、VWAP、
# 封单额等 6 处。港股接入后那 6 处会静默把换手率算高 100 倍、VWAP 算低
# 100 倍, 且不报错。系数因此收敛到本模块一处。
# ================================================================

_SHARES_PER_VOLUME_UNIT: dict[Market, float] = {
    Market.CN: 100.0,  # volume 单位 = 手
    Market.HK: 1.0,    # volume 单位 = 股
}


def shares_per_volume_unit(market: object = Market.CN) -> float:
    """内部 ``volume`` 字段 → **股数** 的换算系数。

    A 股 volume 单位是手 → ``100.0``; 港股 volume 单位是股 → ``1.0``。

    用法 (换手率与 VWAP 都必须经此函数, 不得再硬编码 ``* 100``)::

        turnover_rate = volume * shares_per_volume_unit(market) * 100 / float_shares
        vwap          = amount / (volume * shares_per_volume_unit(market))

    ⚠️ **不存在**「把港股 volume 折算成手」这条路: 港股每手股数 (board lot)
    逐股不同 (腾讯 100 / 汇丰 400 / 中国银行 1000), 折算需要逐标的 lot_size,
    且碎股 (odd lot) 会产生非整数手。把无损的原始量纲换成有损且依赖外部
    元数据的量纲是错的方向, 因此内部单位随市场而变是**刻意设计**。

    量比 (今日量 / 前 5 日均量) 是比值, 分子分母同单位即约掉, **不需要**此系数;
    半日市影响的是量比的**时间**折算 (330/150), 与本函数无关。

    与 :func:`rules_for` 同一约定: 无法识别的 market (含 ``None``) **抛错**,
    不兜底成 CN —— 静默兜底会让港股按 100 倍系数算换手率, 且全程无声。
    """
    parsed = parse_market(market)
    if parsed is None:
        raise ValueError(f"未知市场: {market!r} (支持: {', '.join(m.value for m in Market)})")
    return _SHARES_PER_VOLUME_UNIT[parsed]


# ================================================================
# 货币符号 (展示用)
#
# ``MarketTradingRules.currency`` 是 ISO 4217 三字母码 (CNY / HKD), 适合做
# 数据契约与 API 字段; 但**给人看**的报告/消息里要的是符号 (¥ / HK$)。
# 两者是不同用途, 不合并成一个字段 —— 把 "HK$" 塞进 currency 会让所有按
# ISO 码比对 currency 的消费方静默失配。
#
# 与规则表一样按 Market 建字典而不是写 ``if market == ...`` 分支
# (见 tests/test_market_architecture_guard.py: 市场差异必须住在表里)。
# ================================================================

_CURRENCY_SYMBOL: dict[Market, str] = {
    Market.CN: "¥",
    Market.HK: "HK$",
}


def currency_symbol(market: object = Market.CN) -> str:
    """市场货币的**展示符号** (``¥`` / ``HK$``)。

    与 :data:`MarketTradingRules.currency` 的分工: 后者是 ISO 码 (``CNY`` /
    ``HKD``), 用于数据契约; 本函数返回给人看的符号, 用于机器人消息、报告抬头。

    与 :func:`rules_for` 同一约定: 无法识别的 market (含 ``None``) **抛错**,
    不兜底成 CN —— 静默兜底会给港股报告打上 ``¥``, 而 ``¥`` 与 ``HK$`` 差
    约 7.8 倍, 是典型的「看起来对但错」(CONTRIBUTING §3)。
    """
    parsed = parse_market(market)
    if parsed is None:
        raise ValueError(f"未知市场: {market!r} (支持: {', '.join(m.value for m in Market)})")
    return _CURRENCY_SYMBOL[parsed]


# ================================================================
# 半日市 (half-day session)
#
# 港交所半日市只交易早盘, 12:00 收市 (无午盘)。日期由 HKEX **逐年公布**
# (惯例为圣诞前夕 12/24、除夕 12/31、农历新年除夕), 无免费公开接口可查,
# 农历日期更无法在不引入农历库的前提下推算。
#
# ⚠️ 表未覆盖时退化为**全日市**, 这是刻意的安全方向:
#   漏标半日市 → 下午多轮询一段无行情的时间; 量比分母偏大导致**低估**,
#                不丢数据, 补上日期后自愈。
#   误标全日市为半日市 → **丢掉整个下午的行情**, 不可自愈。
# 因此宁可漏标, 不可误标。新增日期前必须核对 HKEX 当年公告, 不得凭惯例推测。
#
# 表默认为空: 未经公告核实的日期一律不写。用 :func:`register_half_days`
# 按年度注入 (后续可接配置文件或日历源)。
# ================================================================

_HALF_DAYS: dict[Market, set[date]] = {market: set() for market in Market}


def register_half_days(market: object, days: object) -> None:
    """登记半日市日期 (按 HKEX/交易所年度公告)。

    Args:
        market: 市场 (目前仅港股有半日市)。
        days: ``date`` 可迭代对象。

    重复登记幂等。传入的日期会做基本合法性检查, 但**不校验是否真的是半日市**
    —— 那是调用方 (人工核对公告) 的责任。
    """
    target = rules_for(market).market
    bucket = _HALF_DAYS.setdefault(target, set())
    for day in days or ():
        if isinstance(day, date):
            bucket.add(day)
        else:
            bucket.add(date.fromisoformat(str(day)[:10]))


def clear_half_days(market: object | None = None) -> None:
    """清空半日市登记表 (测试用)。``market=None`` 清空全部。"""
    targets = list(Market) if market is None else [rules_for(market).market]
    for target in targets:
        _HALF_DAYS.setdefault(target, set()).clear()


def half_day_close(market: object, day: date) -> dt_time | None:
    """该日的半日市收市时间; 非半日市返回 ``None``。"""
    rules = rules_for(market)
    if day not in _HALF_DAYS.get(rules.market, ()):
        return None
    return rules.sessions[0].end if rules.sessions else None


def sessions_for(market: object, day: date) -> tuple[Session, ...]:
    """该日的实际交易时段 —— **半日市感知**。

    所有时段判断都必须走这里, 不要直接读 ``rules.sessions``
    (那是全日市模板, 半日市会多出下午盘)。
    """
    rules = rules_for(market)
    if half_day_close(rules.market, day) is not None:
        return rules.sessions[:1]
    return rules.sessions


def trading_total_minutes(market: object, day: date) -> int:
    """该日的总交易分钟数 —— **半日市感知**。

    量比 (volume ratio) 的分母。用全日市常量 240/330 会在半日市把量比算错。
    """
    return sum(s.minutes for s in sessions_for(market, day))
