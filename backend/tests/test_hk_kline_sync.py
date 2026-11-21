"""港股日K同步: universe 口径、单位透传、软失败、增量起点与落库隔离。

网络一律用假 client 挡掉 (测试触网不可复现; 且腾讯 ifzq 是港股当前**唯一**可用
日K源, 不该被测试流量消耗配额)。

🔴 2026-11 换源: 日K由东财 ``push2his`` 改走腾讯 ``ifzq`` (push2 集群对本机 IP 级
封禁)。本文件的假 client 与样本行随之从「东财 CSV 字符串 + secid 116」改为
「腾讯行列表 + hk 前缀代码」; 被钉住的**契约本身不变** (原始价、量单位股、
canonical 列、软失败、增量起点、落库隔离)。复权因子仍走东财 datacenter-web,
故 ``test_hk_adj_factor.py`` 不受影响。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.market import Market
from app.plugins.tencent.client import TencentError
from app.services import kline_sync_hk as sync_hk
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository

#: 实测 00700 腾讯日K原始行 (2026-11-20, ``ifzq.gtimg.cn/appstock/app/kline/kline``)。
#: 🔴 列序 ``[date, 开, **收**, 高, 低, 量]`` —— 收盘在 index 2, 不是常见 OHLC 序。
#: 🔴 ``volume=41094469`` 单位是**股** (A 股同位置是手): 与同一日腾讯快照
#:    ``qt.gtimg.cn`` 的 ``[6]`` 位 (``41094469.0``) **逐位相同**, 而快照该位已由
#:    ``test_hk_tencent_spot`` 钉死为股 —— 两个独立端点互证, 见下
#:    :data:`SNAPSHOT_00700_VOLUME`。
KLINE_00700 = ["2026-11-20", "341.000", "344.000", "349.600", "339.600", "41094469.000"]
KLINE_00700_PREV = ["2026-11-19", "361.800", "348.400", "365.000", "345.000", "76426269.000"]

#: 腾讯快照 ``hk00700`` 在 2026-11-20 的 ``[6]`` 位实测值 (单位: 股)。
#: 换源前本文件用「成交额/成交量落在当日高低价之间」当单位与列序的自洽探针;
#: 腾讯 6 列端点**不提供成交额**, 故改用这个更强的交叉验证 (两个独立端点逐位比对,
#: 而 VWAP 探针自身在 960 行实测里还有 1 行越界)。
SNAPSHOT_00700_VOLUME = 41094469.0


class _FakeClient:
    """按腾讯代码返回预置日K行; 记录调用参数以便断言 code/beg/end/count。

    🔴 换源前这里模拟东财 ``client.kline(secid, beg, end, klt=101, fqt=0)``;
    现改为模拟 :meth:`app.plugins.tencent.client.TencentClient.kline_daily`。
    """

    def __init__(self, table: dict[str, list | Exception]):
        self._table = table
        self.calls: list[dict] = []
        self.closed = False

    def kline_daily(self, code, count=2000, *, beg=None, end=None):
        self.calls.append({"code": code, "count": count, "beg": beg, "end": end})
        out = self._table.get(code[len("hk"):])
        if isinstance(out, Exception):
            raise out
        return list(out or [])

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

    腾讯日K原生单位就是股。在这里乘 100 会让下游换手率全部错位, 且**不报错**。

    🔴 换源后的交叉验证方式: 旧断言用「成交额/成交量落在当日高低价之间」当单位与
    列序的自洽探针, 但腾讯 6 列端点**不提供成交额**。改用**同日腾讯快照**的 ``[6]``
    位逐位比对 —— 快照该位由 ``test_hk_tencent_spot`` 独立钉死为股, 两个端点互证,
    比 VWAP 探针更强 (后者自身在 960 行实测里有 1 行越界)。
    """
    client = _FakeClient({"00700": [KLINE_00700]})
    frames = list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"]))

    assert len(frames) == 1
    row = next(iter(frames)).row(0, named=True)
    assert row["symbol"] == "00700.HK"
    assert row["date"] == dt.date(2026, 11, 20)
    assert row["volume"] == SNAPSHOT_00700_VOLUME, (
        f"日K量 {row['volume']} 与同日快照量 {SNAPSHOT_00700_VOLUME} 不一致 (单位被换算过?)"
    )
    assert "amount" not in row, "腾讯 6 列端点不提供成交额, 不得凭空造一列"


def test_ohlc_column_order_is_close_before_high_low():
    """腾讯日K列序是 ``日期,开,**收**,高,低,量`` —— 收在第 3 列, 与常见 OHLC 序不同。

    映射错位的症状是 high/low 互换、close 变成 high, 而数据看起来「还挺像」,
    极难发现。用一行各值互不相同的**实测**样本钉死。
    """
    client = _FakeClient({"00700": [KLINE_00700]})
    row = next(iter(sync_hk.iter_hk_daily_frames(client, ["00700.HK"]))).row(0, named=True)
    assert row["open"] == 341.0
    assert row["close"] == 344.0
    assert row["high"] == 349.6
    assert row["low"] == 339.6


def test_frames_use_canonical_daily_columns():
    """落库列必须是 A 股 canonical 的**子集且同序**, 否则 unified 视图/读端会分叉。

    🔴 换源后不再等于全集: 腾讯 6 列端点不提供成交额, ``amount`` 缺席。
    ``_normalize_daily`` 只保留存在的 canonical 列且按 canonical 排序, 故断言
    方式是「子集 + 同序」而非「相等」。下游对此是宽容的 (``append_hk_enriched``
    按存在的列落盘, 指标计算不用成交额, 读端该列默认隐藏)。
    """
    from app.services.kline_sync import CANONICAL_DAILY_COLS

    client = _FakeClient({"00700": [KLINE_00700]})
    df = next(iter(sync_hk.iter_hk_daily_frames(client, ["00700.HK"])))
    expected = [c for c in CANONICAL_DAILY_COLS if c != "amount"]
    assert df.columns == expected
    assert set(df.columns) <= set(CANONICAL_DAILY_COLS), "出现了 canonical 之外的列"


def test_no_fqt_knob_remains_on_the_service():
    """🔴 换源前这里钉的是「fqt 必须为 0」; 现在 ``fqt`` 入参**已删除**。

    钉住「删除」这个事实, 而不是留一个恒为 0 的惰性参数: 「落库原始价」这条契约
    改由**结构**保证 —— 插件只调不复权端点 ``kline/kline``, 从不调等差复权的
    ``hkfqkline`` (由 ``test_hk_tencent_kline.test_uses_raw_endpoint_never_hkfqkline``
    钉死)。留着 fqt 入参会让人以为还能调, 而腾讯侧一旦传 ``qfq`` 拿到的是**等差**
    复权价, 反推乘性因子会凭空造出除权事件 (汇丰实测 207 个, 真实约 3 个)。
    """
    import inspect

    assert "fqt" not in inspect.signature(sync_hk.iter_hk_daily_frames).parameters


def test_tencent_code_uses_hk_prefix():
    """🔴 换源前钉的是东财 secid ``116.00700``; 腾讯侧代码形如 ``hk00700``。

    前缀错 (或漏) 的症状是上游返回空数据而非报错 —— 整轮同步「成功」写入 0 行。
    """
    client = _FakeClient({"00700": [KLINE_00700]})
    list(sync_hk.iter_hk_daily_frames(client, ["00700.HK"]))
    assert client.calls[0]["code"] == "hk00700"


def test_beg_end_are_passed_through():
    """🔴 换源后 beg/end 是 ``date`` 且以 **ISO** 格式下发 (东财要 ``YYYYMMDD``)。

    格式错的症状同样是静默空返回, 不是报错。
    """
    client = _FakeClient({"00700": [KLINE_00700]})
    list(sync_hk.iter_hk_daily_frames(
        client, ["00700.HK"], beg=dt.date(2026, 1, 1), end=dt.date(2026, 11, 20)
    ))
    assert client.calls[0]["beg"] == "2026-01-01"
    assert client.calls[0]["end"] == "2026-11-20"


# ---------------------------------------------------------------------------
# 软失败
# ---------------------------------------------------------------------------

def test_single_symbol_failure_does_not_abort_the_run(caplog):
    """港股有长期停牌标的, 腾讯对其返回空/报错。一只失败不该中断整轮同步。"""
    client = _FakeClient({
        "00700": TencentError("boom"),
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
    assert (tmp_path / "kline_hk_daily" / "date=2026-11-20").is_dir()


def test_synced_data_is_queryable_via_view(tmp_path):
    _seed_universe(tmp_path, ["00700.HK"])
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeClient({"00700": [KLINE_00700]})
    sync_hk.sync_hk_daily(repo, client=client, incremental=False)

    rows = repo.execute_all("SELECT symbol, volume FROM kline_hk_daily")
    assert rows == [("00700.HK", 41094469.0)]


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

    assert sync_hk.latest_hk_daily_date(repo) == dt.date(2026, 11, 20)

    client2 = _FakeClient({"00700": []})
    sync_hk.sync_hk_daily(repo, client=client2)
    # 🔴 换源后 beg 以 **ISO** 下发 (东财要 ``YYYYMMDD``); 增量起点语义不变,
    # 仍是「已落库最大日 + 1 天」。
    assert client2.calls[0]["beg"] == "2026-11-21"


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

def _seed_hk_adj_factor(data_dir, rows: list[dict] | None = None,
                        covered: list[str] | None = None) -> None:
    """写入港股复权因子库 ``adj_factor_hk/all.parquet``。

    ``rows=None`` → **空库**, 语义是「因子同步跑过了, 这些标的从未除权」。这不是
    测试专用的假状态: 真实部署里绝大多数港股在某段时间窗内确实没有除净事件。

    为什么必须播: ``sync_hk_enriched`` 默认 ``require_adj_factor=True``, 因子库
    **不存在**即中止 (fail closed)。空库与「不存在」是两回事 —— 前者是已确认无事件,
    后者是未知, 未知时静默用原始价算 MA/MACD 会产出失真指标 (方案 §13)。

    ``covered`` 另外播**完整性凭据** ``adj_factor_hk/coverage.json``: enriched 的第二道
    闸门按标的放行, 不在 ``covered`` 里的一律拒算。生产里这份凭据由
    ``sync_hk_adj_factor`` 落盘, 测试不播就会 (正确地) fail closed。
    """
    out = data_dir / sync_hk.HK_ADJ_FACTOR_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    else:
        df = pl.DataFrame(schema={
            "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
            "dividend": pl.Float64, "bonus": pl.Float64, "allot": pl.Float64,
            "allot_price": pl.Float64, "prev_close": pl.Float64,
        })
    atomic_write_parquet(df, out)
    sync_hk._write_hk_adj_coverage(data_dir, set(covered or ()), set())


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
    # enriched 的前置条件之一: 因子库 + 完整性凭据必须存在 (见 _seed_hk_adj_factor)。
    _seed_hk_adj_factor(data_dir, covered=[symbol])


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
    # 5 个标的的分红史都记为「已拉全」, 否则完整性闸门会把 4 个拒算, 批数就不是 3 了。
    sync_hk._write_hk_adj_coverage(tmp_path, {f"0070{i}.HK" for i in range(5)}, set())

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
    # 三个标的的分红史都记为「已拉全」, 只是后两个还没有本地日K —— 空批次必须来自
    # 「无日K」而不是被完整性闸门拒算, 才是本用例要钉的场景。
    sync_hk._write_hk_adj_coverage(
        tmp_path, {"00700.HK", "09988.HK", "03690.HK"}, set()
    )

    calls: list[tuple[int, int]] = []
    n = sync_hk.sync_hk_enriched(
        repo,
        symbols=["00700.HK", "09988.HK", "03690.HK"],  # 后两个无日K → 空批次
        batch_size=1,
        on_progress=lambda d, t: calls.append((d, t)),
    )
    assert n == 6, "只有 00700.HK 有数据"
    assert calls == [(1, 3), (2, 3), (3, 3)], f"进度必须走满 3 批, 实得 {calls}"
