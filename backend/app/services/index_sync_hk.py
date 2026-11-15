"""港股指数同步 → ``data/kline_hk_index_daily/`` + 实时快照 (PR3, 方案 §11)。

与 A 股 :mod:`app.services.index_sync` **物理与代码双隔离** (方案 §4 闸3), 且与
港股**个股**链路 :mod:`app.services.kline_sync_hk` 用的**不是同一个数据源**:

============ ==================== =========================================
链路          源                   为什么
============ ==================== =========================================
港股个股      东财 push2            用户决策「行情/K线直连东财」
港股指数      新浪快照 + 腾讯日K     见下三条
============ ==================== =========================================

① 东财 ``push2`` 集群 (含 ``82./17./push2delay/push2his`` 四个镜像) 2026-11 实测
   **IP 级封禁**: DNS/TCP/TLS 1.3 全通、证书有效、ALPN 协商成功, 随后服务端直接
   关闭连接不返回任何 HTTP 响应 (``curl: (52) Empty reply``, ``http=000``)。
   浏览器 UA + Referer 无效。封禁是**按集群**而非按域名, 故 ``datacenter-web``
   (F10 财报) 仍可用。
② 新浪指数**日K** (``finance.sina.com.cn/stock/hkstock/{sym}/klc2_kl.js``) 需要
   ``py_mini_racer`` 执行 ``hk_js_decode`` 解密 —— 正是方案 §3.5 警告的 V8 /
   aarch64 依赖陷阱, **刻意不引入**。
③ 腾讯日K 实测可用, 且 OHLC 与新浪快照**逐位吻合** (HSI 2026-11-13:
   开 23871.910 / 收 23637.600 / 高 23930.130 / 低 23595.360 两源完全相同)。

复用的**纯函数** (与市场/路由无关, 与 ``kline_sync_hk`` 同款复用口径):

- ``kline_sync._normalize_daily``  规范化 + 停牌日过滤 + canonical 列裁剪
- ``repo.append_daily_asset("index", df, market=Market.HK)``  按日分区 merge-upsert
- ``repo.append_hk_index_enriched``  enriched 窄表落盘
- ``indicators.pipeline.compute_enriched(..., market=Market.HK)``

🔴 指数代码**过不了股票符号校验**: ``infer_market("HSI")`` 返回 ``Market.CN``
(兜底分支), ``is_valid_symbol("HSI", Market.HK)`` 为 ``False``。本模块所有落库
一律**显式传 ``market=Market.HK``**, 从不调用 ``infer_market``。详见
:mod:`app.plugins.sina.hk_index` 坑 5。

🔴 量额单位两源矛盾 (新浪 195857007 vs 腾讯 195857007140, 恰好 ×1000), 因此
**原值透传、严禁乘除折算**。腾讯日K 不提供成交额 → 本模块**不造 amount 列**
(填 0 或复制 volume 都是编造数据)。缺 amount 是安全的: ``filter_halt_days`` 的
``(volume==0)&(amount==0)`` 判据仅在两列都存在时生效, 此时退化为只看
``open/high==0``。但 volume **必须非空** —— polars 里 ``null==0`` 求值为 null,
``~null`` 仍是 null, 整帧会被当停牌滤空 (实测 58 行 → 0 行)。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from datetime import date

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.market import Market
from app.plugins.sina import hk_index as sina_hk_index
from app.plugins.sina.client import SinaClient
from app.plugins.tencent.client import TencentClient, TencentError
from app.services.kline_sync import _normalize_daily

logger = logging.getLogger(__name__)

#: 单标的日K默认回填根数。实测腾讯 ``count=900`` 仍正常返回 (hkHSI 回溯到
#: 2023-03-17), 无低上限; 取 400 覆盖 A 股指数同步的 365 天默认窗口并留余量。
HK_INDEX_DAILY_COUNT = 400

#: 腾讯港股指数代码前缀 (``hkHSI``)。与新浪的 ``hk`` 前缀同形但**各自独立定义**:
#: 两个源的代码空间将来可能分叉, 共用一个常量会让一处改动静默波及另一处。
_TENCENT_INDEX_PREFIX = "hk"

#: 腾讯日K单行列数: ``[date, open, close, high, low, volume]``。
_TENCENT_DAILY_ROW_LEN = 6

#: 单标的日K拉取结果三态。**必须区分「源无覆盖」与「拉取失败」**:
#: 实测腾讯港股指数只覆盖 27 个白名单代码中的 14 个 (中证香港系列 CSHK*/CSRHK50、
#: 标普 HKL、恒生 HSIDI/HSSCID/HSSCPB 均返回 0 行), 把「无覆盖」记成失败会让
#: 调度器每轮都告警、并误判为部分失败而跳过下游 —— 重蹈 PR2 ``sync_hk_adj_factor``
#: 「部分失败但存储非空 → 三态闸门误判完整」的覆辙。
STATUS_OK = "ok"
STATUS_NO_COVERAGE = "no_coverage"
STATUS_ERROR = "error"


def hk_index_universe(symbols: Iterable[str] | None = None) -> list[str]:
    """要同步的港股指数代码 (排序 → URL 与落盘顺序可复现)。

    ``symbols=None`` → :data:`~app.plugins.sina.hk_index.HK_INDEX_WHITELIST` 全集。
    显式传入时**必须全部在白名单内**, 否则 ``ValueError`` —— fail closed:
    静默丢弃会让调用方以为同步了 N 个而实际只同步了 M 个。
    """
    if symbols is None:
        return sorted(sina_hk_index.HK_INDEX_WHITELIST)
    wanted = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not wanted:
        raise ValueError("港股指数同步: symbols 传入了空集合")
    unknown = [s for s in wanted if s not in sina_hk_index.HK_INDEX_WHITELIST]
    if unknown:
        raise ValueError(
            f"港股指数同步: 代码不在白名单内 {unknown[:5]} (共 {len(unknown)} 个)。"
            "白名单是唯一允许写入 kline_hk_index_daily 的集合, 见 plugins/sina/hk_index.py"
        )
    return wanted


def _parse_tencent_daily_rows(symbol: str, rows: Sequence[Sequence]) -> pl.DataFrame:
    """腾讯日K原始行 → canonical 日K frame (``symbol/date/OHLC/volume``)。

    🔴 **列序是 ``[date, open, CLOSE, high, low, volume]`` —— 收盘在 index 2**,
    不是常见的 OHLC 序 (与 ``TencentClient.kline_m1`` 同款反直觉排列)。按 ``row[2]``
    当最高价会让上下影线全错、MA/ATR 全偏。

    日期解析失败的行**跳过并告警**, 不兜底成今天 —— 兜底会把历史数据写成当日数据,
    且 ``date=null`` 会产出 ``date=null`` 分区目录污染存储。

    ⚠️ 腾讯所有数值以**字符串**下发, 此处即 cast 成 Float64: 本函数是列序陷阱的
    唯一居所, 产出的必须是类型正确的 canonical 帧。留给下游 ``_normalize_daily``
    转也能工作 (它先 cast 再 ``filter_halt_days``, 顺序刚好救场), 但任何绕过它
    直接把本函数输出喂给 ``compute_enriched`` 的调用方都会炸在字符串列上。
    收盘价 cast 后为 null 的行整根丢弃 —— 缺 close 的 K 线会污染 MA/MACD。
    """
    recs: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < _TENCENT_DAILY_ROW_LEN:
            logger.warning("港股指数 %s 日K行字段数不足 %d, 跳过: %r", symbol, _TENCENT_DAILY_ROW_LEN, row)
            continue
        try:
            day = date.fromisoformat(str(row[0]).strip())
        except ValueError:
            logger.warning("港股指数 %s 日K日期无法解析, 跳过该行: %r", symbol, row[0])
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
                # 刻意不造 amount: 腾讯日K不提供, 填 0/复制 volume 都是编造数据
            }
        )
    if not recs:
        return pl.DataFrame()
    frame = pl.DataFrame(recs).with_columns(
        [pl.col(c).cast(pl.Float64, strict=False) for c in ("open", "high", "low", "close", "volume")]
    )
    bad = frame.filter(pl.col("close").is_null()).height
    if bad:
        logger.warning("港股指数 %s 日K有 %d 行收盘价无法解析为数值, 已丢弃", symbol, bad)
        frame = frame.filter(pl.col("close").is_not_null())
    return frame


def iter_hk_index_daily_frames(
    client: TencentClient,
    symbols: Sequence[str],
    count: int = HK_INDEX_DAILY_COUNT,
) -> Iterator[tuple[str, pl.DataFrame, str]]:
    """逐标的产出 ``(symbol, frame, status)``, **每个 symbol 都产出一项**。

    status 三态见 :data:`STATUS_OK` / :data:`STATUS_NO_COVERAGE` / :data:`STATUS_ERROR`。
    单标的异常**不中断整批** (告警后继续), 由调用方按 status 分类统计。

    与 A 股批量接口不同, 腾讯日K是单标的端点 → 天然逐个请求。限速/熔断/重试
    由 :class:`TencentClient` 自己持有 (令牌桶 + 熔断器按主机分桶), 此处不叠加 sleep。
    """
    for symbol in symbols:
        code = f"{_TENCENT_INDEX_PREFIX}{symbol}"
        try:
            rows = client.kline_daily(code, count=count)
        except TencentError as e:
            logger.warning("港股指数 %s 日K拉取失败: %s", symbol, e)
            yield symbol, pl.DataFrame(), STATUS_ERROR
            continue
        frame = _parse_tencent_daily_rows(symbol, rows)
        if frame.is_empty():
            # 源返回 0 行 = 腾讯不覆盖该指数, 不是故障 → INFO 而非 WARNING
            logger.info("港股指数 %s 腾讯无日K覆盖 (返回 %d 行), 跳过", symbol, len(rows))
            yield symbol, pl.DataFrame(), STATUS_NO_COVERAGE
            continue
        yield symbol, frame, STATUS_OK


def sync_hk_index_daily(
    repo,
    *,
    symbols: Iterable[str] | None = None,
    count: int | None = None,
    client: TencentClient | None = None,
    on_progress=None,
) -> tuple[int, list[str]]:
    """同步港股指数日K + enriched → ``kline_hk_index_daily/`` / ``kline_hk_index_enriched/``。

    返回 ``(写入行数, 失败代码列表)``。**失败列表只含真正的拉取错误**
    (:data:`STATUS_ERROR`), 不含「源无覆盖」—— 后者是腾讯指数覆盖面的客观事实
    (27 个白名单代码中仅 14 个有日K), 记为失败会让调用方每轮误判。
    无覆盖的代码单独以 INFO 汇总, 其**实时快照仍可用** (新浪覆盖全部 27 个)。

    on_progress: ``(done, total)`` 回调, 与 ``sync_hk_daily`` 同签名。
    client: 复用外部 ``TencentClient`` (共享令牌桶); None 时自建并在 finally 关闭。

    ⚠️ enriched 必须用 ``market=Market.HK`` 生成 (换手率的 volume 单位系数按市场
    取值)。指数无流通股本 → ``instruments=None``, 不产换手率/涨跌停信号列。
    """
    universe = hk_index_universe(symbols)
    limit = int(count) if count else HK_INDEX_DAILY_COUNT
    total = len(universe)

    own_client = client is None
    if own_client:
        client = TencentClient()

    written = 0
    failed: list[str] = []
    no_coverage: list[str] = []
    frames: list[pl.DataFrame] = []
    done = 0
    try:
        for symbol, frame, status in iter_hk_index_daily_frames(client, universe, limit):
            done += 1
            if status is STATUS_OK:
                frames.append(frame)
            elif status is STATUS_NO_COVERAGE:
                no_coverage.append(symbol)
            else:
                failed.append(symbol)
            if on_progress is not None:
                on_progress(done, total)

        if frames:
            raw = _normalize_daily(pl.concat(frames, how="vertical_relaxed"))
            if not raw.is_empty():
                # 显式 market=Market.HK —— 指数代码过不了 infer_market (模块 docstring)
                repo.append_daily_asset("index", raw, market=Market.HK)
                enriched = compute_enriched(raw, factors=None, instruments=None, market=Market.HK)
                repo.append_hk_index_enriched(enriched)
                written = raw.height
    finally:
        if own_client:
            client.close()

    # 无论成功与否都刷视图: 部分成功时若不刷, 已写入的行对 SQL 读端不可见
    # (CONTRIBUTING §「写入后存在已知缓存不一致」是拒收情形)。
    repo.refresh_hk_index_views()

    if no_coverage:
        logger.info(
            "港股指数日K: %d/%d 个代码腾讯无覆盖 (实时快照仍可用) %s",
            len(no_coverage), total, no_coverage,
        )
    if failed:
        logger.warning("港股指数日K: %d/%d 个代码拉取失败 %s", len(failed), total, failed[:8])
    if written:
        logger.info("港股指数日K同步完成: %d 行 / %d 个代码", written, len(frames))
    else:
        logger.warning("港股指数日K同步写入 0 行 (请求 %d 个代码)", total)
    return written, failed


def fetch_hk_index_quotes(
    client: SinaClient | None = None,
    *,
    symbols: Iterable[str] | None = None,
) -> list[dict]:
    """港股指数实时快照 → 已归一化的行情记录 (键见 ``map_hk_index_quote``)。

    薄封装: 白名单校验/过滤、GBK 解码、涨跌幅 ÷100 全在插件层完成
    (CONTRIBUTING §4: 插件层是数据边界, 归一化归插件)。此处只负责 client 生命周期。

    抛出 :class:`~app.plugins.sina.client.SinaError` —— 含「响应为空」, 上游靠异常
    与「成功但确实无数据」区分, 不可吞成 ``[]``。
    """
    codes = hk_index_universe(symbols)
    own_client = client is None
    if own_client:
        client = SinaClient()
    try:
        return sina_hk_index.fetch_hk_index_spot(client, codes)
    finally:
        if own_client:
            client.close()
