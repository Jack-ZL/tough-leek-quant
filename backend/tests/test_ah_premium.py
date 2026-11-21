"""``app.services.ah_premium`` + ``GET /api/hk/ah-premium`` 测试。

重点回归闸 (按危害排序):

1. 🔴 **汇率失败必须抛错, 绝不退化成 1.0**。溢价 = ``A / (H × rate) − 1``, 汇率错 1%
   就让每一行溢价平移 1%, 而数字看起来完全正常 —— 这是本服务唯一能造成"静默错误
   结论"的路径。
2. 🔴 **消歧必须 fail closed**: 一个 A 股对应多个 H 柜台、或一个 H 柜台被多个 A 股
   认领时, 只有在有确定性依据 (上市日最早 / 名称精确相等且唯一) 时才选, 否则丢弃
   并计入 ``ambiguous_a_symbols``。**猜错配对会算出一个完全无意义的溢价**。
3. 🔴 **价格错位分级处理**: A/H 交易日历不同, ≤5 天照算但暴露两侧日期 +
   ``price_lag_count``; >5 天置 null + ``stale_price_count``。要求日期严格相等会在
   港股独有假期把整页打空。
4. 🔴 **缺价 → null, 不是 0**: 退市股 (``000585.SZ 东电退``) 在本地无 K 线, 溢价必须
   是 null 并计入 ``missing_price_count``, 且排在列表末尾而不是混在中间。
5. 🔴 **读的是不复权原始价**: 溢价是同一时点两地挂牌价之比, 复权价会引入不存在的
   价差。本测试用带除权的分区数据钉住这一点。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.market import Market
from app.plugins.eastmoney.ah_pair import parse_ah_pair_candidates
from app.plugins.eastmoney.client import EastmoneyError
from app.plugins.sina.client import SinaError
from app.services import ah_premium as svc
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository, daily_dirname
from tests.test_eastmoney_ah_pair import (
    ROW_DONGDIAN_TUI,
    ROW_ICBC,
    ROW_LUOYANG,
    ROW_NO_H,
    ROW_PANDA,
    ROW_SHANGHAI_YIYAO,
    ROW_SHANGYAO_ZHUANHUAN,
    ROW_ST_CHENMING,
    ROW_VANKE,
    ROW_ZIJIN,
)
from tests.test_sina_fx import SNAPSHOT_OK, FakeSina

# 2026-11-20 的真实收盘价 (不复权原始价), 用于算出可核对的溢价
DAY = dt.date(2026, 11, 20)
FX_RATE = 0.8519701810  # 新浪 fx_shkdcny 实测值


class _FakeEmClient:
    """只实现 ``datacenter_report_all`` 与 ``close``。"""

    def __init__(self, rows: list[dict] | Exception):
        self._rows = rows
        self.calls: list[dict] = []
        self.closed = False

    def datacenter_report_all(self, report_name, flt=None, **kw):
        self.calls.append({"report_name": report_name, "flt": flt, **kw})
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows)

    def close(self):
        self.closed = True


@pytest.fixture()
def repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def _seed_hk_instruments(data_dir, rows: list[dict]) -> None:
    schema = {"symbol": pl.String, "name": pl.String, "listing_date": pl.Date}
    path = data_dir / HK_INSTRUMENT_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows, schema=schema, strict=False), path)


def _seed_closes(data_dir, market: Market, day: dt.date, rows: list[tuple[str, float]]) -> None:
    """写一个 ``date=YYYY-MM-DD/part.parquet`` 分区。"""
    dirname = daily_dirname("stock", market=market)
    path = data_dir / dirname / f"date={day.isoformat()}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(
        pl.DataFrame(
            {
                "symbol": [s for s, _ in rows],
                "date": [day] * len(rows),
                "close": [c for _, c in rows],
            },
            schema={"symbol": pl.String, "date": pl.Date, "close": pl.Float64},
        ),
        path,
    )


def _cands(*rows: dict):
    """上游原始行 → ``AhPairCandidate`` (``_resolve_pairs`` 的真实入参类型)。"""
    return parse_ah_pair_candidates(list(rows))


def _premium(repo, *, em_rows, sina_text=SNAPSHOT_OK):
    """跑一次服务, 返回 (result, em_client, sina_client)。"""
    em = _FakeEmClient(em_rows)
    sina = FakeSina(sina_text)
    got = svc.ah_premium(repo, client=em, sina_client=sina)
    return got, em, sina


# ---------------------------------------------------------------------------
# _latest_closes: 倒序扫分区 + 提前退出
# ---------------------------------------------------------------------------

def test_latest_closes_reads_newest_partition_first(tmp_path):
    _seed_closes(tmp_path, Market.CN, DAY - dt.timedelta(days=1), [("601398.SH", 99.0)])
    _seed_closes(tmp_path, Market.CN, DAY, [("601398.SH", 7.35)])
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.CN), ["601398.SH"])
    assert got.row(0, named=True) == {
        "symbol": "601398.SH",
        "close": 7.35,
        "price_date": DAY,
    }


def test_latest_closes_falls_back_to_older_partition(tmp_path):
    """最新分区里没有该标的时, 继续往前找 (停牌/未同步)。"""
    _seed_closes(tmp_path, Market.CN, DAY - dt.timedelta(days=3), [("601398.SH", 7.30)])
    _seed_closes(tmp_path, Market.CN, DAY, [("000001.SZ", 12.0)])
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.CN), ["601398.SH"])
    assert got.height == 1
    assert got["close"][0] == 7.30
    assert got["price_date"][0] == DAY - dt.timedelta(days=3)


def test_latest_closes_stops_scanning_once_all_symbols_found(tmp_path):
    """🔴 性能闸: 找齐就停, 不得扫满 lookback 个分区。

    ``kline_hk_daily`` 有 9102 个分区, 全量扫描不可接受。
    """
    for i in range(10):
        _seed_closes(tmp_path, Market.HK, DAY - dt.timedelta(days=i), [("01398.HK", 6.0 + i)])
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.HK), ["01398.HK"])
    assert got.height == 1
    assert got["close"][0] == 6.0  # 最新那天 (i=0)


def test_latest_closes_missing_dir_returns_empty_not_raise(tmp_path):
    """目录不存在 (从未同步过 K 线) → 空表, 让上层诚实降级成 null 溢价。"""
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.HK), ["01398.HK"])
    assert got.is_empty()
    assert got.columns == ["symbol", "close", "price_date"]


def test_latest_closes_ignores_unrelated_symbols(tmp_path):
    """分区里有 5000+ 标的, 只取需要的那些 (谓词下推)。"""
    _seed_closes(
        tmp_path,
        Market.CN,
        DAY,
        [("601398.SH", 7.35), ("000001.SZ", 12.0), ("600519.SH", 1500.0)],
    )
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.CN), ["601398.SH"])
    assert got["symbol"].to_list() == ["601398.SH"]


def test_latest_closes_dedupes_within_partition(tmp_path):
    """同一分区里同 symbol 出现两次也只留一行。

    🔴 不去重的话, 后续 left join 会**静默把行数翻倍**, 溢价表出现重复配对。
    """
    _seed_closes(tmp_path, Market.CN, DAY, [("601398.SH", 7.35), ("601398.SH", 7.36)])
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.CN), ["601398.SH"])
    assert got.height == 1


def test_latest_closes_skips_unreadable_partition(tmp_path):
    """坏分区跳过并告警, 不整批失败。"""
    _seed_closes(tmp_path, Market.CN, DAY - dt.timedelta(days=1), [("601398.SH", 7.30)])
    bad = tmp_path / daily_dirname("stock", market=Market.CN) / f"date={DAY.isoformat()}"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "part.parquet").write_bytes(b"not a parquet file")
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.CN), ["601398.SH"])
    assert got.height == 1
    assert got["close"][0] == 7.30


def test_latest_closes_empty_symbol_list_returns_empty(tmp_path):
    got = svc._latest_closes(tmp_path, daily_dirname("stock", market=Market.CN), [])
    assert got.is_empty()


# ---------------------------------------------------------------------------
# _read_hk_instruments: 维表读取的降级与去重
# ---------------------------------------------------------------------------

def test_read_hk_instruments_returns_name_and_listing_date(tmp_path):
    _seed_hk_instruments(
        tmp_path,
        [
            {"symbol": "01398.HK", "name": "工商银行", "listing_date": dt.date(2006, 10, 27)},
            {"symbol": "02899.HK", "name": "紫金矿业", "listing_date": dt.date(2003, 12, 23)},
        ],
    )
    got = svc._read_hk_instruments(tmp_path)
    assert got.columns == ["symbol", "name", "listing_date"]
    assert got.height == 2
    row = got.filter(pl.col("symbol") == "02899.HK").row(0, named=True)
    assert row == {"symbol": "02899.HK", "name": "紫金矿业", "listing_date": dt.date(2003, 12, 23)}


def test_read_hk_instruments_missing_file_returns_empty(tmp_path):
    """维表从未同步 → 空表 (名称回退上游, 上市日消歧退化为丢弃)。"""
    got = svc._read_hk_instruments(tmp_path)
    assert got.is_empty()
    assert got.columns == ["symbol", "name", "listing_date"]


def test_read_hk_instruments_missing_columns_become_null(tmp_path):
    """🔴 回归闸: 维表缺列时补 null, 不能 select 不存在的列。

    曾写成 ``df.select(["symbol"])`` 再 ``with_columns(pl.col("name"))``, 直接
    ``ColumnNotFoundError``。
    """
    path = tmp_path / HK_INSTRUMENT_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({"symbol": ["01398.HK"]}), path)
    got = svc._read_hk_instruments(tmp_path)
    assert got.height == 1
    assert got["name"][0] is None
    assert got["listing_date"][0] is None


def test_read_hk_instruments_dedupes(tmp_path):
    """重复 symbol 会让下游 left join 静默翻倍, 必须去重。"""
    _seed_hk_instruments(
        tmp_path,
        [
            {"symbol": "01398.HK", "name": "工商银行", "listing_date": dt.date(2006, 10, 27)},
            {"symbol": "01398.HK", "name": "工商银行H", "listing_date": dt.date(2006, 10, 27)},
        ],
    )
    got = svc._read_hk_instruments(tmp_path)
    assert got.height == 1


# ---------------------------------------------------------------------------
# _resolve_pairs: 消歧规则 (全部 fail closed)
# ---------------------------------------------------------------------------

def test_resolve_single_h_counter_is_direct():
    pairs, ambiguous = svc._resolve_pairs(_cands(ROW_ICBC), {}, {})
    assert pairs == [("601398.SH", "工商银行", "01398.HK", "工商银行")]
    assert ambiguous == []


def test_resolve_multi_h_counter_picks_earliest_listing_date():
    """🔴 一 A 对多 H 柜台 → 取**上市日最早**的那个 (原始柜台)。

    实测: ``601899.SH 紫金矿业 → 02958.HK (2026-11-13 新设, 每手 2000 股) /
    02899.HK (2003-12-23, 每手 500 股)``。新柜台是港交所「不同每手股数并行柜台」
    机制的产物, 用它会算出与全市场口径不一致的溢价。
    """
    listing = {"02958.HK": dt.date(2026, 11, 13), "02899.HK": dt.date(2003, 12, 23)}
    pairs, ambiguous = svc._resolve_pairs(_cands(ROW_ZIJIN), listing, {})
    # 位置对齐: 02899.HK 是 STR_CODEH 里的第 2 个, 对应名称「紫金矿业(五百)」
    assert pairs == [("601899.SH", "紫金矿业", "02899.HK", "紫金矿业(五百)")]
    assert ambiguous == []


def test_resolve_multi_h_counter_without_listing_date_is_dropped():
    """缺上市日 → 无法判定哪个是原始柜台 → **丢弃并计入歧义**, 不猜。"""
    pairs, ambiguous = svc._resolve_pairs(_cands(ROW_ZIJIN), {}, {})
    assert pairs == []
    assert ambiguous == ["601899.SH"]


def test_resolve_multi_h_counter_tied_listing_date_is_dropped():
    """上市日相同 → 依据不足 → 丢弃。"""
    listing = {"02958.HK": dt.date(2003, 12, 23), "02899.HK": dt.date(2003, 12, 23)}
    pairs, ambiguous = svc._resolve_pairs(_cands(ROW_ZIJIN), listing, {})
    assert pairs == []
    assert ambiguous == ["601899.SH"]


def test_resolve_multi_h_counter_partial_listing_date_is_dropped():
    """只有一个柜台有上市日也不行 —— "另一个缺失"不等于"另一个更晚"。"""
    pairs, ambiguous = svc._resolve_pairs(_cands(ROW_ZIJIN), {"02899.HK": dt.date(2003, 12, 23)}, {})
    assert pairs == []
    assert ambiguous == ["601899.SH"]


def test_resolve_duplicate_h_claim_picks_earliest_a_listing_date():
    """🔴 一个 H 被多个 A 认领 → 取 A 股上市日更早的那个。"""
    rows = [
        {**ROW_SHANGYAO_ZHUANHUAN, "LISTING_DATE": "2010-05-06"},
        {**ROW_SHANGHAI_YIYAO, "LISTING_DATE": "1994-03-24"},
    ]
    pairs, ambiguous = svc._resolve_pairs(_cands(*rows), {}, {})
    assert pairs == [("601607.SH", "上海医药", "02607.HK", "上海医药")]
    assert ambiguous == []


def test_resolve_duplicate_h_claim_tie_broken_by_exact_name_match():
    """🔴 A 股上市日**并列**时, 用名称精确相等消歧 (实测真实场景)。

    ``02607.HK`` 同时被 ``600849.SH 上药转换`` 与 ``601607.SH 上海医药`` 认领, 两者
    ``LISTING_DATE`` 都是 1994-03-24 (转换关系共享上市日), 且都在本地维表里。
    H 股简称是「上海医药」, 只有 601607.SH 精确相等。
    """
    rows = [ROW_SHANGYAO_ZHUANHUAN, ROW_SHANGHAI_YIYAO]  # 两者 LISTING_DATE 均为 1994-03-24
    names = {"02607.HK": "上海医药"}
    pairs, ambiguous = svc._resolve_pairs(_cands(*rows), {}, names)
    assert pairs == [("601607.SH", "上海医药", "02607.HK", "上海医药")]
    assert ambiguous == []


def test_resolve_name_match_must_be_unique():
    """名称相等但**不唯一** → 依据不足 → 两个都丢。"""
    rows = [
        {**ROW_SHANGYAO_ZHUANHUAN, "SECURITY_NAME_ABBR": "上海医药"},
        ROW_SHANGHAI_YIYAO,
    ]
    pairs, ambiguous = svc._resolve_pairs(_cands(*rows), {}, {"02607.HK": "上海医药"})
    assert pairs == []
    assert ambiguous == ["600849.SH", "601607.SH"]


def test_resolve_duplicate_h_claim_unresolvable_is_dropped():
    """上市日并列 + 名称都不等 → 两个都丢, 不随机选一个。"""
    pairs, ambiguous = svc._resolve_pairs(
        _cands(ROW_SHANGYAO_ZHUANHUAN, ROW_SHANGHAI_YIYAO),
        {},
        {"02607.HK": "上實医药"},
    )
    assert pairs == []
    assert ambiguous == ["600849.SH", "601607.SH"]


def test_resolve_name_equality_is_only_a_tiebreak_not_a_general_filter():
    """🔴 名称不等**本身不构成**剔除理由。

    A/H 简称不同是常态 (``万科A`` vs ``万科企业``、``南京熊猫`` vs
    ``南京熊猫电子股份``)。若把名称相等当通用规则, 会误杀大批正确配对。
    """
    pairs, ambiguous = svc._resolve_pairs(_cands(ROW_VANKE, ROW_PANDA), {}, {})
    assert [p[0] for p in pairs] == ["000002.SZ", "600775.SH"]
    assert ambiguous == []


def test_resolve_passes_through_upstream_h_name():
    """``_resolve_pairs`` 只透传上游 ``STR_NAMEH``, 不做本地维表替换。

    「本地名优先」是 :func:`ah_premium` 的职责 (它才读维表), 见
    :func:`test_h_name_prefers_local_instruments_over_upstream`。
    """
    pairs, _ = svc._resolve_pairs(_cands(ROW_ICBC), {}, {"01398.HK": "本地名不该生效"})
    assert pairs[0][3] == "工商银行"


def test_resolve_pairs_sorted_by_a_symbol():
    pairs, _ = svc._resolve_pairs(_cands(ROW_VANKE, ROW_ICBC, ROW_PANDA), {}, {})
    assert [p[0] for p in pairs] == sorted(p[0] for p in pairs)


def test_resolve_empty_input():
    assert svc._resolve_pairs(_cands(), {}, {}) == ([], [])


# ---------------------------------------------------------------------------
# ah_premium: 主流程
# ---------------------------------------------------------------------------

def _seed_pair_prices(data_dir, *, a_close=7.35, h_close=6.80, a_day=DAY, h_day=DAY):
    _seed_closes(data_dir, Market.CN, a_day, [("601398.SH", a_close)])
    _seed_closes(data_dir, Market.HK, h_day, [("01398.HK", h_close)])


def test_premium_formula_uses_hkd_cny_rate(repo, tmp_path):
    """溢价 = ``A / (H × HKD→CNY) − 1``, 单位 %。"""
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path, a_close=7.35, h_close=6.80)
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC])

    assert got.fx_rate == pytest.approx(FX_RATE)
    assert got.fx_source == "sina_hq_fx"
    assert got.currency == "CNY"
    row = got.items.row(0, named=True)
    expected = (7.35 / (6.80 * FX_RATE) - 1.0) * 100.0
    assert row["premium_pct"] == pytest.approx(expected)
    # 实测口径核对: 工行 A/H 溢价长期在 +25%~+30% 区间
    assert 20.0 < row["premium_pct"] < 40.0
    assert row["a_symbol"] == "601398.SH"
    assert row["h_symbol"] == "01398.HK"
    assert row["h_name"] == "工商银行"
    assert row["a_price_date"] == DAY
    assert row["h_price_date"] == DAY


def test_h_name_prefers_local_instruments_over_upstream(repo, tmp_path):
    """H 股名称以本地 ``instruments_hk`` 为准, 上游 ``STR_NAMEH`` 只作兜底。

    本地维表是港股面板其余页面 (行情/详情/分红) 的名称来源, 溢价表若另用一套名称,
    同一个 ``01398.HK`` 会在两个页面显示成两个名字。
    """
    _seed_hk_instruments(
        tmp_path, [{"symbol": "01398.HK", "name": "工商银行(本地)", "listing_date": None}]
    )
    _seed_pair_prices(tmp_path)
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC])  # 上游 STR_NAMEH = "工商银行"
    assert got.items["h_name"][0] == "工商银行(本地)"


def test_h_name_falls_back_to_upstream_when_not_in_instruments(repo, tmp_path):
    """维表里没有该 H 股 (未同步) → 退回上游名称, 不留空。"""
    _seed_hk_instruments(tmp_path, [])
    _seed_pair_prices(tmp_path)
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC])
    assert got.items["h_name"][0] == "工商银行"


def test_a_and_h_name_differences_are_preserved(repo, tmp_path):
    """🔴 A/H 简称不同是常态, 不得互相覆盖或据此剔除配对。"""
    _seed_hk_instruments(
        tmp_path, [{"symbol": "02202.HK", "name": "万科企业", "listing_date": None}]
    )
    _seed_closes(tmp_path, Market.CN, DAY, [("000002.SZ", 8.0)])
    _seed_closes(tmp_path, Market.HK, DAY, [("02202.HK", 8.0)])
    got, _, _ = _premium(repo, em_rows=[ROW_VANKE])
    row = got.items.row(0, named=True)
    assert row["a_name"] == "万科A"
    assert row["h_name"] == "万科企业"


def test_premium_uses_raw_close_not_adjusted(repo, tmp_path):
    """🔴 必须读不复权原始价。

    构造一个"除权日": 同一标的在旧分区里是复权后的等价价格。若服务读错列或读错
    分区, 溢价会明显偏离。这里通过钉住取值来源 (base 分区的 ``close``) 来验证 ——
    本地 ``kline_daily`` / ``kline_hk_daily`` 存的**就是**原始价
    (``pipeline.py`` 用 ``pl.col("close").alias("raw_close")`` 证明)。
    """
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    # 旧分区里放一个明显不同的价格, 若被误读溢价会变成 ~+236%
    _seed_closes(tmp_path, Market.CN, DAY - dt.timedelta(days=30), [("601398.SH", 20.0)])
    _seed_closes(tmp_path, Market.HK, DAY - dt.timedelta(days=30), [("01398.HK", 20.0)])
    _seed_pair_prices(tmp_path, a_close=7.35, h_close=6.80)
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC])
    row = got.items.row(0, named=True)
    assert row["a_close"] == 7.35
    assert row["h_close"] == 6.80
    assert row["a_price_date"] == DAY
    assert row["h_price_date"] == DAY


def test_missing_price_yields_null_premium_and_counter(repo, tmp_path):
    """🔴 退市股无本地 K 线 → 溢价 null + 计入 ``missing_price_count``, 不是 0。"""
    _seed_hk_instruments(tmp_path, [{"symbol": "00042.HK", "name": "东北电气", "listing_date": None}])
    _seed_closes(tmp_path, Market.HK, DAY, [("00042.HK", 0.182)])
    # A 股 000585.SZ 东电退 刻意不写分区
    got, _, _ = _premium(repo, em_rows=[ROW_DONGDIAN_TUI])
    assert got.missing_price_count == 1
    assert got.candidate_count == 1
    row = got.items.row(0, named=True)
    assert row["a_close"] is None
    assert row["premium_pct"] is None
    assert row["h_close"] == 0.182


def test_missing_prices_sort_last_not_in_the_middle(repo, tmp_path):
    """null 溢价排末尾 (``nulls_last``), 不能混在正常配对中间。"""
    _seed_hk_instruments(
        tmp_path,
        [
            {"symbol": "01398.HK", "name": "工商银行", "listing_date": None},
            {"symbol": "00042.HK", "name": "东北电气", "listing_date": None},
        ],
    )
    _seed_closes(tmp_path, Market.CN, DAY, [("601398.SH", 7.35)])
    # 🔴 同一天的分区必须一次写完: 后写会整片覆盖前写
    _seed_closes(tmp_path, Market.HK, DAY, [("01398.HK", 6.80), ("00042.HK", 0.182)])
    got, _, _ = _premium(repo, em_rows=[ROW_DONGDIAN_TUI, ROW_ICBC])
    assert got.items.height == 2
    assert got.items["premium_pct"][-1] is None
    assert got.items["premium_pct"][0] is not None


def test_price_lag_within_threshold_still_computed(repo, tmp_path):
    """🔴 A/H 交易日历不同: ≤5 天错位照算, 但暴露两侧日期 + 计数。

    要求日期严格相等会在港股独有假期 (如复活节) 把整页打空。
    """
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path, a_day=DAY, h_day=DAY - dt.timedelta(days=3))
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC])
    row = got.items.row(0, named=True)
    assert row["premium_pct"] is not None
    assert row["a_price_date"] == DAY
    assert row["h_price_date"] == DAY - dt.timedelta(days=3)
    assert got.price_lag_count == 1
    assert got.stale_price_count == 0
    assert got.missing_price_count == 0


def test_price_lag_beyond_threshold_yields_null(repo, tmp_path):
    """错位 >5 天 → 溢价 null + ``stale_price_count``, 价格仍展示。"""
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path, a_day=DAY, h_day=DAY - dt.timedelta(days=30))
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC])
    row = got.items.row(0, named=True)
    assert row["premium_pct"] is None
    assert row["a_close"] == 7.35  # 价格照常给出, 只是不算溢价
    assert row["h_close"] == 6.80
    assert got.stale_price_count == 1
    assert got.price_lag_count == 0
    assert svc.AH_PREMIUM_MAX_PRICE_LAG_DAYS == 5


def test_items_sorted_by_premium_descending(repo, tmp_path):
    _seed_hk_instruments(
        tmp_path,
        [
            {"symbol": "01398.HK", "name": "工商银行", "listing_date": None},
            {"symbol": "01812.HK", "name": "晨鸣纸业", "listing_date": None},
        ],
    )
    _seed_closes(tmp_path, Market.CN, DAY, [("601398.SH", 7.35), ("000488.SZ", 1.60)])
    _seed_closes(tmp_path, Market.HK, DAY, [("01398.HK", 6.80), ("01812.HK", 0.32)])
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC, ROW_ST_CHENMING])
    premiums = got.items["premium_pct"].to_list()
    assert premiums == sorted(premiums, reverse=True)
    # ST晨鸣 溢价 ~+487% 应排第一 (实测最高值)
    assert got.items["a_symbol"][0] == "000488.SZ"
    assert premiums[0] == pytest.approx(486.875, abs=0.5)


def test_st_and_delisted_pairs_are_kept(repo, tmp_path):
    """🔴 ST / 退市股**刻意保留**: 它们恰恰是溢价最极端的样本。

    实测 ``000488.SZ ST晨鸣 → 01812.HK 晨鸣纸业`` 溢价 +487%, 是全场最高。
    """
    _seed_hk_instruments(tmp_path, [{"symbol": "01812.HK", "name": "晨鸣纸业", "listing_date": None}])
    _seed_closes(tmp_path, Market.CN, DAY, [("000488.SZ", 1.60)])
    _seed_closes(tmp_path, Market.HK, DAY, [("01812.HK", 0.32)])
    got, _, _ = _premium(repo, em_rows=[ROW_ST_CHENMING])
    assert got.items.height == 1
    assert got.items["a_name"][0] == "ST晨鸣"
    assert got.items["premium_pct"][0] > 400


def test_ambiguous_pairs_are_reported_not_guessed(repo, tmp_path):
    """歧义配对不进 items, 但计入 ``ambiguous_a_symbols`` 供前端提示。"""
    got, _, _ = _premium(repo, em_rows=[ROW_ZIJIN, ROW_ICBC])  # 紫金无上市日 → 歧义
    assert got.ambiguous_a_symbols == ("601899.SH",)
    assert got.items["a_symbol"].to_list() == ["601398.SH"]


def test_rows_without_h_counter_are_not_candidates(repo, tmp_path):
    """无 H 股的普通 A 股不进候选 (8387 行里的绝大多数)。"""
    got, _, _ = _premium(repo, em_rows=[ROW_NO_H])
    assert got.candidate_count == 0
    assert got.items.is_empty()


def test_fx_failure_raises_never_degrades_to_parity(repo, tmp_path):
    """🔴🔴 汇率拿不到必须抛错, **绝不**退化成 1.0。

    这是本服务唯一能造成"静默错误结论"的路径: 溢价 = A/(H×rate)−1, rate 错 1%
    就让每一行平移 1%, 而数字看起来完全正常。本仓库默认禁止汇率换算, AH 溢价是
    唯一例外, 代价就是必须 fail closed。
    """
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path)
    with pytest.raises(SinaError):
        svc.ah_premium(repo, client=_FakeEmClient([ROW_ICBC]), sina_client=FakeSina(""))


def test_fx_failure_when_rate_out_of_band(repo, tmp_path):
    """汇率超出合理区间 (抓错字段/上游改版) 同样抛错。"""
    bad = 'var hq_str_fx_shkdcny="2026-11-21 10:00:00,85.1970,0,0,0,0,0,0,0,港币,0,0,0,中国人民银行,0,0,,2026-11-21";'
    with pytest.raises(SinaError):
        svc.ah_premium(repo, client=_FakeEmClient([ROW_ICBC]), sina_client=FakeSina(bad))


def test_ah_pair_fetch_failure_raises(repo):
    """配对表拉不到 → 抛错 (没有配对就没有溢价可言)。"""
    with pytest.raises(EastmoneyError):
        svc.ah_premium(
            repo,
            client=_FakeEmClient(EastmoneyError("限流")),
            sina_client=FakeSina(SNAPSHOT_OK),
        )


def test_empty_candidate_set_still_returns_fx_metadata(repo, tmp_path):
    """候选为空时仍给出汇率与口径 (前端要显示数据源), items 为空表。"""
    got, _, _ = _premium(repo, em_rows=[])
    assert got.items.is_empty()
    assert got.items.columns == list(svc.AH_PREMIUM_COLS)
    assert got.candidate_count == 0
    assert got.fx_rate == pytest.approx(FX_RATE)
    assert got.fx_as_of == dt.date(2026, 11, 21)
    assert got.currency == "CNY"


def test_counters_are_disjoint_and_sum_to_candidates(repo, tmp_path):
    """三个计数器互斥: 缺价 / 过期 / 可算, 加起来等于候选数。"""
    _seed_hk_instruments(
        tmp_path,
        [
            {"symbol": "01398.HK", "name": "工商银行", "listing_date": None},
            {"symbol": "01812.HK", "name": "晨鸣纸业", "listing_date": None},
            {"symbol": "00042.HK", "name": "东北电气", "listing_date": None},
        ],
    )
    _seed_closes(tmp_path, Market.CN, DAY, [("601398.SH", 7.35), ("000488.SZ", 1.60)])
    # 晨鸣 H 股价格错位 30 天 → stale; 东电退 A 股无价 → missing
    _seed_closes(tmp_path, Market.HK, DAY, [("01398.HK", 6.80), ("00042.HK", 0.182)])
    _seed_closes(tmp_path, Market.HK, DAY - dt.timedelta(days=30), [("01812.HK", 0.32)])
    got, _, _ = _premium(repo, em_rows=[ROW_ICBC, ROW_ST_CHENMING, ROW_DONGDIAN_TUI])
    assert got.candidate_count == 3
    assert got.missing_price_count == 1
    assert got.stale_price_count == 1
    computable = got.items.filter(pl.col("premium_pct").is_not_null()).height
    assert computable == 1
    assert got.missing_price_count + got.stale_price_count + computable == got.candidate_count


def test_service_closes_self_built_clients(monkeypatch, repo):
    """``client=None`` / ``sina_client=None`` 时自建自关。"""
    em = _FakeEmClient([ROW_ICBC])
    sina = FakeSina(SNAPSHOT_OK)
    monkeypatch.setattr(svc, "EastmoneyClient", lambda: em)
    monkeypatch.setattr(svc, "SinaClient", lambda: sina)
    svc.ah_premium(repo)
    assert em.closed is True
    assert sina.closed is True


def test_service_closes_self_built_clients_even_on_error(monkeypatch, repo):
    em = _FakeEmClient(EastmoneyError("限流"))
    sina = FakeSina(SNAPSHOT_OK)
    monkeypatch.setattr(svc, "EastmoneyClient", lambda: em)
    monkeypatch.setattr(svc, "SinaClient", lambda: sina)
    with pytest.raises(EastmoneyError):
        svc.ah_premium(repo)
    assert em.closed is True
    assert sina.closed is True


def test_service_does_not_close_injected_clients(repo, tmp_path):
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path)
    em = _FakeEmClient([ROW_ICBC])
    sina = FakeSina(SNAPSHOT_OK)
    svc.ah_premium(repo, client=em, sina_client=sina)
    assert em.closed is False
    assert sina.closed is False


def test_service_requests_both_sources(repo, tmp_path):
    """一次请求同时打配对表与汇率, 且配对表走的是 F10 机构资料报表。"""
    _got, em, sina = _premium(repo, em_rows=[ROW_ICBC])
    assert em.calls[0]["report_name"] == "RPT_F10_BASIC_ORGINFO"
    assert "fx_shkdcny" in sina.calls[0]["path"]


# ---------------------------------------------------------------------------
# API 层: GET /api/hk/ah-premium
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None
    return TestClient(app), tmp_path, monkeypatch


def _patch_sources(monkeypatch, em_rows, sina_text=SNAPSHOT_OK):
    em = _FakeEmClient(em_rows)
    sina = FakeSina(sina_text)
    monkeypatch.setattr(svc, "EastmoneyClient", lambda: em)
    monkeypatch.setattr(svc, "SinaClient", lambda: sina)
    return em, sina


def test_endpoint_shape_and_fx_visibility(api):
    """🔴 汇率与取值日期**必须**出现在响应里 (换算即伪造的唯一豁免条件)。"""
    client, tmp_path, monkeypatch = api
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path)
    _patch_sources(monkeypatch, [ROW_ICBC])
    r = client.get("/api/hk/ah-premium")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1
    assert d["candidate_count"] == 1
    assert d["currency"] == "CNY"
    assert d["fx_rate"] == pytest.approx(FX_RATE)
    assert d["fx_as_of"] == "2026-11-21"  # date 已序列化成 ISO 串
    assert d["fx_source"] == "sina_hq_fx"
    assert d["source"] == "eastmoney_datacenter_f10_orginfo+local_kline"
    assert d["missing_price_count"] == 0
    assert d["stale_price_count"] == 0
    assert d["price_lag_count"] == 0
    assert d["ambiguous_a_symbols"] == []
    assert list(d["items"][0]) == list(svc.AH_PREMIUM_COLS)
    assert d["items"][0]["a_price_date"] == DAY.isoformat()


def test_endpoint_returns_502_on_ah_pair_failure(api):
    client, _tmp_path, monkeypatch = api
    _patch_sources(monkeypatch, EastmoneyError("限流"))
    r = client.get("/api/hk/ah-premium")
    assert r.status_code == 502
    assert "AH 配对数据源不可用" in r.json()["detail"]


def test_endpoint_returns_502_on_fx_failure(api):
    """🔴 汇率失败 → 502, **不得**用 1.0 兜底算出一整页错误溢价。"""
    client, tmp_path, monkeypatch = api
    _seed_hk_instruments(tmp_path, [{"symbol": "01398.HK", "name": "工商银行", "listing_date": None}])
    _seed_pair_prices(tmp_path)
    _patch_sources(monkeypatch, [ROW_ICBC], sina_text="")
    r = client.get("/api/hk/ah-premium")
    assert r.status_code == 502
    assert "港币汇率数据源不可用" in r.json()["detail"]


def test_endpoint_empty_candidates_is_200(api):
    client, _tmp_path, monkeypatch = api
    _patch_sources(monkeypatch, [])
    r = client.get("/api/hk/ah-premium")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 0
    assert d["items"] == []
    assert d["fx_rate"] == pytest.approx(FX_RATE)


def test_endpoint_reports_ambiguous_symbols(api):
    client, _tmp_path, monkeypatch = api
    _patch_sources(monkeypatch, [ROW_ZIJIN, ROW_LUOYANG])
    d = client.get("/api/hk/ah-premium").json()
    assert d["ambiguous_a_symbols"] == ["601899.SH", "603993.SH"]
    assert d["total"] == 0
    assert d["candidate_count"] == 2
