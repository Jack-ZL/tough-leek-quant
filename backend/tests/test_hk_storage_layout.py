"""港股存储隔离 (方案 §4 闸3) 与目录映射的市场维度。

闸3 的核心主张是: **港股数据落在物理独立的目录**, 因此 A 股那 11 处
``kline_daily/**/*.parquet`` 递归 glob 永远扫不到港股文件, 反之亦然。目录级隔离
比 ``if market == HK`` 分支可靠 —— 分支会漏, 目录不会。本文件把这条主张钉成断言,
而不是停留在文档里。

同时覆盖两处容易漂移的清单:
- ``DataStore._register_views`` (启动时注册)
- ``KlineRepository.rebuild_views`` (盘后管道 / 清库后重挂, 自称「唯一权威实现」)

只改前者会导致「视图启动时存在, 但盘后跑完不刷新 → 读到旧数据」, 即 CONTRIBUTING
的拒收情形「写入后存在已知缓存不一致」。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.market import Market
from app.tickflow.repository import (
    DataStore,
    KlineRepository,
    daily_dirname,
    enriched_dirname,
)

#: 方案 §4 闸3 约定的港股目录名。改名即破坏隔离契约, 必须同步改这里。
HK_DIRS = ("instruments_hk", "kline_hk_daily", "kline_hk_enriched")


# ---------------------------------------------------------------------------
# A 股目录名逐字不变 (存量回归护栏)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset_type,daily,enriched", [
    ("stock", "kline_daily", "kline_daily_enriched"),
    ("etf", "kline_etf_daily", "kline_etf_enriched"),
    ("index", "kline_index_daily", "kline_index_enriched"),
])
def test_cn_dirnames_are_unchanged(asset_type, daily, enriched):
    """加 market 维度后, A 股三类资产的目录名必须逐字不变。

    这是「存量调用方零改动」的前提: 全仓 12 处调用都按位置传 asset_type,
    任何一个目录名变了都会让存量数据「凭空消失」(写到新目录、旧目录读不到)。
    """
    assert daily_dirname(asset_type) == daily
    assert enriched_dirname(asset_type) == enriched
    # 显式传 CN 与不传等价
    assert daily_dirname(asset_type, market=Market.CN) == daily
    assert enriched_dirname(asset_type, market=Market.CN) == enriched


def test_market_is_keyword_only():
    """market 必须仅关键字 —— 保护「所有调用方按位置传 asset_type」的兼容性。

    若允许位置传参, ``daily_dirname("stock", Market.HK)`` 这种写法就能编译通过,
    而 ``daily_dirname("stock", "etf")`` 之类的历史误用也会静默改变语义。
    """
    with pytest.raises(TypeError):
        daily_dirname("stock", Market.HK)  # type: ignore[misc]
    with pytest.raises(TypeError):
        enriched_dirname("stock", Market.HK)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 港股目录映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset_type,daily,enriched", [
    ("stock", "kline_hk_daily", "kline_hk_enriched"),
    ("index", "kline_hk_index_daily", "kline_hk_index_enriched"),
])
def test_hk_dirnames_are_physically_separate(asset_type, daily, enriched):
    """港股目录名与 A 股无任何前缀重叠, 递归 glob 不可能互相扫到。"""
    assert daily_dirname(asset_type, market=Market.HK) == daily
    assert enriched_dirname(asset_type, market=Market.HK) == enriched
    # 与 A 股同名即隔离失败
    assert daily != daily_dirname(asset_type)
    assert enriched != enriched_dirname(asset_type)


def test_hk_etf_is_rejected_rather_than_silently_routed_to_cn():
    """港股无 ETF (本仓 ETF 链路是 A 股场内基金专有) → KeyError, 不回退 A 股目录。

    静默回退会把港股数据写进 A 股分区, 污染全市场统计且**难以事后分离**
    (混在一个目录里, 只能靠 symbol 后缀逐行挑)。fail-closed 让错误在写入前暴露。
    """
    with pytest.raises(KeyError):
        daily_dirname("etf", market=Market.HK)
    with pytest.raises(KeyError):
        enriched_dirname("etf", market=Market.HK)


@pytest.mark.parametrize("market", ["US", "XX", None, "", 123])
def test_unknown_market_raises_instead_of_defaulting_to_cn(market):
    """未知市场一律 ValueError —— 与 ``rules_for`` 同一约定: 不静默当 A 股处理。

    静默回退 CN 的后果是港股数据写进 ``kline_daily``, 且**不报错**。
    """
    with pytest.raises(ValueError):
        daily_dirname("stock", market=market)
    with pytest.raises(ValueError):
        enriched_dirname("stock", market=market)


def test_market_accepts_str_and_enum_equivalently():
    """``Market`` 是 StrEnum, 传 "HK" 与 Market.HK 必须等价 (调用方不必都 import 枚举)。"""
    assert daily_dirname("stock", market="HK") == daily_dirname("stock", market=Market.HK)
    assert daily_dirname("stock", market="CN") == daily_dirname("stock", market=Market.CN)


# ---------------------------------------------------------------------------
# DataStore 目录创建
# ---------------------------------------------------------------------------

def test_datastore_creates_hk_dirs(tmp_path):
    """港股目录随 DataStore 初始化创建, 与 A 股目录同批 (不依赖首次写入)。"""
    store = DataStore(tmp_path)
    for sub in HK_DIRS:
        assert (store.data_dir / sub).is_dir(), f"{sub} 未创建"


# ---------------------------------------------------------------------------
# 隔离性质: 递归 glob 互不可见
# ---------------------------------------------------------------------------

def _write_daily(root, subdir: str, symbol: str, day: dt.date) -> None:
    part = root / subdir / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol],
        "date": [day],
        "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
        "volume": [1000.0], "amount": [1500.0],
    }).write_parquet(part / "part.parquet")


def test_cn_recursive_glob_cannot_see_hk_files(tmp_path):
    """闸3 的实证: A 股 ``kline_daily/**/*.parquet`` 扫不到港股, 反之亦然。

    这是整个隔离方案唯一真正重要的性质 —— 前面那些目录名断言只是它的前提。
    """
    day = dt.date(2026, 11, 13)
    _write_daily(tmp_path, daily_dirname("stock"), "600000.SH", day)
    _write_daily(tmp_path, daily_dirname("stock", market=Market.HK), "00700.HK", day)

    cn_files = sorted(p.name for p in (tmp_path / "kline_daily").rglob("*.parquet"))
    hk_files = sorted(p.name for p in (tmp_path / "kline_hk_daily").rglob("*.parquet"))
    assert len(cn_files) == 1 and len(hk_files) == 1

    repo = KlineRepository(DataStore(tmp_path))
    repo.rebuild_views()
    cn_symbols = {r[0] for r in repo.execute_all("SELECT symbol FROM kline_daily")}
    hk_symbols = {r[0] for r in repo.execute_all("SELECT symbol FROM kline_hk_daily")}
    assert cn_symbols == {"600000.SH"}
    assert hk_symbols == {"00700.HK"}


def test_unified_views_exclude_hk(tmp_path):
    """unified 视图 (instruments_all / 统一日K) 不得 UNION 港股 —— 闸3 要求独立。

    混进去会让所有按 asset_type 过滤的存量查询突然多出港股行: 全市场统计、
    选股器、板块轮动的分母都会被改变, 且 A 股的 T+1/涨跌停假设对港股不成立。
    """
    day = dt.date(2026, 11, 13)
    _write_daily(tmp_path, daily_dirname("stock"), "600000.SH", day)
    _write_daily(tmp_path, daily_dirname("stock", market=Market.HK), "00700.HK", day)

    repo = KlineRepository(DataStore(tmp_path))
    repo.rebuild_views()
    rows = repo.execute_all("SELECT symbol FROM kline_daily_all")
    symbols = {r[0] for r in rows}
    assert "600000.SH" in symbols
    assert "00700.HK" not in symbols, "港股泄漏进 unified 视图"


# ---------------------------------------------------------------------------
# 两份视图清单必须同步 (rebuild_views 漂移护栏)
# ---------------------------------------------------------------------------

def test_rebuild_views_registers_hk_views(tmp_path):
    """``rebuild_views`` 必须覆盖港股视图, 否则盘后管道跑完港股视图不刷新。

    回归背景: ``_register_views`` (启动) 与 ``rebuild_views`` (盘后/清库) 是两份
    独立清单。只在启动那份加港股, 症状是「重启后能看到新数据, 不重启就一直是旧的」
    —— 极难排查, 且正好命中 CONTRIBUTING 的拒收条件「写入后存在已知缓存不一致」。
    """
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    day = dt.date(2026, 11, 13)

    # 启动时目录为空 → 视图尚未注册 (空目录 read_parquet 无匹配, 降级为 debug)
    _write_daily(store.data_dir, daily_dirname("stock", market=Market.HK), "00700.HK", day)
    repo.rebuild_views()

    symbols = {r[0] for r in repo.execute_all("SELECT symbol FROM kline_hk_daily")}
    assert symbols == {"00700.HK"}, "rebuild_views 未重挂港股视图"


def test_rebuild_views_picks_up_hk_data_written_after_startup(tmp_path):
    """写入 → rebuild → 立即可读, 不需要重启进程。"""
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    repo.rebuild_views()

    _write_daily(store.data_dir, daily_dirname("stock", market=Market.HK), "09988.HK",
                 dt.date(2026, 11, 14))
    repo.rebuild_views()

    count = repo.execute_one("SELECT count(*) FROM kline_hk_daily")[0]
    assert count == 1


# ---------------------------------------------------------------------------
# append_daily_asset 的市场维度 (公开落库入口)
# ---------------------------------------------------------------------------

def _daily_frame(symbol: str, day: dt.date) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "date": [day],
        "open": [10.0], "high": [11.0], "low": [9.5], "close": [10.5],
        "volume": [1000.0], "amount": [10500.0],
    })


@pytest.mark.parametrize("asset_type,expected_dir", [
    ("stock", "kline_daily"),
    ("index", "kline_index_daily"),
    ("etf", "kline_etf_daily"),
])
def test_append_daily_asset_cn_dirs_unchanged(tmp_path, asset_type, expected_dir):
    """A 股默认路径逐字不变 (存量回归护栏): 不传 market 与传 Market.CN 等价。"""
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily_asset(asset_type, _daily_frame("600000.SH", dt.date(2026, 11, 13)))
    repo.append_daily_asset(
        asset_type, _daily_frame("600000.SH", dt.date(2026, 11, 14)), market=Market.CN
    )

    assert (tmp_path / expected_dir).is_dir(), f"{asset_type} 未落到 {expected_dir}"
    assert not list((tmp_path / "kline_hk_daily").rglob("*.parquet")), "A 股写入污染了港股目录"


def test_append_daily_asset_routes_hk_to_hk_dir(tmp_path):
    """港股经**查表**落到 kline_hk_daily, 且 A 股目录一个字节都不该有。"""
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily_asset(
        "stock", _daily_frame("00700.HK", dt.date(2026, 11, 13)), market=Market.HK
    )

    assert list((tmp_path / "kline_hk_daily").rglob("*.parquet")), "港股未落到独立目录"
    assert not list((tmp_path / "kline_daily").rglob("*.parquet")), "港股污染了 A 股目录"


def test_append_daily_asset_accepts_str_market(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily_asset("stock", _daily_frame("00700.HK", dt.date(2026, 11, 13)), market="HK")
    assert list((tmp_path / "kline_hk_daily").rglob("*.parquet"))


def test_append_daily_asset_rejects_hk_etf_rather_than_falling_back(tmp_path):
    """港股无 ETF 链路 → KeyError, 不静默写进 A 股 ETF 分区。"""
    repo = KlineRepository(DataStore(tmp_path))
    with pytest.raises(KeyError):
        repo.append_daily_asset("etf", _daily_frame("00700.HK", dt.date(2026, 11, 13)),
                                market=Market.HK)
    assert not list((tmp_path / "kline_etf_daily").rglob("*.parquet"))


def test_append_daily_asset_rejects_unknown_market(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    with pytest.raises(ValueError):
        repo.append_daily_asset("stock", _daily_frame("AAPL", dt.date(2026, 11, 13)), market="US")


def test_append_daily_asset_empty_frame_is_noop(tmp_path):
    """空帧不建目录 (与存量 append_daily 行为一致)。"""
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily_asset("stock", pl.DataFrame(), market=Market.HK)
    assert not list((tmp_path / "kline_hk_daily").rglob("*.parquet"))


def test_append_hk_enriched_writes_only_storage_cols(tmp_path):
    """港股 enriched 与 A 股同款: 磁盘只写 ENRICHED_STORAGE_COLS 窄表。"""
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS

    repo = KlineRepository(DataStore(tmp_path))
    df = _daily_frame("00700.HK", dt.date(2026, 11, 13)).with_columns(
        pl.lit(1.23).alias("turnover_rate"), pl.lit("不该落盘的列").alias("_transient")
    )
    repo.append_hk_enriched(df)

    written = pl.read_parquet(next((tmp_path / "kline_hk_enriched").rglob("*.parquet")))
    assert "_transient" not in written.columns
    assert set(written.columns) <= set(ENRICHED_STORAGE_COLS)
    assert "turnover_rate" in written.columns
