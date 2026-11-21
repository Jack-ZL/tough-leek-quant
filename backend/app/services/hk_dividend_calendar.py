"""港股分红日历 —— 全市场除净/派息事件的**只读**聚合。

为什么这个功能不需要新同步、不需要新存储
--------------------------------------
:func:`app.plugins.eastmoney.hk_dividend.fetch_hk_dividend_calendar` 实测可以
**不带 ``SECUCODE``** 查 ``RPT_HKF10_MAIN_DIVBASIC``: 一次翻页请求就覆盖全市场
(13 个月窗口 1592 行 / 8 页)。既然上游支持按除净日窗口取全市场, 日历就没有理由
先落一张表再读它 —— 落库只会带来「同步没跑 → 日历是旧的」这一类额外失败模式,
而分红方案本身还会被公司修订 (``UPDATE_DATE`` 列存在即为此), 实时读反而更准。

因此本模块**不写任何存储**, 每次调用 = 1~8 次上游请求 + 两次本地小读取
(维表名称、最近几个日K分区的收盘价)。上游主机的令牌桶与熔断由
:class:`~app.plugins.eastmoney.client.EastmoneyClient` 负责, 这里不另加缓存:
读路径无写后一致性问题 (§6.1), 而前端 TanStack Query 的 ``staleTime`` 已经能
挡住重复打开页面造成的重复请求。

🔴 与 ``adj_factor_hk`` 的关系: **刻意不读它**。那边存的是为复权链服务的
``ex_factor``, 只覆盖 ``sync_hk_adj_factor`` 跑过的标的 (实测 565/2862), 且只含
解析成功、通过比例合理性闸的事件。拿它当日历会同时犯两个错: 覆盖率假象 (没同步
过的标的看起来「从不派息」) 和口径混淆 (因子是价格调整量, 不是每股派息额)。
两条链路共用同一个上游报表与同一个解析器, 但**各自独立取数**。

单位口径 (CONTRIBUTING §3.1)
---------------------------
``dividend_yield_pct`` 是**百分比数值**: ``5.0`` 表示 ``5.0%``, 不是 ``0.05``。
后缀 ``_pct`` 与 enriched 的 ``turnover_rate`` (同为百分比数值) 一致; 换算在
本模块内一次做完, 前端**不得**再乘 100, 也禁止「小于 1 就乘 100」的启发式。
分母用**不复权**收盘价 (``kline_hk_daily.close``, 即 enriched 的 ``raw_close``):
派息额是真实每股现金, 用前复权价会把有分红史的标的股息率系统性算高 (§3.2)。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.market import Market
from app.plugins.eastmoney.client import EastmoneyClient
from app.plugins.eastmoney.hk_dividend import (
    HkDividendEvent,
    fetch_hk_dividend_calendar,
    parse_hk_dividend_rows,
)
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import daily_dirname

logger = logging.getLogger(__name__)

#: 输出列顺序。API 层直接 ``to_dicts()``, 故这里就是响应字段顺序的唯一事实来源。
HK_DIVIDEND_CALENDAR_COLS = (
    "symbol",
    "name",
    "industry",
    "ex_date",
    "pay_date",
    "notice_date",
    "fiscal_year",
    "report_type",
    "plan",
    "dividend_hkd",
    "bonus_ratio",
    "parsed",
    "raw_close",
    "price_date",
    "dividend_yield_pct",
)

#: 取最新收盘价时往回看几个**日K分区**。日历里的标的多数当天有行情, 但停牌股会
#: 缺最近若干个分区; 10 个分区 ≈ 两周, 足以覆盖常规停牌, 而成本只是 10 个小文件
#: (``kline_hk_daily`` 实测有 9102 个分区, 全扫是不可接受的)。
#: 超出这个窗口仍无收盘价的标的, ``raw_close``/``dividend_yield_pct`` 留 null
#: (fail closed), **不**用更旧的价格冒充当前价。
HK_DIVIDEND_CLOSE_LOOKBACK_PARTITIONS = 10


def _hk_daily_dir(data_dir: Path) -> Path:
    return data_dir / daily_dirname("stock", market=Market.HK)


def _latest_hk_closes(
    data_dir: Path,
    symbols: list[str],
    *,
    lookback: int = HK_DIVIDEND_CLOSE_LOOKBACK_PARTITIONS,
) -> pl.DataFrame:
    """每只标的**最近一个有行情的分区**里的收盘价, 列为 ``symbol/raw_close/price_date``。

    🔴 读的是 ``kline_hk_daily`` 而不是 enriched: 项目契约是「落库必须原始价」,
    所以这里的 ``close`` 就是不复权价, 与 enriched 的 ``raw_close`` 同值, 但不需要
    触发 enriched 的懒加载重建 (``get_hk_enriched_latest(refresh=True)`` 会全量
    scan + 重算指标, 对一个只读日历来说太重)。

    从最新分区往回扫, 每只标的取**第一次出现**的收盘价即为其最近价; 扫满
    ``lookback`` 个分区或集齐全部标的即停。目录不存在/不可读时返回空表 —— 调用方
    据此把股息率留 null, 而不是抛错让整个日历 500。
    """
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "raw_close": pl.Float64, "price_date": pl.Date}
    )
    if not symbols:
        return empty

    daily_dir = _hk_daily_dir(data_dir)
    try:
        # 分区目录名是 ``date=YYYY-MM-DD``, 字典序即时间序
        partitions = sorted(
            (p for p in daily_dir.iterdir() if p.is_dir() and p.name.startswith("date=")),
            key=lambda p: p.name,
            reverse=True,
        )[:lookback]
    except OSError as e:
        logger.debug("港股日K目录不可读 (%s): %s", daily_dir, e)
        return empty
    if not partitions:
        return empty

    wanted = set(symbols)
    frames: list[pl.DataFrame] = []
    for part in partitions:
        path = part / "part.parquet"
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path, columns=["symbol", "date", "close"])
        except Exception as e:  # noqa: BLE001 — 单个分区损坏不该让整个日历失败
            logger.warning("港股日K分区读取失败, 跳过 %s: %s", path, e)
            continue
        df = df.filter(pl.col("symbol").is_in(list(wanted)))
        if df.is_empty():
            continue
        frames.append(
            df.rename({"close": "raw_close", "date": "price_date"}).select(
                "symbol", "raw_close", "price_date"
            )
        )
        # 越新的分区越先扫, 所以已收进来的标的可以从候选里剔除
        wanted -= set(df["symbol"].to_list())
        if not wanted:
            break

    if not frames:
        return empty
    # 新分区在前, ``keep="first"`` 即每只标的的最近价
    return pl.concat(frames, how="vertical").unique(subset=["symbol"], keep="first")


def _read_hk_instrument_names(data_dir: Path) -> pl.DataFrame:
    """维表里的 ``symbol/name/industry``; 维表缺失或不可读时返回空表。

    直读 parquet 而不走 ``instruments_hk`` 视图 —— 与 ``app.api.hk._read_hk_instruments``
    同一理由: 视图在目录为空时不注册, 查询会 500 而不是如实表达「还没同步维表」。
    名称缺失时日历仍应可用 (只是显示代码), 故这里降级为空表而非抛错。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "name": pl.String, "industry": pl.String})
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return empty
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败, 日历将只显示代码: %s", e)
        return empty
    cols = [c for c in ("symbol", "name", "industry") if c in df.columns]
    if "symbol" not in cols:
        return empty
    out = df.select(cols)
    for missing in ("name", "industry"):
        if missing not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.String).alias(missing))
    # 维表按构造应当 symbol 唯一, 但左连接遇到重复键会**静默把日历行翻倍**
    # (一次除净变两行), 这种错误在页面上看不出来。一行 unique 换掉这个失败模式。
    return out.select("symbol", "name", "industry").unique(subset=["symbol"], keep="first")


def hk_dividend_calendar(
    repo,
    *,
    start: date,
    end: date,
    client: EastmoneyClient | None = None,
) -> pl.DataFrame:
    """``[start, end]`` 内除净的**全市场**港股分红事件, 已 join 名称与股息率。

    Args:
        repo: ``Repository``, 只用它的 ``store.data_dir`` (读维表与日K分区)。
        start/end: 除净日窗口 (含端点)。
        client: 复用已有的东财客户端; 为 ``None`` 时自建并在结束时关闭。

    Returns:
        列为 :data:`HK_DIVIDEND_CALENDAR_COLS` 的 DataFrame, 按 ``(ex_date, symbol)``
        升序。窗口内无事件时返回**空表而不是抛错** —— 「这段时间没人除净」是合法
        答案 (港股分红高度集中在年报季)。上游请求失败则原样抛
        :class:`~app.plugins.eastmoney.client.EastmoneyError`, 由 API 层转成 502;
        **不得**把失败降级成空表, 那会把「拉不到」伪装成「没有分红」(§4)。

    🔴 ``dividend_yield_pct`` 是百分比数值 (``5.0`` = ``5.0%``), 分母是**不复权**
    收盘价, 且是**单次派息**的收益率而非 TTM。三个口径都写在模块 docstring 里,
    改动任一项都必须同步改 ``tests/test_hk_dividend_calendar.py`` 的单位测试。

    ⚠️ ``raw_close``/``price_date``/``dividend_yield_pct`` 为 null 表示「最近
    :data:`HK_DIVIDEND_CLOSE_LOOKBACK_PARTITIONS` 个日K分区里没有这只标的的收盘价」
    (未同步 / 长期停牌), **不是** 0。前端必须显示为「—」。
    """
    own_client = client is None
    if own_client:
        client = EastmoneyClient()
    try:
        rows = fetch_hk_dividend_calendar(client, start=start, end=end)
    finally:
        if own_client:
            client.close()

    events: list[HkDividendEvent] = parse_hk_dividend_rows(rows, start=start, end=end)
    schema = {
        "symbol": pl.String,
        "ex_date": pl.Date,
        "pay_date": pl.Date,
        "notice_date": pl.Date,
        "fiscal_year": pl.String,
        "report_type": pl.String,
        "plan": pl.String,
        "dividend_hkd": pl.Float64,
        "bonus_ratio": pl.Float64,
        "parsed": pl.Boolean,
    }
    if not events:
        df = pl.DataFrame(schema=schema)
    else:
        # HkDividendEvent 是 NamedTuple, 字段顺序即 schema 顺序 (两者都在本仓库内,
        # 由 test_calendar_schema_matches_event_fields 钉住), 故可直接按行构造。
        df = pl.DataFrame([tuple(e) for e in events], schema=schema, orient="row")

    data_dir = repo.store.data_dir
    symbols = df["symbol"].unique().to_list() if df.height else []
    df = df.join(_read_hk_instrument_names(data_dir), on="symbol", how="left")
    df = df.join(_latest_hk_closes(data_dir, symbols), on="symbol", how="left")

    # 🔴 股息率 = 单次每股现金分红 ÷ 最新**不复权**收盘 × 100 (百分比数值)。
    # 分母非正或缺失时留 null: 0 会被读成「零股息」, 而真相是「算不出来」。
    df = df.with_columns(
        (
            pl.when(
                pl.col("raw_close").is_not_null()
                & pl.col("raw_close").is_finite()
                & (pl.col("raw_close") > 0)
                & pl.col("dividend_hkd").is_not_null()
            )
            .then(pl.col("dividend_hkd") / pl.col("raw_close") * 100.0)
            .otherwise(None)
        )
        .cast(pl.Float64)
        .alias("dividend_yield_pct")
    )

    return df.select(list(HK_DIVIDEND_CALENDAR_COLS)).sort(["ex_date", "symbol"])


__all__ = [
    "HK_DIVIDEND_CALENDAR_COLS",
    "HK_DIVIDEND_CLOSE_LOOKBACK_PARTITIONS",
    "hk_dividend_calendar",
]
