"""港股日K同步: universe 口径、单位透传、软失败、增量起点与落库隔离。

网络一律用假 client 挡掉 (东财 push2 对突发请求有 IP 级临时封禁, 测试触网不可复现)。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.market import Market
from app.plugins.eastmoney.client import EastmoneyError
from app.services import kline_sync_hk as sync_hk
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository

#: 实测 00700 在 2026-11-13 的东财日K行。列序: 日期,开,收,高,低,量,额,振幅%,涨跌幅%,涨跌额,换手率%
#: ⚠️ volume=31493353 的单位是**股** (A 股同位置是手), 这是本文件最重要的断言对象。
KLINE_00700 = "2026-11-13,393.000,386.600,395.200,385.600,31493353,12251162810,3.2,1.1,4.2,0.35"
KLINE_00700_PREV = "2026-11-12,388.000,393.000,394.000,386.000,28000000,10900000000,2.1,1.3,5.0,0.31"


class _FakeClient:
    """按 symbol 返回预置 kline; 记录调用参数以便断言 beg/end/fqt。"""

    def __init__(self, table: dict[str, list[str] | Exception]):
        self._table = table
        self.calls: list[dict] = []
        self.closed = False

    def kline(self, secid, beg, end, *, klt=101, fqt=0):
        self.calls.append({"secid": secid, "beg": beg, "end": end, "klt": klt, "fqt": fqt})
        out = self._table.get(secid.split(".", 1)[1])
        if isinstance(out, Exception):
            raise out
        return out or []

    def close(self):
        self.closed = True


def _seed_universe(data_dir, symbols: list[str], cny: list[str] | None = None) -> None:
    cny = cny or []
    rows = []
    for s in symbols:
        rows.append({"symbol": s, "name": s, "is_cny_counter": s in cny})
    out = data_dir / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


# ---------------------------------------------------------------------------
# universe 口径
# ---------------------------------------------------------------------------

def test_universe_excludes_cny_counter_by_default(tmp_path):
    """默认只拉港币柜台: 双柜台是同一份基本面, 双拉会把请求数翻倍并重复计一家公司。"""
    _seed_universe(tmp_path, ["00700.HK", "09988.HK", "89988.HK"], cny=["89988.HK"])
    assert sync_hk.hk_daily_universe(tmp_path) == ["00700.HK", "09988.HK"]


def test_universe_can_include_cny_counter(tmp_path):
    _seed_universe(tmp_path, ["09988.HK", "89988.HK"], cny=["89988.HK"])
    got = sync_hk.hk_daily_universe(tmp_path, include_cny_counter=True)
    assert got == ["09988.HK", "89988.HK"]


def test_universe_is_empty_when_table_missing(tmp_path):
    """维表缺失 → [] (调用方跳过), 不去猜一个 universe 也不抛错。"""
    assert sync_hk.hk_daily_universe(tmp_path) == []


def test_universe_tolerates_missing_is_cny_counter_column(tmp_path):
    """维表没有该列时不能崩 (旧维表/手工维表), 按全部保留处理。"""
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({"symbol": ["00700.HK"], "name": ["腾讯控股"]}), out)
    assert sync_hk.hk_daily_universe(tmp_path) == ["00700.HK"]


# ---------------------------------------------------------------------------
# 行映射与单位
# ---------------------------------------------------------------------------

def test_volume_is_persisted_in_shares_not_lots():
    """🔴 港股 volume 落库必须是**股**, 不得乘 100。

    东财原生单位就是股 (两条独立交叉验证见 docs/hk-market-plan.md §3.1.2)。
    在这里乘 100 会让下游换手率/VWAP 全部错位, 且**不报错**。
    """
    client = _FakeClient({"00700": [KLINE_00700]})
    frames = list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"]))

    assert len(frames) == 1
    row = next(iter(frames)).row(0, named=True)
    assert row["symbol"] == "00700.HK"
    assert row["date"] == dt.date(2026, 11, 13)
    assert row["volume"] == 31493353.0, "volume 被换算过"
    # 自洽性: 成交额 / 成交量 必须落在当日高低价之间 (否则单位或列序错了)
    vwap = row["amount"] / row["volume"]
    assert row["low"] <= vwap <= row["high"], f"VWAP {vwap} 不在 [{row['low']}, {row['high']}]"


def test_ohlc_column_order_is_close_before_high_low():
    """东财 CSV 列序是 日期,开,**收**,高,低 —— 收在第 3 列, 与常见 OHLC 序不同。

    映射错位的症状是 high/low 互换、close 变成 high, 而数据看起来「还挺像」,
    极难发现。用一行各值互不相同的样本钉死。
    """
    client = _FakeClient({"00700": [KLINE_00700]})
    row = next(iter(sync_hk.iter_hk_daily_frames(client, ["00700.HK"]))).row(0, named=True)
    assert row["open"] == 393.0
    assert row["close"] == 386.6
    assert row["high"] == 395.2
    assert row["low"] == 385.6


def test_frames_use_canonical_daily_columns():
    """落库列必须与 A 股 canonical 一致, 否则 unified 视图/读端会分叉。"""
    from app.services.kline_sync import CANONICAL_DAILY_COLS

    client = _FakeClient({"00700": [KLINE_00700]})
    df = next(iter(sync_hk.iter_hk_daily_frames(client, ["00700.HK"])))
    assert df.columns == CANONICAL_DAILY_COLS


def test_fqt_must_be_zero():
    """项目契约: 落库原始价, 复权在读端按除权因子重建。前复权价落库会污染历史。"""
    client = _FakeClient({"00700": [KLINE_00700]})
    with pytest.raises(ValueError):
        list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"], fqt=1))


def test_secid_uses_hk_prefix_116():
    client = _FakeClient({"00700": [KLINE_00700]})
    list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"]))
    assert client.calls[0]["secid"] == "116.00700"
    assert client.calls[0]["klt"] == 101


def test_beg_end_are_passed_through():
    client = _FakeClient({"00700": [KLINE_00700]})
    list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"], beg="20260101", end="20261113"))
    assert client.calls[0]["beg"] == "20260101"
    assert client.calls[0]["end"] == "20261113"


# ---------------------------------------------------------------------------
# 软失败
# ---------------------------------------------------------------------------

def test_single_symbol_failure_does_not_abort_the_run(caplog):
    """港股有长期停牌标的, 东财对其返回空/报错。一只失败不该中断整轮同步。"""
    client = _FakeClient({
        "00700": EastmoneyError("boom"),
        "09988": [KLINE_00700],
    })
    with caplog.at_level("WARNING"):
        frames = list(sync_hk.iter_hk_daily_frames(client, ["00700.HK", "09988.HK"]))

    assert len(frames) == 1, "失败标的应被跳过, 成功标的照常产出"
    assert "00700.HK" in caplog.text


def test_non_hk_symbol_in_universe_is_skipped_not_fatal(caplog):
    """维表混进 A 股 symbol (脏数据) → 跳过 + warning, 不中断。"""
    client = _FakeClient({"00700": [KLINE_00700]})
    with caplog.at_level("WARNING"):
        frames = list(sync_hk.iter_hk_daily_frames(client, ["600000.SH", "00700.HK"]))

    assert len(frames) == 1
    assert "600000.SH" in caplog.text


def test_empty_klines_is_not_an_error(caplog):
    """空返回是停牌/未上市的正常情形 → debug 级, 不刷 warning。"""
    client = _FakeClient({"00700": []})
    with caplog.at_level("WARNING"):
        assert list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"])) == []
    assert "00700.HK" not in caplog.text


# ---------------------------------------------------------------------------
# 落库与隔离
# ---------------------------------------------------------------------------

def test_sync_writes_to_hk_dir_and_never_to_cn_dir(tmp_path):
    """闸3 实证: 港股日K落 kline_hk_daily/, A 股 kline_daily/ 一个字节都不该有。"""
    _seed_universe(tmp_path, ["00700.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700_PREV, KLINE_00700]})

    n = sync_hk.sync_hk_daily(repo, client=client, incremental=False)

    assert n == 2
    assert list((tmp_path / "kline_hk_daily").rglob("*.parquet")), "港股目录未写入"
    assert not list((tmp_path / "kline_daily").rglob("*.parquet")), "污染了 A 股目录"
    # 按日分区, 与 A 股同一布局
    assert (tmp_path / "kline_hk_daily" / "date=2026-11-13").is_dir()


def test_synced_data_is_queryable_via_view(tmp_path):
    _seed_universe(tmp_path, ["00700.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700]})
    sync_hk.sync_hk_daily(repo, client=client, incremental=False)

    rows = repo.execute_all("SELECT symbol, volume FROM kline_hk_daily")
    assert rows == [("00700.HK", 31493353.0)]


def test_sync_skips_when_universe_empty(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    assert sync_hk.sync_hk_daily(repo, incremental=False) == 0


def test_latest_date_is_none_on_empty_store(tmp_path):
    """空目录下视图未注册 → 按「无历史」处理, 不抛错 (首次同步的正常状态)。"""
    repo = KlineRepository(DataStore(tmp_path))
    assert sync_hk.latest_hk_daily_date(repo) is None


def test_incremental_start_is_day_after_latest(tmp_path):
    """增量起点 = 已落库最大日期 + 1 天, 不重复拉全历史。"""
    _seed_universe(tmp_path, ["00700.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700_PREV, KLINE_00700]})
    sync_hk.sync_hk_daily(repo, client=client, incremental=False)

    assert sync_hk.latest_hk_daily_date(repo) == dt.date(2026, 11, 13)

    client2 = _FakeClient({"00700": []})
    sync_hk.sync_hk_daily(repo, client=client2)
    assert client2.calls[0]["beg"] == "20261114"


def test_incremental_noop_when_already_up_to_date(tmp_path):
    """起点晚于终点 → 直接返回 0, 不触网。"""
    _seed_universe(tmp_path, ["00700.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700]})
    sync_hk.sync_hk_daily(repo, client=client, incremental=False)

    client2 = _FakeClient({"00700": []})
    n = sync_hk.sync_hk_daily(
        repo, client=client2, start=dt.date(2026, 11, 20), end=dt.date(2026, 11, 14)
    )
    assert n == 0
    assert client2.calls == []


def test_sync_closes_only_self_created_client(tmp_path):
    _seed_universe(tmp_path, ["00700.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700]})
    sync_hk.sync_hk_daily(repo, client=client, incremental=False)
    assert client.closed is False, "不该关掉调用方传进来的 client"


def test_progress_callback_fires_per_symbol(tmp_path):
    _seed_universe(tmp_path, ["00700.HK", "09988.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700], "09988": [KLINE_00700]})
    seen: list[tuple[int, int]] = []
    sync_hk.sync_hk_daily(
        repo, client=client, incremental=False, on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen == [(1, 2), (2, 2)]


# ---------------------------------------------------------------------------
# enriched 生成
#
# PR2a 的最后一个功能缺口: append_hk_enriched / kline_hk_enriched 视图 / _DIRNAMES
# 全都就位, 但**没有任何调用方**, 目录恒空 → §3.1.2 的换手率单位修复在实践中走不到。
# ---------------------------------------------------------------------------

def _seed_hk_instruments(data_dir, symbol: str, *, float_shares: int,
                         name: str = "腾讯控股") -> None:
    out = data_dir / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame([{
        "symbol": symbol, "name": name,
        "total_shares": float_shares, "float_shares": float_shares,
        # 港股无涨跌停 → 哨兵列恒 None (见 hk.hk_instrument_rows)
        "limit_up": None, "limit_down": None,
        "listing_date": None, "is_cny_counter": False,
    }]), out)


def _seed_hk_daily(repo, symbol: str, *, days: int = 30, volume: float = 1_000_000.0,
                   start: dt.date = dt.date(2026, 9, 1)) -> pl.DataFrame:
    """写入 days 个交易日的港股**原始**日K。volume 单位是「股」, 不是「手」。"""
    rows = []
    for i in range(days):
        px = 100.0 + i
        rows.append({
            "symbol": symbol, "date": start + dt.timedelta(days=i),
            "open": px, "high": px + 2.0, "low": px - 2.0, "close": px + 1.0,
            "volume": volume, "amount": volume * px, "quote_ts": 0,
        })
    df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    repo.append_hk_daily(df)
    return df


def _read_hk_enriched(data_dir) -> pl.DataFrame:
    return pl.read_parquet(data_dir / "kline_hk_enriched" / "**" / "*.parquet").sort("date")


def test_enriched_turnover_uses_hk_volume_unit(tmp_path):
    """🔴 本函数存在的核心理由: 港股 volume 单位是股, 走 CN 默认会让换手率高 100 倍。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK", volume=1_000_000)

    assert sync_hk.sync_hk_enriched(repo) == 30

    tr = _read_hk_enriched(tmp_path)["turnover_rate"].drop_nulls()
    assert not tr.is_empty(), "换手率全 null: instruments 没接上"
    # volume(股) * shares_per_volume_unit(HK)=1 * 100 / float_shares = 1e6*100/2e8
    assert tr[0] == pytest.approx(0.5), f"期望 0.5%, 实得 {tr[0]}"


def test_hk_turnover_is_100x_smaller_than_cn():
    """同一组数值 (volume / float_shares), 各用自己市场的代码 → 比值必须精确 100。

    上一条断言绝对值, 这条断言**两个市场之间的关系**: 即使将来换手率公式整体改动,
    100 倍这个关系也必须保持 (它来自 shares_per_volume_unit 的 手→股 换算)。

    ⚠️ 不能拿港股代码去跑 ``market=CN`` 做对比 —— PR1 的 ``assert_symbol_market``
    护栏会当场抛 ``SymbolMarketMismatchError``, 而那正是它的设计目的 (港股代码静默
    套用 A 股规则会产出「看起来合理的错误数字」)。所以两边各用自己市场的代码。
    """
    from app.indicators.pipeline import compute_enriched

    volume, shares, n = 1_000_000.0, 200_000_000.0, 5
    dates = [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(n)]

    def frame(sym: str) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": [sym] * n, "date": dates,
            "open": [100.0] * n, "high": [102.0] * n,
            "low": [98.0] * n, "close": [101.0] * n,
            "volume": [volume] * n, "amount": [volume * 100.0] * n,
            "quote_ts": [0] * n,
        })

    def inst(sym: str, name: str) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": [sym], "name": [name], "float_shares": [shares],
            "listing_date": pl.Series([None], dtype=pl.Date),
            "limit_up": pl.Series([None], dtype=pl.Float64),
            "limit_down": pl.Series([None], dtype=pl.Float64),
        })

    hk = compute_enriched(frame("00700.HK"), instruments=inst("00700.HK", "腾讯控股"),
                          market=Market.HK)
    cn = compute_enriched(frame("600000.SH"), instruments=inst("600000.SH", "浦发银行"))
    ratio = (cn["turnover_rate"] / hk["turnover_rate"]).drop_nulls().drop_nans()
    assert not ratio.is_empty(), "两边换手率都是 null, 比值无从比较"
    assert all(r == pytest.approx(100.0) for r in ratio.to_list()), ratio.to_list()


def test_enriched_limit_prices_are_null_for_hk(tmp_path):
    """港股无涨跌停 → 限价列恒 null, 连板数恒 0。这是**正确结果**不是缺数据。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK")

    sync_hk.sync_hk_enriched(repo)
    df = _read_hk_enriched(tmp_path)
    assert df["consecutive_limit_ups"].fill_null(0).abs().max() == 0
    assert df["consecutive_limit_downs"].fill_null(0).abs().max() == 0


def test_enriched_hk_big_move_is_not_flagged_as_limit_up(tmp_path):
    """港股单日 +30% **不该**被记成涨停 (连板数恒 0), 且限价列恒 null。

    🔴 变异检验逼出来的测试缺口。``polars_price_limit_pct`` 是**表达式级**函数,
    无法逐行抛错 (见其 docstring), 所以漏传 ``market`` 时它不会报错, 而是按 A 股
    主板 10% 构造表达式 —— ``00700.HK`` 不匹配 300/301/302/688/689/.BJ 任何前缀,
    落到默认 10%。而 ``limit_up_price`` **不在 ENRICHED_STORAGE_COLS 里**, 落盘根本
    看不出来, 只有 ``consecutive_limit_ups`` 会露馅。

    同文件的 ``test_enriched_limit_prices_are_null_for_hk`` 用的数据日涨幅仅 ~1%,
    永远触发不了 10% 阈值 → 把 ``market=market`` 删掉后 121 项测试全绿。本用例用
    连续 +30% 把这个盲区堵上。
    """
    from app.indicators.pipeline import compute_enriched

    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)

    closes = [100.0, 130.0, 169.0]  # 连续两天 +30%, 远超 A 股任何板块涨跌幅
    rows = [{
        "symbol": "00700.HK", "date": dt.date(2026, 9, 1) + dt.timedelta(days=i),
        "open": c * 0.99, "high": c * 1.01, "low": c * 0.98, "close": c,
        "volume": 1_000_000.0, "amount": 1_000_000.0 * c, "quote_ts": 0,
    } for i, c in enumerate(closes)]
    raw = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    repo.append_hk_daily(raw)

    # 落盘口径: 连板数必须全 0
    sync_hk.sync_hk_enriched(repo)
    df = _read_hk_enriched(tmp_path).sort("date")
    assert df["consecutive_limit_ups"].to_list() == [0, 0, 0], (
        f"港股无涨跌停: +30% 也不能记成涨停, 实得 {df['consecutive_limit_ups'].to_list()}")
    assert df["consecutive_limit_downs"].to_list() == [0, 0, 0]

    # 内存口径: 限价列虽不落盘, 但实时/打分链路会读, 必须是 null 而非 10% 算出来的价
    inst = pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)
    full = compute_enriched(raw, instruments=inst, market=Market.HK)
    assert full["limit_up_price"].drop_nulls().is_empty(), "港股不该有涨停价"
    assert full["limit_down_price"].drop_nulls().is_empty(), "港股不该有跌停价"


def test_enriched_lands_in_hk_dir_not_ashare(tmp_path):
    """闸3 隔离: 港股 enriched 只进 kline_hk_enriched/, 绝不污染 A 股目录。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK")

    sync_hk.sync_hk_enriched(repo)
    assert list((tmp_path / "kline_hk_enriched").glob("date=*/*.parquet")), "未写入港股目录"
    assert not list((tmp_path / "kline_daily_enriched").glob("date=*/*.parquet")), "污染了 A 股目录"


def test_enriched_stores_only_storage_cols(tmp_path):
    """磁盘只落 ENRICHED_STORAGE_COLS 窄表, 宽指标读时重算 (与 A 股同款契约)。"""
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS

    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK")

    sync_hk.sync_hk_enriched(repo)
    cols = set(_read_hk_enriched(tmp_path).columns)
    assert cols <= set(ENRICHED_STORAGE_COLS), f"多写了列: {cols - set(ENRICHED_STORAGE_COLS)}"
    assert {"symbol", "date", "close", "turnover_rate"} <= cols


def test_enriched_noop_when_no_daily(tmp_path):
    """原始日K为空 → 返回 0 且不建目录, 不崩 (首次同步前的正常状态)。"""
    repo = KlineRepository(DataStore(tmp_path))
    assert sync_hk.sync_hk_enriched(repo) == 0


def test_enriched_refuses_without_instruments(tmp_path):
    """维表缺失 → **明确拒绝**(返回 0, 不落盘), 而不是产出缺列的分区。

    为什么是拒绝而不是「换手率 null 照写」: compute_limit_signals 在 instruments
    为空时提前返回, 落盘分区里连 turnover_rate 这一列都不存在。实测 DuckDB 下
    「所有分区都缺该列」→ 视图无此列, SELECT 直接 BinderException; 「部分分区缺」
    → union-by-name 补 NULL。同一张表两种行为 = 已知的缓存不一致。且港股无涨跌停,
    换手率是 compute_limit_signals 对 HK 唯一有意义的产出, 缺它则产出无价值。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_daily(repo, "00700.HK")
    assert not (tmp_path / HK_INSTRUMENT_RELPATH).exists(), "前置条件: 无维表"

    assert sync_hk.sync_hk_enriched(repo) == 0, "无维表却照写 = 会产出缺列分区"
    assert not list((tmp_path / "kline_hk_enriched").glob("date=*/*.parquet")), "不该落盘"
    # 原始日K 不受影响 (拒绝只作用于 enriched 这一步)。查分区文件而非 SQL 视图:
    # written==0 时 sync_hk_enriched 不触发 rebuild_views, 视图尚未注册。
    assert list((tmp_path / "kline_hk_daily").glob("date=*/*.parquet")), "原始日K 不该被动"


def test_enriched_start_filters_published_rows_only(tmp_path):
    """``start`` 只裁剪**发布**范围; 计算仍吃完整历史 (指标预热 + 连板递推)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK", days=30, start=dt.date(2026, 9, 1))

    n = sync_hk.sync_hk_enriched(repo, start=dt.date(2026, 9, 20))
    assert n == 11, "9/20~9/30 共 11 天"
    df = _read_hk_enriched(tmp_path)
    assert df["date"].min() == dt.date(2026, 9, 20)
    # 换手率不依赖预热窗口, 发布出来的行都该有值
    assert df["turnover_rate"].drop_nulls().len() == 11


def test_enriched_symbols_filter(tmp_path):
    """只重算指定标的, 其余不动。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK", days=10)
    _seed_hk_daily(repo, "09988.HK", days=10)

    n = sync_hk.sync_hk_enriched(repo, symbols=["00700.HK"])
    assert n == 10
    assert _read_hk_enriched(tmp_path)["symbol"].unique().to_list() == ["00700.HK"]


def test_enriched_progress_callback_counts_batches(tmp_path):
    """分批进度回调次数 = ceil(标的数 / 批大小), 供前端进度条使用。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    for i in range(5):
        _seed_hk_daily(repo, f"0070{i}.HK", days=6)

    calls: list[tuple[int, int]] = []
    sync_hk.sync_hk_enriched(repo, batch_size=2, on_progress=lambda d, t: calls.append((d, t)))
    assert [t for _, t in calls] == [3, 3, 3], "5 标的 / 批 2 = 3 批"
    assert [d for d, _ in calls] == [1, 2, 3], "进度必须单调递增"


def test_enriched_is_idempotent(tmp_path):
    """重跑不产生重复行 (按日分区 merge-upsert)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK", days=10)

    sync_hk.sync_hk_enriched(repo)
    first = _read_hk_enriched(tmp_path)
    sync_hk.sync_hk_enriched(repo)
    second = _read_hk_enriched(tmp_path)
    assert second.height == first.height == 10
    assert second["turnover_rate"].to_list() == first["turnover_rate"].to_list()


def test_enriched_progress_reports_empty_batches(tmp_path):
    """空批次也必须报进度, 否则进度条永远到不了 100%。

    回归钉: 早先的实现把 ``raw.is_empty()`` 写成 ``done += 1; continue``, 跳过了
    on_progress, 前端进度会卡在 (有数据的批数 / 总批数)。

    空批次**只在显式传 ``symbols=`` 时可达**: 默认路径的标的清单是从
    ``kline_hk_daily`` 本身派生的 (``raw_scan.select("symbol").unique()``), 扫出来的
    标的必然有行。所以这里模拟真实场景 —— 调用方点名了尚未同步日K的标的
    (代码打错、或维表有但日K没跟上)。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_hk_instruments(tmp_path, "00700.HK", float_shares=200_000_000)
    _seed_hk_daily(repo, "00700.HK", days=6)

    calls: list[tuple[int, int]] = []
    n = sync_hk.sync_hk_enriched(
        repo,
        symbols=["00700.HK", "09988.HK", "03690.HK"],  # 后两个无日K → 空批次
        batch_size=1,
        on_progress=lambda d, t: calls.append((d, t)),
    )
    assert n == 6, "只有 00700.HK 有数据"
    assert calls == [(1, 3), (2, 3), (3, 3)], f"进度必须走满 3 批, 实得 {calls}"
