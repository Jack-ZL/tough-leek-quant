"""东方财富沪深港通 (HSGT) 资金流 → 南向/港股通每日净买入 — 直连扩展模块。

**为什么需要本模块**: 港股面板此前只有个股行情与分红, 没有**市场级资金流**。南向
(港股通) 净买入是港股最重要的增量资金指标 —— 它解释了 2024 年以来港股定价权的
结构性变化, 缺了它, 面板上的港股涨跌只能看不能解释。

数据源: ``datacenter-web.eastmoney.com`` 报表 ``RPT_MUTUAL_DEAL_HISTORY``, 与
:mod:`app.plugins.eastmoney.hk_dividend` / ``hk_financials`` 同主机 —— 该主机独立
于 push2 的封禁池 (push2 已被集群级 IP 封禁, 见方案 §1976), 不与行情同步争抢配额。

实测口径 (2026-11-22 活体验证, 全部结论均可复现):

- ``MUTUAL_TYPE`` 有**六个**取值, 不只是常见的四个: ``001`` 沪股通 / ``002``
  港股通(沪) / ``003`` 深股通 / ``004`` 港股通(深) / **``005`` 北向合计** /
  **``006`` 南向合计**。``005``/``006`` 是上游**预先算好的精确聚合**, 实测
  2026-11-11..20 连续 8 个交易日 ``006 == 002 + 004`` 逐字段精确吻合
  (``NET_DEAL_AMT`` 6562.38+5249.12=11811.50, ``DEAL_AMT``
  56017.04+41664.60=97681.64)。**必须直接取 ``006``, 不可自己加总** —— 自己加
  会在上游修数/补数时与服务端产生分歧, 且多一次出错机会。
- 🔴 **``*_AMT`` 字段单位是「百万元」, 不是元也不是万元** (两条独立证据):
  (1) 笔均成交额 ``DEAL_AMT/DEAL_NUM`` = 56017.04e6/1314762 = **42606 元/笔**,
  合理; 若按元算是 0.04 元/笔 (荒谬), 按万元算是 426 元/笔 (偏小一个量级)。
  (2) 北向 125062.53e6/7564729 = **16532 元/笔**, 同样合理。本模块在插件边界
  统一换算成**元** (CONTRIBUTING §3.1: 单位在边界一次定死, 不向下游扩散)。
- 🔴 **``ACCUM_DEAL_AMT`` 的列名在撒谎 —— 它是「历史累计净流入」, 不是累计成交
  额**。证据: ``002`` 的 ``ACCUM_DEAL_AMT=3253659.6`` 与另一张报表
  ``RPT_MUTUAL_NETINFLOW_STATISTICS`` 的 ``HISTORY_INFLOW_SH=3253659.6``
  **逐字节相同**; 且 ``006`` 的值 5534878.67 == 002+004 == 该报表的
  ``HISTORY_TOTAL_INFLOW``。故本模块把它改名为 ``accum_net_inflow``, 让最新一行
  免费给出「历史累计净流入」, 无需额外请求。
- 🔴 **日期过滤只认「横杠 + 单引号」**: ``(TRADE_DATE>='2026-11-01')`` 生效;
  横杠 + 双引号报 ``9501 filter字段中日期参数格式错误``, 斜杠 (任意引号) 报
  ``9501 参数预处理错误:InputMismatchException``。这与同主机港股分红报表的
  ``EX_DIVIDEND_DATE`` **恰好相反** (那边只认斜杠 + 双引号, 横杠会静默给错答
  案)。同一主机、同类字段、相反格式 —— 故两个模块各自持有自己的格式常量, 刻意
  不抽公共 helper (抽了反而诱导照搬)。所幸本报表错格式是**响亮报错**而非静默错。
- ✅ **跨源交叉验证**: 另一张报表 ``RPT_MUTUAL_NETINFLOW_DETAILS``
  (``DIRECTION_TYPE="2"`` = 南向, 2014-11-17 起 2755 行) 的
  ``NET_INFLOW_SH/SZ/BOTH`` 与本报表 ``002/004/006`` 的 ``NET_DEAL_AMT``
  **连续 8 个交易日精确相同**。但那张报表**必须携带硬编码 token**
  (``894050c76af8597a853f5b408b759f5d``, 从其 SPA 的 JS bundle 里提取), token 一
  旦轮换即全线失效。既然两者数据等价, **本模块刻意只用免 token 的
  ``RPT_MUTUAL_DEAL_HISTORY``**, 不引入这个脆弱依赖。
- ⚠️ **``HOLD_MARKET_CAP`` (持股市值) 刻意不暴露** —— 该字段口径无法定性, 详见
  :data:`HSGT_EXCLUDED_COLUMNS`。
- ⚠️ **币种是推断而非上游声明**: 报表**没有任何币种列**。判为**人民币**的依据:
  (1) 同一列在整张报表里只有一种口径, 而北向 ``DEAL_AMT`` 是 A 股成交额, 无歧义
  是人民币; (2) 同主机 ``RPT_MUTUAL_TOP10DEAL`` 可用 ``MUTUAL_RATIO`` 独立校验
  —— 中国平安 2015-06-17 ``DEAL_AMT/DEAL_AMOUNT`` = 353785517/8911609856 =
  **3.97%** 恰等于其 ``MUTUAL_RATIO``, 且 ``CLOSE_PRICE=87`` 与当日 A 股人民币
  收盘价吻合; (3) 上游 SPA 的图表 tooltip 统一按 ``fix:"亿元"`` 格式化。故 API
  响应**显式携带 ``currency`` 字段**, 让口径可见、可纠正, 而不是埋在代码里。
- ``FUND_INFLOW`` / ``QUOTA_BALANCE`` 在近期行上均为 ``None`` (上游已停更),
  不基于它们构建任何逻辑; ``QUOTA_BALANCE_TEXT`` 恒为 ``'额度充足'``, 无信息量。
- ``005`` (北向合计) 的 ``HOLD_MARKET_CAP`` 是 ``0`` 而非 ``null`` —— 港交所
  2024 年起停止逐日披露北向持股, 不可把 0 当真实值展示。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, NamedTuple

from app.plugins.eastmoney.client import EastmoneyClient

logger = logging.getLogger(__name__)

#: 沪深港通每日成交报表。一行 = 一个 ``MUTUAL_TYPE`` × 一个交易日。
RPT_MUTUAL_DEAL_HISTORY = "RPT_MUTUAL_DEAL_HISTORY"

#: ``MUTUAL_TYPE`` 取值 (实测六值, 非四值)。
MUTUAL_TYPE_HGT = "001"  # 沪股通 (北向)
MUTUAL_TYPE_GGT_SH = "002"  # 港股通(沪) (南向)
MUTUAL_TYPE_SGT = "003"  # 深股通 (北向)
MUTUAL_TYPE_GGT_SZ = "004"  # 港股通(深) (南向)
MUTUAL_TYPE_NORTH_TOTAL = "005"  # 北向合计
MUTUAL_TYPE_SOUTH_TOTAL = "006"  # 南向合计

#: 南向三个通道。``006`` 是上游精确聚合, 必须直接取用 (见模块 docstring)。
SOUTHBOUND_MUTUAL_TYPES = (MUTUAL_TYPE_GGT_SH, MUTUAL_TYPE_GGT_SZ, MUTUAL_TYPE_SOUTH_TOTAL)

#: ``*_AMT`` 字段单位「百万元」→「元」的换算因子。
HSGT_AMT_UNIT = 1_000_000

#: 拉取列。**刻意不含 ``HOLD_MARKET_CAP`` / ``FUND_INFLOW`` / ``QUOTA_BALANCE``**
#: (见 :data:`HSGT_EXCLUDED_COLUMNS`), 也不含 ``LEAD_STOCKS_*`` (领涨股属锦上添花,
#: 按 CONTRIBUTING §5.2 最小改动原则留给后续 PR)。
HSGT_COLUMNS = (
    "TRADE_DATE,MUTUAL_TYPE,BUY_AMT,SELL_AMT,NET_DEAL_AMT,DEAL_AMT,"
    "ACCUM_DEAL_AMT,INDEX_CLOSE_PRICE,INDEX_CHANGE_RATE"
)

#: 刻意排除的列及其理由。**写在代码里而不是只写在 docstring**, 是为了让后来者
#: 想「顺手加一列」时先撞上这段说明。
HSGT_EXCLUDED_COLUMNS: dict[str, str] = {
    # 🔴 口径无法定性, 且两种解释相差整整 2 倍 —— 宁可不出, 不可出错 (§13)。
    # 实测 002(港股通沪) 与 004(港股通深) 的持股市值比值**跨年单调收敛**:
    #   2019-01-02 → 1.008795   2021-01-04 → 1.004983   2023-01-03 → 1.002444
    #   2025-01-02 → 1.000750   2026-06-01 → 1.000351   近 14 日 → 恒为 1.00029
    # 两个真实不同的股票池 (沪市约 300 只 / 深市约 200 只, 成分与权重均不同)
    # 不可能维持这种单调收敛到 1 的比值 —— 这是机械联动的特征, 说明两行携带的
    # 是**同一个市场级总量**, 于是 006 = 002+004 就成了 2 倍的错值。
    # 量级交叉验证支持这一判断: 历史累计净流入 5.5349e12 元 ÷ 002 的持股市值
    # 5.3108e12 元 = **1.0422** (恒指 2014→2026 基本走平, 累计净买入本应≈当前
    # 持股市值); 若按 002+004 = 1.0623e13 算, 比值是 0.5210, 意味着需要 100%
    # 的价格涨幅才能自洽, 不成立。
    # 但「上游把总量重复写到两行」终究是对服务端行为的推测, 且无独立数据源可
    # 交叉验证 (``RPT_MUTUAL_HOLDRANK_NEW`` 实测只有北向 001/003 且为季度频,
    # 南向逐股持股报表不存在), 故 v1 直接不提供该指标。
    "HOLD_MARKET_CAP": "口径无法定性 (002/004 疑似重复计数, 006 或为 2 倍错值)",
    "FUND_INFLOW": "近期行恒为 None, 上游已停更",
    "QUOTA_BALANCE": "近期行恒为 None, 上游已停更",
    "QUOTA_BALANCE_TEXT": "恒为 '额度充足', 无信息量",
}

#: 单页行数。南向全史 2756 行 × 3 通道 ≈ 8268 行, 500/页 → 17 页。
HSGT_PAGE_SIZE = 500

#: 防跑飞硬闸 (同 :meth:`EastmoneyClient.datacenter_report_all` 的语义)。
HSGT_MAX_PAGES = 40

#: 排序键。**必须带 ``MUTUAL_TYPE`` 作次键**: 翻页要求全序稳定, 只按 ``TRADE_DATE``
#: 排序时同一天的三个通道顺序由服务端决定, 跨页会漂移 (同 ``hk_dividend`` 的
#: ``HK_DIV_SORT_COLUMNS`` 纪律)。升序输出, 前端图表无需再反转。
HSGT_SORT_COLUMNS = "TRADE_DATE,MUTUAL_TYPE"
HSGT_SORT_TYPES = "1,1"

#: 币种。上游无币种列, 判为人民币的依据见模块 docstring; API 响应显式携带此值。
HSGT_CURRENCY = "CNY"

#: 数据源标识 (与 ``hk_dividend`` 的 source 字段同一用途: 让前端/日志可追溯)。
HSGT_SOURCE = "eastmoney_datacenter_hsgt"


class SouthboundDaily(NamedTuple):
    """南向资金流的一个交易日 (宽表: 合计 + 沪深两通道拆分)。

    金额字段一律为**元** (人民币), 已在插件边界完成「百万元 → 元」换算。
    ``None`` 表示上游该日该通道无数据 (例如深港通 2016-12-05 才开通, 之前的
    ``ggt_sz_*`` 全为 ``None``) —— **不用 0 填充**, 0 与「无数据」语义不同。
    """

    trade_date: date
    #: 南向合计净买入 (元)。买入为正, 卖出为负。
    net_buy: float | None
    #: 南向合计买入额 / 卖出额 / 成交额 (元)。实测 ``buy - sell == net_buy``
    #: 且 ``buy + sell == turnover`` (31289.71-24727.33=6562.38 ✓)。
    buy: float | None
    sell: float | None
    turnover: float | None
    #: 历史累计净流入 (元), 截至本交易日。上游列名 ``ACCUM_DEAL_AMT`` 有误,
    #: 实为累计净流入而非累计成交额 (证据见模块 docstring)。
    accum_net_inflow: float | None
    #: 港股通(沪) / 港股通(深) 分通道拆分 (元)。
    ggt_sh_net_buy: float | None
    ggt_sz_net_buy: float | None
    ggt_sh_turnover: float | None
    ggt_sz_turnover: float | None
    #: 当日恒生指数收盘与涨跌幅 (%)。南向行携带的是恒指 (实测 22410.8),
    #: 北向行携带的是上证 (3533.79) —— 同一列在不同 ``MUTUAL_TYPE`` 下指代
    #: 不同指数, 故只在南向语境下取用。
    index_close: float | None
    index_change_pct: float | None


def _to_float(value: Any) -> float | None:
    """上游数值 → ``float``; 空/非数值 → ``None`` (不抛, 不填 0)。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN 归一成 None


def _amt_to_yuan(value: Any) -> float | None:
    """「百万元」→「元」, 并抹掉上游的浮点尾噪。

    🔴 尾噪是真实存在的: 实测 ``004`` 的 ``NET_DEAL_AMT`` 返回
    ``-482.780000000001`` 而同一天的 ``RPT_MUTUAL_NETINFLOW_DETAILS`` 返回
    ``-482.78``。上游精度是 0.01 百万元 (= 1 万元), 换算成元后按 2 位小数取整
    既消除尾噪又不丢有效精度。
    """
    raw = _to_float(value)
    if raw is None:
        return None
    return round(raw * HSGT_AMT_UNIT, 2)


def _parse_trade_date(value: Any) -> date | None:
    """``TRADE_DATE`` → :class:`date`; 为空或无法解析 → ``None``。

    实测返回 ``'2026-11-20 00:00:00'`` (横杠 + 时间尾巴), 与同主机港股分红报表的
    ``NOTICE_DATE`` 同形。仍两种分隔符都接受 —— 上游在同一主机内混用斜杠/横杠已
    有先例 (``EX_DIVIDEND_DATE`` 就是斜杠), 不值得为省一行 ``replace`` 赌它不变。
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19].replace("/", "-")).date()
    except ValueError:
        logger.warning("HSGT 交易日无法解析, 跳过该行: %r", value)
        return None


def hsgt_date_bound(value: date, op: str) -> str:
    """拼一个 ``TRADE_DATE`` 过滤子句。

    🔴 **只认「横杠 + 单引号」**。横杠 + 双引号 → ``9501 filter字段中日期参数格式
    错误``; 斜杠 (任意引号) → ``9501 参数预处理错误``。与 ``hk_dividend`` 的
    ``EX_DIVIDEND_DATE`` (只认斜杠 + 双引号) **恰好相反**, 不可照搬。

    :param op: ``">="`` 或 ``"<="``。
    """
    return f"(TRADE_DATE{op}'{value.isoformat()}')"


def hsgt_filter(
    mutual_types: Iterable[str] = SOUTHBOUND_MUTUAL_TYPES,
    start: date | None = None,
    end: date | None = None,
) -> str:
    """拼 ``RPT_MUTUAL_DEAL_HISTORY`` 的完整 filter。

    实测多值语法 ``(MUTUAL_TYPE in ("002","004","006"))`` 生效 (count=7801 =
    2756+2289+2756), 一次请求取回三个通道, 避免 N+1 (CONTRIBUTING §6.3)。
    """
    quoted = ",".join(f'"{t}"' for t in mutual_types)
    parts = [f"(MUTUAL_TYPE in ({quoted}))"]
    if start is not None:
        parts.append(hsgt_date_bound(start, ">="))
    if end is not None:
        parts.append(hsgt_date_bound(end, "<="))
    return "".join(parts)


def parse_southbound_daily(rows: Iterable[Mapping[str, Any]]) -> list[SouthboundDaily]:
    """原始行 (长表, 一行一通道) → 按交易日透视的宽表, 升序。

    🔴 **``006`` 缺失时不用 ``002+004`` 兜底**。虽然已实测两者精确相等, 但兜底会
    把「上游停止发布合计行」这一变化悄悄掩盖掉 —— 图表上只是少一个点, 没人会去
    查。宁可留 ``None`` 让缺口可见 (fail closed, CONTRIBUTING §4)。分通道字段
    仍照常输出, 前端可自行判断展示哪一层。
    """
    by_date: dict[date, dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        trade_date = _parse_trade_date(row.get("TRADE_DATE"))
        if trade_date is None:
            continue
        mutual_type = str(row.get("MUTUAL_TYPE") or "").strip()
        if not mutual_type:
            continue
        by_date.setdefault(trade_date, {})[mutual_type] = row

    out: list[SouthboundDaily] = []
    for trade_date in sorted(by_date):
        channels = by_date[trade_date]
        total = channels.get(MUTUAL_TYPE_SOUTH_TOTAL)
        sh = channels.get(MUTUAL_TYPE_GGT_SH)
        sz = channels.get(MUTUAL_TYPE_GGT_SZ)
        if total is None:
            logger.warning(
                "HSGT %s 缺南向合计行 (MUTUAL_TYPE=%s), 合计字段置空不兜底; 实到通道=%s",
                trade_date.isoformat(),
                MUTUAL_TYPE_SOUTH_TOTAL,
                sorted(channels),
            )
        # 指数优先取合计行, 缺失时退回港股通(沪) —— 两者实测同为恒指。
        index_src = total if total is not None else sh
        out.append(
            SouthboundDaily(
                trade_date=trade_date,
                net_buy=_amt_to_yuan((total or {}).get("NET_DEAL_AMT")),
                buy=_amt_to_yuan((total or {}).get("BUY_AMT")),
                sell=_amt_to_yuan((total or {}).get("SELL_AMT")),
                turnover=_amt_to_yuan((total or {}).get("DEAL_AMT")),
                accum_net_inflow=_amt_to_yuan((total or {}).get("ACCUM_DEAL_AMT")),
                ggt_sh_net_buy=_amt_to_yuan((sh or {}).get("NET_DEAL_AMT")),
                ggt_sz_net_buy=_amt_to_yuan((sz or {}).get("NET_DEAL_AMT")),
                ggt_sh_turnover=_amt_to_yuan((sh or {}).get("DEAL_AMT")),
                ggt_sz_turnover=_amt_to_yuan((sz or {}).get("DEAL_AMT")),
                index_close=_to_float((index_src or {}).get("INDEX_CLOSE_PRICE")),
                index_change_pct=_to_float((index_src or {}).get("INDEX_CHANGE_RATE")),
            )
        )
    return out


def fetch_southbound_flow(
    client: EastmoneyClient,
    *,
    start: date | None = None,
    end: date | None = None,
    page_size: int = HSGT_PAGE_SIZE,
    max_pages: int = HSGT_MAX_PAGES,
) -> list[SouthboundDaily]:
    """拉取南向 (港股通) 每日资金流, 升序。

    :param start: 起始交易日 (含)。``None`` = 不限, 会一路取到 2014-11-17。
    :param end: 结束交易日 (含)。``None`` = 不限 (最新一日)。
    :raises EastmoneyError: 报表名/列名/过滤格式错误等**非空**失败。
        「窗口内无交易日」是合法空结果 (``code=9201``), 由 client 归一成 ``[]``。
    """
    rows = client.datacenter_report_all(
        RPT_MUTUAL_DEAL_HISTORY,
        flt=hsgt_filter(SOUTHBOUND_MUTUAL_TYPES, start, end),
        columns=HSGT_COLUMNS,
        page_size=page_size,
        max_pages=max_pages,
        sort_columns=HSGT_SORT_COLUMNS,
        sort_types=HSGT_SORT_TYPES,
    )
    return parse_southbound_daily(rows)


__all__ = [
    "HSGT_AMT_UNIT",
    "HSGT_COLUMNS",
    "HSGT_CURRENCY",
    "HSGT_EXCLUDED_COLUMNS",
    "HSGT_MAX_PAGES",
    "HSGT_PAGE_SIZE",
    "HSGT_SORT_COLUMNS",
    "HSGT_SORT_TYPES",
    "HSGT_SOURCE",
    "MUTUAL_TYPE_GGT_SH",
    "MUTUAL_TYPE_GGT_SZ",
    "MUTUAL_TYPE_HGT",
    "MUTUAL_TYPE_NORTH_TOTAL",
    "MUTUAL_TYPE_SGT",
    "MUTUAL_TYPE_SOUTH_TOTAL",
    "RPT_MUTUAL_DEAL_HISTORY",
    "SOUTHBOUND_MUTUAL_TYPES",
    "SouthboundDaily",
    "fetch_southbound_flow",
    "hsgt_date_bound",
    "hsgt_filter",
    "parse_southbound_daily",
]
