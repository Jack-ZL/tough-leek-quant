"""港股分红日历: 上游 filter 格式、解析容错、股息率单位与 fail-closed 口径。

三层各测各的, 不跨层打桩:

* **插件层** (``app.plugins.eastmoney.hk_dividend``): 钉住 ``EX_DIVIDEND_DATE``
  的 filter **必须**用斜杠格式 —— 横杠格式不报错但静默给出错答案 (下界过宽 /
  上界差一整年 / 双界空集), 实测矩阵写在该模块的 :func:`_ex_date_filter_bound`。
* **服务层** (``app.services.hk_dividend_calendar``): 钉住股息率的**单位**
  (百分比数值, 不是小数) 与**分母** (不复权收盘), 以及「算不出来 → null 而不是 0」。
* **API 层** (``GET /api/hk/dividends``): 钉住窗口校验、日期序列化、两个数据质量
  计数, 以及上游失败必须 502 而**不**降级成空列表。

网络一律不打: 插件层用假 client 捕获 filter 串, 服务层直接喂原始行, API 层
monkeypatch 服务模块里的取数函数与 client 类。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.market import Market
from app.plugins.eastmoney import hk_dividend as hkd
from app.plugins.eastmoney.client import EastmoneyError
from app.services import hk_dividend_calendar as svc
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository, daily_dirname

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

#: 上游真实返回的行形状 (2026-11-21 活体抽样, 字段名与类型逐字照抄)。
#: 🔴 ``EX_DIVIDEND_DATE``/``DIVIDEND_DATE`` 返回值是**横杠**格式, 而 filter 里
#: 必须写**斜杠** —— 这个不一致正是本文件要钉住的坑, 故样本保留原样。
def _row(
    symbol: str,
    ex: str,
    *,
    plan: str | None = "每股派港币1.0元",
    pay: str | None = "2026-12-28",
    notice: str | None = "2026-08-28 00:00:00",
    year: str | None = "2026",
    report_type: str | None = "中期分配",
) -> dict:
    return {
        "SECUCODE": symbol,
        "SECURITY_CODE": symbol.split(".")[0],
        "EX_DIVIDEND_DATE": ex,
        "DIVIDEND_DATE": pay,
        "NOTICE_DATE": notice,
        "YEAR": year,
        "REPORT_TYPE": report_type,
        "PLAN_EXPLAIN": plan,
        "IS_BFP": "0",
    }


class _FakeEmClient:
    """记录 ``datacenter_report_all`` 的调用参数, 返回预置行。"""

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


def _seed_instruments(data_dir: Path, rows: list[dict]) -> None:
    path = data_dir / HK_INSTRUMENT_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), path)


def _seed_daily_partition(data_dir: Path, day: dt.date, rows: list[dict]) -> None:
    """写一个 ``kline_hk_daily/date=<day>/part.parquet`` 分区。

    列集与真实落库一致 (``_normalize_daily`` 的 canonical 列), ``close`` 是
    **不复权**原始价 —— 项目契约「落库必须原始价」。
    """
    dirname = daily_dirname("stock", market=Market.HK)
    path = data_dir / dirname / f"date={day.isoformat()}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(rows).select(
        pl.col("symbol"),
        pl.lit(day, dtype=pl.Date).alias("date"),
        pl.col("close").cast(pl.Float64),
    )
    atomic_write_parquet(df, path)


@pytest.fixture()
def repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """最小 FastAPI app + **不打网络**的取数打桩。

    同时替换服务模块里的 ``EastmoneyClient``: 端点走 ``client=None`` 分支会自建
    client, 只 patch 取数函数的话仍会构造一个真 client (虽然不发请求, 但没必要
    依赖这个前提)。
    """
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None

    class _Stub:
        def close(self):
            return None

    monkeypatch.setattr(svc, "EastmoneyClient", _Stub)
    return TestClient(app), repo, tmp_path, monkeypatch


def _patch_rows(monkeypatch, rows: list[dict] | Exception) -> None:
    def _fake(client, *, start, end, **kw):
        if isinstance(rows, Exception):
            raise rows
        return [r for r in rows]

    monkeypatch.setattr(svc, "fetch_hk_dividend_calendar", _fake)


# ---------------------------------------------------------------------------
# 插件层: filter 日期格式 (🔴 斜杠 vs 横杠)
# ---------------------------------------------------------------------------

def test_ex_date_filter_bound_uses_slash_format():
    """filter 里的除净日**必须**是 ``YYYY/MM/DD``。

    横杠写法不报错, 但服务端会静默给出错答案 (实测: 横杠下界把窗口外两个月的数据
    混进来, 横杠上界差一整年, 横杠双界直接空集)。这个测试是那道实测矩阵的回归闸 —
    —— 谁把它改成 ``isoformat()`` 就会让日历静默包含错误区间的事件。
    """
    assert hkd._ex_date_filter_bound(dt.date(2026, 11, 20)) == "2026/11/20"
    assert hkd._ex_date_filter_bound(dt.date(2027, 1, 5)) == "2027/01/05"
    assert "/" in hkd._ex_date_filter_bound(dt.date(2026, 11, 20))
    assert hkd._ex_date_filter_bound(dt.date(2026, 11, 20)) != dt.date(2026, 11, 20).isoformat()


def test_fetch_calendar_filter_carries_slash_bounds_and_is_bfp():
    """全市场日历请求的 filter / 排序 / 翻页参数逐项钉死。"""
    client = _FakeEmClient([])
    hkd.fetch_hk_dividend_calendar(
        client, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31)
    )
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["report_name"] == hkd.RPT_HK_DIVIDEND
    flt = call["flt"]
    # IS_BFP="0" 必须带: 不带会混进「未派发或宣派股息」的占位行
    assert '(IS_BFP="0")' in flt
    assert '(EX_DIVIDEND_DATE>="2026/11/20")' in flt
    assert '(EX_DIVIDEND_DATE<="2026/12/31")' in flt
    assert "2026-11-20" not in flt, "横杠日期会让服务端静默错答"
    # 不带 SECUCODE: 全市场一次取完, 这是日历不需要逐标的同步的前提
    assert "SECUCODE" not in flt
    # 双键排序: 只按除净日排序时跨页顺序会漂移 (实测 37 行重复 vs 10 行)
    assert call["sort_columns"] == hkd.HK_DIV_CALENDAR_SORT_COLUMNS
    assert call["sort_types"] == hkd.HK_DIV_CALENDAR_SORT_TYPES
    assert "SECUCODE" in call["sort_columns"]


# ---------------------------------------------------------------------------
# 插件层: 行解析
# ---------------------------------------------------------------------------

def test_parse_rows_rechecks_window_locally():
    """服务端说过滤了也要本地复核: 窗口外的行必须被丢掉。

    上游过滤语义是实测得出的、无契约保证; 本地复核是一次比较, 换来「上游改了
    语义也不会把窗口外的事件塞进日历」。
    """
    rows = [
        _row("00700.HK", "2026-11-19"),  # 早于 start
        _row("09988.HK", "2026-11-20"),  # 命中下界 (含端点)
        _row("01810.HK", "2026-12-31"),  # 命中上界 (含端点)
        _row("03690.HK", "2027-01-01"),  # 晚于 end
    ]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31)
    )
    assert [e.symbol for e in out] == ["09988.HK", "01810.HK"]


def test_parse_rows_accepts_slash_dates_from_upstream():
    """返回值也可能是斜杠格式 (上游同一报表两种分隔符并存) —— 都要能解析。"""
    rows = [_row("00005.HK", "2026/11/20", pay="2026/12/28")]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 11, 1), end=dt.date(2026, 12, 31)
    )
    assert len(out) == 1
    assert out[0].ex_date == dt.date(2026, 11, 20)
    assert out[0].pay_date == dt.date(2026, 12, 28)


def test_parse_rows_drops_null_ex_date_and_dedupes():
    """无除净日的行丢掉 (日历以除净日为轴); 跨页重复行只留一条。"""
    dup = _row("00700.HK", "2026-11-20")
    rows = [
        {"SECUCODE": "01810.HK", "EX_DIVIDEND_DATE": None, "IS_BFP": "0"},
        dup,
        dict(dup),  # 翻页重复: 四个键完全同值
        _row("00700.HK", "2026-11-20", plan="每股派港币2.0元"),  # 同日**不同**方案, 要保留
    ]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 11, 1), end=dt.date(2026, 12, 31)
    )
    assert len(out) == 2
    assert {e.dividend_hkd for e in out} == {1.0, 2.0}


def test_parse_rows_keeps_unparsed_plan_with_null_amount():
    """解析不出金额的行**保留**, 金额置 null 而不是 0, 也不是整行丢掉。

    除净日是事实, 不能因为源数据没给港币折算值就把整条公司行动藏掉; 而 0.0 会被
    下游读成「确认不派现金」, 与「不知道」必须可区分 (CONTRIBUTING §4)。
    """
    rows = [
        _row("00005.HK", "2026-11-20", plan="每股派美元0.1元(可选择以股代息)"),
        _row("00999.HK", "2026-11-21", plan=None),
    ]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 11, 1), end=dt.date(2026, 12, 31)
    )
    assert len(out) == 2
    for e in out:
        assert e.parsed is False
        assert e.dividend_hkd is None
        assert e.bonus_ratio is None
    assert out[0].plan == "每股派美元0.1元(可选择以股代息)", "原文必须留给前端展示"
    assert out[1].plan is None


def test_parse_rows_pure_bonus_issue_has_zero_cash_not_null():
    """纯送转: ``dividend_hkd == 0.0`` (确认无现金) 而**非** null (未知)。"""
    rows = [_row("06656.HK", "2026-12-17", plan="每10股转20股")]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 12, 1), end=dt.date(2026, 12, 31)
    )
    assert len(out) == 1
    assert out[0].parsed is True
    assert out[0].dividend_hkd == 0.0
    assert out[0].bonus_ratio == pytest.approx(2.0)


def test_parse_rows_skips_malformed_secucode_without_raising():
    """单行畸形代码 warn+skip: 只读展示路径不该让一条坏行把整个日历打成 500。"""
    rows = [
        _row("NOT_A_SYMBOL", "2026-11-20"),
        _row("", "2026-11-20"),
        {"SECUCODE": None, "EX_DIVIDEND_DATE": "2026-11-20", "IS_BFP": "0"},
        _row("00700.HK", "2026-11-20"),
    ]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 11, 1), end=dt.date(2026, 12, 31)
    )
    assert [e.symbol for e in out] == ["00700.HK"]


def test_parse_rows_sorts_by_ex_date_then_symbol():
    rows = [
        _row("09988.HK", "2026-11-25"),
        _row("01810.HK", "2026-11-20"),
        _row("00700.HK", "2026-11-25"),
    ]
    out = hkd.parse_hk_dividend_rows(
        rows, start=dt.date(2026, 11, 1), end=dt.date(2026, 12, 31)
    )
    assert [(e.ex_date.isoformat(), e.symbol) for e in out] == [
        ("2026-11-20", "01810.HK"),
        ("2026-11-25", "00700.HK"),
        ("2026-11-25", "09988.HK"),
    ]


# ---------------------------------------------------------------------------
# 服务层: schema / 股息率单位 / fail-closed
# ---------------------------------------------------------------------------

def test_calendar_schema_matches_event_fields():
    """服务层按行构造 DataFrame, 故 ``HkDividendEvent`` 的字段顺序**必须**与
    schema 的键顺序一致。

    两者都在本仓库内, 但相隔两个文件; 顺序一旦漂移, polars 会按位置塞值 ——
    例如把 ``pay_date`` 塞进 ``notice_date`` 列, 类型相同所以**不报错**, 只是
    日历上的日期全错。这条测试就是那道闸。
    """
    schema_keys = [
        "symbol", "ex_date", "pay_date", "notice_date", "fiscal_year",
        "report_type", "plan", "dividend_hkd", "bonus_ratio", "parsed",
    ]
    assert list(hkd.HkDividendEvent._fields) == schema_keys
    # 输出列必须包含事件的全部字段 (join 出来的名称/收盘价是额外的)
    for field in hkd.HkDividendEvent._fields:
        assert field in svc.HK_DIVIDEND_CALENDAR_COLS


def test_dividend_yield_pct_is_percentage_value_not_fraction(repo, tmp_path):
    """🔴 单位测试 (CONTRIBUTING §3.1): ``5.0`` 表示 ``5.0%``, 不是 ``0.05``。

    用真实抽样数据钉死: 江波龙 09976.HK 除净 2026-11-23, 每股派港币 2.546 元,
    不复权收盘 112.4 ⇒ 2.546 / 112.4 × 100 = 2.265125 (%)。
    若误用小数口径会得到 0.02265125, 相差 100 倍 —— 前端再乘一次 100 就「看起来
    对了」, 这正是 §3.1 禁止启发式转换的原因。
    """
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20),
                          [{"symbol": "09976.HK", "close": 112.4}])
    client = _FakeEmClient([
        _row("09976.HK", "2026-11-23", plan="每股派人民币2.18元(相当于港币2.546元)"),
    ])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["dividend_hkd"] == pytest.approx(2.546)
    assert row["dividend_yield_pct"] == pytest.approx(2.546 / 112.4 * 100)
    assert row["dividend_yield_pct"] == pytest.approx(2.265125, abs=1e-6)
    assert row["dividend_yield_pct"] > 1.0, "若得到小数口径 (0.0226) 这条会失败"


def test_yield_denominator_is_raw_close_from_daily_partition(repo, tmp_path):
    """分母是 ``kline_hk_daily.close`` (不复权原始价), 逐字等于落库值。

    用前复权价会把有分红史的标的股息率系统性算高 (§3.2)。这里 seeded 的 close
    就是落库值, 断言相等即钉住「没有偷偷换成 enriched 的复权 close」。
    """
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20),
                          [{"symbol": "00700.HK", "close": 512.5}])
    client = _FakeEmClient([_row("00700.HK", "2026-11-25", plan="每股派港币5.3元")])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    row = df.to_dicts()[0]
    assert row["raw_close"] == pytest.approx(512.5)
    assert row["price_date"] == dt.date(2026, 11, 20)
    assert row["dividend_yield_pct"] == pytest.approx(5.3 / 512.5 * 100)


def test_yield_null_when_no_close_available(repo, tmp_path):
    """取不到收盘价 → 股息率 null, **不是 0** (fail closed)。

    0 会被读成「零股息」, 而真相是「算不出来」(未同步 / 长期停牌 / 人民币柜台
    不在日K里)。实测这类行确实存在: 80992.HK 联想集团–R。
    """
    client = _FakeEmClient([_row("80992.HK", "2026-12-09", plan="每股派港币0.09元")])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    assert df.height == 1, "没有收盘价也该出现在日历里, 除净日是事实"
    row = df.to_dicts()[0]
    assert row["raw_close"] is None
    assert row["price_date"] is None
    assert row["dividend_yield_pct"] is None
    assert row["dividend_hkd"] == pytest.approx(0.09), "金额本身不受收盘价影响"


def test_yield_null_when_close_is_zero_or_negative(repo, tmp_path):
    """分母非正 → null。0 会除零, 负价是脏数据, 两者都不该产出数字。"""
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20), [
        {"symbol": "00700.HK", "close": 0.0},
        {"symbol": "09988.HK", "close": -1.5},
    ])
    client = _FakeEmClient([
        _row("00700.HK", "2026-11-25"),
        _row("09988.HK", "2026-11-26"),
    ])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    assert df["dividend_yield_pct"].null_count() == 2


def test_yield_zero_for_pure_bonus_issue_is_not_null(repo, tmp_path):
    """纯送转的股息率是 **0.0** (确认无现金), 与「算不出来」的 null 必须可区分。"""
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20),
                          [{"symbol": "06656.HK", "close": 62.65}])
    client = _FakeEmClient([_row("06656.HK", "2026-12-17", plan="每10股转20股")])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 12, 1), end=dt.date(2026, 12, 31), client=client
    )
    row = df.to_dicts()[0]
    assert row["dividend_yield_pct"] == 0.0
    assert row["bonus_ratio"] == pytest.approx(2.0)


def test_latest_close_prefers_newest_partition(repo, tmp_path):
    """多个分区都有该标的时取**最新**那个; 停牌股回落到最近一个有行情的分区。"""
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 18), [
        {"symbol": "00700.HK", "close": 500.0},
        {"symbol": "01810.HK", "close": 30.0},
    ])
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20), [
        {"symbol": "00700.HK", "close": 512.5},  # 01810 停牌, 不在最新分区
    ])
    client = _FakeEmClient([
        _row("00700.HK", "2026-11-25"),
        _row("01810.HK", "2026-11-26"),
    ])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["00700.HK"]["raw_close"] == pytest.approx(512.5)
    assert by_symbol["00700.HK"]["price_date"] == dt.date(2026, 11, 20)
    assert by_symbol["01810.HK"]["raw_close"] == pytest.approx(30.0)
    assert by_symbol["01810.HK"]["price_date"] == dt.date(2026, 11, 18)


def test_close_older_than_lookback_window_stays_null(repo, tmp_path):
    """超出回看窗口的旧价格**不**拿来冒充当前价 (fail closed)。

    窗口 = ``HK_DIVIDEND_CLOSE_LOOKBACK_PARTITIONS`` 个分区。这里造 1 个旧分区 +
    N 个更新的空分区把它挤出窗口, 断言股息率为 null 而不是一个基于陈旧价格的数字。
    """
    _seed_daily_partition(tmp_path, dt.date(2026, 1, 5),
                          [{"symbol": "00700.HK", "close": 400.0}])
    for i in range(svc.HK_DIVIDEND_CLOSE_LOOKBACK_PARTITIONS):
        day = dt.date(2026, 11, 1) + dt.timedelta(days=i)
        _seed_daily_partition(tmp_path, day, [{"symbol": "09988.HK", "close": 100.0}])
    client = _FakeEmClient([_row("00700.HK", "2026-11-25")])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    row = df.to_dicts()[0]
    assert row["raw_close"] is None
    assert row["dividend_yield_pct"] is None


def test_calendar_joins_name_and_industry(repo, tmp_path):
    _seed_instruments(tmp_path, [
        {"symbol": "00700.HK", "name": "腾讯控股", "industry": "软件服务"},
        {"symbol": "09988.HK", "name": "阿里巴巴-W", "industry": "专业零售"},
    ])
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20),
                          [{"symbol": "00700.HK", "close": 512.5}])
    client = _FakeEmClient([
        _row("00700.HK", "2026-11-25"),
        _row("01810.HK", "2026-11-26"),  # 维表里没有
    ])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["00700.HK"]["name"] == "腾讯控股"
    assert by_symbol["00700.HK"]["industry"] == "软件服务"
    # 维表缺该标的时名称留 null, 但**行不能丢** —— 除净日与代码本身就是有用信息
    assert by_symbol["01810.HK"]["name"] is None
    assert by_symbol["01810.HK"]["ex_date"] == dt.date(2026, 11, 26)


def test_duplicate_instrument_row_does_not_duplicate_calendar_row(repo, tmp_path):
    """维表里同一 symbol 出现两次时, 日历**仍是一行**。

    左连接遇到重复键会静默把行翻倍 —— 一次除净在页面上变成两条, 而数字看起来
    全都「合理」, 属于肉眼发现不了的那类错。维表按构造应当唯一, 这里钉的是
    「即便上游给了脏维表也不会污染日历」。
    """
    _seed_instruments(tmp_path, [
        {"symbol": "00700.HK", "name": "腾讯控股", "industry": "软件服务"},
        {"symbol": "00700.HK", "name": "腾讯控股(重复)", "industry": "软件服务"},
    ])
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20),
                          [{"symbol": "00700.HK", "close": 512.5}])
    client = _FakeEmClient([_row("00700.HK", "2026-11-25")])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    assert df.height == 1, "维表重复键把日历行翻倍了"
    assert df.to_dicts()[0]["name"] == "腾讯控股"


def test_calendar_tolerates_missing_instruments_file(repo, tmp_path):
    """维表还没同步 → 日历仍可用 (只有代码没有名称), 不是 500。"""
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20),
                          [{"symbol": "00700.HK", "close": 512.5}])
    client = _FakeEmClient([_row("00700.HK", "2026-11-25")])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    assert df.height == 1
    assert df.to_dicts()[0]["name"] is None
    assert df.to_dicts()[0]["dividend_yield_pct"] == pytest.approx(1.0 / 512.5 * 100)


def test_calendar_empty_window_returns_empty_frame_with_full_schema(repo):
    """窗口内无事件 → 空表但**列齐全**。

    列齐全很重要: 前端按列名渲染, 空表少列会让「这段时间没人除净」变成渲染报错。
    """
    client = _FakeEmClient([])
    df = svc.hk_dividend_calendar(
        repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
    )
    assert df.height == 0
    assert df.columns == list(svc.HK_DIVIDEND_CALENDAR_COLS)


def test_calendar_propagates_upstream_error(repo):
    """上游失败**原样抛出**, 不得降级成空表 (§4)。

    「拉不到」和「这段时间没人除净」是两种状态, 合并成一种就是把故障伪装成正常。
    """
    client = _FakeEmClient(EastmoneyError("datacenter 报表失败: 报表不存在"))
    with pytest.raises(EastmoneyError):
        svc.hk_dividend_calendar(
            repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31), client=client
        )


def test_calendar_closes_self_built_client(tmp_path):
    """``client=None`` 时自建自关, 不泄漏连接。"""
    repo = KlineRepository(DataStore(tmp_path))
    client = _FakeEmClient([])
    monkey = pytest.MonkeyPatch()
    monkey.setattr(svc, "EastmoneyClient", lambda: client)
    try:
        svc.hk_dividend_calendar(
            repo, start=dt.date(2026, 11, 20), end=dt.date(2026, 12, 31)
        )
    finally:
        monkey.undo()
    assert client.closed is True


# ---------------------------------------------------------------------------
# API 层: GET /api/hk/dividends
# ---------------------------------------------------------------------------

def test_dividends_endpoint_shape_and_quality_counts(api, tmp_path):
    """响应形状 + 两个数据质量计数 + 日期已序列化成 ISO 串。"""
    client, _, tmp_path, monkeypatch = api
    _seed_instruments(tmp_path, [{"symbol": "00700.HK", "name": "腾讯控股",
                                  "industry": "软件服务"}])
    _seed_daily_partition(tmp_path, dt.date(2026, 11, 20), [
        {"symbol": "00700.HK", "close": 512.5},
        # 未解析的标的**也**给收盘价: 两个质量计数是独立信号, 不能让它们互相遮蔽
        # (若这里不 seed, 00005 会同时进两个计数, 断言就测不出「各自数对了什么」)
        {"symbol": "00005.HK", "close": 48.0},
    ])
    _patch_rows(monkeypatch, [
        _row("00700.HK", "2026-11-25"),
        _row("00005.HK", "2026-11-26", plan="每股派美元0.1元"),  # 未解析, 有价
        _row("80992.HK", "2026-12-09"),                          # 已解析, 无价
    ])
    r = client.get("/api/hk/dividends", params={"start": "2026-11-20", "end": "2026-12-31"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["start"] == "2026-11-20"
    assert body["end"] == "2026-12-31"
    assert body["market"] == "HK"
    assert body["currency"] == "HKD"
    assert body["unparsed_count"] == 1
    assert body["missing_close_count"] == 1
    # 两个计数指向**不同**的行, 证明它们各自数对了东西而不是同一个缺陷数两遍
    by_sym = {it["symbol"]: it for it in body["items"]}
    assert by_sym["00005.HK"]["parsed"] is False
    assert by_sym["00005.HK"]["raw_close"] == pytest.approx(48.0)
    assert by_sym["80992.HK"]["parsed"] is True
    assert by_sym["80992.HK"]["raw_close"] is None
    first = body["items"][0]
    assert first["symbol"] == "00700.HK"
    assert first["name"] == "腾讯控股"
    assert first["ex_date"] == "2026-11-25", "date 必须已转成 ISO 串"
    assert first["price_date"] == "2026-11-20"
    assert first["dividend_yield_pct"] == pytest.approx(1.0 / 512.5 * 100)
    assert set(first) == set(svc.HK_DIVIDEND_CALENDAR_COLS)


def test_dividends_default_window_starts_today(api, monkeypatch):
    """缺省窗口 = 今天 → 今天 + ``HK_DIVIDEND_DEFAULT_FORWARD_DAYS``。"""
    client, _, _, monkeypatch = api
    captured: dict = {}

    def _fake(client_, *, start, end, **kw):
        captured["start"], captured["end"] = start, end
        return []

    monkeypatch.setattr(svc, "fetch_hk_dividend_calendar", _fake)
    r = client.get("/api/hk/dividends")
    assert r.status_code == 200
    today = dt.date.today()
    assert captured["start"] == today
    assert captured["end"] == today + dt.timedelta(
        days=hk_api.HK_DIVIDEND_DEFAULT_FORWARD_DAYS
    )


def test_dividends_rejects_inverted_window(api):
    client, _, _, _ = api
    r = client.get("/api/hk/dividends", params={"start": "2026-12-31", "end": "2026-11-20"})
    assert r.status_code == 400
    assert "不能晚于" in r.json()["detail"]


def test_dividends_rejects_oversized_window(api):
    """窗口硬闸: 太宽会让一次 HTTP 请求翻几十页并占满上游令牌桶。"""
    client, _, _, _ = api
    limit = hk_api.HK_DIVIDEND_MAX_WINDOW_DAYS
    r = client.get("/api/hk/dividends", params={
        "start": "2026-01-01",
        "end": (dt.date(2026, 1, 1) + dt.timedelta(days=limit + 1)).isoformat(),
    })
    assert r.status_code == 400
    assert str(limit) in r.json()["detail"]


def test_dividends_returns_502_on_upstream_error(api, monkeypatch):
    """上游失败 → 502, **不是** 200 + 空列表 (§4 fail closed)。"""
    client, _, _, monkeypatch = api
    _patch_rows(monkeypatch, EastmoneyError("datacenter 报表失败"))
    r = client.get("/api/hk/dividends", params={"start": "2026-11-20", "end": "2026-12-31"})
    assert r.status_code == 502
    assert "港股分红数据源不可用" in r.json()["detail"]


def test_dividends_empty_window_is_200_with_zero_total(api, monkeypatch):
    """「这段时间没人除净」是合法答案, 与 502 必须可区分。"""
    client, _, _, monkeypatch = api
    _patch_rows(monkeypatch, [])
    r = client.get("/api/hk/dividends", params={"start": "2026-11-20", "end": "2026-12-31"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "items": [], "total": 0, "start": "2026-11-20", "end": "2026-12-31",
        "market": "HK", "currency": "HKD", "source": "eastmoney_datacenter",
        "unparsed_count": 0, "missing_close_count": 0,
    }
