"""交易日探针 (oracle) — 回答「今天是否某市场的交易日」。

消费方 (实时行情轮询 / 盘中分钟增量) 在周几+时段门控之后调用, 用于把
「工作日但休市」的节假日从轮询窗口里剔除; 返回 None (未知) 时调用方
维持现状行为 (周几近似 + 快照新鲜度判据兜底), 不引入新依赖。

多市场: 本模块的**全部**状态与判据 (结论缓存 / single-flight key / 探针篮子 /
开盘缓冲窗 / 时区) 都按 market 分桶, 规则取自 :mod:`app.market` 规则表。
默认 ``market=Market.CN``, 因此现有调用方逐位不变。

⚠️ 缓存**必须**按市场分桶。改造前 ``_CACHE`` 是单个模块级实例: 港股探针一旦
上线, A 股先探得 ``True`` 写进缓存, 港股同一天来读就会**拿到 A 股的结论** ——
两个市场的节假日并不重合 (国庆 A 股连休、港股仅 10/1 休市; 农历新年港股休市
更长; 港股还有 A 股没有的圣诞/复活节假期)。这类串市场不会抛异常, 只会让港股
在休市日空转轮询, 或在交易日误判休市而**整天不拉行情**。

探测链 (按确定性排序, 先到先得; **链本身按市场注册**):
  1. fuyao 交易日历 (已配置 fuyao 时, **A 股专属**):
     GET /api/a-share/calendar/trading-days, 今天在近一年交易日列表内 ⇔ 交易日。
     权威日历, 无时段依赖, 无开盘缓冲问题。
  2. tickflow 实时行情时间戳: 拉一篮流动性票快照 (单请求), max(timestamp)
     日期 == 今天 ⇔ 交易日。非交易日全市场戳停在上一交易日 (2026-08-29 周六
     实测 5551/5551, 含停牌股 — 戳是快照定版时刻, 非最后成交时刻);
     交易日集合竞价阶段 (9:15-9:30) 戳是否已翻新未实测 → 开盘缓冲窗内
     戳过期不作数, 保守视为未知。
  3. 均不可用 → None: 调用方按周几近似继续。

港股链目前只有第 2 环 (fuyao 无港股日历端点), 且 tickflow 尚无港股实时权限 →
实际会稳定返回 None, 调用方退回周几近似。这是**预期的安全降级**, 不是缺陷:
本 PR 只铺多市场骨架, 港股日历接入见 docs/hk-market-plan.md 的 PR2/PR3。

安全约束:
  - 周末直接返回 False (周几判断零成本, 不打任何请求)。
  - 探针是纯读: 只产出一个布尔判定, 不落盘、不进行情管道、不碰归属链路。
  - 只用于「降档」(休市不轮询); 休市结论 TTL 较短 (30 分钟) 定期复探,
    探针误判最坏损失一段快照且可自愈; 未知结论短 TTL (5 分钟) 防止
    轮询循环每拍重打失败的探测。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.market import Market, MarketTradingRules, rules_for
from app.market_time import market_now
from app.services.shared_work import SharedWork, WorkBusyError

_TTL_TRADING_S = 3600.0   # 交易日结论每小时复探 (跨日天然失效)
_TTL_HOLIDAY_S = 1800.0   # 休市结论 30 分钟复探, 误判自愈上限
_TTL_UNKNOWN_S = 300.0    # 未知结论 5 分钟后重试探测

_CACHE_LOCK = threading.Lock()
_CACHE_EPOCH = 0
# Followers degrade to the documented "unknown" state quickly while a slow
# oracle finishes; they must not occupy the whole HTTP thread pool or probe again.
_PROBES = SharedWork("market.calendar", max_pending=8, wait_timeout_s=0.25)


@dataclass
class _Cache:
    day: object | None = None
    verdict: bool | None = None
    probed_at: float = 0.0


# 按市场分桶, 见模块 docstring 的串市场说明。
_CACHES: dict[Market, _Cache] = {market: _Cache() for market in Market}
# 向后兼容别名 = A 股那个桶。tests/test_trading_day.py 直接拨它的 probed_at。
# _Cache 是可变 dataclass, 且 reset_cache 只改字段、不重新绑定字典值,
# 所以这个别名指向的对象恒有效 (不会被换成另一个实例)。
_CACHE = _CACHES[Market.CN]

# 探测链按市场注册, 存的是**函数名字符串**而不是函数对象。
#
# 为什么用字符串: tests/test_trading_day.py 用 monkeypatch 替换
# _probe_fuyao / _probe_tickflow 来隔离真实网络。若在模块加载期就把函数对象
# 存进链里, 链持有的是**替换前**的引用, monkeypatch 不生效 → 测试会打真实
# 网络探测。按名字在调用时 getattr 才能拿到当前模块属性。
_PROBE_CHAIN_NAMES: dict[Market, tuple[str, ...]] = {
    # fuyao 日历端点是 /api/a-share/calendar/trading-days, A 股专属, 不入港股链。
    Market.CN: ("_probe_fuyao", "_probe_tickflow"),
    Market.HK: ("_probe_tickflow_hk",),
}


def _run_probe_chain(rules: MarketTradingRules, now: datetime) -> bool | None:
    """按市场的探测链依次探测, 第一个非 None 的结论即生效。"""
    module = sys.modules[__name__]
    for name in _PROBE_CHAIN_NAMES[rules.market]:
        probe: Callable[[datetime], bool | None] = getattr(module, name)
        verdict = probe(now)
        if verdict is not None:
            return verdict
    return None


def cached_verdict(now: datetime | None = None, *, market: object = Market.CN) -> bool | None:
    """Read the current day's verdict without starting network work in API readers."""
    rules = rules_for(market)
    now = now or market_now(rules.market)
    if now.weekday() >= 5:
        return False
    cache = _CACHES[rules.market]
    with _CACHE_LOCK:
        if cache.day == now.date() and time.monotonic() - cache.probed_at < _ttl_of(cache.verdict):
            return cache.verdict
    return None


def reset_cache(market: object | None = None) -> None:
    """清空探针缓存 (测试用)。``market=None`` 清空**所有**市场。"""
    global _CACHE_EPOCH
    targets = Market if market is None else (rules_for(market).market,)
    with _CACHE_LOCK:
        _CACHE_EPOCH += 1
        for target in targets:
            cache = _CACHES[target]
            cache.day = None
            cache.verdict = None
            cache.probed_at = 0.0


def _probe_fuyao(now: datetime) -> bool | None:
    """fuyao 交易日历: 今天在列表内 ⇔ 交易日。未配置 fuyao / 失败 → None。

    **A 股专属**: 端点是 ``/api/a-share/calendar/trading-days``, 返回的是 A 股
    交易日。拿它判港股会把 A 股节假日当成港股节假日 (反之亦然), 因此它只出现在
    ``_PROBE_CHAIN_NAMES[Market.CN]`` 里 —— 用「不入链」而不是「入链后判市场」
    来隔离, 这样就不存在 ``if market == "hk"`` 分支。
    """
    try:
        from app.data_providers import custom as custom_sources

        if not custom_sources.is_custom_provider("fuyao"):
            return None
        provider = custom_sources.get_provider("fuyao")
        days = provider.trading_days()
        return now.date() in days if days else None
    except Exception:  # noqa: BLE001 — 探针失败按未知处理, 不上抛
        return None


def _tickflow_verdict(now: datetime, rules: MarketTradingRules) -> bool | None:
    """tickflow 行情时间戳: max(timestamp) 日期 == 今天 ⇔ 交易日。

    戳停在上一交易日: 开盘缓冲窗内 → None (可能是竞价未翻新), 窗后 → False。
    无实时权限 / 网络失败 / 无有效戳 → None。

    探针篮子、时区、开盘缓冲窗全部取自 ``rules`` —— 这三样都是市场属性:
    篮子必须是**该市场**的流动性票 (拿 A 股票探港股, 港股休市日 A 股照常交易,
    戳永远是今天 → 恒判 True, 港股节假日门控彻底失效); 时区决定戳落到哪一天;
    缓冲窗对应该市场的集合竞价结束时刻。
    """
    try:
        from app.tickflow.client import get_client

        rows = get_client().quotes.get(symbols=list(rules.probe_basket)) or []
        stamps = [r.get("timestamp") for r in rows if isinstance(r, dict)]
        valid = [int(t) for t in stamps if isinstance(t, (int, float)) and t]
        if not valid:
            return None
        latest_day = datetime.fromtimestamp(max(valid) / 1000, tz=rules.tz).date()
        if latest_day == now.date():
            return True
        if now.time() < rules.stale_buffer_until:
            return None
        return False
    except Exception:  # noqa: BLE001 — 无权限/网络失败按未知处理
        return None


def _probe_tickflow(now: datetime) -> bool | None:
    """A 股 tickflow 戳探针。签名保持 ``(now)`` 单参以便 monkeypatch 拦截。"""
    return _tickflow_verdict(now, rules_for(Market.CN))


def _probe_tickflow_hk(now: datetime) -> bool | None:
    """港股 tickflow 戳探针。

    tickflow 目前没有港股实时权限 → 稳定返回 None, 港股退回周几近似。
    接入港股行情源后此函数无需改动, 自动开始产出结论。
    """
    return _tickflow_verdict(now, rules_for(Market.HK))


def is_trading_day(
    now: datetime | None = None, *, market: object = Market.CN
) -> bool | None:
    """今天是否该市场交易日。True=交易日, False=确定休市, None=未知 (维持周几近似)。

    周末零成本直判; 工作日走该市场的探测链 (A 股: fuyao 日历 → tickflow 时间戳;
    港股: 仅 tickflow), 结论按 market 分桶、按 TTL 缓存。
    线程安全: 实时行情与分钟增量两个线程共用。
    """
    rules = rules_for(market)
    now = now or market_now(rules.market)
    if now.weekday() >= 5:
        return False

    cache = _CACHES[rules.market]
    with _CACHE_LOCK:
        epoch = _CACHE_EPOCH
        # 「未知」(None) 也是一个结论, 同样按 TTL 缓存 —— 它正是 _TTL_UNKNOWN_S 要
        # 挡住的场景 (未配 fuyao 且 tickflow 不可用时, 轮询每拍都会重打一次探测)。
        # cache.day 只在探测写回时设置, 因此「当天已探过」用它判定即可。
        if (
            cache.day == now.date()
            and (time.monotonic() - cache.probed_at) < _ttl_of(cache.verdict)
        ):
            return cache.verdict

    def probe():
        # A caller can reach single-flight just after another probe retired.
        with _CACHE_LOCK:
            if (cache.day == now.date()
                    and time.monotonic() - cache.probed_at < _ttl_of(cache.verdict)):
                return cache.verdict
        verdict = _run_probe_chain(rules, now)

        with _CACHE_LOCK:
            if epoch != _CACHE_EPOCH:
                return None
            cache.day = now.date()
            cache.verdict = verdict
            cache.probed_at = time.monotonic()
        return verdict

    try:
        # key 必须带 market: 否则 A 股与港股在同一天会命中同一个 single-flight
        # 条目, 后到的市场直接拿走先到市场的结论 (与 _CACHES 分桶同因)。
        return _PROBES.run((rules.market.value, now.date(), epoch), probe)
    except WorkBusyError:
        return None


def _ttl_of(verdict: bool | None) -> float:
    if verdict is True:
        return _TTL_TRADING_S
    if verdict is False:
        return _TTL_HOLIDAY_S
    return _TTL_UNKNOWN_S


def next_trading_day(now: datetime | None = None, *, market: object = Market.CN) -> date:
    """下一个交易日 (自 tomorrow 起, 跳过周末)。

    未来节假日无公开交易日历可查 (fuyao 交易日历仅覆盖 [今日-1年, 今日],
    且只有 A 股), 故按周一~周五近似 —— 与 assistant 侧「无交易日历, 按周一~
    周五近似, 节假日会高估」口径一致。仅用于侧栏「下一交易日」轻提示,
    非交易判定依据。

    ``market`` 只影响「今天」是哪一天 (各市场时区), 不影响跳周末的逻辑
    (A 股与港股的周末都是周六日)。港股节假日与 A 股不重合, 因此对港股这个
    近似偏差更大 —— 消费方不得把它当作港股的权威交易日。
    """
    rules = rules_for(market)
    d = (now or market_now(rules.market)).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
