"""港股数据画像聚合 —— ``GET /api/hk/status`` 的唯一实现。

与 A 股 ``GET /api/data/status`` (``app/api/data.py``) 同构: 每个数据集一个
``{rows, earliest_date, latest_date, trading_days, symbols_covered}`` 块, 没有
数据就是 ``None``。前端因此能复用同一套画像卡渲染, 不必为港股另造一套字段名。

🔴 一律直读目录/文件, **不走 DuckDB 视图**: ``instruments_hk`` / ``kline_hk_*``
视图只在目录里确实有 parquet 时才注册 (``read_parquet`` 无匹配文件会抛,
``_register_views`` 把异常降级成 debug 日志)。走视图会让「还没同步过」变成 500
而不是如实的 ``None`` —— 与 ``GET /api/hk/instruments`` 直读维表是同一个理由。

🔴 **不做服务端缓存**: A 股 ``/status`` 的 ``DataStatusCache`` 靠写入路径调
``invalidate_data_cache(table)`` 维持一致, 而港股写入链路 (``sync_hk_daily`` /
``sync_hk_enriched`` / ``jobs/hk_pipeline``) 没有接这套失效钩子。加缓存就会
「同步跑完画像还是旧的」, 正是 CONTRIBUTING §12 明列的拒收情形「写入后存在已知
缓存不一致」。本模块的聚合只有目录名扫描 + 单个分区/小文件读取 (毫秒级), 不缓存
也够快; 一致性交给前端 TanStack Query 在同步结束后显式 invalidate。

🔴 全部用 polars **eager** ``read_parquet``, 不用 LazyFrame + ``collect``:
本函数在页面读接口里同步执行, 而 polars 共享执行器在多线程并发 collect 下有死锁
史 (见 ``app/polars_guard.py``)。eager 读小文件不占 collect 闸位, 也就不必和后台
大计算抢车道。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from datetime import date as dt_date
from pathlib import Path

import polars as pl

from app.market import Market
from app.services import financial_sync_hk, kline_sync_hk
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import daily_dirname, enriched_dirname

logger = logging.getLogger(__name__)

_DATE_PREFIX = "date="

#: 画像卡里出现的港股数据集顺序 (前端按此顺序渲染, 改这里即改卡片行序)。
HK_STATUS_TABLES: tuple[str, ...] = (
    "instruments",
    "daily",
    "enriched",
    "index_daily",
    "index_enriched",
    "adj_factor",
    "financials",
)

#: ``financials_hk`` 根目录名。``financial_sync_hk`` 只暴露「按表」的路径构造器,
#: 这里从它反推根目录名而不是再抄一个字面量 —— 写入方改了目录, 画像与「清空数据」
#: 会自动跟上, 不会留下「清了 A 股漏了港股」这种静默不一致。
_HK_FINANCIALS_DIRNAME = financial_sync_hk.hk_financials_dir(
    Path("."), "main_indicator"
).parent.name


# ---------------------------------------------------------------------------
# 目录名 —— 从存储层的权威映射取, 不在本模块硬编码字符串
# ---------------------------------------------------------------------------

def hk_kline_dirnames() -> dict[str, str]:
    """港股四个 K 线目录名。

    取自 ``repository._DIRNAMES`` (``tests/test_hk_storage_layout.py`` 把这份映射
    钉成了断言), 而不是在这里再抄一遍字面量 —— 抄两份就会漂移, 而目录名漂移的
    后果是「画像永远显示未同步」这种静默错误。
    """
    return {
        "daily": daily_dirname("stock", market=Market.HK),
        "enriched": enriched_dirname("stock", market=Market.HK),
        "index_daily": daily_dirname("index", market=Market.HK),
        "index_enriched": enriched_dirname("index", market=Market.HK),
    }


def hk_storage_dirnames() -> dict[str, str]:
    """存储占用统计覆盖的港股目录 (含非 K 线的维表/因子/财务)。"""
    return {
        **hk_kline_dirnames(),
        "instruments": Path(HK_INSTRUMENT_RELPATH).parent.name,
        "adj_factor": Path(kline_sync_hk.HK_ADJ_FACTOR_RELPATH).parent.name,
        "financials": _HK_FINANCIALS_DIRNAME,
    }


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _partition_dates(dirpath: Path) -> list[str]:
    """``date=YYYY-MM-DD`` 分区目录名 → 升序日期列表 (零数据扫描)。"""
    if not dirpath.exists():
        return []
    dates = [
        d.name[len(_DATE_PREFIX):]
        for d in dirpath.iterdir()
        if d.is_dir() and d.name.startswith(_DATE_PREFIX)
    ]
    dates.sort()
    return dates


def _field_count(dirpath: Path) -> int:
    """parquet 列数 —— 只读 footer 元数据, 不碰数据。读不到返回 0。"""
    try:
        return len(pl.read_parquet_schema(str(dirpath / "**" / "*.parquet")))
    except Exception as e:  # noqa: BLE001
        logger.debug("港股 %s schema 读取失败: %s", dirpath.name, e)
        return 0


def _read_partition_symbols(dirpath: Path, day: str) -> pl.DataFrame | None:
    """读**单个** ``date=`` 分区的 symbol 列。分区缺失/损坏 → ``None``。"""
    try:
        return pl.read_parquet(
            str(dirpath / f"{_DATE_PREFIX}{day}" / "*.parquet"), columns=["symbol"]
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("港股 %s 分区 %s 读取失败: %s", dirpath.name, day, e)
        return None


def _as_iso(value: object) -> str | None:
    """``date`` / ``datetime`` / 字符串 → ISO 字符串; 空值 → ``None``。"""
    if value is None:
        return None
    if isinstance(value, (dt_date, datetime)):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# 各数据集聚合
# ---------------------------------------------------------------------------

def aggregate_hk_instruments(data_dir: Path) -> dict | None:
    """港股维表画像。未同步 → ``None`` (不是 0 行, 也不是 500)。

    🔴 港币/人民币双柜台**分开计数**: 同一家公司的两个柜台是同一份基本面, 全市场
    口径只算港币柜台 (``tests/test_hk_dual_counter.py`` 钉死了这条策略, 行业排名
    与涨跌家数都依赖它)。画像同时给出两个数, 用户才知道「N 只」里有多少是重复
    公司 —— 只报总数会让人以为港股有 N 家上市公司。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return None
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败: %s", e)
        return None
    if df.is_empty():
        return None

    cols = set(df.columns)
    cny = 0
    if "is_cny_counter" in cols:
        # cast(strict=False): 历史维表里该列可能是 Int8/Null, 直接 fill_null(False)
        # 会因类型不符抛错, 而画像不该因为一个可选列的类型漂移就整块返 None。
        cny = int(
            df.get_column("is_cny_counter").cast(pl.Boolean, strict=False).fill_null(False).sum()
        )
    named = 0
    if "name" in cols:
        named = int(df.filter(pl.col("name").is_not_null() & (pl.col("name") != "")).height)
    industries = (
        int(df.get_column("industry").drop_nulls().n_unique()) if "industry" in cols else 0
    )
    return {
        "rows": int(df.height),
        "symbols_covered": int(df.get_column("symbol").n_unique()) if "symbol" in cols else 0,
        "named": named,
        "latest_as_of": _as_iso(df.get_column("as_of").max()) if "as_of" in cols else None,
        "hkd_counters": int(df.height) - cny,
        "cny_counters": cny,
        "industries": industries,
    }


def aggregate_hk_kline(data_dir: Path, dirname: str, *, enriched: bool = False) -> dict | None:
    """个股日K / enriched 画像 —— 零全表扫描。

    日期范围与交易日数来自 ``date=`` 分区目录名; 标的数只读**最新一个**分区
    (~2600 行)。``rows`` 恒为 0, 与 A 股 ``_safe_aggregate_daily`` 同一个约定:
    这里的 0 表示「没统计」而不是「没有数据」(全历史行数要打开上千个 parquet,
    画像卡回答不了这个问题也不需要)。前端据 ``trading_days`` 判断有无数据。
    """
    dirpath = data_dir / dirname
    dates = _partition_dates(dirpath)
    if not dates:
        return None
    latest_df = _read_partition_symbols(dirpath, dates[-1])
    stats: dict = {
        "rows": 0,
        "earliest_date": dates[0],
        "latest_date": dates[-1],
        "trading_days": len(dates),
        "symbols_covered": (
            int(latest_df.get_column("symbol").n_unique())
            if latest_df is not None and not latest_df.is_empty()
            else 0
        ),
    }
    if enriched:
        stats["fields"] = _field_count(dirpath)
    return stats


def aggregate_hk_index(data_dir: Path, dirname: str, *, enriched: bool = False) -> dict | None:
    """指数日K / enriched 画像 —— **精确**统计。

    港股指数只有白名单 27 只 × ``HK_INDEX_DAILY_COUNT`` 天 (万行级), 直接整表读
    symbol 列算真实行数与全历史标的数, 不必像个股那样退化成目录名扫描。
    """
    dirpath = data_dir / dirname
    dates = _partition_dates(dirpath)
    if not dates:
        return None
    try:
        df = pl.read_parquet(str(dirpath / "**" / "*.parquet"), columns=["symbol"])
    except Exception as e:  # noqa: BLE001
        logger.debug("港股 %s 读取失败: %s", dirname, e)
        return None
    if df.is_empty():
        return None
    stats = {
        "rows": int(df.height),
        "earliest_date": dates[0],
        "latest_date": dates[-1],
        "trading_days": len(dates),
        "symbols_covered": int(df.get_column("symbol").n_unique()),
    }
    if enriched:
        stats["fields"] = _field_count(dirpath)
    return stats


def aggregate_hk_adj_factor(data_dir: Path) -> dict | None:
    """港股复权因子库 + **完整性凭据** 画像。

    🔴 凭据 (``adj_factor_hk/coverage.json``) 的 covered/uncovered 必须露出来:
    ``sync_hk_enriched`` 据此**逐标的拒算**, 而 ``sync_hk_adj_factor`` 目前
    **没有独立 HTTP 入口** (只由港股定时管道调用)。用户在页面上看到
    ``uncovered > 0`` 或 ``has_coverage = false``, 才知道「enriched 行数偏少」是
    被闸门挡了、要等下一轮定时同步补齐, 而不是数据源没数据 —— 后端刻意把
    ``uncovered`` 一路回报到 HTTP 层就是为了不让这件事静默。
    """
    factor_path = data_dir / kline_sync_hk.HK_ADJ_FACTOR_RELPATH
    coverage_path = data_dir / kline_sync_hk.HK_ADJ_COVERAGE_RELPATH
    if not factor_path.exists() and not coverage_path.exists():
        return None
    coverage = kline_sync_hk.load_hk_adj_coverage(data_dir)
    rows = 0
    symbols = 0
    if factor_path.exists():
        try:
            df = pl.read_parquet(factor_path, columns=["symbol"])
            rows = int(df.height)
            symbols = int(df.get_column("symbol").n_unique())
        except Exception as e:  # noqa: BLE001
            logger.debug("港股复权因子库读取失败: %s", e)
    return {
        "rows": rows,
        "symbols_covered": symbols,
        "has_coverage": coverage is not None,
        # 🔴 「凭据文件在但读不出来」(损坏 / schema 版本不符) 与「从来没有凭据」
        # 必须分开: 两者都让 ``sync_hk_enriched`` fail closed, 但处置完全不同 ——
        # 前者说明 ``sync_hk_adj_factor`` 跑过而产物已失效, 要去查版本迁移; 后者
        # 只是还没跑过。合并成 ``has_coverage=false`` 会把前一种情况静默掉。
        "coverage_unreadable": coverage is None and coverage_path.exists(),
        "covered": len(coverage.covered) if coverage else 0,
        "uncovered": len(coverage.uncovered) if coverage else 0,
        "coverage_updated_at": coverage.updated_at if coverage else None,
    }


def hk_financials_status(data_dir: Path) -> dict:
    """港股财务同步状态。未同步时如实返回 ``synced=false`` 而非 500。

    原先内联在 ``app/api/hk.py`` 的 ``GET /financials/status`` 里; 搬到这里是为了
    让画像 (``hk_data_status``) 与那个端点**共用同一份实现** —— 两处各算一遍的话,
    「卡片说已同步、财务页说没同步」这种自相矛盾迟早会出现。响应形状逐字未变。
    """
    wide = financial_sync_hk.get_hk_financial_df(data_dir, "main_indicator")
    if wide.is_empty():
        return {
            "synced": False,
            "rows": 0,
            "symbols": 0,
            "latest_report_date": None,
            "lot_size_coverage": None,
            "tables": {
                t: financial_sync_hk.get_hk_financial_df(data_dir, t).height
                for t in ("income", "balance")
            },
        }
    latest = financial_sync_hk.latest_hk_report_date(data_dir)
    lots = financial_sync_hk.hk_lot_size_map(data_dir)
    return {
        "synced": True,
        "rows": wide.height,
        "symbols": wide.get_column("symbol").n_unique(),
        "latest_report_date": latest.isoformat() if latest else None,
        # 每手股数覆盖率: 该列由财务表回填进 instruments_hk, 是港股下单取整的依据
        "lot_size_coverage": len(lots),
        "period_names": sorted(
            {str(p) for p in wide.get_column("period_name").drop_nulls().to_list()}
        ),
        "tables": {
            t: financial_sync_hk.get_hk_financial_df(data_dir, t).height
            for t in ("income", "balance")
        },
    }


def hk_storage(data_dir: Path) -> dict:
    """港股各目录的文件数与占用 (MB), 外加 ``total_size_mb``。

    用 ``rglob`` 而不是 A 股 ``_compute_storage`` 那套 ``os.scandir`` 单遍优化:
    港股没有分钟线, 目录数与文件数都小两个量级, 简单实现足够快。
    """
    stats: dict[str, float] = {}
    total = 0.0
    for key, dirname in hk_storage_dirnames().items():
        dirpath = data_dir / dirname
        count = 0
        size = 0
        if dirpath.exists():
            for f in dirpath.rglob("*"):
                try:
                    if f.is_file():
                        count += 1
                        size += f.stat().st_size
                except OSError:
                    pass
        mb = round(size / 1048576, 2)
        total += mb
        stats[f"{key}_files"] = count
        stats[f"{key}_size_mb"] = mb
    stats["total_size_mb"] = round(total, 2)
    return stats


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def hk_data_status(data_dir: Path) -> dict:
    """港股数据画像总入口 → ``GET /api/hk/status`` 的响应体。

    每个数据集独立 try 住: 某一个目录损坏不该让整张画像 500, 该块返 ``None``
    即可 (前端显示「暂无数据」), 其余照常。
    """
    dirs = hk_kline_dirnames()
    out: dict[str, object] = {}
    for key, fn in (
        ("instruments", lambda: aggregate_hk_instruments(data_dir)),
        ("daily", lambda: aggregate_hk_kline(data_dir, dirs["daily"])),
        ("enriched", lambda: aggregate_hk_kline(data_dir, dirs["enriched"], enriched=True)),
        ("index_daily", lambda: aggregate_hk_index(data_dir, dirs["index_daily"])),
        (
            "index_enriched",
            lambda: aggregate_hk_index(data_dir, dirs["index_enriched"], enriched=True),
        ),
        ("adj_factor", lambda: aggregate_hk_adj_factor(data_dir)),
        ("financials", lambda: hk_financials_status(data_dir)),
    ):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            logger.warning("港股画像 %s 聚合失败: %s", key, e)
            out[key] = None
    out["storage"] = hk_storage(data_dir)
    out["checked_at"] = (
        datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    return out


__all__ = [
    "HK_STATUS_TABLES",
    "aggregate_hk_adj_factor",
    "aggregate_hk_index",
    "aggregate_hk_instruments",
    "aggregate_hk_kline",
    "hk_data_status",
    "hk_financials_status",
    "hk_kline_dirnames",
    "hk_storage",
    "hk_storage_dirnames",
]
