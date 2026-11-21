"""腾讯港股**个股日K** — ``ifzq.gtimg.cn`` 的不复权日线 (方案 §3.1)。

为什么需要这个模块 (而不是继续用 :func:`app.plugins.eastmoney.hk.fetch_hk_kline`):
东财 ``push2`` 集群 (含 ``push2his`` 日K镜像) 对本机 **IP 级封禁** (2026-11 实测:
DNS/TCP/TLS 全通、证书有效、ALPN 协商成功, 随后服务端直接关闭连接不返回任何 HTTP
响应)。封禁按**集群**生效, 故 ``datacenter-web`` (F10 财报/分红) 仍可用 —— 这也是
为什么本模块只接管**日K行情**, 复权因子仍走
:func:`app.plugins.eastmoney.hk_dividend.fetch_hk_dividend_events`。

腾讯 ``ifzq.gtimg.cn/appstock/app/kline/kline`` 实测可用, 且返回的就是**不复权原始
成交价**, 符合项目契约「落库原始价, 复权在读端按除权因子重建」。

============ ======================================================
端点          语义
============ ======================================================
``kline/kline``      **不复权**原始价, 6 列, 无成交额 ← 本模块只用这个
``hkfqkline/get``    等差复权价, 9 列, 尾部带成交额 (🔴 价格不可用, 见下)
``fqkline/get``      对港股忽略 fq 后缀, 等价于 ``kline/kline``
============ ======================================================

🔴 **列序是 ``[date, open, CLOSE, high, low, volume]`` —— 收盘在 index 2**, 不是
常见的 OHLC 序 (与 :meth:`TencentClient.kline_m1` / 港股指数日K 同款反直觉排列)。
按 ``row[2]`` 当最高价会让上下影线全错、MA/ATR 全偏, 而数据「看起来还挺像」。
同一陷阱在 :func:`app.services.index_sync_hk._parse_tencent_daily_rows` 另有一份
实现 (指数链路先落地、已真机验证, 刻意不回头重构它以免连累可用链路)。

🔴 **``count`` 服务端硬上限 = 2000, 且超限是静默失败**: 实测 ``count=2000`` 正常
返回 2000 行, ``count=2500/3000/5000`` 一律返回 ``code=0`` + **0 行**, 不报错。
「要太多就什么都拿不到」比报错危险得多 —— 会被当成「该标的无数据」而静默跳过。
故 :data:`HK_KLINE_PAGE_SIZE` 钉在 2000, 且 :meth:`TencentClient.kline_daily`
的 clamp 是**承重**的, 不是防御性冗余。

🔴 **日期区间 + count 组合返回的是区间「尾部」count 根**, 不是从头开始:
实测 ``hk00005,day,2020-01-01,2026-11-21,800`` → 800 行但只覆盖 2023-08-23 起。
因此全量回填必须**从近往远翻页** (每页取回后把 ``end`` 改成「本页最早日 − 1 天」),
从远往近翻会永远拿同一段最近数据、死循环到 max_pages。实测翻页零重叠零缝隙:
2018-10-10..2026-11-20 / 2010-08-27..2018-10-09 / 2002-07-29..2010-08-26。

🔴 **不要用 ``hkfqkline`` 的价格**: 它是**等差** (additive) 复权, 不是等比。实测
汇丰 ``00005`` 2026-10-26: raw 开 142.300 / qfq 开 141.516, raw 收 142.200 /
qfq 收 141.416 —— 差恒为 **0.784**; 而近期无分红处 (2026-11-18~20) raw 与 qfq
逐位相同。比值 ``qfq/raw`` 随价格水平漂移 ⇒ **反推不出乘性因子**。同一结论在
:func:`app.plugins.eastmoney.hk.fetch_hk_kline` 的 docstring 里另有记录 (在汇丰身上
按比值差法产出 **207 个假除权事件**, 真事件约 3 个)。项目走东财 F10 分红事件自建
因子, 见 :mod:`app.plugins.eastmoney.hk_dividend`。

单位口径 (CONTRIBUTING §3):
- ``volume`` 产出**股**, 与东财港股原生单位一致 (A 股是手)。交叉验证: 腾讯日K
  2026-11-20 ``hk00700`` 量 ``41094469.000`` 与腾讯快照 ``[6]`` ``41094469.0``
  **逐位相同**, 而快照该位已由 :mod:`app.plugins.tencent.hk_spot` 钉死为股。
  🔴 原样透传, 不得乘除折算。下游换算走 :func:`app.market.shares_per_volume_unit`。
- 价格产出 **HKD**, 不做汇率换算 (换算即伪造)。

原始价的可信度交叉验证 (2026-11-21 实测, 两个独立腾讯端点互证):
``hk00005`` 快照昨收 133.300 == raw 前一日收 133.300, 快照今开 130.400 == raw
当日开 130.400; ``hk00700`` 同样逐位吻合 (348.400 / 341.000)。

刻意不做:
- **不产出 ``amount`` (成交额) 列**。``kline/kline`` 只有 6 列, 上游不提供;
  填 0 或复制 volume 都是编造数据 (与 ``index_sync_hk`` 同一纪律)。缺 amount 是
  安全的: 全链路 amount 均为可选列 (``_normalize_daily`` 按 ``if c in df.columns``
  裁剪, ``append_hk_*`` 同, ``pipeline`` 无 VWAP 指标), ``filter_halt_days`` 的
  ``(volume==0)&(amount==0)`` 判据仅在两列都存在时生效, 此时退化为只看
  ``open/high==0``。
  ⚠️ **若将来确实需要成交额**, 已探明可行路径 (2026-11-21 实测, 勿重复调研):
  ``hkfqkline/get?param={code},day,,,{n},qfq`` 的 ``qfqday`` 行**第 9 列**即成交额,
  单位**万元 (10k HKD)** —— 交叉验证 ``hk00700`` 2026-11-20: 1408999.287 万元
  ÷ 41094469 股 = 342.9 HKD/股, 落在当日 [339.6, 349.6] 内; 且与快照 ``[37]``
  (单位**元**) 恰好差 1e4 倍。该端点的 **volume 与 ``kline/kline`` 逐行完全一致**
  (3 只标的各 320 根, 日期 320/320 全对齐、量 0 处不一致), 故可按 date join 取额、
  按 ``kline/kline`` 取价。代价是**每标的 2 个请求** (全市场约 6 分钟 → 12 分钟),
  在 ifzq 是唯一可用源的前提下翻倍请求量不划算, 故当前不做。第 8 列是换手率 %
  (``hk00700`` 0.450 ≈ 41094469/9.19e9), 属上游派生值, 按项目纪律不采用。
- **不处理港股指数** (``hkHSI`` 等)。指数已有独立链路
  :mod:`app.services.index_sync_hk` (新浪快照 + 腾讯日K + 白名单闸门), 且
  :func:`app.plugins.tencent.hk_spot.hk_spot_code` 对非 5 位数字代码段返回
  ``None``, 天然挡住指数代码。
- **不做跨源回退**。与 :mod:`app.plugins.tencent.hk_spot` 同一决策: 硬切源而非
  运行时兜底 —— 东财那条链路在封禁解除前是**必然失败**, 留着只会让每轮同步白等
  一次超时; 东财原函数保留且仍被测试钉住, 解封后可整体切回。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta

import polars as pl

from app.market import SymbolMarketMismatchError
from app.plugins.tencent.client import KLINE_DAILY_MAX, TencentClient
from app.plugins.tencent.hk_spot import hk_spot_code

logger = logging.getLogger(__name__)

#: 单页根数。🔴 **直接取传输层的 :data:`KLINE_DAILY_MAX`** (= 服务端硬上限 2000),
#: 不另立一个数字: 若本常量大于传输层 clamp, 客户端实际只请求到 clamp 根, 而下面的
#: 翻页终止条件 ``len(page) < size`` 会在**第一页**就误判为「已到头」, 静默只落
#: 800 根且不报错 —— 两个常量必须同源。
HK_KLINE_PAGE_SIZE = KLINE_DAILY_MAX

#: 翻页数上限 = 2000 × 60 = 12 万根 ≈ 480 年交易日, 纯「跑飞保护」。
#: 触发即说明翻页终止条件失效 (上游改了区间语义), 记 WARNING 让人能发现,
#: 与 :meth:`EastmoneyClient.datacenter_report_all` 的 ``max_pages`` 同一纪律。
HK_KLINE_MAX_PAGES = 60

#: 腾讯日K单行最少列数: ``[date, open, close, high, low, volume]``。
#: 用「不少于」而非「等于」: 有除权/回购事件的行尾部会多挂一个 dict
#: (``{'cqr':…, 'FHcontent':…, 'HGcontent':…}``), 实测 ``hk00005`` 2026-11-18 即 7 列。
_TENCENT_DAILY_ROW_LEN = 6


def fetch_hk_kline_rows(
    client: TencentClient,
    symbol: str,
    *,
    beg: date | None = None,
    end: date | None = None,
    page_size: int = HK_KLINE_PAGE_SIZE,
    max_pages: int = HK_KLINE_MAX_PAGES,
) -> list[list]:
    """单只港股日K → 原始行列表, **按日期升序** (与东财原路径同序)。

    ``beg``/``end`` 为 ``None`` 时该侧留空 (腾讯接受空串, 语义是「不限」)。
    非港股 symbol → :class:`SymbolMarketMismatchError`; 网络/上游失败 →
    :class:`app.plugins.tencent.client.TencentError` (由 client 抛出后透传)。
    两者刻意分开, 供调用方分别告警 (见下)。

    🔴 翻页方向必须是**从近往远**: 上游对「区间 + count」返回的是区间**尾部**
    count 根 (见模块 docstring)。每页取回后把下一页的 ``end`` 设为「本页最早日
    − 1 天」, 直到某页不足 ``page_size`` 根 (说明已触到 ``beg`` 或上市日)。

    ``max_pages`` 只是跑飞保护, 正常标的远达不到 (最老的港股约 7 页)。
    """
    code = hk_spot_code(symbol)
    if code is None:
        # 与东财原路径 (``hk.fetch_hk_kline`` → ``assert_symbol_market``) 同一异常
        # 类型: 调用方据此把「维表脏数据」与「网络失败」分开告警 —— 前者是永久性的
        # 数据完整性问题, 后者下一轮自愈, 混成一条日志运维就分不出来了。
        raise SymbolMarketMismatchError(f"非港股股票代码, 拒绝走港股日K通道: {symbol!r}")
    size = max(1, min(int(page_size), HK_KLINE_PAGE_SIZE))

    out: list[list] = []
    pages: list[list] = []
    cursor = end
    for page_no in range(1, max_pages + 1):
        page = client.kline_daily(
            code,
            count=size,
            beg=beg.isoformat() if beg is not None else None,
            end=cursor.isoformat() if cursor is not None else None,
        )
        if not page:
            break
        pages.append(page)
        if len(page) < size:
            break
        if page_no == max_pages:
            logger.warning(
                "腾讯港股日K %s 翻到 max_pages=%d 上限仍未结束, 已截断 (累计 %d 行) "
                "— 上游分页语义可能变了, 需人工复核",
                symbol, max_pages, sum(len(p) for p in pages),
            )
            break
        try:
            earliest = date.fromisoformat(str(page[0][0]).strip())
        except (ValueError, IndexError, TypeError) as e:
            logger.warning("腾讯港股日K %s 首页日期无法解析, 停止翻页: %r", symbol, e)
            break
        nxt = earliest - timedelta(days=1)
        if beg is not None and nxt < beg:
            break
        cursor = nxt
    # 🔴 只反转**页序**, 不动页内顺序: 每页自身是日期升序的, 页与页之间才是由近及远。
    # 直接 ``out.reverse()`` 会把页内也翻成降序 —— 落库帧就不是升序了, 而下游
    # MA/MACD 与增量落盘都按「帧内日期升序」假设工作。
    for pg in reversed(pages):
        out.extend(pg)
    return out


def parse_hk_kline_rows(symbol: str, rows: Sequence[Sequence]) -> pl.DataFrame:
    """腾讯日K原始行 → canonical 日K frame (``symbol/date/OHLC/volume``)。

    🔴 **收盘在 index 2** (列序 ``[date, open, CLOSE, high, low, volume]``),
    见模块 docstring。本函数是该列序陷阱在个股链路的唯一居所。

    日期解析失败的行**跳过并告警**, 不兜底成今天 —— 兜底会把历史数据写成当日数据,
    且 ``date=null`` 会产出 ``date=null`` 分区目录污染存储。

    ⚠️ 腾讯所有数值以**字符串**下发, 此处即 cast 成 Float64: 产出的必须是类型正确
    的 canonical 帧。留给下游 ``_normalize_daily`` 转也能工作 (它先 cast 再
    ``filter_halt_days``, 顺序刚好救场), 但任何绕过它直接把本函数输出喂给
    ``compute_enriched`` 的调用方都会炸在字符串列上。收盘价 cast 后为 null 的行
    整根丢弃 —— 缺 close 的 K 线会污染 MA/MACD。

    🔴 **volume 必须非空**: polars 里 ``null==0`` 求值为 null, ``~null`` 仍是 null,
    ``filter_halt_days`` 会把整帧当停牌滤空 (指数链路实测 58 行 → 0 行)。
    """
    recs: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < _TENCENT_DAILY_ROW_LEN:
            logger.warning(
                "腾讯港股日K %s 行字段数不足 %d, 跳过: %r",
                symbol, _TENCENT_DAILY_ROW_LEN, row,
            )
            continue
        try:
            day = date.fromisoformat(str(row[0]).strip())
        except ValueError:
            logger.warning("腾讯港股日K %s 日期无法解析, 跳过该行: %r", symbol, row[0])
            continue
        recs.append(
            {
                "symbol": symbol,
                "date": day,
                "open": row[1],
                "close": row[2],  # ← 收盘在第 3 列
                "high": row[3],
                "low": row[4],
                "volume": row[5],
                # 刻意不造 amount: 上游 6 列端点不提供, 填 0/复制 volume 都是编造数据
            }
        )
    if not recs:
        return pl.DataFrame()
    frame = pl.DataFrame(recs, infer_schema_length=None).with_columns(
        [pl.col(c).cast(pl.Float64, strict=False) for c in ("open", "high", "low", "close", "volume")]
    )
    bad = frame.filter(pl.col("close").is_null()).height
    if bad:
        logger.warning("腾讯港股日K %s 有 %d 行收盘价无法解析为数值, 已丢弃", symbol, bad)
        frame = frame.filter(pl.col("close").is_not_null())
    return frame
