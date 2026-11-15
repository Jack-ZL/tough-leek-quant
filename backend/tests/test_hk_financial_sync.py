"""港股财务同步服务层契约测试 (全离线, 用假 client 注入)。

覆盖的都是**会造成静默数据错误**的语义, 而非单纯的行覆盖率:

- 合并语义: 新行覆盖旧行 (财报重述), 但**滚动窗口外的历史必须保留**;
- 0 行 / 拉取失败时**保留历史不覆盖** (否则一次网络抖动会清空整张表);
- ``fiscal_year`` 必须由 ``period_end`` 重新推导成 Int64 —— 东财的
  ``FISCAL_YEAR`` 是 ``"12-31"`` 字符串 (财年结日), 与 A 股同名不同义;
- ``announce_date`` / ``report_date_ms`` 恒为 null, **不用报告期冒充公告日**
  (CONTRIBUTING:586 要求按公告日对齐历史股本, 伪造会污染下游);
- ``backfill_hk_lot_size`` 只填空与纠错, **绝不删除**已有值, 且保持列序。
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl
import pytest

from app.plugins.eastmoney import hk_financials as hf
from app.plugins.eastmoney.client import EastmoneyError
from app.services import financial_sync_hk as fs
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeClient:
    """按报表名返回预设 payload; 记录调用次数与是否被 close。"""

    def __init__(self, payloads=None, *, exc=None, report=None):
        self._payloads = payloads or []
        self._exc = exc
        self._report = report
        self.calls: list[dict] = []
        self.closed = False

    def datacenter_report(self, report_name, flt=None, **kw):
        self.calls.append({"report": report_name, "flt": flt, **kw})
        if self._exc is not None:
            raise self._exc
        if self._report is not None and report_name != self._report:
            return []
        # 只在第一页返回数据, 短页即终止翻页
        return list(self._payloads) if kw.get("page_number", 1) == 1 else []

    def close(self):
        self.closed = True


def _mi(code="00700", report_date="2026-06-30", **over) -> dict:
    row = {
        "SECUCODE": f"{code}.HK", "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "腾讯控股", "ORG_TYPE": "一般企业",
        "REPORT_DATE": f"{report_date} 00:00:00", "DATE_TYPE_CODE": "002",
        "REPORT_TYPE": "2026年中报", "FISCAL_YEAR": "12-31",
        "CURRENCY": "HKD", "IS_CNY_CODE": 0, "PER_SHARES": 100,
        "BASIC_EPS": 12.6, "BPS": 98.5, "ROE_AVG": 9.97, "ROA": 5.44,
        "GROSS_PROFIT_RATIO": 57.2, "NET_PROFIT_RATIO": 29.25,
        "DEBT_ASSET_RATIO": 43.4, "CURRENT_RATIO": 1.8,
        "OPERATE_INCOME": 4.01e11, "OPERATE_INCOME_YOY": 10.07,
        "HOLDER_PROFIT": 1.14e11, "HOLDER_PROFIT_YOY": 10.31,
        "OPERATE_PROFIT": 1.35e11, "PRETAX_PROFIT": 1.44e11,
        "TOTAL_ASSETS": 2.16e12, "TOTAL_LIABILITIES": 9.37e11,
        "TOTAL_PARENT_EQUITY": 1.14e12, "NETCASH_OPERATE": 1.54e11,
        "NETCASH_INVEST": -3.2e10, "NETCASH_FINANCE": -4.1e10,
        "OCF_SALES": 0.38, "PE_TTM": 13.17, "PB_TTM": 2.69,
        "TOTAL_MARKET_CAP": 3.5e12,
    }
    row.update(over)
    return row


def _st(code="00700", item="004001001", name="营业额", report_date="2026-06-30", **over) -> dict:
    row = {
        "SECUCODE": f"{code}.HK", "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "腾讯控股", "ORG_TYPE": "一般企业",
        "REPORT_DATE": f"{report_date} 00:00:00", "DATE_TYPE_CODE": "002",
        "REPORT_TYPE": "中报", "FISCAL_YEAR": "12-31",
        "ACCOUNT_STANDARD": "国际会计准则", "CURRENCY": "人民币",
        "CURRENCY_CODE": "CNY", "STD_ITEM_CODE": item, "ITEM_NAME": name,
        "AMOUNT": 3.96e11, "YOY_RATIO": 9.64,
    }
    row.update(over)
    return row


def _mf(payloads: list[dict]) -> pl.DataFrame:
    """原始 eastmoney payload → **已映射**宽表帧。

    服务层 (合并/投影/落库) 操作的一律是映射后的内部列名, 测试必须走同一条
    映射路径, 否则测的是另一套 schema。
    """
    return pl.DataFrame(hf.hk_main_indicator_rows(payloads)).select(
        list(hf.HK_MAIN_INDICATOR_COLUMNS)
    )


def _seed_wide(tmp_path, payloads: list[dict]) -> None:
    """直接落一份宽表 parquet, 绕过网络。"""
    out = fs.hk_financials_path(tmp_path, "main_indicator")
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(_mf(payloads), out)


def _seed_dim(tmp_path, rows: list[dict]) -> None:
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


# ---------------------------------------------------------------------------
# 路径与默认窗口
# ---------------------------------------------------------------------------
def test_table_paths(tmp_path):
    assert fs.hk_financials_dir(tmp_path, "main_indicator") == tmp_path / "financials_hk" / "main_indicator"
    assert fs.hk_financials_path(tmp_path, "income").name == "part.parquet"


@pytest.mark.parametrize("bad", ["cash_flow", "metrics", "", "MAIN_INDICATOR"])
def test_unknown_table_raises(tmp_path, bad):
    """拼错表名必须立刻抛错: 静默建一个新目录会让数据写进没人读的地方。"""
    with pytest.raises(ValueError):
        fs.hk_financials_dir(tmp_path, bad)


def test_statement_reports_are_the_two_long_tables():
    assert fs.HK_STATEMENT_REPORTS == {"income": hf.RPT_HK_INCOME, "balance": hf.RPT_HK_BALANCE}


def test_financial_tables_tuple():
    assert fs.HK_FINANCIAL_TABLES == ("main_indicator", "income", "balance")


def test_default_since_is_rolling_six_years():
    got = fs.default_hk_fin_since(today=dt.date(2026, 11, 15))
    assert got == "2020-11-15"


def test_default_since_honours_years():
    assert fs.default_hk_fin_since(years=2, today=dt.date(2026, 11, 15)) == "2024-11-15"


def test_default_since_handles_leap_day():
    """2/29 往前推一年没有 2/29, 必须回退到 2/28 而不是抛 ValueError。"""
    assert fs.default_hk_fin_since(years=1, today=dt.date(2024, 2, 29)) == "2023-02-28"


# ---------------------------------------------------------------------------
# 读取: 缺失/损坏一律降级为空帧
# ---------------------------------------------------------------------------
def test_get_df_missing_returns_empty(tmp_path):
    """首次使用 (从未同步) 必须返回空帧而非抛错, 否则 API 会 500。"""
    df = fs.get_hk_financial_df(tmp_path, "main_indicator")
    assert df.is_empty()


def test_get_df_corrupt_returns_empty(tmp_path, caplog):
    path = fs.hk_financials_path(tmp_path, "main_indicator")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file")
    with caplog.at_level(logging.WARNING):
        df = fs.get_hk_financial_df(tmp_path, "main_indicator")
    assert df.is_empty()
    assert "失败" in caplog.text


def test_latest_report_date_none_when_empty(tmp_path):
    assert fs.latest_hk_report_date(tmp_path) is None


def test_latest_report_date_picks_max(tmp_path):
    _seed_wide(tmp_path, [_mi(report_date="2025-12-31"), _mi(report_date="2026-09-30")])
    assert fs.latest_hk_report_date(tmp_path) == dt.date(2026, 9, 30)


def test_symbols_and_rows_iterators(tmp_path):
    _seed_wide(tmp_path, [_mi("00700"), _mi("09988", SECURITY_NAME_ABBR="阿里巴巴-W")])
    assert fs.hk_financial_symbols(tmp_path) == ["00700.HK", "09988.HK"]
    rows = list(fs.iter_hk_financial_rows(tmp_path, "main_indicator"))
    assert len(rows) == 2
    assert rows[0]["symbol"] == "00700.HK"


def test_symbols_empty_store(tmp_path):
    assert fs.hk_financial_symbols(tmp_path) == []
    assert list(fs.iter_hk_financial_rows(tmp_path, "main_indicator")) == []


# ---------------------------------------------------------------------------
# 合并语义
# ---------------------------------------------------------------------------
def test_merge_new_wins_on_restatement():
    """财报重述: 同 (symbol, report_date, date_type_code) 的新行必须覆盖旧行。"""
    old = _mf([_mi(BASIC_EPS=10.0)])
    new = _mf([_mi(BASIC_EPS=12.6)])
    merged = fs._merge_frames(old, new)
    assert merged.height == 1
    assert merged["basic_eps"][0] == 12.6


def test_merge_preserves_history_outside_window():
    """🔴 滚动窗口只影响"本次拉取范围", 不能删掉窗口外的历史行。"""
    old = _mf([_mi(report_date="2019-12-31"), _mi(report_date="2026-06-30")])
    new = _mf([_mi(report_date="2026-06-30", BASIC_EPS=12.6)])
    merged = fs._merge_frames(old, new)
    assert merged.height == 2
    assert sorted(merged["report_date"].to_list()) == ["2019-12-31", "2026-06-30"]


def test_merge_keeps_other_symbols():
    old = _mf([_mi("09988")])
    new = _mf([_mi("00700")])
    merged = fs._merge_frames(old, new)
    assert sorted(merged["symbol"].to_list()) == ["00700.HK", "09988.HK"]


def test_merge_is_idempotent():
    frame = _mf([_mi()])
    once = fs._merge_frames(frame, frame)
    twice = fs._merge_frames(once, frame)
    assert once.height == twice.height == 1


def test_merge_distinguishes_period_kinds():
    """去重键含 ``date_type_code``: 同一 REPORT_DATE 的年报与中报是两行, 不能互相覆盖。"""
    old = _mf([_mi(DATE_TYPE_CODE="001", REPORT_TYPE="2025年报")])
    new = _mf([_mi(DATE_TYPE_CODE="002")])
    assert fs._merge_frames(old, new).height == 2


def test_merge_tolerates_schema_drift():
    """上游新增字段时旧文件缺列, ``diagonal_relaxed`` 必须补 null 而非抛错。"""
    old = _mf([_mi()])
    new = old.drop("pe_ttm")
    merged = fs._merge_frames(old, new)
    assert "pe_ttm" in merged.columns


# ---------------------------------------------------------------------------
# sync_hk_main_indicator
# ---------------------------------------------------------------------------
def test_sync_writes_rows(tmp_path):
    c = FakeClient([_mi("00700"), _mi("09988", SECURITY_NAME_ABBR="阿里巴巴-W")])
    n = fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=c)
    assert n == 2
    df = fs.get_hk_financial_df(tmp_path, "main_indicator")
    assert sorted(df["symbol"].to_list()) == ["00700.HK", "09988.HK"]
    assert df.width == len(hf.HK_MAIN_INDICATOR_COLUMNS)


def test_sync_is_idempotent(tmp_path):
    """重复同步同一批数据不得产生重复行。"""
    payloads = [_mi("00700"), _mi("09988")]
    fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=FakeClient(payloads))
    n = fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=FakeClient(payloads))
    assert n == 2


def test_sync_merges_with_existing(tmp_path):
    _seed_wide(tmp_path, [_mi("09988")])
    n = fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=FakeClient([_mi("00700")]))
    assert n == 2


def test_sync_zero_rows_keeps_history(tmp_path, caplog):
    """🔴 拉到 0 行时**保留历史不覆盖**: 否则一次上游异常会清空整张表。"""
    _seed_wide(tmp_path, [_mi("00700"), _mi("09988")])
    with caplog.at_level(logging.WARNING):
        n = fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=FakeClient([]))
    assert n == 2
    assert "保留历史不覆盖" in caplog.text
    assert fs.get_hk_financial_df(tmp_path, "main_indicator").height == 2


def test_sync_error_keeps_history(tmp_path, caplog):
    """网络/限流异常降级为告警并返回既有行数, 不抛给调用方 (长任务里抛会中断整批)。"""
    _seed_wide(tmp_path, [_mi("00700")])
    c = FakeClient(exc=EastmoneyError("banned"))
    with caplog.at_level(logging.WARNING):
        n = fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=c)
    assert n == 1
    assert "同步失败" in caplog.text


def test_sync_batches_secucodes_by_server_limit(tmp_path):
    """``in`` 批量上限 200, 超过会 HTTP 414; 服务层必须自动分批。"""
    codes = [f"{i:05d}" for i in range(hf.HK_FIN_IN_BATCH_MAX + 5)]
    c = FakeClient([_mi("00700")])
    fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", secucodes=codes, client=c)
    assert len(c.calls) == 2
    assert c.calls[0]["flt"].count(".HK") == hf.HK_FIN_IN_BATCH_MAX
    assert c.calls[1]["flt"].count(".HK") == 5


def test_sync_accepts_bare_and_suffixed_symbols(tmp_path):
    """``00700`` 与 ``00700.HK`` 等价, 且 filter 里不得出现双后缀。"""
    c = FakeClient([_mi("00700")])
    fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", secucodes=["00700", "09988.HK"], client=c)
    assert c.calls[0]["flt"] == "(REPORT_DATE>='2024-01-01')(SECUCODE in (\"00700.HK\",\"09988.HK\"))"


def test_sync_annual_only_filter(tmp_path):
    c = FakeClient([_mi()])
    fs.sync_hk_main_indicator(
        tmp_path, since="2024-01-01", date_type_code=hf.HK_DATE_TYPE_ANNUAL, client=c
    )
    assert '(DATE_TYPE_CODE="001")' in c.calls[0]["flt"]


def test_sync_reports_progress(tmp_path):
    seen = []
    fs.sync_hk_main_indicator(
        tmp_path, since="2024-01-01", client=FakeClient([_mi()]),
        on_progress=lambda stage, pct, msg: seen.append((stage, pct)),
    )
    stages = [s for s, _ in seen]
    pcts = [p for _, p in seen]
    assert set(stages) == {"hk_financials"}
    assert pcts[0] == 5 and pcts[-1] == 100
    assert pcts == sorted(pcts)  # 进度必须单调, 否则前端进度条会倒退


def test_sync_closes_own_client(tmp_path, monkeypatch):
    """未传 client 时自建并负责关闭 (泄漏会耗尽连接池)。"""
    made = []

    class Spy(FakeClient):
        def __init__(self):
            super().__init__([_mi()])
            made.append(self)

    monkeypatch.setattr("app.plugins.eastmoney.client.EastmoneyClient", Spy)
    fs.sync_hk_main_indicator(tmp_path, since="2024-01-01")
    assert made and made[0].closed


def test_sync_does_not_close_injected_client(tmp_path):
    """调用方传入的 client 由调用方管理生命周期。"""
    c = FakeClient([_mi()])
    fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=c)
    assert not c.closed


def test_sync_backfills_lot_size_into_dim_table(tmp_path):
    """⭐ ``PER_SHARES`` 回填 ``instruments_hk.lot_size``: 东财 clist 不提供每手股数,
    这是唯一已验证的免费来源 (实测 8/8 匹配真值)。"""
    _seed_dim(tmp_path, [
        {"symbol": "00700.HK", "name": "腾讯控股", "lot_size": None},
        {"symbol": "01398.HK", "name": "工商银行", "lot_size": None},
    ])
    fs.sync_hk_main_indicator(
        tmp_path, since="2024-01-01",
        client=FakeClient([_mi("00700", PER_SHARES=100), _mi("01398", PER_SHARES=1000)]),
    )
    dim = pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)
    assert dict(zip(dim["symbol"], dim["lot_size"], strict=True)) == {
        "00700.HK": 100, "01398.HK": 1000,
    }


def test_sync_backfill_can_be_disabled(tmp_path):
    _seed_dim(tmp_path, [{"symbol": "00700.HK", "name": "腾讯控股", "lot_size": None}])
    fs.sync_hk_main_indicator(
        tmp_path, since="2024-01-01", client=FakeClient([_mi()]), backfill_lot_size=False
    )
    dim = pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)
    assert dim["lot_size"][0] is None


def test_sync_backfill_failure_degrades_to_warning(tmp_path, caplog, monkeypatch):
    """回填失败不能影响财务表本身已落库 (降级为告警)。"""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fs, "backfill_hk_lot_size", boom)
    with caplog.at_level(logging.WARNING):
        n = fs.sync_hk_main_indicator(tmp_path, since="2024-01-01", client=FakeClient([_mi()]))
    assert n == 1
    assert "回填港股每手股数失败" in caplog.text
    assert fs.get_hk_financial_df(tmp_path, "main_indicator").height == 1


# ---------------------------------------------------------------------------
# sync_hk_statement (长表, 按标的)
# ---------------------------------------------------------------------------
def test_statement_sync_rejects_ashare_symbol_before_network(tmp_path):
    """🔴 先校验再触网: A 股代码打错时立刻报错, 而不是拿到空结果让人以为"没有财报"。"""
    c = FakeClient([_st()], report=hf.RPT_HK_INCOME)
    with pytest.raises(Exception) as ei:
        fs.sync_hk_statement(tmp_path, "600519.SH", table="income", client=c)
    assert c.calls == []  # 未触网
    assert "600519" in str(ei.value) or "market" in str(ei.value).lower()


def test_statement_sync_rejects_unknown_table(tmp_path):
    with pytest.raises(ValueError):
        fs.sync_hk_statement(tmp_path, "00700.HK", table="cash_flow", client=FakeClient())


def test_statement_sync_writes_and_keeps_other_symbols(tmp_path):
    c1 = FakeClient([_st("00700", item="004001001", name="营业额")], report=hf.RPT_HK_INCOME)
    fs.sync_hk_statement(tmp_path, "00700.HK", table="income", client=c1)
    c2 = FakeClient([_st("09988", item="004001001", name="营业额")], report=hf.RPT_HK_INCOME)
    n = fs.sync_hk_statement(tmp_path, "09988.HK", table="income", client=c2)
    df = fs.get_hk_financial_df(tmp_path, "income")
    assert n == df.height == 2
    assert sorted(df["symbol"].unique().to_list()) == ["00700.HK", "09988.HK"]


def test_statement_sync_replaces_only_that_symbol(tmp_path):
    """重同步同一标的: 旧行被替换 (不累积), 其他标的保留。"""
    fs.sync_hk_statement(
        tmp_path, "00700.HK", table="income",
        client=FakeClient([_st("00700", AMOUNT=1.0)], report=hf.RPT_HK_INCOME),
    )
    fs.sync_hk_statement(
        tmp_path, "09988.HK", table="income",
        client=FakeClient([_st("09988", AMOUNT=2.0)], report=hf.RPT_HK_INCOME),
    )
    n = fs.sync_hk_statement(
        tmp_path, "00700.HK", table="income",
        client=FakeClient([_st("00700", AMOUNT=3.0)], report=hf.RPT_HK_INCOME),
    )
    df = fs.get_hk_financial_df(tmp_path, "income")
    assert n == df.height == 2
    assert df.filter(pl.col("symbol") == "00700.HK")["amount"][0] == 3.0


def test_statement_sync_zero_rows_keeps_history(tmp_path):
    fs.sync_hk_statement(
        tmp_path, "00700.HK", table="income",
        client=FakeClient([_st()], report=hf.RPT_HK_INCOME),
    )
    n = fs.sync_hk_statement(
        tmp_path, "00700.HK", table="income",
        client=FakeClient([], report=hf.RPT_HK_INCOME),
    )
    assert n == 1


def test_statement_sync_error_keeps_history(tmp_path, caplog):
    fs.sync_hk_statement(
        tmp_path, "00700.HK", table="income",
        client=FakeClient([_st()], report=hf.RPT_HK_INCOME),
    )
    with caplog.at_level(logging.WARNING):
        n = fs.sync_hk_statement(
            tmp_path, "00700.HK", table="income",
            client=FakeClient(exc=EastmoneyError("banned"), report=hf.RPT_HK_INCOME),
        )
    assert n == 1
    assert "同步失败" in caplog.text


def test_statement_sync_legacy_file_without_symbol_column(tmp_path, caplog):
    """历史文件缺 ``symbol`` 列 (损坏/旧格式): 以新帧覆盖, 不能 diagonal concat
    出一整列无法定位标的的孤儿行。"""
    path = fs.hk_financials_path(tmp_path, "income")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = pl.DataFrame(hf.hk_statement_rows([_st()])).select(
        list(hf.HK_STATEMENT_COLUMNS)
    ).drop("symbol")
    atomic_write_parquet(legacy, path)
    with caplog.at_level(logging.WARNING):
        n = fs.sync_hk_statement(
            tmp_path, "00700.HK", table="income",
            client=FakeClient([_st()], report=hf.RPT_HK_INCOME),
        )
    assert n == 1
    assert "缺 symbol 列" in caplog.text
    assert fs.get_hk_financial_df(tmp_path, "income")["symbol"].to_list() == ["00700.HK"]


def test_statement_sync_uses_correct_report_name(tmp_path):
    c = FakeClient([_st()], report=hf.RPT_HK_BALANCE)
    fs.sync_hk_statement(tmp_path, "00700.HK", table="balance", client=c)
    assert c.calls[0]["report"] == hf.RPT_HK_BALANCE


# ---------------------------------------------------------------------------
# backfill_hk_lot_size
# ---------------------------------------------------------------------------
def _dim(symbol, name, lot=None, **extra) -> dict:
    return {"symbol": symbol, "name": name, "lot_size": lot, **extra}


def test_backfill_fills_nulls(tmp_path):
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股"), _dim("00005.HK", "汇丰控股")])
    n = fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100, "00005.HK": 400})
    assert n == 2
    dim = pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)
    assert dim["lot_size"].to_list() == [100, 400]


def test_backfill_corrects_wrong_existing_value(tmp_path):
    """已有值与权威源不一致时**纠正** (每手股数会因拆股/合股变更)。"""
    _seed_dim(tmp_path, [_dim("01398.HK", "工商银行", lot=999)])
    n = fs.backfill_hk_lot_size(tmp_path, {"01398.HK": 1000})
    assert n == 1
    assert pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)["lot_size"][0] == 1000


def test_backfill_leaves_unlisted_symbols_alone(tmp_path):
    """🔴 只填空与纠错, **绝不删除**未在本次结果里的标的的已有值。"""
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股", lot=100), _dim("09988.HK", "阿里巴巴-W", lot=100)])
    n = fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100})
    assert n == 0
    dim = pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)
    assert dim["lot_size"].to_list() == [100, 100]


def test_backfill_preserves_column_order_and_other_columns(tmp_path):
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股", industry="软件服务", tick_size=0.1)])
    fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100})
    dim = pl.read_parquet(tmp_path / HK_INSTRUMENT_RELPATH)
    assert dim.columns == ["symbol", "name", "lot_size", "industry", "tick_size"]
    assert dim["industry"][0] == "软件服务"
    assert dim["tick_size"][0] == 0.1


def test_backfill_is_idempotent(tmp_path):
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股")])
    assert fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100}) == 1
    assert fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100}) == 0


def test_backfill_no_dim_table_returns_zero(tmp_path):
    """维表尚未同步时返回 0 而不是抛错 (财务同步不该被维表状态阻断)。"""
    assert fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100}) == 0


def test_backfill_empty_lots_returns_zero(tmp_path):
    _seed_dim(tmp_path, [_dim("00700.HK", "腾讯控股")])
    assert fs.backfill_hk_lot_size(tmp_path, {}) == 0


def test_backfill_adds_column_when_dim_lacks_it(tmp_path):
    """维表是旧格式 (无 lot_size 列) 时补列, 不抛错。"""
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({"symbol": ["00700.HK"], "name": ["腾讯控股"]}), out)
    assert fs.backfill_hk_lot_size(tmp_path, {"00700.HK": 100}) == 1
    assert pl.read_parquet(out)["lot_size"][0] == 100


def test_lot_size_map_takes_latest_period(tmp_path):
    _seed_wide(tmp_path, [
        _mi(report_date="2024-12-31", PER_SHARES=100),
        _mi(report_date="2026-06-30", PER_SHARES=200),
    ])
    assert fs.hk_lot_size_map(tmp_path) == {"00700.HK": 200}


def test_lot_size_map_empty_store(tmp_path):
    assert fs.hk_lot_size_map(tmp_path) == {}


def test_lot_size_is_preserved_across_instrument_resync():
    """🔴 回归: ``lot_size`` 必须在 ``_HK_PRESERVE_FIELDS`` 里。

    东财 clist **不提供**每手股数, 维表重同步会写 None; 若不在保留名单里,
    每次重同步都会抹掉回填结果, 下单取整随即失效。
    """
    from app.services import instrument_sync_hk

    assert "lot_size" in instrument_sync_hk._HK_PRESERVE_FIELDS


# ---------------------------------------------------------------------------
# hk_financials_canonical: 与 A 股共页的列契约
# ---------------------------------------------------------------------------
def _wide_frame(payloads=None) -> pl.DataFrame:
    return _mf(payloads or [_mi()])


@pytest.mark.parametrize("table", ["metrics", "income", "balance_sheet", "cash_flow"])
def test_canonical_emits_the_four_ashare_tables(table):
    assert not fs.hk_financials_canonical(_wide_frame(), table).is_empty()


@pytest.mark.parametrize("bad", ["main_indicator", "nope", ""])
def test_canonical_rejects_unknown_table(bad):
    with pytest.raises(ValueError):
        fs.hk_financials_canonical(_wide_frame(), bad)


def test_canonical_fiscal_year_is_int_derived_from_period_end():
    """🔴 东财 ``FISCAL_YEAR`` 是 ``"12-31"`` (财年结日), A 股 ``fiscal_year`` 是
    Int64 年份。同名不同义, 必须由 ``period_end`` 重新推导。"""
    out = fs.hk_financials_canonical(_wide_frame(), "metrics")
    assert out.schema["fiscal_year"] == pl.Int64
    assert out["fiscal_year"][0] == 2026
    assert "12-31" not in [str(v) for v in out["fiscal_year"].to_list()]


def test_canonical_fiscal_year_follows_non_calendar_year_end():
    """港股年结日不统一 (实测年报 REPORT_DATE 月份跨 02~12), fiscal_year 取
    period_end 的年份而非"上一个日历年"。"""
    out = fs.hk_financials_canonical(
        _wide_frame([_mi(report_date="2026-03-31", DATE_TYPE_CODE="001")]), "metrics"
    )
    assert out["period_end"][0] == "2026-03-31"
    assert out["fiscal_year"][0] == 2026


def test_canonical_announce_date_is_null_not_faked():
    """🔴 港股宽表不提供公告日。**不用报告期冒充**: CONTRIBUTING:586 要求按公告日
    对齐历史股本, 伪造会污染下游换手率计算。"""
    out = fs.hk_financials_canonical(_wide_frame(), "metrics")
    assert "announce_date" in out.columns
    assert out["announce_date"].null_count() == out.height
    assert out["report_date_ms"].null_count() == out.height


def test_canonical_period_end_and_ms(tmp_path):
    out = fs.hk_financials_canonical(_wide_frame(), "metrics")
    assert out["period_end"][0] == "2026-06-30"
    expected = int(dt.datetime(2026, 6, 30, tzinfo=dt.UTC).timestamp() * 1000)
    assert out["period_end_ms"][0] == expected
    assert out.schema["period_end_ms"] == pl.Int64


def test_canonical_metrics_column_set():
    """列集即前端契约, 变动需同步改前端。"""
    out = fs.hk_financials_canonical(_wide_frame(), "metrics")
    assert set(out.columns) == {
        "symbol", "period_end", "announce_date", "fiscal_year", "report_date_ms",
        "period_end_ms", "eps_basic", "bps", "revenue_yoy", "net_income_yoy", "roa",
        "roe", "gross_margin", "net_margin", "current_ratio", "debt_to_asset_ratio",
        "operating_cash_to_revenue", "name", "org_type", "period_name",
        "date_type_code", "pricing_currency", "lot_size", "pe_ttm", "pb_ttm",
        "total_market_cap",
    }


def test_canonical_income_column_set():
    out = fs.hk_financials_canonical(_wide_frame(), "income")
    assert {"revenue", "operating_profit", "total_profit", "net_income_attributable",
            "basic_eps"} <= set(out.columns)


def test_canonical_balance_sheet_column_set():
    out = fs.hk_financials_canonical(_wide_frame(), "balance_sheet")
    assert {"total_assets", "total_liabilities", "total_equity"} <= set(out.columns)


def test_canonical_cash_flow_column_set():
    out = fs.hk_financials_canonical(_wide_frame(), "cash_flow")
    assert {"net_operating_cash_flow", "net_investing_cash_flow",
            "net_financing_cash_flow"} <= set(out.columns)


def test_canonical_maps_operating_cash_to_revenue():
    """``OCF_SALES`` (经营现金流/营业收入) 与 A 股 ``operating_cash_to_revenue`` 同义。"""
    out = fs.hk_financials_canonical(_wide_frame([_mi(OCF_SALES=0.38)]), "metrics")
    assert out["operating_cash_to_revenue"][0] == 0.38


def test_canonical_missing_source_becomes_null_column():
    """上游缺字段时补 null 列而非抛错 (前端按列名取值, 缺列会 KeyError)。"""
    frame = _wide_frame().drop("pe_ttm")
    out = fs.hk_financials_canonical(frame, "metrics")
    assert "pe_ttm" in out.columns
    assert out["pe_ttm"].null_count() == out.height


def test_canonical_empty_frame_keeps_schema():
    """空帧投影必须仍带完整列集, 否则前端拿到 {} 会崩。"""
    empty = _wide_frame().head(0)
    out = fs.hk_financials_canonical(empty, "metrics")
    assert out.is_empty()
    assert len(out.columns) == 26


@pytest.mark.parametrize("table", ["metrics", "income", "balance_sheet", "cash_flow"])
def test_canonical_empty_frame_column_order_matches_populated(table):
    """🔴 空帧与非空帧的**列序必须一致**: 契约是"列集恒定", 若空帧退化成
    另一种形态, 读端就要分别处理两条路径。"""
    assert list(fs.hk_canonical_schema(table)) == list(
        fs.hk_financials_canonical(_wide_frame(), table).columns
    )
    assert list(fs.hk_financials_canonical(_wide_frame().head(0), table).columns) == list(
        fs.hk_financials_canonical(_wide_frame(), table).columns
    )


def test_canonical_schema_dtypes_match_ashare_keys():
    """键列 dtype 必须与 A 股实测一致, 否则两市场帧无法 concat。"""
    schema = fs.hk_canonical_schema("metrics")
    assert schema["symbol"] == pl.Utf8
    assert schema["period_end"] == pl.Utf8
    assert schema["announce_date"] == pl.Utf8
    assert schema["fiscal_year"] == pl.Int64
    assert schema["report_date_ms"] == pl.Int64
    assert schema["period_end_ms"] == pl.Int64


def test_canonical_schema_rejects_unknown_table():
    with pytest.raises(ValueError):
        fs.hk_canonical_schema("nope")


def test_canonical_hk_only_field_missing_becomes_null_column():
    """🔴 港股独有字段 (``pe_ttm`` 等) 源列缺失时也要补 null 列, 不能省略 ——
    与 A 股同名映射列的处理必须一致, 否则"列集恒定"只在部分字段上成立。"""
    frame = _wide_frame().drop("pe_ttm", "lot_size", "org_type")
    out = fs.hk_financials_canonical(frame, "metrics")
    for col in ("pe_ttm", "lot_size", "org_type"):
        assert col in out.columns, col
        assert out[col].null_count() == out.height
    assert out.schema["lot_size"] == pl.Int64
    assert out.schema["org_type"] == pl.Utf8
    assert out.schema["pe_ttm"] == pl.Float64


def test_canonical_does_not_emit_turnover_days_as_ratio():
    """🔴 港股 ``*_TDAYS`` 是周转天数, A 股 ``*_turnover*`` 是周转率, 不可混用。"""
    frame = _wide_frame([_mi(INVENTORY_TDAYS=45.0, ACCOUNTS_RECE_TDAYS=30.0)])
    out = fs.hk_financials_canonical(frame, "metrics")
    for banned in ("inventory_turnover", "receive_account_turnover_ratio",
                   "total_assets_turnover_ratio", "current_assets_turnover_ratio"):
        assert banned not in out.columns


def test_canonical_does_not_emit_end_cash_as_cash_and_equivalents():
    """🔴 ``END_CASH`` 是现金流量表期末现金, ≠ 资产负债表货币资金。"""
    frame = _wide_frame([_mi(END_CASH=1.2e11)])
    out = fs.hk_financials_canonical(frame, "balance_sheet")
    assert "cash_and_equivalents" not in out.columns


def test_canonical_no_duplicate_column_names():
    """规范名与港股独有字段重名会让 ``select`` 抛错, 投影必须去重。"""
    for table in ("metrics", "income", "balance_sheet", "cash_flow"):
        out = fs.hk_financials_canonical(_wide_frame(), table)
        assert len(out.columns) == len(set(out.columns)), table


def test_canonical_preserves_hk_only_context_fields():
    """``org_type`` / ``pricing_currency`` / ``lot_size`` 是港股渲染分支的依据, 必须带出。"""
    out = fs.hk_financials_canonical(_wide_frame([_mi(ORG_TYPE="银行", PER_SHARES=1000)]), "metrics")
    assert out["org_type"][0] == "银行"
    assert out["pricing_currency"][0] == "HKD"
    assert out["lot_size"][0] == 1000


# ---------------------------------------------------------------------------
# align_hk_to_ashare: 两市场共用一个页面
# ---------------------------------------------------------------------------
_ASHARE_METRICS = pl.DataFrame({
    "symbol": ["600519.SH"], "period_end": ["2026-06-30"], "announce_date": ["2026-08-01"],
    "eps_basic": [1.0], "cash_ratio": [2.5], "quick_ratio": [2.4],
})


def test_align_fills_missing_ashare_columns():
    hk = fs.hk_financials_canonical(_wide_frame(), "metrics")
    out = fs.align_hk_to_ashare(hk, _ASHARE_METRICS)
    assert set(_ASHARE_METRICS.columns) <= set(out.columns)
    assert out.height == hk.height


def test_align_uses_reference_dtype_not_null_type():
    """🔴 补的列必须用**参照帧的 dtype**, 不能是 ``Null`` 类型 ——
    否则 ``pl.concat`` 两个市场的帧会因类型不匹配报错。"""
    hk = fs.hk_financials_canonical(_wide_frame(), "metrics")
    out = fs.align_hk_to_ashare(hk, _ASHARE_METRICS)
    for col in ("cash_ratio", "quick_ratio", "announce_date"):
        assert out.schema[col] == _ASHARE_METRICS.schema[col], col


def test_align_puts_hk_only_columns_last():
    hk = fs.hk_financials_canonical(_wide_frame(), "metrics")
    out = fs.align_hk_to_ashare(hk, _ASHARE_METRICS)
    head = list(out.columns)[: len(_ASHARE_METRICS.columns)]
    assert head == list(_ASHARE_METRICS.columns)
    assert "pe_ttm" in list(out.columns)[len(_ASHARE_METRICS.columns):]


def test_align_result_concatenates_with_ashare():
    """共用页面的硬要求: 两市场的帧能直接纵向拼接。"""
    hk = fs.align_hk_to_ashare(
        fs.hk_financials_canonical(_wide_frame(), "metrics"), _ASHARE_METRICS
    )
    both = pl.concat([_ASHARE_METRICS, hk.select(list(_ASHARE_METRICS.columns))], how="vertical")
    assert both.height == 2
    assert set(both["symbol"].to_list()) == {"600519.SH", "00700.HK"}


def test_align_empty_reference_returns_input_unchanged():
    """A 股财务未同步时无从得知目标列集, **原样返回**而不是猜一份硬编码列名。"""
    hk = fs.hk_financials_canonical(_wide_frame(), "metrics")
    out = fs.align_hk_to_ashare(hk, pl.DataFrame())
    assert list(out.columns) == list(hk.columns)


def test_align_is_noop_when_columns_already_present():
    hk = fs.hk_financials_canonical(_wide_frame(), "metrics")
    ref = hk.select(["symbol", "period_end"])
    out = fs.align_hk_to_ashare(hk, ref)
    assert set(hk.columns) == set(out.columns)


# ---------------------------------------------------------------------------
# hk_latest_financials
# ---------------------------------------------------------------------------
def test_latest_financials_returns_all_four_tables(tmp_path):
    _seed_wide(tmp_path, [_mi("00700"), _mi("09988", SECURITY_NAME_ABBR="阿里巴巴-W")])
    got = fs.hk_latest_financials(tmp_path, "00700.HK")
    assert set(got) == {"metrics", "income", "balance_sheet", "cash_flow"}
    assert got["metrics"].height == 1
    assert got["metrics"]["symbol"][0] == "00700.HK"


def test_latest_financials_filters_by_symbol(tmp_path):
    _seed_wide(tmp_path, [_mi("00700"), _mi("09988", SECURITY_NAME_ABBR="阿里巴巴-W")])
    got = fs.hk_latest_financials(tmp_path, "09988.HK")
    assert got["metrics"]["symbol"].to_list() == ["09988.HK"]


def test_latest_financials_sorted_desc_and_limited(tmp_path):
    _seed_wide(tmp_path, [_mi(report_date=d) for d in
                          ("2023-12-31", "2024-12-31", "2025-12-31", "2026-06-30")])
    got = fs.hk_latest_financials(tmp_path, "00700.HK", limit=2)
    assert got["metrics"]["period_end"].to_list() == ["2026-06-30", "2025-12-31"]


def test_latest_financials_empty_store(tmp_path):
    got = fs.hk_latest_financials(tmp_path, "00700.HK")
    assert all(f.is_empty() for f in got.values())
    assert all(len(f.columns) > 0 for f in got.values())  # 仍带 schema


def test_latest_financials_accepts_bare_code(tmp_path):
    _seed_wide(tmp_path, [_mi("00700")])
    assert fs.hk_latest_financials(tmp_path, "00700")["metrics"].height == 1


def test_latest_financials_unknown_symbol(tmp_path):
    _seed_wide(tmp_path, [_mi("00700")])
    got = fs.hk_latest_financials(tmp_path, "01234.HK")
    assert got["metrics"].is_empty()
