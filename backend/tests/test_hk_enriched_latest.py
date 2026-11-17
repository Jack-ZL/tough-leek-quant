"""港股 enriched「最新日全量快照」读取器契约测试 (:meth:`get_hk_enriched_latest`)。

为什么单独一个文件
------------------
``tests/test_hk_api.py`` 钉的是 **HTTP 契约**, 且逐标的 (:meth:`get_hk_daily`)。
本文件钉的是 **仓库层的全量快照读取器** —— 自选/监控这类「每标的一行」消费方的
数据入口, 此前港股侧完全缺失 (只有 N 次单标的 scan)。

🔴 最关键的一条是 ``test_latest_snapshot_matches_get_hk_daily_last_row``: 自选列表
那一行与详情抽屉最后一根蜡烛**必须逐字相等**。两条路径各自 scan 各自的预热窗,
窗口一旦分叉就会给出两个都「看起来对」的 MA/MACD —— 正是 CONTRIBUTING §3 要拦的
形状。共享常量 ``_HK_ENRICHED_WARMUP_DAYS`` 是手段, 这条测试才是保证。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import _HK_ENRICHED_WARMUP_DAYS, DataStore, KlineRepository

END = dt.date(2026, 11, 13)


@pytest.fixture()
def repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def _enriched_frame(
    symbol: str, dates: list[dt.date], closes: list[float], *, volumes: list[float] | None = None
) -> pl.DataFrame:
    """存储窄表 enriched (与 :meth:`append_hk_enriched` 落盘的 14 列同 dtype)。

    dtype 必须与 ``ENRICHED_STORAGE_SCHEMA`` 对齐: 读取端 ``scan_enriched_parquet``
    钉了 schema, 类型不符会让扫描抛错并被吞成空帧, 测试就会「通过」在一条根本没
    读到数据的路径上。与 ``test_hk_api._enriched_frame`` 逐字同款。
    """
    n = len(dates)
    vols = volumes or [1_000_000.0] * n
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": vols,
        "amount": [c * v for c, v in zip(closes, vols, strict=True)],
        "raw_close": closes, "raw_high": closes, "raw_low": closes,
        "turnover_rate": [None] * n,
        "consecutive_limit_ups": [0] * n,
        "consecutive_limit_downs": [0] * n,
        "quote_ts": [0] * n,
    }).with_columns(
        pl.col("turnover_rate").cast(pl.Float64),
        pl.col("consecutive_limit_ups").cast(pl.UInt32),
        pl.col("consecutive_limit_downs").cast(pl.UInt32),
        pl.col("quote_ts").cast(pl.Int64),
    )


def _dates(n: int, end: dt.date = END) -> list[dt.date]:
    """``end`` 往前 n 个**日历日** (升序)。指标只关心顺序与根数, 不关心是否交易日。"""
    return [end - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]


def _ramp(n: int, start_price: float = 100.0, step: float = 0.5) -> list[float]:
    """单调递增价格: 让 MA/MACD 有非平凡值, 全平序列会让「指标算没算」看不出来。"""
    return [start_price + i * step for i in range(n)]


def _seed_dim(tmp_path, rows: list[dict]) -> None:
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


# ---------------------------------------------------------------------------
# 空态: 从未同步过是正常状态, 不是故障
# ---------------------------------------------------------------------------

def test_empty_before_any_sync(repo):
    """零 parquet → 空帧 + None, 不抛 (与 _scan_hk_enriched_symbol 同口径)。"""
    df, as_of = repo.get_hk_enriched_latest()
    assert df.is_empty()
    assert as_of is None


def test_empty_dir_exists_but_no_partitions(repo, tmp_path):
    """目录建了但没有任何 date= 分区 → 同样空帧 + None。"""
    (tmp_path / "kline_hk_enriched").mkdir(parents=True, exist_ok=True)
    df, as_of = repo.get_hk_enriched_latest()
    assert df.is_empty()
    assert as_of is None


def test_refresh_false_on_cold_cache_does_not_scan(repo, monkeypatch):
    """🔴 ``refresh=False`` 冷缓存**不得**触发同步重建。

    行情轮询线程走这条路: 在 tick 热路径上做 scan+compute 会阻塞 A 股行情。
    """
    calls: list[bool] = []
    monkeypatch.setattr(
        repo, "_refresh_hk_enriched", lambda: calls.append(True), raising=True
    )
    df, as_of = repo.get_hk_enriched_latest(refresh=False)
    assert df.is_empty()
    assert as_of is None
    assert calls == [], "refresh=False 仍然重建了缓存"


# ---------------------------------------------------------------------------
# 正常路径: 最新日每标的一行
# ---------------------------------------------------------------------------

def test_returns_one_row_per_symbol_on_latest_date(repo):
    """两只港股 → 两行, as_of = 最新日, 且**只**含最新日 (不带历史行)。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    repo.append_hk_enriched(_enriched_frame("09988.HK", _dates(30), _ramp(30, 80.0)))

    df, as_of = repo.get_hk_enriched_latest()
    assert as_of == END
    assert df.height == 2, f"应每标的一行, 实得 {df.height} 行: {df['date'].to_list()}"
    assert sorted(df["symbol"].to_list()) == ["00700.HK", "09988.HK"]
    assert df["date"].unique().to_list() == [END]


def test_latest_date_wins_when_symbols_are_staggered(repo):
    """🔴 as_of 取**全表最新日**; 停在旧日的标的仍返回它自己那一行。

    港股半日市/停牌会让各标的最新日不齐。若按「每标的各自最新日」取行, as_of 就
    说不清是哪天; 这里与 A 股 ``get_enriched_latest`` 同语义 —— 一个全局 as_of,
    旧标的如实缺席 (由调用方 LEFT JOIN 成 null 行), 不拿旧价冒充新价。
    """
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    stale = [d - dt.timedelta(days=3) for d in _dates(30)]
    repo.append_hk_enriched(_enriched_frame("09988.HK", stale, _ramp(30, 80.0)))

    df, as_of = repo.get_hk_enriched_latest()
    assert as_of == END
    assert df["symbol"].to_list() == ["00700.HK"], "停在旧日的标的不得混进最新日快照"


def test_indicators_are_recomputed_not_null(repo):
    """磁盘只有 14 列窄表 → 指标必须读时重算, 否则自选/监控无列可评估。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(80), _ramp(80)))

    df, _ = repo.get_hk_enriched_latest()
    for col in ("ma5", "ma20", "ma60"):
        assert col in df.columns, f"缺指标列 {col}"
        assert df[col][0] is not None, f"{col} 未重算 (预热窗不够?)"


def test_symbols_filter(repo):
    """``symbols=`` 只收窄返回行, 不改 as_of。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    repo.append_hk_enriched(_enriched_frame("09988.HK", _dates(30), _ramp(30, 80.0)))

    df, as_of = repo.get_hk_enriched_latest(symbols=["09988.HK"])
    assert df["symbol"].to_list() == ["09988.HK"]
    assert as_of == END

    empty, still = repo.get_hk_enriched_latest(symbols=["600000.SH"])
    assert empty.is_empty()
    assert still == END, "过滤到空不该把 as_of 也丢掉"


# ---------------------------------------------------------------------------
# 🔴 与逐标的路径的一致性 (共享预热窗常量)
# ---------------------------------------------------------------------------

def test_latest_snapshot_matches_get_hk_daily_last_row(repo):
    """自选那一行 == 详情抽屉最后一根蜡烛, **逐列**相等。

    两条路径独立 scan 各自的预热窗; 窗口分叉 → MA/MACD 分叉 → 两个都「看起来对」
    的数。这条测试是 ``_HK_ENRICHED_WARMUP_DAYS`` 存在的理由。
    """
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(200), _ramp(200)))

    snap, as_of = repo.get_hk_enriched_latest()
    assert as_of == END
    drawer = repo.get_hk_daily("00700.HK", END, END)
    assert drawer.height == 1

    row_snap = snap.row(0, named=True)
    row_drawer = drawer.row(0, named=True)
    shared = [c for c in snap.columns if c in drawer.columns]
    assert len(shared) > 20, f"列集重叠过少, 比较无意义: {shared}"
    diffs = {c: (row_snap[c], row_drawer[c]) for c in shared if row_snap[c] != row_drawer[c]}
    assert not diffs, f"最新日快照与逐标的日K分叉: {diffs}"


def test_warmup_window_constant_is_shared(repo):
    """常量本身也要钉住: 150 日历日 ≈ 100 交易日 > MA60/EMA60 的 60 根需求。"""
    assert _HK_ENRICHED_WARMUP_DAYS == 150
    assert _HK_ENRICHED_WARMUP_DAYS > 60 * 1.7, "预热窗不足以喂饱 MA60"


def test_warmup_feeds_ma_across_snapshot_boundary(repo):
    """🔴 只有最新日一行落盘时指标算不出 (height<2 直接返回); 有历史就必须算出。

    钉住「预热窗真的被 scan 到了」而不是只读了最新日分区。
    """
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(70), _ramp(70)))
    df, _ = repo.get_hk_enriched_latest()
    assert df["ma60"][0] is not None, "预热窗未生效: 70 根历史算不出 MA60"


# ---------------------------------------------------------------------------
# 市场口径: 差异住在数据里, 不住在分支里
# ---------------------------------------------------------------------------

def test_hk_turnover_uses_share_unit_not_lot(repo, tmp_path):
    """换手率 volume 单位按「股」(A 股按「手」, 差 100 倍)。"""
    _seed_dim(tmp_path, [{
        "symbol": "00700.HK", "name": "腾讯控股", "is_cny_counter": False,
        "hkd_counter": None, "listing_date": None, "industry": None,
        "float_shares": 1_000_000_000.0,
    }])
    repo.append_hk_enriched(_enriched_frame(
        "00700.HK", _dates(30), _ramp(30), volumes=[31_493_353.0] * 30
    ))

    df, _ = repo.get_hk_enriched_latest()
    rate = df["turnover_rate"][0]
    assert rate is not None, "维表已就位却没算出换手率"
    assert rate == pytest.approx(31_493_353.0 / 1_000_000_000.0 * 100, rel=1e-6), (
        f"换手率 {rate} 不是「股」口径 (若按手会小 100 倍)"
    )


def test_limit_columns_are_null_for_hk(repo, tmp_path):
    """港股无涨跌停 → 限价列恒 null, 大涨大跌都不记连板。"""
    _seed_dim(tmp_path, [{
        "symbol": "00700.HK", "name": "腾讯控股", "is_cny_counter": False,
        "hkd_counter": None, "listing_date": None, "industry": None,
        "float_shares": 1_000_000_000.0,
    }])
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30, step=3.0)))

    df, _ = repo.get_hk_enriched_latest()
    for col in ("limit_up_price", "limit_down_price", "signal_limit_up", "signal_limit_down"):
        assert col in df.columns, f"维表已就位却没算出 {col} (断言会假通过)"
        assert df[col][0] is None, f"{col} 对港股必须恒 null"
    assert df["consecutive_limit_ups"][0] == 0


# ---------------------------------------------------------------------------
# 缓存与失效
# ---------------------------------------------------------------------------

def test_second_call_hits_cache_without_rescanning(repo, monkeypatch):
    """热路径反复调用 (tick 轮询) 不得每次重扫。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    repo.get_hk_enriched_latest()

    calls: list[bool] = []
    monkeypatch.setattr(
        repo, "_refresh_hk_enriched", lambda: calls.append(True), raising=True
    )
    df, as_of = repo.get_hk_enriched_latest()
    assert df.height == 1 and as_of == END
    assert calls == [], "缓存已热却仍重建"


def test_append_invalidates_cache(repo):
    """🔴 落盘即失效: 否则同步完自选仍读旧缓存 (看板显示旧数据的根因形状)。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    _, as_of = repo.get_hk_enriched_latest()
    assert as_of == END

    newer = END + dt.timedelta(days=1)
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30, newer), _ramp(30)))
    df, as_of2 = repo.get_hk_enriched_latest()
    assert as_of2 == newer, "append 后缓存未失效"
    assert df["date"].unique().to_list() == [newer]


def test_invalidate_then_reread(repo):
    """显式失效后再读会重建 (懒加载契约)。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    assert repo.get_hk_enriched_latest()[0].height == 1
    repo.invalidate_hk_enriched()
    assert repo._hk_enriched_cache is None
    df, as_of = repo.get_hk_enriched_latest()
    assert df.height == 1 and as_of == END


def test_clear_cache_drops_hk_snapshot(repo):
    """``clear_cache`` (清数据/重置场景) 必须连港股快照一起清。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    repo.get_hk_enriched_latest()
    repo.clear_cache()
    assert repo._hk_enriched_cache is None
    assert repo._hk_enriched_cache_date is None


# ---------------------------------------------------------------------------
# 🔴 市场隔离: 港股快照不得混进 A 股行, 反之亦然
# ---------------------------------------------------------------------------

def test_hk_snapshot_never_contains_ashare_rows(repo):
    """A 股 enriched 与港股 enriched 物理隔离; 混表会让消费方静默读错市场。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    df, _ = repo.get_hk_enriched_latest()
    assert all(s.endswith(".HK") for s in df["symbol"].to_list())


def test_ashare_cache_unaffected_by_hk_writes(repo):
    """写港股不得污染 A 股 ``get_enriched_latest`` (它零 market 参数, 只认 A 股表)。"""
    repo.append_hk_enriched(_enriched_frame("00700.HK", _dates(30), _ramp(30)))
    repo.get_hk_enriched_latest()
    df_cn, _ = repo.get_enriched_latest()
    assert df_cn.is_empty() or not any(
        s.endswith(".HK") for s in df_cn["symbol"].to_list()
    ), "A 股 enriched 缓存里出现了港股行"
