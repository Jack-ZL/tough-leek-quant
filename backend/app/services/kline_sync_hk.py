"""港股日K同步 → ``data/kline_hk_daily/``, 及复权因子 → ``data/adj_factor_hk/``。

与 A 股 :mod:`app.services.kline_sync` **物理与代码双隔离** (方案 §4 闸3):
A 股那条链路深度耦合能力路由/偏好档位/TickFlow 补齐, 港股按用户决策是「行情/K线
直连东财」, 不走偏好路由。硬塞进 ``sync_and_persist_daily_batch`` 会让两个市场的
失败互相连累, 且 A 股那条是关键路径。

复用的是**纯函数**部分, 这些与路由无关:
- ``hk.fetch_hk_kline``      港股单标的日K (secid 前缀 116)
- ``provider._map_kline_row`` CSV 行 → 内部日K契约 (港股行格式与 A 股实测一致)
- ``kline_sync._normalize_daily`` 规范化 + 停牌日过滤 + canonical 列裁剪
- ``repo.append_hk_daily``   按日分区 merge-upsert 落库
- ``hk_dividend.*``          F10 分红事件 → 复权因子 (港股无 TickFlow 因子源)
- ``kline_sync._merge_adj_factor_store`` 因子库 merge-upsert, 与 A 股**同一个函数**

⚠️ **成交量单位**: 港股东财原生单位是「股」, A 股是「手」。本模块按原样落库
(不做单位换算), 下游经 :func:`app.market.shares_per_volume_unit` 换算。落库前
必须确认 enriched 生成走 ``market=Market.HK``, 否则换手率高 100 倍 ——
见 ``docs/hk-market-plan.md`` §3.1.2。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.market import Market, SymbolMarketMismatchError
from app.plugins.eastmoney import hk, hk_dividend
from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError
from app.plugins.eastmoney.provider import _map_kline_row
from app.services.fs_utils import atomic_write_text
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.services.kline_sync import _merge_adj_factor_store, _normalize_daily

logger = logging.getLogger(__name__)

#: 全量回填起点。与 A 股 ``provider._KLINE_BEG_DEFAULT`` 同值, 但**不 import**:
#: 港股最早上市标的远晚于 1990, 用同一起点只是让「全量」语义一致, 东财按标的
#: 实际上市日返回, 不会产生空行。
HK_KLINE_BEG_DEFAULT = "19900101"


def hk_daily_universe(
    data_dir: Path, *, include_cny_counter: bool = False
) -> list[str]:
    """从 ``instruments_hk`` 读出港股日K的标的清单。

    默认**排除人民币柜台** (``is_cny_counter``): 同一公司的两个柜台是同一份基本面,
    双份拉取会把请求数翻倍 (直接加剧东财 IP 封禁风险), 且全市场统计会重复计一家公司。
    维表里两个代码都保留 (用户决策), 只是行情默认只拉港币柜台。

    维表缺失/为空时返回 ``[]`` —— 调用方据此跳过, 不去猜一个 universe。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        logger.warning("港股维表不存在 (%s), 跳过日K同步", path)
        return []
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败, 跳过日K同步: %s", e)
        return []
    if df.is_empty() or "symbol" not in df.columns:
        return []
    if not include_cny_counter and "is_cny_counter" in df.columns:
        df = df.filter(~pl.col("is_cny_counter").fill_null(False))
    return sorted(s for s in df["symbol"].drop_nulls().unique().to_list() if s)


def iter_hk_daily_frames(
    client: EastmoneyClient,
    symbols: Sequence[str],
    *,
    beg: str = HK_KLINE_BEG_DEFAULT,
    end: str = "20500101",
    fqt: int = 0,
) -> Iterator[pl.DataFrame]:
    """逐标的拉港股日K, 一标的一帧流式产出 (供调用方逐批落盘, 控制峰值内存)。

    单标的软失败 (记 warning + 跳过): 港股有长期停牌标的, 东财对其返回空 klines,
    一只失败不该中断整轮同步。限速由 client 令牌桶承担。

    ``fqt`` 必须为 **0** (不复权): 项目契约是落库原始价, 复权在读端按除权因子重建。
    """
    if fqt != 0:
        raise ValueError(f"港股日K落库必须用原始价 (fqt=0), 收到 fqt={fqt}")
    for symbol in symbols:
        try:
            lines = hk.fetch_hk_kline(client, symbol, beg, end, fqt=fqt)
        except EastmoneyError as e:
            logger.warning("港股日K %s 拉取失败, 跳过: %s", symbol, e)
            continue
        except SymbolMarketMismatchError as e:
            # 维表里混进了非港股 symbol (脏数据/上游改口径)。跳过而非中断:
            # 一只脏数据不该让整轮同步失败, 但必须记 warning 让人能发现。
            logger.warning("港股日K %s 非港股 symbol, 跳过: %s", symbol, e)
            continue
        if not lines:
            logger.debug("港股日K %s 返回 0 行 (可能长期停牌或未上市)", symbol)
            continue
        rows = [r for r in (_map_kline_row(symbol, line) for line in lines) if r]
        if not rows:
            logger.warning("港股日K %s 返回 %d 行但全部无法解析", symbol, len(lines))
            continue
        df = _normalize_daily(pl.DataFrame(rows), default_symbol=symbol)
        if not df.is_empty():
            yield df


def latest_hk_daily_date(repo) -> date | None:
    """已落库港股日K的最大交易日; 无数据返回 None (供增量同步定起点)。"""
    try:
        row = repo.execute_one("SELECT max(date) FROM kline_hk_daily")
    except Exception as e:  # noqa: BLE001
        # 视图在空目录下不注册 (read_parquet 无匹配文件), 这是首次同步的正常状态
        logger.debug("kline_hk_daily 视图不可用, 按无历史处理: %s", e)
        return None
    if not row or row[0] is None:
        return None
    d = row[0]
    return d if isinstance(d, date) else date.fromisoformat(str(d))


def sync_hk_daily(
    repo,
    *,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    include_cny_counter: bool = False,
    incremental: bool = True,
    client: EastmoneyClient | None = None,
    on_progress=None,
) -> int:
    """同步港股日K → ``kline_hk_daily/``。返回写入的行数。

    ``incremental=True`` 且未显式给 ``start`` 时, 起点取已落库最大日期的**次日**;
    无历史则全量回填。港股无 T+1 限制但日K口径与 A 股一致 (按交易日分区)。

    ``on_progress(done, total)`` 供前端进度条使用, 每标的回调一次。
    """
    data_dir = repo.store.data_dir
    universe = list(symbols) if symbols is not None else hk_daily_universe(
        data_dir, include_cny_counter=include_cny_counter
    )
    if not universe:
        logger.info("港股日K同步: 标的清单为空, 跳过")
        return 0

    if start is None and incremental:
        latest = latest_hk_daily_date(repo)
        if latest is not None:
            start = latest + timedelta(days=1)
    beg = start.strftime("%Y%m%d") if start else HK_KLINE_BEG_DEFAULT
    end_s = end.strftime("%Y%m%d") if end else "20500101"
    if start is not None and end is not None and start > end:
        logger.info("港股日K同步: 起点 %s 晚于终点 %s, 已是最新", start, end)
        return 0

    own_client = client is None
    if own_client:
        client = EastmoneyClient()
    written = 0
    done = 0
    total = len(universe)
    try:
        for df in iter_hk_daily_frames(client, universe, beg=beg, end=end_s):
            repo.append_hk_daily(df)
            written += df.height
            done += 1
            if on_progress is not None:
                on_progress(done, total)
    finally:
        if own_client:
            client.close()

    if written:
        repo.rebuild_views()
    logger.info(
        "港股日K同步完成: %d 标的 / 写入 %d 行 (beg=%s end=%s)",
        total, written, beg, end_s,
    )
    return written


#: enriched 重算的默认标的批大小, 与 A 股 ``recompute_daily_symbols`` 同量级:
#: 批内要同时驻留完整原始历史与全套指标宽表, 太大峰值内存吃不消。
HK_ENRICHED_BATCH_SIZE = 200


def hk_enriched_instruments(data_dir: Path) -> pl.DataFrame:
    """读 ``instruments_hk`` 供 enriched 计算 (换手率需要 ``float_shares``)。

    维表缺失/损坏 → 返回**空帧**, 由调用方 :func:`sync_hk_enriched` 决定中止。
    **不猜一个默认股本** —— 猜出来的换手率「看起来对但错」, 比缺数据危险得多
    (CONTRIBUTING §3)。这里只负责如实报告「读不到」, 不负责降级策略。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        logger.warning("港股维表不存在: %s", path)
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败 (%s): %s", path, e)
        return pl.DataFrame()


#: 港股复权因子库 (单文件 merge-upsert, 与 A 股 ``adj_factor/all.parquet`` 同构)。
HK_ADJ_FACTOR_RELPATH = "adj_factor_hk/all.parquet"


def hk_adj_factor_path(data_dir: Path) -> Path:
    """港股复权因子库的绝对路径。"""
    return data_dir / HK_ADJ_FACTOR_RELPATH


def load_hk_adj_factors(
    data_dir: Path, symbols: Sequence[str] | None = None
) -> pl.DataFrame | None:
    """读港股复权因子库, 返回值**三态**, 调用方必须区分 (fail-closed 的关键):

    - ``None``   —— 因子库不存在或读取失败, 即 :func:`sync_hk_adj_factor` 从未
      成功跑过。此时「没有因子」意味着 *不知道有没有除权事件*, **不是** *无事件*。
    - **空帧**   —— 库存在但 (该批标的) 无行 = 确实无除权事件, 原始价即复权价,
      可安全按空因子计算。这与 A 股 ``fetch_adj_factor_single(strict=True)``
      「空帧是合法返回」的语义一致。
    - **非空帧** —— 规范三列 + 明细列。

    读取失败 (文件损坏) 也返回 ``None`` 并记 error: 宁可中止, 也不拿半截因子去
    复权 —— 那会让价格序列在损坏点前后各错一半, 比完全不复权更难发现。
    """
    path = hk_adj_factor_path(data_dir)
    if not path.exists():
        return None
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.error("港股复权因子库读取失败 (%s): %s", path, e)
        return None
    if symbols is not None and not df.is_empty():
        df = df.filter(pl.col("symbol").is_in(list(symbols)))
    return df


#: 完整性凭据的 schema 版本。语义变更时递增 —— 读到别的版本一律当「无凭据」处理
#: (fail closed), 而不是猜旧字段的含义。
HK_ADJ_COVERAGE_VERSION = 1

#: 🔴 凭据**必须是 .json 而不是 .parquet**: ``repository._register_views`` 与
#: ``rebuild_views`` 都用 ``adj_factor_hk/**/*.parquet`` 递归 glob 注册 ``adj_factor_hk``
#: 视图, 同目录放一个 parquet 会被 ``union_by_name`` 并进因子表, 污染 schema。
HK_ADJ_COVERAGE_RELPATH = "adj_factor_hk/coverage.json"


class HkAdjCoverage(NamedTuple):
    """``sync_hk_adj_factor`` 的**完整性凭据**: 每只标的的分红史是否已知。

    因子库本身回答不了这个问题 —— 一只标的在库里没有行, 可能是「确认无除权事件」,
    也可能是「东财 F10 拉取失败所以不知道」。两者在 ``_apply_adj_factor`` 里都走
    ``cum_factor.fill_null(1.0)``, 而后者会被当成「该股从无分红」→ 前复权价静默偏高、
    MA/MACD 静默失真, 属 CONTRIBUTING §3 的「看起来对但错」。

    - ``covered``   —— 拉取成功。库里没有它的行 = **确认无除权事件**,
      ``fill_null(1.0)`` 是正确语义, 可以算。
    - ``uncovered`` —— 拉取失败, 或因本地无原始日K而定不出参考价。库里没有它的行 =
      **不知道**, 必须拒算。
    """

    covered: frozenset[str]
    uncovered: frozenset[str]
    updated_at: str | None


def hk_adj_coverage_path(data_dir: Path) -> Path:
    """港股复权因子完整性凭据的绝对路径。"""
    return data_dir / HK_ADJ_COVERAGE_RELPATH


def load_hk_adj_coverage(data_dir: Path) -> HkAdjCoverage | None:
    """读完整性凭据。不存在 / 损坏 / 版本不符 → ``None``, 调用方**必须 fail closed**。

    与 :func:`load_hk_adj_factors` 的 ``None`` 同义: 「不知道」绝不降级成「没有」。
    """
    path = hk_adj_coverage_path(data_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.error("港股复权因子凭据读取失败 (%s): %s", path, e)
        return None
    version = raw.get("version") if isinstance(raw, dict) else None
    if version != HK_ADJ_COVERAGE_VERSION:
        logger.error(
            "港股复权因子凭据版本不符 (%r != %d), 按无凭据处理: %s",
            version, HK_ADJ_COVERAGE_VERSION, path,
        )
        return None
    return HkAdjCoverage(
        covered=frozenset(raw.get("covered") or ()),
        uncovered=frozenset(raw.get("uncovered") or ()),
        updated_at=raw.get("updated_at"),
    )


def _write_hk_adj_coverage(data_dir: Path, covered: set[str], uncovered: set[str]) -> None:
    """原子写凭据。半截 JSON 会让下游永久 fail closed 且看不出原因, 故走 tmp+replace。"""
    path = hk_adj_coverage_path(data_dir)
    # 全轮失败时因子库 parquet 一个都没落, 目录可能还不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "version": HK_ADJ_COVERAGE_VERSION,
                "updated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                "covered": sorted(covered),
                "uncovered": sorted(uncovered),
            },
            ensure_ascii=False,
            indent=1,
        ),
    )


def sync_hk_adj_factor(
    repo,
    *,
    symbols: Sequence[str] | None = None,
    client: EastmoneyClient | None = None,
    batch_size: int | None = None,
    on_progress=None,
    failed_out: list[str] | None = None,
) -> tuple[int, list[str]]:
    """同步港股复权因子 → ``adj_factor_hk/all.parquet``。返回 (新增行数, 因子变化的标的)。

    链路: 东财 F10 分红事件 (:func:`hk_dividend.fetch_hk_dividend_events`) →
    解析中文方案为港币数值 → 用**本地** ``kline_hk_daily`` 的原始收盘价定参考价 →
    ``ex_factor = P / ((P − D) / (1 + B))``。因子口径与落库细节见
    :mod:`app.plugins.eastmoney.hk_dividend`。

    ``changed`` 列表供 enriched 局部重算: 只有 ``ex_factor`` 新增或变化 (差 > 1e-9)
    的标的才算, 明细列回填不改变复权价故不计入 —— 与 A 股
    :func:`kline_sync._merge_adj_factor_store` 同一判定。

    **失败语义** (对齐 A 股 ``sync_adj_factor``): 单标的取数抛
    :class:`EastmoneyError` → 记 warning + 追加进 ``failed_out`` + 继续, 一只失败
    不中断整轮; 但**绝不**把失败伪装成「该标的无除权事件」—— 上层据 ``failed_out``
    重跑。无除权事件本身是合法的空结果, 不记失败。

    一轮**全部成功**却无任何事件时仍会落一个空库 —— 那是「已同步、确认无事件」的
    凭据, :func:`sync_hk_enriched` 的 fail-closed 闸门靠它放行; 有标的失败时刻意
    不落, 宁让 enriched 中止, 也不用空库谎称数据齐全。

    🔴 但那道闸门只区分「库在不在」, 区分不了「库非空但缺了某几只」—— 逐批 merge
    意味着部分失败时成功的行已经落盘且**不回滚**。故本函数另外落一份**按标的**的
    完整性凭据 ``adj_factor_hk/coverage.json`` (:class:`HkAdjCoverage`), 记下每只
    标的的分红史是「拉到了」还是「不知道」; :func:`sync_hk_enriched` 据此**按标的
    拒算**, 而不是让缺因子的标的走 ``fill_null(1.0)`` 被当成「从无分红」。

    ``on_progress(done, total)`` 每标的回调一次, 与 :func:`sync_hk_daily` 同口径。
    """
    from app.parquet import scan_daily_parquet

    data_dir = repo.store.data_dir
    universe = list(symbols) if symbols is not None else hk_daily_universe(data_dir)
    if not universe:
        logger.info("港股复权因子同步: 标的清单为空, 跳过")
        return 0, []

    files = repo.daily_repair_files("stock", market=Market.HK)
    if not files:
        # 没有原始日K就定不出参考价 P。这不是「无除权事件」, 而是前置数据缺失,
        # 记 warning 后返回空 —— 调用方 (bootstrap) 应先跑 sync_hk_daily。
        logger.warning("港股复权因子同步: kline_hk_daily 无原始日K, 无法定参考价, 跳过")
        return 0, []

    # 只取算因子必需的三列: 全市场原始日K行数远大于此, 少读 5 列显著降峰值内存。
    raw_scan = scan_daily_parquet(files).select(["symbol", "date", "close"])

    own_client = client is None
    if own_client:
        client = EastmoneyClient()

    step = max(1, min(batch_size or HK_ENRICHED_BATCH_SIZE, HK_ENRICHED_BATCH_SIZE))
    out_path = hk_adj_factor_path(data_dir)
    added = 0
    changed: list[str] = []
    # 调用方没给 failed_out 也要自己记一份: 下面「是否落空库」的判定依赖它。
    failed: list[str] = [] if failed_out is None else failed_out
    # 完整性凭据的两个桶: processed = 本轮做过判定的标的, succeeded = 其中分红史
    # 真的拉到的。差集即「本轮新增的 uncovered」。
    processed: set[str] = set()
    succeeded: set[str] = set()
    done = 0
    total = len(universe)
    try:
        for i in range(0, total, step):
            batch = universe[i:i + step]
            raw = raw_scan.filter(pl.col("symbol").is_in(batch)).sort(
                ["symbol", "date"]
            ).collect(engine="streaming")
            closes_by_sym: dict[str, dict[date, float]] = {}
            for key, part in raw.partition_by("symbol", as_dict=True, include_key=False).items():
                sym = key[0] if isinstance(key, tuple) else key
                closes_by_sym[sym] = dict(
                    zip(part["date"].to_list(), part["close"].to_list(), strict=True)
                )

            frames: list[pl.DataFrame] = []
            for sym in batch:
                closes = closes_by_sym.get(sym)
                processed.add(sym)
                if not closes:
                    # 定不出参考价 P 就算不出因子。这**不是**「无除权事件」, 故不进
                    # succeeded —— 凭据里留作 uncovered, 下游拒算而非 fill_null(1.0)。
                    logger.debug("港股复权因子 %s: 本地无原始日K, 跳过", sym)
                else:
                    try:
                        events = hk_dividend.fetch_hk_dividend_events(client, sym)
                    except EastmoneyError as e:
                        logger.warning("港股分红事件 %s 拉取失败, 记为失败: %s", sym, e)
                        failed.append(sym)
                        events = None
                    if events is not None:
                        succeeded.add(sym)
                        f = hk_dividend.build_hk_adj_factors(sym, events, closes)
                        if not f.is_empty():
                            frames.append(f)
                done += 1
                if on_progress is not None:
                    on_progress(done, total)

            if not frames:
                continue
            new_data = pl.concat(frames, how="diagonal")
            a, c = _merge_adj_factor_store(out_path, new_data)
            added += a
            changed.extend(c)
    finally:
        if own_client:
            client.close()

    # 完整性凭据: **按标的合并** —— 本轮处理过的按本轮结果重写, 没处理的保留上一轮
    # 状态。这样 symbols=[...] 的局部重跑不会把其他标的的凭据抹掉。
    # 🔴 有失败也照写: 这正是凭据存在的理由 —— 让下游能区分「确认无除权事件」与
    # 「拉取失败所以不知道」, 而不是把两者都交给 fill_null(1.0)。
    prev = load_hk_adj_coverage(data_dir)
    covered = set(prev.covered) if prev is not None else set()
    uncovered = set(prev.uncovered) if prev is not None else set()
    covered -= processed
    uncovered -= processed
    covered |= succeeded
    uncovered |= processed - succeeded
    _write_hk_adj_coverage(data_dir, covered, uncovered)

    if not out_path.exists() and not failed:
        # 一轮**全部成功**却没有任何除权事件时也要落一个空库: 空库是「已同步、确认
        # 无事件」的凭据。不落文件的话 sync_hk_enriched 的 fail-closed 闸门永远打不开
        # —— 它区分不了「没同步过」与「同步过但无事件」(见 load_hk_adj_factors 三态)。
        # 有标的失败时**刻意不落**: 那轮结果不完整, 宁可让 enriched 继续中止, 也不能
        # 用一个空库谎称数据齐全。
        _merge_adj_factor_store(
            out_path,
            pl.DataFrame(schema={
                "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
            }),
        )

    logger.info(
        "港股复权因子同步完成: %d 标的 / 新增 %d 行 / 因子变化 %d 标的 / 失败 %d 标的",
        total, added, len(set(changed)), len(set(failed)),
    )
    return added, sorted(set(changed))


def sync_hk_enriched(
    repo,
    *,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    batch_size: int | None = None,
    on_progress=None,
    require_adj_factor: bool = True,
    uncovered_out: list[str] | None = None,
) -> int:
    """从已落库的港股**原始**日K重算 enriched → ``kline_hk_enriched/``。返回写入行数。

    与 A 股 ``pipeline.recompute_daily_symbols`` 同构: 按标的分批、输入用**完整本地
    原始历史**(指标需要预热窗口)、只发布 ``start`` 起的行。

    **刻意不走 ``EnrichedPublication`` 代次锁**: 那套机制服务于 matrix / 选股器 /
    回测的读一致性, 而港股 enriched 目前没有任何这类读方 (方案 §4 闸3: 港股不进
    unified 视图, 由 ``test_unified_views_exclude_hk`` 钉死)。加锁只会白付代价,
    且 ``_write_daily_partition`` 的 ``generation_asset`` 映射本来就不含港股目录。

    **复权**: 落库是原始价 (``iter_hk_daily_frames`` 强制 ``fqt=0``), 本函数从
    ``adj_factor_hk/all.parquet`` 取因子交给 ``compute_enriched`` 重建复权价, 与
    A 股同构。``require_adj_factor=True`` (默认) 时**因子库不存在即中止并返回 0**
    —— 缺因子会让每个除净日留下假跳空、MA/MACD 随之失真, 属 CONTRIBUTING §3
    警告的「看起来对但错」, 故 fail closed 而非静默用原始价 (方案 §13/§15)。
    仅当明确知道自己在做什么 (如构造测试夹具) 才传 ``False``。

    🔴 **两处已知近似 —— 是数据源现状不是 bug, 但读端必须知道**:

    1. **约 3% 的分红事件无法折算为港币数值** (外币宣派未给折算值 / 分派股份
       spin-off / 红利认股权证), :func:`hk_dividend.build_hk_adj_factors` 对它们
       **warn + 跳过**, 这些除净日前后仍会有跳变失真。逐条告警可见, 不是静默。
    2. **换手率用当前流通股本回溯全历史**。港股无历史股本表 (``historical_shares=None``),
       而 CONTRIBUTING §3.4 要求「优先使用公告日不晚于目标交易日的历史股本」。港股
       全流通、股本变动主要来自增发/回购, 误差通常小于 A 股, 但**不等于没有**。

    港股无涨跌停 (``HK_RULES.has_price_limit=False``) → ``limit_up_price`` /
    ``limit_down_price`` 恒 null, ``consecutive_limit_ups/downs`` 恒 0。**这是正确
    结果不是缺数据**; 读端不得把 null 限价解读成别的语义 (方案 §9.1 AI 护栏)。

    ``market=Market.HK`` 是本函数存在的核心理由: 港股 volume 单位是「股」而 A 股是
    「手」, 走默认 CN 会让换手率**高 100 倍** (方案 §3.1.2)。

    🔴 **两道 fail-closed 闸门** (``require_adj_factor=True`` 时):
    ① 因子库 ``adj_factor_hk/all.parquet`` 不存在 → 整轮中止返回 0;
    ② 完整性凭据 ``adj_factor_hk/coverage.json`` 不存在 → 整轮中止返回 0; 凭据存在
       但某些标的 ``uncovered`` → **只拒算这些标的**, 其余照常, 被拒的写进
       ``uncovered_out``。闸门 ② 补的是闸门 ① 的洞: 逐批 merge 且失败不回滚, 部分
       失败时库照样非空, 缺因子的标的会被 ``fill_null(1.0)`` 当成「从无分红」。
    """
    from app.indicators.pipeline import compute_enriched
    from app.parquet import scan_daily_parquet

    data_dir = repo.store.data_dir
    files = repo.daily_repair_files("stock", market=Market.HK)
    if not files:
        logger.info("港股 enriched 重算: kline_hk_daily 无原始日K, 跳过")
        return 0

    raw_scan = scan_daily_parquet(files)
    if symbols is not None:
        selected = sorted(set(symbols))
    else:
        selected = sorted(raw_scan.select("symbol").unique().collect()["symbol"].drop_nulls().to_list())
    if not selected:
        logger.info("港股 enriched 重算: 标的清单为空, 跳过")
        return 0

    instruments = hk_enriched_instruments(data_dir)
    if instruments.is_empty():
        # 明确拒绝而不是「算出来但缺列」。理由有二:
        # 1) compute_limit_signals 在 instruments 为空时提前返回, 落盘分区里连
        #    turnover_rate 这一列都**不存在**。实测: 若所有分区都缺该列, DuckDB 视图
        #    就没有这个列, 消费方 `SELECT turnover_rate` 直接 BinderException; 若只有
        #    部分分区缺, union-by-name 会补成 NULL —— 同一张表两种行为, 属于
        #    CONTRIBUTING:825 的「写入后存在已知缓存不一致」。
        # 2) 港股无涨跌停, compute_limit_signals 对 HK **只有换手率这一项产出有意义**
        #    (见其 docstring)。没有股本就等于唯一有意义的列缺失, 产出无价值。
        # 注: 港股日K 本就依赖维表 (hk_daily_universe 从它取标的), 走到这里说明维表
        # 被删/损坏, 属异常状态, 该报错而不是静默降级。
        logger.error(
            "港股 enriched 重算中止: 维表 %s 缺失或为空, 无法取得 float_shares。"
            "请先跑 sync_hk_instruments。", HK_INSTRUMENT_RELPATH,
        )
        return 0

    factors_all = load_hk_adj_factors(data_dir)
    if factors_all is None and require_adj_factor:
        # fail closed: 缺因子 ≠ 无除权事件。用原始价算出来的 MA/MACD 会在每个
        # 除净日出现假跳空, 且**数值看起来完全正常**, 属 CONTRIBUTING §3 的
        # 「看起来对但错」, 比直接报错危险得多。
        logger.error(
            "港股 enriched 重算中止: 复权因子库 %s 不存在 (从未成功同步)。"
            "请先跑 sync_hk_adj_factor; 确要以原始价计算须显式传 "
            "require_adj_factor=False。", HK_ADJ_FACTOR_RELPATH,
        )
        return 0
    if factors_all is None:
        logger.warning(
            "港股 enriched 重算: 复权因子库 %s 不存在, 按 require_adj_factor=False "
            "以**原始价**计算 —— 跨除净日的 MA/MACD 会失真", HK_ADJ_FACTOR_RELPATH,
        )

    if require_adj_factor:
        # 🔴 第二道闸门 —— **按标的**的完整性凭据。上面那道只判「因子库在不在」, 判不出
        # 「库非空但某几只的分红史没拉到」: sync_hk_adj_factor 逐批 merge 且失败不回滚,
        # 部分失败时库照样非空。放行的话缺因子的标的会在 _apply_adj_factor 里拿到
        # cum_factor.fill_null(1.0), 被当成「从无分红」→ 前复权价静默偏高。
        # 故凭据缺失一律中止, 凭据里 uncovered 的标的**逐个拒算**。
        coverage = load_hk_adj_coverage(data_dir)
        if coverage is None:
            logger.error(
                "港股 enriched 重算中止: 复权因子完整性凭据 %s 缺失或不可读。"
                "因子库存在也无法判定各标的的分红史是否拉全 —— 缺凭据时 fill_null(1.0) "
                "会把拉取失败的标的当成「从无分红」。请重跑 sync_hk_adj_factor 生成凭据; "
                "确要以原始价计算须显式传 require_adj_factor=False。",
                HK_ADJ_COVERAGE_RELPATH,
            )
            return 0
        uncovered = [s for s in selected if s not in coverage.covered]
        if uncovered:
            shown = ", ".join(uncovered[:20]) + ("…" if len(uncovered) > 20 else "")
            logger.error(
                "港股 enriched: %d 个标的复权因子覆盖不完整, 本轮**拒算** "
                "(fail closed, 等下一轮 sync_hk_adj_factor 补齐): %s",
                len(uncovered), shown,
            )
            if uncovered_out is not None:
                uncovered_out.extend(uncovered)
            selected = [s for s in selected if s in coverage.covered]
            if not selected:
                return 0

    step = max(1, min(batch_size or HK_ENRICHED_BATCH_SIZE, HK_ENRICHED_BATCH_SIZE))
    batches = [selected[i:i + step] for i in range(0, len(selected), step)]
    written = 0
    for done, batch in enumerate(batches, start=1):
        raw = raw_scan.filter(pl.col("symbol").is_in(batch)).sort(["symbol", "date"]).collect(
            engine="streaming"
        )
        # 空批次 (点名了尚未同步日K的标的) 不写盘, 但**仍要报进度** —— 进度上报只留
        # 这一个出口, 从结构上杜绝「某条分支忘了调 on_progress 导致进度条卡在 99%」
        # 这类漂移。
        if not raw.is_empty():
            enriched = compute_enriched(
                raw,
                # 空帧 = 该批标的无除权事件, _apply_adj_factor 会原样返回 raw;
                # None 仅在 require_adj_factor=False 且因子库缺失时出现。
                factors=(
                    None if factors_all is None
                    else factors_all.filter(pl.col("symbol").is_in(batch))
                ),
                instruments=instruments.filter(pl.col("symbol").is_in(batch)),
                historical_shares=None,
                market=Market.HK,
            )
            # 计算用完整历史 (预热 + 连板递推), 发布只取 start 起的行 —— 与 A 股一致。
            output = enriched if start is None else enriched.filter(pl.col("date") >= start)
            if not output.is_empty():
                repo.append_hk_enriched(output)
                written += output.height
        if on_progress is not None:
            on_progress(done, len(batches))

    if written:
        repo.rebuild_views()
    logger.info(
        "港股 enriched 重算完成: %d 标的 / 写入 %d 行 (start=%s)",
        len(selected), written, start or "全量",
    )
    return written


__all__ = [
    "HK_ADJ_COVERAGE_RELPATH",
    "HK_ADJ_COVERAGE_VERSION",
    "HK_ADJ_FACTOR_RELPATH",
    "HK_ENRICHED_BATCH_SIZE",
    "HK_KLINE_BEG_DEFAULT",
    "HkAdjCoverage",
    "hk_adj_coverage_path",
    "hk_adj_factor_path",
    "hk_daily_universe",
    "hk_enriched_instruments",
    "iter_hk_daily_frames",
    "latest_hk_daily_date",
    "load_hk_adj_coverage",
    "load_hk_adj_factors",
    "sync_hk_adj_factor",
    "sync_hk_daily",
    "sync_hk_enriched",
]
