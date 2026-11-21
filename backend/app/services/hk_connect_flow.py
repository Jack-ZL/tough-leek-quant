"""港股通 (南向) 资金流 —— **只读**聚合。

为什么这个功能不需要新同步、不需要新存储
--------------------------------------
上游报表 ``RPT_MUTUAL_DEAL_HISTORY`` 支持按 ``TRADE_DATE`` 窗口过滤, 一次翻页请求
就能取回窗口内全部数据 (实测 20 个交易日 × 3 通道 = 15 行宽表 / 1 页 / ≈0.7s)。
既然上游支持按窗口取, 就没有理由先落一张表再读它 —— 落库只会带来「同步没跑 →
资金流是旧的」这一类额外失败模式, 而资金流恰恰是**时效性最强**的指标 (§4)。

因此本模块**不写任何存储**, 与 :mod:`app.services.hk_dividend_calendar` /
:mod:`app.services.ah_premium` 同一取舍: 上游限速/熔断由 client 负责, 这里不另加
缓存, 前端 TanStack Query 的 ``staleTime`` 挡住重复打开页面造成的重复请求。

单位与口径 (CONTRIBUTING §3.1)
---------------------------
所有金额字段一律为**元** (人民币), 换算在插件层一次做完
(:mod:`app.plugins.eastmoney.hsgt` 把上游的「百万元」×1e6)。本模块**不做任何单位
运算**, 只做聚合 —— 单位在边界定死、不向下游扩散。

``net_buy`` 是**净买入**: 买入为正, 卖出为负。实测上游满足
``buy − sell == net_buy`` 且 ``buy + sell == turnover``, 三个字段由
``tests/test_hk_connect_flow.py`` 用真实探测值钉住。

🔴 **``accum_net_inflow`` 是「历史累计净流入」, 不是累计成交额** —— 上游列名
``ACCUM_DEAL_AMT`` 是错的 (证据见 :mod:`app.plugins.eastmoney.hsgt` 的 docstring)。
它由上游逐日维护, 取最新一行即得, **不需要**自己把 2756 个交易日加总 (那样既慢
又会在上游修数时与服务端产生分歧)。

🔴 **合计行 (``MUTUAL_TYPE=006``) 缺失时不用 ``002+004`` 兜底**, 宁可让
``net_buy`` 为 null。虽然已实测两者精确相等, 但兜底会把「上游停止发布合计行」这一
变化悄悄掩盖掉 —— 图表上只是少一个点, 没人会去查 (§4 fail closed)。

⚠️ **币种是推断而非上游声明**: 报表没有任何币种列。判为人民币的三条依据记录在
:mod:`app.plugins.eastmoney.hsgt` 的 docstring 里; 本模块把
:data:`~app.plugins.eastmoney.hsgt.HSGT_CURRENCY` 透传进 API 响应, 让口径**可见、
可纠正**, 而不是埋在代码里当既定事实 (§13)。

⚠️ **刻意不提供「持股市值」**: 上游 ``HOLD_MARKET_CAP`` 的口径无法定性 (两种解释
相差整整 2 倍), 且无独立数据源可交叉验证。完整证据与推理见
:data:`~app.plugins.eastmoney.hsgt.HSGT_EXCLUDED_COLUMNS`。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import NamedTuple

import polars as pl

from app.plugins.eastmoney.client import EastmoneyClient
from app.plugins.eastmoney.hsgt import (
    HSGT_CURRENCY,
    HSGT_SOURCE,
    SouthboundDaily,
    fetch_southbound_flow,
)

logger = logging.getLogger(__name__)

#: 输出列顺序。API 层直接 ``to_dicts()``, 故这里就是响应字段顺序的唯一事实来源。
HK_CONNECT_FLOW_COLS = (
    "trade_date",
    "net_buy",
    "buy",
    "sell",
    "turnover",
    "accum_net_inflow",
    "ggt_sh_net_buy",
    "ggt_sz_net_buy",
    "ggt_sh_turnover",
    "ggt_sz_turnover",
    "index_close",
    "index_change_pct",
)

#: 汇总卡片的滚动窗口 (交易日数, 不是日历日)。5 ≈ 一周, 20 ≈ 一个月。
HK_CONNECT_FLOW_ROLL_5D = 5
HK_CONNECT_FLOW_ROLL_20D = 20


class HkConnectFlowResult(NamedTuple):
    """:func:`hk_connect_flow` 的结果: 日序列 + 汇总 + 口径元信息。

    汇总字段全部是**窗口内**的口径, 只有 ``accum_net_inflow`` 是上游维护的
    「自 2014-11-17 开通以来」的全史累计 —— 两者语义不同, 不可混用, 故分开命名。
    """

    #: 列为 :data:`HK_CONNECT_FLOW_COLS` 的表, 按 ``trade_date`` **升序**
    #: (前端图表无需再反转)。
    items: pl.DataFrame
    #: 金额字段币种 (:data:`~app.plugins.eastmoney.hsgt.HSGT_CURRENCY`)。
    currency: str
    #: 数据源标识。
    source: str
    #: 实际请求的窗口 (含端点)。``None`` 表示该侧未限定。
    start: date | None
    end: date | None
    #: 窗口内交易日数 (= ``items`` 行数)。
    trading_days: int
    #: 最新一个交易日的南向净买入 (元); 窗口内无数据时为 ``None``。
    latest_net_buy: float | None
    #: 最新交易日; 窗口内无数据时为 ``None``。
    latest_trade_date: date | None
    #: 截至最新交易日的**历史累计净流入** (元, 上游维护, 自 2014-11-17 起)。
    accum_net_inflow: float | None
    #: 最近 5 / 20 个交易日的净买入合计 (元)。**窗口内不足该天数时为 ``None``**
    #: 而不是「把现有的加起来」—— 3 天的和冒充 5 天和, 在卡片上看不出来 (§4)。
    net_buy_5d: float | None
    net_buy_20d: float | None
    #: 最新交易日的恒生指数收盘与涨跌幅 (%)。
    index_close: float | None
    index_change_pct: float | None


def _empty_items() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "trade_date": pl.Date,
            "net_buy": pl.Float64,
            "buy": pl.Float64,
            "sell": pl.Float64,
            "turnover": pl.Float64,
            "accum_net_inflow": pl.Float64,
            "ggt_sh_net_buy": pl.Float64,
            "ggt_sz_net_buy": pl.Float64,
            "ggt_sh_turnover": pl.Float64,
            "ggt_sz_turnover": pl.Float64,
            "index_close": pl.Float64,
            "index_change_pct": pl.Float64,
        }
    )


def _roll_sum(values: list[float | None], window: int) -> float | None:
    """最近 ``window`` 个值的合计; 不足 ``window`` 个或其中任一为 null → ``None``。

    🔴 **两个条件都必须 fail closed**:

    - 不足窗口就返回 ``None``: 窗口只有 3 天时把 3 天加起来标成「近 5 日净买入」,
      数字看着完全正常, 没人会发现少了 2 天。
    - 含 null 就返回 ``None``: ``sum()`` 会把 null 当 0 跳过, 于是「某天上游缺数」
      会静默变成一个偏小的合计。缺数与「那天净买入恰好为 0」是两回事。
    """
    if len(values) < window:
        return None
    tail = values[-window:]
    if any(v is None for v in tail):
        return None
    return round(sum(tail), 2)  # type: ignore[arg-type]


def hk_connect_flow(
    *,
    start: date | None = None,
    end: date | None = None,
    client: EastmoneyClient | None = None,
) -> HkConnectFlowResult:
    """``[start, end]`` 内的南向 (港股通) 每日资金流 + 汇总。

    Args:
        start: 起始交易日 (含)。``None`` = 不限, 会一路取到 2014-11-17
            (8268 行 / 17 页 / ≈4s) —— **调用方应当给窗口**, API 层已设默认值与上限。
        end: 结束交易日 (含)。``None`` = 不限 (最新一日)。
        client: 复用已有的东财客户端; ``None`` 时自建并在结束时关闭。

    Returns:
        :class:`HkConnectFlowResult`。窗口内无交易日 (例如整段是假期) 时返回
        **空表 + 全 null 汇总, 而不是抛错** —— 「这段时间没有交易」是合法答案。

    Raises:
        EastmoneyError: 上游报表失败 (报表名/列名/过滤格式错误、限流、熔断)。
            **不得**降级成空表 —— 那会把「拉不到」伪装成「这段时间没有资金流」,
            而资金流为 0 与资金流未知是完全不同的两件事 (§4)。由 API 层转 502。
    """
    own_client = client is None
    if own_client:
        client = EastmoneyClient()
    try:
        daily: list[SouthboundDaily] = fetch_southbound_flow(client, start=start, end=end)
    finally:
        if own_client:
            client.close()

    if not daily:
        return HkConnectFlowResult(
            items=_empty_items(),
            currency=HSGT_CURRENCY,
            source=HSGT_SOURCE,
            start=start,
            end=end,
            trading_days=0,
            latest_net_buy=None,
            latest_trade_date=None,
            accum_net_inflow=None,
            net_buy_5d=None,
            net_buy_20d=None,
            index_close=None,
            index_change_pct=None,
        )

    # SouthboundDaily 是 NamedTuple, 字段名与 HK_CONNECT_FLOW_COLS 一一对应但顺序
    # 不同 (NamedTuple 里 net_buy 在前、index_* 在后), 故按列名显式取, 不靠位置。
    df = pl.DataFrame(
        {col: [getattr(row, col) for row in daily] for col in HK_CONNECT_FLOW_COLS},
        schema={
            "trade_date": pl.Date,
            **{col: pl.Float64 for col in HK_CONNECT_FLOW_COLS if col != "trade_date"},
        },
    ).sort("trade_date")

    # fetch_southbound_flow 已按 TRADE_DATE 升序返回, 但排序键含次键 MUTUAL_TYPE,
    # 这里再显式取一次 max, 不依赖上游排序契约。
    newest = max(daily, key=lambda row: row.trade_date)
    # 滚动窗口从**已排序的 df** 取, 不从 daily 取: 后者顺序依赖上游排序契约,
    # 一旦上游改了排序方向, 「近 5 日」会静默变成「最早 5 日」。
    net_buys = df["net_buy"].to_list()

    return HkConnectFlowResult(
        items=df.select(list(HK_CONNECT_FLOW_COLS)),
        currency=HSGT_CURRENCY,
        source=HSGT_SOURCE,
        start=start,
        end=end,
        trading_days=df.height,
        latest_net_buy=newest.net_buy,
        latest_trade_date=newest.trade_date,
        accum_net_inflow=newest.accum_net_inflow,
        net_buy_5d=_roll_sum(net_buys, HK_CONNECT_FLOW_ROLL_5D),
        net_buy_20d=_roll_sum(net_buys, HK_CONNECT_FLOW_ROLL_20D),
        index_close=newest.index_close,
        index_change_pct=newest.index_change_pct,
    )


__all__ = [
    "HK_CONNECT_FLOW_COLS",
    "HK_CONNECT_FLOW_ROLL_5D",
    "HK_CONNECT_FLOW_ROLL_20D",
    "HkConnectFlowResult",
    "hk_connect_flow",
]
