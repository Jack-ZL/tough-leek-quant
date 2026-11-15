"""港股财务数据同步 — 落库、合并、规范化投影与每手股数回填。

与 :mod:`app.services.financial_sync` (A 股) 的关系
─────────────────────────────────────────────────
**不复用其取数路径**: A 股财务走 TickFlow SDK / 自定义 provider 并受
``Cap.FINANCIAL`` (Expert 档) 能力门控; 港股直连东财 datacenter, 不经过
偏好路由与能力矩阵 (与港股 K 线同步同一决策, 见
:mod:`app.services.kline_sync_hk` 的模块注释)。

**复用其存储形态**: ``financials_hk/<table>/part.parquet`` 全量单文件, 与 A 股
``financials/<table>/part.parquet`` 同构, 但**目录物理分离** (方案 §4 闸3)。

🔴 **不注册 DuckDB 视图** —— 读路径一律 :func:`get_hk_financial_df` 直读
parquet。理由: 本仓有**两处**独立的视图清单 (``DataStore._register_views``
启动时注册, ``KlineRepository.rebuild_views`` 由日终流水线与 ``/api/data/clear``
调用), 只改一处会导致"写入后读到旧视图"这类隐蔽不一致 (CONTRIBUTING:825 列为
PR 拒绝项)。A 股财务的 ``_refresh_financials_views`` 就是个只打日志的空壳,
视图实际只在启动注册 —— 不复制这个坑, 从根上避开双清单漂移。

数据源与实测口径见 :mod:`app.plugins.eastmoney.hk_financials` 的模块注释。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.plugins.eastmoney import hk_financials as hf
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

#: 港股财务表名 (即 ``financials_hk/`` 下的子目录名)。
#:
#: - ``main_indicator`` 宽表: 一行 = 一个标的 × 一个报告期, **全市场分页同步**
#:   (实测 2 年窗口仅 32 页), 是财务概况页的主数据源。
#: - ``income`` / ``balance`` 长表: 一行 = 一个会计科目, 全市场同步需
#:   835~1276 页, **只按单标的取** (详情页下钻)。
HK_FINANCIAL_TABLES: tuple[str, ...] = ("main_indicator", "income", "balance")

#: 长表表名 → 东财报表名。
HK_STATEMENT_REPORTS: dict[str, str] = {
    "income": hf.RPT_HK_INCOME,
    "balance": hf.RPT_HK_BALANCE,
}

#: 默认回溯年数。全市场宽表实测规模: 2 年窗口 15,774 行 / 32 页,
#: 全历史 103,863 行 / 208 页。取 6 年在"够用"与"有界"之间折中
#: (约 60~70 页, 1 qps 下一分多钟)。
#:
#: 用**滚动窗口**而非固定起始日: 固定日期会让取数量随时间无界增长。
#: 窗口外的历史由 :func:`_merge_frames` 保留, 不会因窗口滚动而丢失。
HK_FIN_DEFAULT_YEARS = 6

#: 宽表去重键。🔴 **不能只用 ``(symbol, report_date)``** —— 港股年结日不统一,
#: 同一标的可能有两个不同财年的年报落在同一年份区间内; 但同一
#: ``(symbol, report_date)`` 仍应唯一, 故该键成立。加 ``date_type_code``
#: 只为防御上游偶发的重复行。
_HK_DEDUP_KEYS = ("symbol", "report_date", "date_type_code")

#: 港股独有字段的落库 dtype。规范化投影在**源列缺失**时必须补出同 dtype 的
#: null 列而非省略该列 —— 前端按列名取值, 缺列会 KeyError; 而 schema 漂移是
#: 真实会发生的 (给 ``HK_MAIN_INDICATOR_FIELDS`` 加字段后, 旧 parquet 没有该列)。
HK_ONLY_CANONICAL_DTYPES: dict[str, pl.DataType] = {
    "name": pl.Utf8,
    "org_type": pl.Utf8,
    "period_name": pl.Utf8,
    "date_type_code": pl.Utf8,
    "pricing_currency": pl.Utf8,
    "lot_size": pl.Int64,
    "pe_ttm": pl.Float64,
    "pb_ttm": pl.Float64,
    "total_market_cap": pl.Float64,
}

#: 四张规范表共有的键列 dtype (与 A 股 ``financials/<table>`` 实测一致)。
_HK_CANONICAL_KEY_DTYPES: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "period_end": pl.Utf8,
    "announce_date": pl.Utf8,
    "fiscal_year": pl.Int64,
    "report_date_ms": pl.Int64,
    "period_end_ms": pl.Int64,
}


# ── 路径 ──────────────────────────────────────────────────────────
def hk_financials_dir(data_dir: Path, table: str) -> Path:
    """``financials_hk/<table>/`` 目录路径。"""
    if table not in HK_FINANCIAL_TABLES:
        raise ValueError(f"未知港股财务表 {table!r}, 可选 {HK_FINANCIAL_TABLES}")
    return Path(data_dir) / "financials_hk" / table


def hk_financials_path(data_dir: Path, table: str) -> Path:
    """``financials_hk/<table>/part.parquet`` 文件路径。"""
    return hk_financials_dir(data_dir, table) / "part.parquet"


def default_hk_fin_since(*, years: int = HK_FIN_DEFAULT_YEARS, today: date | None = None) -> str:
    """默认回溯窗口的起始日 (``YYYY-MM-DD``)。"""
    base = today or date.today()
    try:
        start = base.replace(year=base.year - years)
    except ValueError:
        # 2/29 → 平年不存在, 退到 2/28
        start = base.replace(year=base.year - years, day=28)
    return start.isoformat()


# ── 读取 ──────────────────────────────────────────────────────────
def get_hk_financial_df(data_dir: Path, table: str) -> pl.DataFrame:
    """读取本地港股财务 parquet。

    文件缺失或损坏返回**空 DataFrame 而非抛异常** —— "尚未同步"是正常状态,
    不应变成 500 (与 A 股 ``get_financial_df`` 同语义)。
    """
    path = hk_financials_path(data_dir, table)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 financials_hk/%s 失败: %s", table, e)
        return pl.DataFrame()


def latest_hk_report_date(data_dir: Path) -> date | None:
    """宽表中最新的报告期。未同步返回 ``None``。"""
    df = get_hk_financial_df(data_dir, "main_indicator")
    if df.is_empty() or "report_date" not in df.columns:
        return None
    try:
        mx = df.get_column("report_date").drop_nulls().max()
    except Exception:  # noqa: BLE001
        return None
    if not mx:
        return None
    return date.fromisoformat(str(mx)[:10])


# ── 合并 ──────────────────────────────────────────────────────────
def _merge_frames(old: pl.DataFrame, new: pl.DataFrame) -> pl.DataFrame:
    """把新同步的行并入历史, 按 :data:`_HK_DEDUP_KEYS` 去重, **新行优先**。

    为什么不是全量覆盖: 同步用滚动窗口 (``HK_FIN_DEFAULT_YEARS``), 全量覆盖会
    把窗口外的历史报告期直接删掉。历史财报不可变, 保留是安全的。

    为什么新行优先: 财报会被**重述** (restatement), 同一报告期后取的值更准。
    实现上把 new 拼在 old 之后再 ``keep="last"``, 排序稳定故新行胜出。
    """
    if new.is_empty():
        return old
    if old.is_empty():
        return new
    keys = [k for k in _HK_DEDUP_KEYS if k in old.columns and k in new.columns]
    if not keys:
        return new
    merged = pl.concat([old, new], how="diagonal_relaxed")
    return merged.unique(subset=keys, keep="last", maintain_order=True)


def _write_hk_table(data_dir: Path, table: str, df: pl.DataFrame) -> int:
    """全量写 ``financials_hk/<table>/part.parquet``, 返回行数。"""
    if df.is_empty():
        return 0
    out = hk_financials_path(data_dir, table)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)
    logger.info("sync_hk_%s done: %d records written", table, len(df))
    return len(df)


# ── 同步: 主指标宽表 ──────────────────────────────────────────────
def sync_hk_main_indicator(
    data_dir: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    date_type_code: str | None = None,
    secucodes: Sequence[str] | None = None,
    backfill_lot_size: bool = True,
    client: Any | None = None,
    on_progress: Callable[[str, int, str], None] | None = None,
) -> int:
    """全市场同步港股财务主指标 (宽表), 返回合并后的总行数。

    取数策略: **全市场分页**而非逐标的。实测 ``(REPORT_DATE>='2024-01-01')``
    仅 32 页即覆盖全部港股; 逐标的要 2600+ 次请求。逐标的 ``in`` 批量虽可用
    (上限 200, 超过触发 HTTP 414), 但只在"补少数几只"时才更划算。

    Args:
        since: ``REPORT_DATE`` 下界; 缺省用 :func:`default_hk_fin_since`。
        until: ``REPORT_DATE`` 上界。
        date_type_code: 只取某类报告期 (``"001"`` 年报等), 缺省全取。
        secucodes: 只取指定标的 (``00700.HK`` 或 ``00700`` 均可)。
        backfill_lot_size: 顺带把 ``PER_SHARES`` 回填到 ``instruments_hk.lot_size``。
        client: 复用已有 :class:`EastmoneyClient`; 缺省自建并负责关闭。
        on_progress: ``(stage, pct, msg)`` 回调, 供长任务进度上报。
    """
    from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError

    start = since or default_hk_fin_since()
    own_client = client is None
    c = client or EastmoneyClient()

    def _report(pct: int, msg: str) -> None:
        if on_progress is not None:
            on_progress("hk_financials", pct, msg)

    try:
        _report(5, f"拉取港股财务主指标 (REPORT_DATE>={start})")
        batches: list[list[dict[str, Any]]]
        if secucodes:
            codes = [hf.hk_fin_secucode(s) for s in secucodes]
            batches = []
            for i in range(0, len(codes), hf.HK_FIN_IN_BATCH_MAX):
                chunk = codes[i : i + hf.HK_FIN_IN_BATCH_MAX]
                batches.append(
                    hf.fetch_hk_main_indicator(
                        c,
                        since=start,
                        until=until,
                        date_type_code=date_type_code,
                        secucodes=chunk,
                        on_progress=lambda n, p: _report(
                            5 + min(70, p * 5), f"已取 {n} 行 (第 {p} 页)"
                        ),
                    )
                )
        else:
            batches = [
                hf.fetch_hk_main_indicator(
                    c,
                    since=start,
                    until=until,
                    date_type_code=date_type_code,
                    on_progress=lambda n, p: _report(
                        5 + min(70, p * 2), f"已取 {n} 行 (第 {p} 页)"
                    ),
                )
            ]
        payloads = [row for batch in batches for row in batch]
    except EastmoneyError as e:
        logger.warning("港股财务主指标同步失败: %s", e)
        return len(get_hk_financial_df(data_dir, "main_indicator"))
    finally:
        if own_client:
            c.close()

    rows = hf.hk_main_indicator_rows(payloads)
    if not rows:
        logger.warning("港股财务主指标返回 0 行 (since=%s), 保留历史不覆盖", start)
        return len(get_hk_financial_df(data_dir, "main_indicator"))

    new = pl.DataFrame(rows).select(list(hf.HK_MAIN_INDICATOR_COLUMNS))
    _report(80, f"映射 {len(new)} 行, 合并历史")
    merged = _merge_frames(get_hk_financial_df(data_dir, "main_indicator"), new)
    written = _write_hk_table(data_dir, "main_indicator", merged)

    if backfill_lot_size:
        lots = hf.hk_lot_sizes(payloads)
        if lots:
            _report(92, f"回填每手股数 ({len(lots)} 只)")
            try:
                backfill_hk_lot_size(data_dir, lots)
            except Exception as e:  # noqa: BLE001
                # 回填失败不影响财务表本身已落库, 降级为告警
                logger.warning("回填港股每手股数失败: %s", e)

    _report(100, f"完成: {written} 行")
    return written


def sync_hk_statement(
    data_dir: Path,
    symbol: str,
    *,
    table: str = "income",
    since: str | None = None,
    client: Any | None = None,
) -> int:
    """按单标的同步港股利润表 / 资产负债表 (长表), 返回合并后的总行数。

    长表**不做全市场同步** (实测 835~1276 页), 只在详情页下钻时按标的取。
    合并语义与宽表一致: 该标的的新行覆盖旧行, **其他标的的历史行保留**。
    """
    from app.market import Market, assert_symbol_market
    from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError

    if table not in HK_STATEMENT_REPORTS:
        raise ValueError(f"table 必须是 {tuple(HK_STATEMENT_REPORTS)}, 收到 {table!r}")
    # 先校验再触网: A 股代码打错时立刻报错, 而不是拿到空结果让人以为是"没有财报"
    assert_symbol_market(symbol, Market.HK)

    own_client = client is None
    c = client or EastmoneyClient()
    try:
        payloads = hf.fetch_hk_statement(
            c, symbol, report=HK_STATEMENT_REPORTS[table], since=since
        )
    except EastmoneyError as e:
        logger.warning("港股 %s 表同步失败 (%s): %s", table, symbol, e)
        return len(get_hk_financial_df(data_dir, table))
    finally:
        if own_client:
            c.close()

    rows = hf.hk_statement_rows(payloads)
    if not rows:
        logger.info("港股 %s 表无数据 (%s), 保留历史", table, symbol)
        return len(get_hk_financial_df(data_dir, table))

    new = pl.DataFrame(rows).select(list(hf.HK_STATEMENT_COLUMNS))
    old = get_hk_financial_df(data_dir, table)
    if not old.is_empty() and "symbol" in old.columns:
        # 只替换本标的的行, 其余标的的历史保留
        old = old.filter(pl.col("symbol") != new.get_column("symbol").first())
    elif not old.is_empty():
        # 历史文件缺 symbol 列 (损坏或旧格式): 不与新帧合并, 直接以新帧为准,
        # 否则 diagonal concat 会产出一整列无法定位标的的孤儿行。
        logger.warning("financials_hk/%s 历史文件缺 symbol 列, 以本次同步结果覆盖", table)
        old = pl.DataFrame()
    merged = _merge_frames(old, new)
    return _write_hk_table(data_dir, table, merged)


# ── 每手股数回填 ──────────────────────────────────────────────────
def backfill_hk_lot_size(data_dir: Path, lots: dict[str, int]) -> int:
    """把 ``{symbol: lot_size}`` 回填进 ``instruments_hk``, 返回实际更新的标的数。

    ⭐ **数据源**: 东财财务主指标表的 ``PER_SHARES`` 字段。实测 8/8 精确匹配
    已知每手股数真值 (汇丰 400 / 腾讯 100 / 工行 1000 / 友邦 200 / 中移动 500
    等), 全市场年报分页可覆盖 2761 只。这填补了 ``hk_instrument_rows`` 里
    "东财 clist 不提供每手股数, 恒 None, 待独立源补齐"的缺口。

    港股每手股数**因股而异** (实测分布从 10 到 100000), 不是市场级常量,
    故 ``app.market.HK_RULES.lot_size`` 保持 ``None``, 由本函数按标的落库。

    只填空与纠正不一致, **不删除**已有值 (维表里可能有更权威的来源);
    维表缺失时静默返回 0 (先同步维表再同步财务是正常顺序)。
    """
    from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH

    if not lots:
        return 0
    path = Path(data_dir) / HK_INSTRUMENT_RELPATH
    if not path.exists():
        logger.debug("instruments_hk 不存在, 跳过每手股数回填")
        return 0
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 instruments_hk 失败, 跳过每手股数回填: %s", e)
        return 0
    if df.is_empty() or "symbol" not in df.columns:
        return 0
    if "lot_size" not in df.columns:
        # 维表由 ``hk_instrument_rows`` 写出, 正常必带 ``lot_size`` 列; 缺列说明
        # 是外部写入或旧格式。补列而非静默跳过 —— 跳过会让回填无声失效,
        # 而"每手股数缺失"要到下单取整时才暴露。
        logger.warning("instruments_hk 缺 lot_size 列, 补列后回填")
        df = df.with_columns(pl.lit(None, pl.Int64).alias("lot_size"))

    patch = pl.DataFrame(
        {"symbol": list(lots), "lot_size_new": [lots[s] for s in lots]},
        schema={"symbol": pl.Utf8, "lot_size_new": pl.Int64},
    )
    joined = df.join(patch, on="symbol", how="left")
    changed = joined.filter(
        pl.col("lot_size_new").is_not_null()
        & (pl.col("lot_size").is_null() | (pl.col("lot_size") != pl.col("lot_size_new")))
    ).height
    if not changed:
        return 0
    out = joined.with_columns(
        pl.col("lot_size_new").cast(pl.Int64).fill_null(pl.col("lot_size")).alias("lot_size")
    ).drop("lot_size_new")
    # 列顺序保持与维表原 schema 一致, 避免读端按位置取列出错
    out = out.select(list(df.columns))
    atomic_write_parquet(out, path)
    logger.info("回填港股每手股数: %d 只更新 (共 %d 只有值)", changed, len(lots))
    return changed


# ── 规范化投影 (与 A 股财务概况页共用契约) ────────────────────────
def hk_canonical_schema(table: str) -> dict[str, pl.DataType]:
    """规范化表的**完整列集与 dtype** (与数据是否为空无关)。

    列序即契约: 键列 → A 股同名映射列 → 港股独有列。空帧也按此 schema 返回,
    使读端只需处理一种形态。
    """
    if table not in hf.HK_CANONICAL_PROJECTION:
        raise ValueError(
            f"未知目标表 {table!r}, 可选 {tuple(hf.HK_CANONICAL_PROJECTION)}"
        )
    schema = dict(_HK_CANONICAL_KEY_DTYPES)
    for canonical in hf.HK_CANONICAL_PROJECTION[table]:
        schema[canonical] = pl.Float64
    for extra in hf.HK_ONLY_CANONICAL_FIELDS:
        # 与规范名重名时以规范名为准 (与投影里的去重逻辑一致)
        schema.setdefault(extra, HK_ONLY_CANONICAL_DTYPES[extra])
    return schema


def hk_financials_canonical(df: pl.DataFrame, table: str) -> pl.DataFrame:
    """把港股宽表投影成**与 A 股财务表同形**的帧, 供共用财务概况页消费。

    A 股 ``financials/<table>/part.parquet`` 的实测契约 (见
    :data:`app.plugins.eastmoney.hk_financials.HK_CANONICAL_PROJECTION` 注释):
    共享键 ``symbol`` / ``period_end`` / ``announce_date``, 派生键
    ``fiscal_year`` (Int64 年份) / ``report_date_ms`` / ``period_end_ms``。

    港股宽表**没有公告日**, 故 ``announce_date`` 与 ``report_date_ms`` 落 null
    (不拿报告期冒充公告日 —— CONTRIBUTING:586 要求换手率等按公告日对齐,
    伪造公告日会污染下游)。

    只映射语义确实等价的字段。**港股宽表没有的 A 股列不会出现在结果里**
    (实测 A 股 metrics 29 列, 港股投影 26 列, 其中 15 列是 A 股独有的周转率/
    现金比率类指标, 港股宽表确实不提供)。要让两个市场的帧**列集完全一致**,
    需再经 :func:`align_hk_to_ashare` 按参照帧补 null —— 该操作需要 A 股帧
    在手, 故放在这里而非投影内部, 避免把 A 股列名硬编码进港股模块。

    Args:
        table: ``metrics`` / ``income`` / ``balance_sheet`` / ``cash_flow``。
    """
    if table not in hf.HK_CANONICAL_PROJECTION:
        raise ValueError(
            f"未知目标表 {table!r}, 可选 {tuple(hf.HK_CANONICAL_PROJECTION)}"
        )
    if df.is_empty():
        # 空帧同样带完整列集: 契约是"规范表列集恒定", 不因无数据而退化成 0 列,
        # 否则读端要分别处理两种形态。
        return pl.DataFrame(schema=hk_canonical_schema(table))
    if "report_date" not in df.columns:
        raise ValueError("输入帧缺少 report_date 列, 不是港股财务宽表")

    proj = hf.HK_CANONICAL_PROJECTION[table]
    report_date = pl.col("report_date").cast(pl.Utf8).str.slice(0, 10)
    # 🔴 必须一次 select 构造完所有列: 若先 select 出键列再 with_columns 引用
    # 源字段, 源字段已被前一步丢弃 → ColumnNotFoundError。
    exprs = [
        pl.col("symbol"),
        report_date.alias("period_end"),
        # 港股宽表无公告日 → null, 不用报告期冒充
        pl.lit(None, pl.Utf8).alias("announce_date"),
        # 🔴 A 股 fiscal_year 是 Int64 年份; 港股东财 FISCAL_YEAR 是 "12-31"
        # 字符串 (财年结日月-日), 同名不同义, 故由 period_end 重新推导。
        report_date.str.slice(0, 4).cast(pl.Int64).alias("fiscal_year"),
        pl.lit(None, pl.Int64).alias("report_date_ms"),
        report_date.str.to_date("%Y-%m-%d").dt.timestamp("ms").alias("period_end_ms"),
    ]
    for canonical, src in proj.items():
        exprs.append(
            pl.col(src).cast(pl.Float64).alias(canonical)
            if src in df.columns
            else pl.lit(None, pl.Float64).alias(canonical)
        )
    for extra in hf.HK_ONLY_CANONICAL_FIELDS:
        # 🔴 源列缺失时补同 dtype 的 null 列, **不可省略该列**: 上面 proj 循环
        # 已经是这个语义, 两处不一致会让"列集恒定"的契约只在部分字段上成立。
        # schema 漂移是真实场景 —— 给 HK_MAIN_INDICATOR_FIELDS 加字段后,
        # 之前落库的 parquet 没有该列。
        dtype = HK_ONLY_CANONICAL_DTYPES[extra]
        exprs.append(
            pl.col(extra).cast(dtype, strict=False).alias(extra)
            if extra in df.columns
            else pl.lit(None, dtype).alias(extra)
        )

    # 防御: 规范名与港股独有字段重名时只保留前者, 否则 select 会报重复列名
    seen: set[str] = set()
    deduped = []
    for e in exprs:
        name = e.meta.output_name()
        if name in seen:
            continue
        seen.add(name)
        deduped.append(e)
    return df.select(deduped)


def align_hk_to_ashare(hk_df: pl.DataFrame, ashare_df: pl.DataFrame) -> pl.DataFrame:
    """把港股规范化帧的**列集与列序**对齐到 A 股参照帧, 供两市场共用一个页面。

    :func:`hk_financials_canonical` 只产出港股确实有的列; 本函数按参照帧补齐
    缺失列 (以参照帧的 dtype 落 null, 而非 ``Null`` 类型 —— 否则后续
    ``pl.concat`` 两个市场的帧会因类型不匹配报错), 并把港股独有列附在末尾。

    参照帧为空 (A 股财务尚未同步) 时**原样返回**: 此时无从得知目标列集,
    猜测一份硬编码列名只会与真实 A 股 schema 漂移。
    """
    if not ashare_df.columns:
        return hk_df
    missing = [c for c in ashare_df.columns if c not in hk_df.columns]
    out = hk_df
    if missing:
        out = out.with_columns(
            [pl.lit(None, ashare_df.schema[c]).alias(c) for c in missing]
        )
    order = [c for c in ashare_df.columns if c in out.columns]
    order += [c for c in out.columns if c not in order]
    return out.select(order)


def hk_latest_financials(
    data_dir: Path, symbol: str, *, limit: int = 8
) -> dict[str, pl.DataFrame]:
    """取单只港股最近 ``limit`` 期的四张规范化表 (财务概况页一次取全)。

    返回 ``{"metrics": df, "income": df, "balance_sheet": df, "cash_flow": df}``;
    未同步时各值为空帧。按 ``period_end`` 降序, 故 ``head(limit)`` 即最近若干期。

    🔴 结果**混合币种**: 金额列是编报币种 (腾讯为 CNY), 估值列 (``pe_ttm`` /
    ``total_market_cap``) 是港币。调用方展示时必须带上 ``pricing_currency``,
    跨标的比较金额前必须先按币种分组。
    """
    df = get_hk_financial_df(data_dir, "main_indicator")
    empty: dict[str, pl.DataFrame] = {
        t: pl.DataFrame(schema=hk_canonical_schema(t)) for t in hf.HK_CANONICAL_PROJECTION
    }
    if df.is_empty() or "symbol" not in df.columns:
        return empty
    # 归一化 symbol: 内部规范形是 ``00700.HK``, 调用方也可能传裸 ``00700``。
    # 🔴 不归一化会**静默返回空结果** —— 与畸形 SECUCODE 属同一类失败模式
    # (不报错、只是没数据), 是本项目里最难定位的一种。
    try:
        key = hf.hk_fin_secucode(symbol)
    except ValueError:
        key = str(symbol or "")
    mine = df.filter(pl.col("symbol") == key)
    if mine.is_empty():
        return empty
    mine = mine.sort("report_date", descending=True).head(limit)
    return {t: hk_financials_canonical(mine, t) for t in hf.HK_CANONICAL_PROJECTION}


def hk_lot_size_map(data_dir: Path) -> dict[str, int]:
    """从已落库的宽表读 ``{symbol: lot_size}`` (每手股数), 供交易规则消费。"""
    df = get_hk_financial_df(data_dir, "main_indicator")
    if df.is_empty() or "lot_size" not in df.columns:
        return {}
    sub = df.select(["symbol", "lot_size", "report_date"]).filter(
        pl.col("lot_size").is_not_null() & (pl.col("lot_size") > 0)
    )
    if sub.is_empty():
        return {}
    # 用 sort + unique(keep="last", maintain_order=True) 而非 group_by().last():
    # 后者不保证沿用输入排序; 而 unique 若不设 maintain_order, "last" 同样不指
    # "输入顺序里的最后一行"。两者都会随机取到某一期而非最新一期。
    latest = sub.sort("report_date").unique(
        subset="symbol", keep="last", maintain_order=True
    )
    return {
        str(s): int(v)
        for s, v in zip(
            latest.get_column("symbol").to_list(),
            latest.get_column("lot_size").to_list(),
            strict=True,
        )
    }


def hk_financial_symbols(data_dir: Path) -> list[str]:
    """已同步财务数据的港股 symbol 列表 (升序)。"""
    df = get_hk_financial_df(data_dir, "main_indicator")
    if df.is_empty() or "symbol" not in df.columns:
        return []
    return sorted(set(df.get_column("symbol").drop_nulls().to_list()))


def iter_hk_financial_rows(data_dir: Path, table: str) -> Iterable[dict[str, Any]]:
    """逐行迭代某张港股财务表 (供导出/巡检, 避免一次性载入大长表)。"""
    path = hk_financials_path(data_dir, table)
    if not path.exists():
        return
    yield from pl.read_parquet(path).to_dicts()
