"""市场时间工具 — 时段/交易分钟数按 market 查表, 固定 UTC+8 无夏令时。

服务器/容器本地时区不可靠 (python:slim 镜像默认 UTC), 交易时段判断、
实时行情落盘日期等必须显式使用市场本地时间, 否则 Docker 部署时轮询窗口
与真实交易时段完全错开 (北京 9:15-15:05 = UTC 1:15-7:05)。

多市场
------
时段与总分钟数不再写成本模块常量, 而是查 :mod:`app.market` 的规则表:

- A 股: 9:30-11:30 + 13:00-15:00 = 240 分钟
- 港股: 9:30-12:00 + 13:00-16:00 = 330 分钟, 且**有半日市** (只交易早盘)

所有函数都带 ``market="cn"`` 默认参数 → 既有 46 处 ``cn_today``、44 处
``CN_TZ``、18 处 ``cn_now`` 及其它消费方**零改动**, A 股行为逐位不变。

⚠️ 传入的 ``datetime`` 必须**已经是该市场的本地时间**。本模块不做时区换算
(保持与改造前完全一致的行为); 需要换算请在调用侧 ``astimezone(rules.tz)``。
唯一例外是 :func:`trading_minutes_elapsed_from_ts`, 它从 Unix 时间戳构造
datetime, 会按市场规则表的 tz 落地。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time

from app.market import Market, rules_for, sessions_for

CN_TZ = timezone(timedelta(hours=8))
"""北京时间 (UTC+8)。**保留导出**: 全仓 44 处引用。

沪深与港交所同为 UTC+8, 因此该常量对两个市场都适用; 需要按市场取时区时
用 ``rules_for(market).tz``。
"""

# A 股交易时段 (北京时间): 上午 9:30-11:30 (120 分钟) + 下午 13:00-15:00 (120 分钟) = 240 分钟
#
# ⚠️ 以下四个常量与 _TRADING_TOTAL_MINUTES 已**不再是判定依据** —— 真源是
# app.market 的规则表 (支持港股与半日市)。保留它们只为兼容可能的外部引用与
# 可读性; 新代码请读 rules_for(market).sessions。
_TRADING_TOTAL_MINUTES = 240
_MORNING_START = dt_time(9, 30)
_MORNING_END = dt_time(11, 30)
_AFTERNOON_START = dt_time(13, 0)
_AFTERNOON_END = dt_time(15, 0)


def cn_now() -> datetime:
    """当前北京时间 (带时区)。"""
    return datetime.now(CN_TZ)


def cn_today() -> date:
    """当前北京日期。"""
    return datetime.now(CN_TZ).date()


def market_now(market: object = Market.CN) -> datetime:
    """当前市场本地时间 (带时区)。``market="cn"`` 时等价于 :func:`cn_now`。"""
    return datetime.now(rules_for(market).tz)


def market_today(market: object = Market.CN) -> date:
    """当前市场本地日期。``market="cn"`` 时等价于 :func:`cn_today`。"""
    return datetime.now(rules_for(market).tz).date()


def in_continuous_session(now: datetime | None = None, market: object = Market.CN) -> bool:
    """连续竞价时段内 (市场本地时间), 仅工作日。**半日市感知**。

    - A股: 9:30-11:30 / 13:00-15:00
    - 港股: 9:30-12:00 / 13:00-16:00; 半日市只有早盘 (下午返回 False)

    ``now`` 为 None 时取该市场的当前本地时间。
    """
    rules = rules_for(market)
    now = now or datetime.now(rules.tz)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(s.start <= t <= s.end for s in sessions_for(rules.market, now.date()))


def trading_minutes_elapsed_from_dt(dt: datetime, market: object = Market.CN) -> float:
    """根据市场本地 datetime 计算当日已交易分钟数。**半日市感知**。

    A 股交易时段: 9:30-11:30 (0~120) + 13:00-15:00 (120~240)。
    港股交易时段: 9:30-12:00 (0~150) + 13:00-16:00 (150~330)。

    - 开盘前 = 0; 午休 = 上午累计 (A股 120 / 港股 150); 收盘后 = 当日总分钟数。
    - 非交易日(周末) = 当日总分钟数 (视作全天, 避免量比被折算成 0)。
    - 半日市 = 早盘长度 (A股无半日市; 港股 150)。

    量比 (volume ratio) 的分母。**用全日市常量会在半日市把量比算错**
    (港股半日市 12:00 收市, 若按 330 分钟折算, 量比会低估约 2.2 倍)。
    """
    rules = rules_for(market)
    if dt.weekday() >= 5:
        return float(rules.trading_total_minutes)
    t = dt.time()
    accumulated = 0.0
    for session in sessions_for(rules.market, dt.date()):
        if t < session.start:
            return accumulated
        if t <= session.end:
            elapsed = (
                t.hour * 60 + t.minute - session.start.hour * 60 - session.start.minute
            )
            return accumulated + elapsed + dt.second / 60.0
        accumulated += session.minutes
    return accumulated


def trading_minutes_elapsed(market: object = Market.CN) -> float:
    """当前已交易分钟数 (基于服务端的市场本地时间)。

    量比折算的兜底: 当行情 timestamp 缺失时用服务端时间。
    优先使用 trading_minutes_elapsed_from_ts (行情真实时间, 更准)。
    """
    return trading_minutes_elapsed_from_dt(datetime.now(rules_for(market).tz), market)


def trading_minutes_elapsed_from_ts(ts_ms: int | float | None, market: object = Market.CN) -> float:
    """从行情时间戳(毫秒)计算当日已交易分钟数。

    优先使用此函数: 行情 timestamp 是真实成交时间, 比服务端时间更准
    (服务端时间含网络/限流延迟)。

    Args:
        ts_ms: 毫秒级 Unix 时间戳 (TickFlow SDK quote.timestamp / kline.timestamp)
        market: 市场 (决定落地时区与交易时段)

    Returns:
        已交易分钟数 (A股 0~240 / 港股 0~330, 半日市更少)。timestamp 为
        None/无效时返回当日总分钟数 (视作全天, 避免量比被折算成 0)。
    """
    rules = rules_for(market)
    if not ts_ms:
        return float(rules.trading_total_minutes)
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=rules.tz)
    except (ValueError, TypeError, OSError):
        return float(rules.trading_total_minutes)
    return trading_minutes_elapsed_from_dt(dt, rules.market)
