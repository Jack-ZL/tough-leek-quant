"""港股恒生行业估值温度计 (PR6): 聚合口径、覆盖率分母、fail-closed 与 PE 缺源明示。

本文件钉住的是**统计口径**而不是算法细节 —— 温度计的每一个数字都会被当成
「哪个行业是价值洼地」的依据, 口径错一位就是整张表错。三组重点:

* 🔴 **中位数只在派息者上算**。实测全市场派息覆盖率仅 36.8%, 若按全体成分统计
  (不派息计 0), 31 个行业里 15 个以上的中位数恰好是 ``0.0``, 排序后第一屏全是
  并列的零 —— 一张分不出高低的温度计。故 ``coverage_pct`` 必须与中位数并列下发,
  两个数一起看才成立。这条设计决策由
  :func:`test_median_is_over_payers_only_not_all_members` 钉住。
* 🔴 **算不出来 ≠ 0**。无收盘价 (未同步/长期停牌) 与派息金额未能解析 (外币未
  折算/纯送股/权证) 两种成因都留 null 并计入 ``excluded_count``, 绝不进统计。
  把「派了但读不出多少」说成「没派」是看不出来的伪造 (§13)。
* 🔴 **PE 缺源必须明示**。``pe_available: false`` + 理由文本一起下发, 而不是让
  前端留一个空列 —— 空列会被读成「这个行业 PE 为空」, 而真相是「我们没有源」。

网络一律不打: 服务层复用分红日历的取数打桩点 (``svc.fetch_hk_dividend_calendar``),
API 层走最小 FastAPI app。
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
from app.plugins.eastmoney.client import EastmoneyError
from app.services import hk_dividend_calendar as cal_svc
from app.services import hk_sector_valuation as svc
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository, daily_dirname

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

#: 固定窗口末日, 让所有断言与「今天」解耦。``days=365`` ⇒ 窗口
#: ``[2025-11-23, 2026-11-22]``。
END = dt.date(2026, 11, 22)
DAYS = 365
WINDOW_START = END - dt.timedelta(days=DAYS - 1)

#: 日K分区日期 (在窗口内, 且足够新以落进收盘价回看的 10 个分区)。
PRICE_DAY = dt.date(2026, 11, 20)


def _div(symbol: str, ex: str, plan: str | None = "每股派港币1.0元") -> dict:
    """一条上游分红行 (字段名与类型照抄真实返回)。

    ``plan=None`` 模拟「源数据没给港币金额」(外币未折算 / 分派股份 / 权证):
    解析器会保留该行但 ``dividend_hkd`` 为 null, 这正是温度计必须排除而非计 0
    的那一类。
    """
    return {
        "SECUCODE": symbol,
        "SECURITY_CODE": symbol.split(".")[0],
        "EX_DIVIDEND_DATE": ex,
        "DIVIDEND_DATE": "2026-12-28",
        "NOTICE_DATE": "2026-08-28 00:00:00",
        "YEAR": "2026",
        "REPORT_TYPE": "中期分配",
        "PLAN_EXPLAIN": plan,
        "IS_BFP": "0",
    }


def _inst(
    symbol: str,
    name: str,
    industry: str | None,
    *,
    cny: bool = False,
) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "industry": industry,
        "is_cny_counter": cny,
    }


def _seed_instruments(data_dir: Path, rows: list[dict]) -> None:
    path = data_dir / HK_INSTRUMENT_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), path)


def _seed_closes(data_dir: Path, prices: dict[str, float], day: dt.date = PRICE_DAY) -> None:
    """写一个 ``kline_hk_daily/date=<day>/part.parquet`` 分区 (不复权原始价)。"""
    dirname = daily_dirname("stock", market=Market.HK)
    path = data_dir / dirname / f"date={day.isoformat()}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"symbol": s, "close": float(c)} for s, c in prices.items()]
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
def patch_cal(monkeypatch):
    """打桩分红日历的上游取数, 并记录服务层传下去的窗口。"""
    seen: dict = {}

    def _apply(rows: list[dict] | Exception):
        def _fake(client, *, start, end, **kw):
            seen["start"] = start
            seen["end"] = end
            if isinstance(rows, Exception):
                raise rows
            return list(rows)

        monkeypatch.setattr(cal_svc, "fetch_hk_dividend_calendar", _fake)

    return _apply, seen


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """最小 FastAPI app + 不打网络的取数打桩。"""
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None

    class _Stub:
        def close(self):
            return None

    monkeypatch.setattr(cal_svc, "EastmoneyClient", _Stub)
    return TestClient(app), repo, tmp_path, monkeypatch


def _row(df: pl.DataFrame, industry: str) -> dict:
    hit = df.filter(pl.col("industry") == industry)
    assert hit.height == 1, f"期望行业 {industry} 恰好一行, 实得 {hit.height}"
    return hit.to_dicts()[0]


# ---------------------------------------------------------------------------
# 契约: 列集与排序
# ---------------------------------------------------------------------------


def test_output_schema_matches_cols(repo, tmp_path, patch_cal):
    """输出列**逐字等于** ``HK_SECTOR_VALUATION_COLS`` (API 层直接 to_dicts)。"""
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01")])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    assert tuple(out.industries.columns) == svc.HK_SECTOR_VALUATION_COLS


def test_sorted_by_median_desc_with_nulls_last(repo, tmp_path, patch_cal):
    """按中位数降序 (方案 §10「按股息率降序」); 无派息者的行业排最后且中位数为 null。"""
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-06-01", "每股派港币5.0元"),  # 综合企业 10%
            _div("00002.HK", "2026-06-01", "每股派港币1.0元"),  # 银行 2%
        ]
    )
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "长和", "综合企业"),
            _inst("00002.HK", "中银香港", "银行"),
            _inst("00003.HK", "某新上市", "半导体"),  # 窗口内不派息
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "00002.HK": 50.0, "00003.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    order = out.industries["industry"].to_list()
    assert order == ["综合企业", "银行", "半导体"]
    med = out.industries["median_yield_pct"].to_list()
    assert med[0] == pytest.approx(10.0)
    assert med[1] == pytest.approx(2.0)
    # 🔴 无派息者 → null 而不是 0: 0 会被读成「查证过, 这个行业股息率为零」
    assert med[2] is None
    assert out.industries["payer_count"].to_list() == [1, 1, 0]


# ---------------------------------------------------------------------------
# 🔴 核心口径: 中位数只在派息者上算
# ---------------------------------------------------------------------------


def test_median_is_over_payers_only_not_all_members(repo, tmp_path, patch_cal):
    """🔴 本模块最重要的口径断言。

    4 只成分里只有 1 只派息 (8%), 其余 3 只确无派息。两种口径给出完全不同的答案:

    * 派息者中位数 = **8.0** ← 本模块采用
    * 全体成分中位数 (不派息计 0) = **0.0** ← 退化, 会让半数行业并列垫底

    实测全市场派息覆盖率 36.8%, 31 个行业里 15 个以上的全体中位数恰好是 0.0,
    排序后第一屏全是并列的零。故中位数只在派息者上算, 并由 ``coverage_pct``
    并列交代「有多少成分真的派息」。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币4.0元")])
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "派息者", "地产"),
            _inst("00002.HK", "不派息甲", "地产"),
            _inst("00003.HK", "不派息乙", "地产"),
            _inst("00004.HK", "不派息丙", "地产"),
        ],
    )
    _seed_closes(
        tmp_path,
        {"00001.HK": 50.0, "00002.HK": 50.0, "00003.HK": 50.0, "00004.HK": 50.0},
    )

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "地产")
    assert row["median_yield_pct"] == pytest.approx(8.0)
    assert row["median_yield_pct"] != 0.0
    assert row["member_count"] == 4
    assert row["priced_count"] == 4
    assert row["payer_count"] == 1
    assert row["coverage_pct"] == pytest.approx(25.0)


def test_coverage_pct_denominator_is_priced_not_member(repo, tmp_path, patch_cal):
    """覆盖率分母是**能算出股息率的成分** (priced), 不是维表成分总数 (member)。

    用 member 当分母会把「未同步/长期停牌」混进「不派息」, 让一个只是行情缺失的
    行业看起来派息文化更差。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "派息者", "银行"),
            _inst("00002.HK", "有价不派息", "银行"),
            _inst("00003.HK", "无收盘价", "银行"),  # 不在日K分区里
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "00002.HK": 50.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "银行")
    assert row["member_count"] == 3
    assert row["priced_count"] == 2
    assert row["excluded_count"] == 1
    assert row["coverage_pct"] == pytest.approx(50.0)  # 1/2, 不是 1/3


def test_median_ignores_penny_stock_outlier_that_wrecks_mean(repo, tmp_path, patch_cal):
    """⚠️ 均值受仙股一次性派息扭曲, 中位数不受影响 —— 故中位数才是排序键。

    实测专业零售 均值 23.6% vs 中位数 8.1% (有标的股价 0.075 而全年派息 0.266,
    单只贡献 354%)。前端不得把均值当头条数字。
    """
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-06-01", "每股派港币0.266元"),  # 股价 0.075 → 354%
            _div("00002.HK", "2026-06-01", "每股派港币0.4元"),  # 股价 50 → 0.8%
            _div("00003.HK", "2026-06-01", "每股派港币0.5元"),  # 股价 50 → 1.0%
        ]
    )
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "仙股", "专业零售"),
            _inst("00002.HK", "正常甲", "专业零售"),
            _inst("00003.HK", "正常乙", "专业零售"),
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 0.075, "00002.HK": 50.0, "00003.HK": 50.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "专业零售")
    assert row["median_yield_pct"] == pytest.approx(1.0)
    assert row["max_yield_pct"] == pytest.approx(354.666667, rel=1e-4)
    assert row["avg_yield_pct"] > 100.0  # 均值被单只仙股拉飞
    assert row["top_symbol"] == "00001.HK"  # 龙头派息者如实暴露, 不藏异常值


# ---------------------------------------------------------------------------
# 🔴 TTM 聚合
# ---------------------------------------------------------------------------


def test_ttm_sums_interim_and_final_dividends(repo, tmp_path, patch_cal):
    """同一标的窗口内的中期息 + 末期息**相加**, 这才是 TTM 口径。"""
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-03-01", "每股派港币1.0元"),
            _div("00001.HK", "2026-09-01", "每股派港币1.5元"),
        ]
    )
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "综合企业")
    assert row["median_yield_pct"] == pytest.approx(5.0)  # (1.0+1.5)/50*100
    assert row["payer_count"] == 1  # 两条事件仍是一只标的


def test_window_is_passed_through_to_calendar(repo, tmp_path, patch_cal):
    """服务层必须把 ``[end-days+1, end]`` 如实传给日历 (窗口错则 TTM 全错)。"""
    apply, seen = patch_cal
    apply([])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    svc.hk_sector_valuation(repo, days=DAYS, end=END)
    assert seen["start"] == WINDOW_START
    assert seen["end"] == END


def test_yield_is_percentage_value_not_fraction(repo, tmp_path, patch_cal):
    """🔴 单位 (CONTRIBUTING §3.1): ``5.0`` 表示 ``5.0%``, 不是 ``0.05``。

    换算在服务层一次做完, 前端不得再乘 100, 也禁止「小于 1 就乘 100」的启发式。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币2.5元")])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "综合企业")
    assert row["median_yield_pct"] == pytest.approx(5.0)
    assert row["coverage_pct"] == pytest.approx(100.0)


def test_yield_denominator_is_raw_close(repo, tmp_path, patch_cal):
    """分母是**不复权**收盘价: 用前复权价会把有分红史的标的股息率系统性算高 (§3.2)。"""
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币1.0元")])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 40.0})  # 日K分区里存的就是原始价

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "综合企业")
    assert row["median_yield_pct"] == pytest.approx(2.5)  # 1.0/40*100


# ---------------------------------------------------------------------------
# 🔴 fail closed: 算不出来 ≠ 0
# ---------------------------------------------------------------------------


def test_unparsed_dividend_amount_is_excluded_not_zero(repo, tmp_path, patch_cal):
    """🔴 派了但金额读不出 (外币未折算/纯送股/权证) → null + ``excluded_count``,
    **绝不能**当成 0%。

    把「派了但读不出多少」说成「没派」是看不出来的伪造 (§13): 页面上显示成一个
    合法的零, 用户无从察觉。
    """
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-06-01", plan=None),  # 金额无法解析
            _div("00002.HK", "2026-06-01", "每股派港币5.0元"),
        ]
    )
    _seed_instruments(
        tmp_path,
        [_inst("00001.HK", "外币派息", "银行"), _inst("00002.HK", "正常派息", "银行")],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "00002.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    row = _row(out.industries, "银行")
    assert row["member_count"] == 2
    assert row["priced_count"] == 1  # 未解析那只被排除
    assert row["excluded_count"] == 1
    assert row["payer_count"] == 1
    assert row["median_yield_pct"] == pytest.approx(10.0)  # 只由可算的那只决定
    assert row["coverage_pct"] == pytest.approx(100.0)  # 分母是 priced=1
    assert out.meta["excluded_total"] == 1


def test_partially_unparsed_symbol_is_excluded_entirely(repo, tmp_path, patch_cal):
    """同一标的**部分**事件金额读不出 → 整只排除。

    只加可解析的那部分会得到一个偏低的 TTM, 而偏低看起来完全正常 —— 比 null
    危险得多。
    """
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-03-01", "每股派港币1.0元"),
            _div("00001.HK", "2026-09-01", plan=None),
        ]
    )
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    row = _row(out.industries, "综合企业")
    assert row["priced_count"] == 0
    assert row["excluded_count"] == 1
    assert row["median_yield_pct"] is None


def test_missing_close_is_excluded_not_zero(repo, tmp_path, patch_cal):
    """无收盘价 (未同步/长期停牌) → 排除, 不是 0%。"""
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [_inst("00001.HK", "有价", "银行"), _inst("00002.HK", "长期停牌", "银行")],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0})  # 00002 不在分区里

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "银行")
    assert row["member_count"] == 2
    assert row["priced_count"] == 1
    assert row["excluded_count"] == 1


def test_zero_or_negative_close_is_excluded(repo, tmp_path, patch_cal):
    """收盘价为 0/负 → 排除。0 当分母会炸, 负价没有金融意义。"""
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-06-01", "每股派港币5.0元"),
            _div("00002.HK", "2026-06-01", "每股派港币5.0元"),
        ]
    )
    _seed_instruments(
        tmp_path,
        [_inst("00001.HK", "零价", "银行"), _inst("00002.HK", "负价", "银行")],
    )
    _seed_closes(tmp_path, {"00001.HK": 0.0, "00002.HK": -1.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "银行")
    assert row["priced_count"] == 0
    assert row["excluded_count"] == 2
    assert row["median_yield_pct"] is None
    assert row["coverage_pct"] is None  # 分母为 0 → null, 不是 0%


def test_confirmed_non_payer_is_zero_not_null(repo, tmp_path, patch_cal):
    """对照组: 有收盘价 + 窗口内**确无**派息事件 → ``0.0`` (查证过的事实)。

    与上面几条 null 的情况必须区分开: 一个是「没有」, 一个是「不知道」。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [_inst("00001.HK", "派息者", "银行"), _inst("00002.HK", "不派息", "银行")],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "00002.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    row = _row(out.industries, "银行")
    assert row["priced_count"] == 2  # 不派息那只**进了**分母
    assert row["payer_count"] == 1
    assert row["coverage_pct"] == pytest.approx(50.0)
    # 但中位数只在派息者上算, 所以不是 (10+0)/2
    assert row["median_yield_pct"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 🔴 成分口径: 全市场、去重、不造假行业
# ---------------------------------------------------------------------------


def test_cny_counter_excluded_from_members(repo, tmp_path, patch_cal):
    """🔴 人民币柜台不计入成分。

    双柜台公司在维表里是两行, 不过滤会让它在行业里占两个席位、派息被重复计入
    (实测 2862 行里 24 行是人民币柜台)。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "长和", "综合企业"),
            _inst("80001.HK", "长和-人民币柜台", "综合企业", cny=True),
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "80001.HK": 45.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    row = _row(out.industries, "综合企业")
    assert row["member_count"] == 1
    assert out.meta["member_total"] == 1


def test_blank_industry_not_grouped_into_unknown(repo, tmp_path, patch_cal):
    """🔴 行业为空的成分**不归进「未知」组**, 只计入 ``unclassified_count``。

    造一个「未知」桶会凭空生出一个成分数排名靠前的假行业 (前端热力图已就同一条
    规则钉过测试)。维表 2838 只里有 7 只 industry 为空 (债券残留)。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "长和", "综合企业"),
            _inst("04286.HK", "政府基债二七一二", None),
            _inst("00002.HK", "空白行业", "   "),
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "04286.HK": 100.0, "00002.HK": 10.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    names = out.industries["industry"].to_list()
    assert names == ["综合企业"]
    assert not any(n in ("未知", "其他", "") for n in names)
    assert out.meta["unclassified_count"] == 2


def test_member_counts_are_auditable(repo, tmp_path, patch_cal):
    """🔴 全市场口径**可审计**: Σ member_count + unclassified == member_total。

    方案 §10 特别警告过 QuantMind 早期误用 141 只港股通子集而返工, 故把分母如实
    交出, 让「是不是全市场」这件事能被一行断言核对。
    """
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "长和", "综合企业"),
            _inst("00002.HK", "中银香港", "银行"),
            _inst("00003.HK", "汇丰", "银行"),
            _inst("04286.HK", "债券残留", None),
            _inst("80001.HK", "人民币柜台", "综合企业", cny=True),
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "00002.HK": 50.0, "00003.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    total = out.industries["member_count"].sum()
    assert total + out.meta["unclassified_count"] == out.meta["member_total"] == 4
    assert out.meta["priced_total"] == 3
    assert out.meta["payer_total"] == 1
    assert out.meta["excluded_total"] == 4 - 0 - 3 - 1  # member - unclassified - priced - 0


def test_duplicate_instrument_row_does_not_double_count(repo, tmp_path, patch_cal):
    """维表意外重复一行时不得让成分数翻倍 (join 重复键会**静默**放大)。"""
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(
        tmp_path,
        [_inst("00001.HK", "长和", "综合企业"), _inst("00001.HK", "长和", "综合企业")],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    assert _row(out.industries, "综合企业")["member_count"] == 1
    assert out.meta["member_total"] == 1


def test_top_payer_is_the_max_yield_member(repo, tmp_path, patch_cal):
    """``top_*`` 三列必须是该行业里股息率最高的那只 (龙头派息者)。"""
    apply, _ = patch_cal
    apply(
        [
            _div("00001.HK", "2026-06-01", "每股派港币1.0元"),  # 2%
            _div("00002.HK", "2026-06-01", "每股派港币6.0元"),  # 12% ← 最高
            _div("00003.HK", "2026-06-01", "每股派港币2.5元"),  # 5%
        ]
    )
    _seed_instruments(
        tmp_path,
        [
            _inst("00001.HK", "甲", "银行"),
            _inst("00002.HK", "乙", "银行"),
            _inst("00003.HK", "丙", "银行"),
        ],
    )
    _seed_closes(tmp_path, {"00001.HK": 50.0, "00002.HK": 50.0, "00003.HK": 50.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "银行")
    assert row["top_symbol"] == "00002.HK"
    assert row["top_name"] == "乙"
    assert row["top_yield_pct"] == pytest.approx(12.0)
    assert row["max_yield_pct"] == pytest.approx(12.0)


def test_top_payer_null_when_industry_has_no_payer(repo, tmp_path, patch_cal):
    apply, _ = patch_cal
    apply([])
    _seed_instruments(tmp_path, [_inst("00001.HK", "甲", "半导体")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    row = _row(svc.hk_sector_valuation(repo, days=DAYS, end=END).industries, "半导体")
    assert row["top_symbol"] is None
    assert row["top_yield_pct"] is None
    assert row["payer_count"] == 0
    assert row["coverage_pct"] == pytest.approx(0.0)  # 分母非 0 → 0% 是事实


# ---------------------------------------------------------------------------
# 缺数据 / 上游失败
# ---------------------------------------------------------------------------


def test_empty_window_returns_empty_frame_with_full_schema(repo, tmp_path, patch_cal):
    """窗口内无人派息 → 空统计**而不是抛错**; 但成分表仍要有行 (覆盖率 0)。"""
    apply, _ = patch_cal
    apply([])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    assert out.industries.height == 1
    assert tuple(out.industries.columns) == svc.HK_SECTOR_VALUATION_COLS
    assert out.meta["event_count"] == 0
    assert out.meta["payer_total"] == 0


def test_missing_instruments_yields_empty_table(repo, tmp_path, patch_cal):
    """维表未同步 → 空表 + 全零 meta, **不抛错** (与日历同处理)。"""
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01")])

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    assert out.industries.is_empty()
    assert tuple(out.industries.columns) == svc.HK_SECTOR_VALUATION_COLS
    assert out.meta["member_total"] == 0


def test_missing_daily_partitions_yields_all_excluded(repo, tmp_path, patch_cal):
    """日K未同步 → 有成分但全部算不出股息率 (excluded), 而不是全部 0%。"""
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    out = svc.hk_sector_valuation(repo, days=DAYS, end=END)
    row = _row(out.industries, "综合企业")
    assert row["member_count"] == 1
    assert row["priced_count"] == 0
    assert row["excluded_count"] == 1
    assert row["median_yield_pct"] is None


def test_upstream_error_propagates(repo, tmp_path, patch_cal):
    """上游失败原样抛, **不得**降级成空表 (那会把「拉不到」伪装成「没人派息」)。"""
    apply, _ = patch_cal
    apply(EastmoneyError("报表不可用"))
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    with pytest.raises(EastmoneyError):
        svc.hk_sector_valuation(repo, days=DAYS, end=END)


def test_default_days_is_ttm():
    assert svc.HK_SECTOR_VALUATION_DEFAULT_DAYS == 365


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_endpoint_shape_and_meta(api, patch_cal):
    client, _repo, tmp_path, _mp = api
    apply, _ = patch_cal
    apply([_div("00001.HK", "2026-06-01", "每股派港币5.0元")])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])
    _seed_closes(tmp_path, {"00001.HK": 50.0})

    r = client.get(f"/api/hk/sector-valuation?days={DAYS}&end={END.isoformat()}")
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "HK"
    assert body["currency"] == "HKD"
    assert body["source"] == "eastmoney_datacenter"
    assert body["days"] == DAYS
    assert body["window_start"] == WINDOW_START.isoformat()
    assert body["window_end"] == END.isoformat()
    assert body["total"] == 1
    item = body["items"][0]
    assert list(item) == list(svc.HK_SECTOR_VALUATION_COLS)
    assert item["median_yield_pct"] == pytest.approx(10.0)


def test_endpoint_declares_pe_unavailable(api, patch_cal):
    """🔴 PE 缺源必须**明示**下发, 而不是让前端留一个空列。

    空列会被读成「这个行业 PE 为空」, 而真相是「我们没有源」—— 两者对用户是
    完全不同的信息 (§4 宁可明说没有)。
    """
    client, _repo, tmp_path, _mp = api
    apply, _ = patch_cal
    apply([])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    body = client.get(f"/api/hk/sector-valuation?end={END.isoformat()}").json()
    assert body["pe_available"] is False
    reason = body["pe_unavailable_reason"]
    assert isinstance(reason, str) and len(reason) > 20
    # 理由要说清是「没有源」而不是「这个行业没有值」
    assert "无可用源" in reason
    # 🔴 响应里绝不能出现任何 PE 字段, 否则前端可能拿 null 当 0 渲染
    assert not any("pe" in k.lower() and k != "pe_available" and "unavailable" not in k
                   for k in body)
    for item in body["items"]:
        assert not any("pe" in k.lower() for k in item)


def test_endpoint_default_window_is_ttm(api, patch_cal):
    client, _repo, tmp_path, _mp = api
    apply, seen = patch_cal
    apply([])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    body = client.get("/api/hk/sector-valuation").json()
    assert body["days"] == 365
    assert (seen["end"] - seen["start"]).days == 364  # 含端点共 365 天


def test_endpoint_rejects_out_of_range_days(api, patch_cal):
    client, _repo, _tmp, _mp = api
    apply, _ = patch_cal
    apply([])
    assert client.get("/api/hk/sector-valuation?days=0").status_code == 422
    assert client.get("/api/hk/sector-valuation?days=9999").status_code == 422


def test_endpoint_returns_502_on_upstream_error(api, patch_cal):
    client, _repo, tmp_path, _mp = api
    apply, _ = patch_cal
    apply(EastmoneyError("报表不可用"))
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    r = client.get(f"/api/hk/sector-valuation?end={END.isoformat()}")
    assert r.status_code == 502
    assert "不可用" in r.json()["detail"]


def test_endpoint_empty_window_is_200(api, patch_cal):
    """「这段时间没人派息」是合法答案 → 200 + 空 items, 不是 502/404。"""
    client, _repo, tmp_path, _mp = api
    apply, _ = patch_cal
    apply([])
    _seed_instruments(tmp_path, [_inst("00001.HK", "长和", "综合企业")])

    r = client.get(f"/api/hk/sector-valuation?end={END.isoformat()}")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1  # 行业行仍在, 只是覆盖率 0
    assert body["items"][0]["payer_count"] == 0
    assert body["payer_total"] == 0
