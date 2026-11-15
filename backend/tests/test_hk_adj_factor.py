"""港股复权因子链路: 分红方案解析 → 因子构建 → 落库 → enriched 复权真正生效。

CONTRIBUTING:708 要求复权类逻辑用**固定样本数值断言**, 故本文件的因子值全是手算
可复核的字面量, 不用「跑一遍再快照」的自证式断言。

网络一律用假 client 挡掉。锚点样本取自实测: 汇丰 (00005.HK) 2026-11-05 除净
``每股派美元0.1元(相当于港币0.784812元(计算值))``, 其 0.784812 与腾讯 hkfqkline
前复权序列在同一日独立测得的加性偏移**逐位吻合** —— 这是本链路口径正确的证据。

⚠️ 腾讯 ``hkfqkline`` 的前复权是**等差**的 (``qfq = raw − Σ未来每股港币分红``),
比值差法在汇丰身上产出 207 个假事件 (真事件约 3 个), 信号被噪声完全淹没, 故**不可**
用作因子源。详见 ``hk_dividend`` 模块 docstring 与 ``docs/hk-market-plan.md`` §3.3。
"""

from __future__ import annotations

import datetime as dt
import logging
import re

import polars as pl
import pytest

from app.data_providers.normalizer import ADJ_FACTOR_COLS
from app.market import Market
from app.plugins.eastmoney import hk_dividend as hd
from app.plugins.eastmoney.client import EastmoneyError
from app.services import kline_sync_hk as sync_hk
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.services.kline_sync import ADJ_DETAIL_COLS
from app.tickflow.repository import DataStore, KlineRepository

#: 实测方案文本 → (每股港币现金, 每股送转比例)。取自 32 只跨行业港股 1159 条真实
#: 事件里出现频次最高的骨架, 覆盖全部币种与简繁体变体。
REAL_PLANS = [
    ("每股派港币5.3元", (5.3, 0.0)),
    ("每股派港币0.5元(可选择以股代息)", (0.5, 0.0)),
    ("每股派人民币0.5元(相当于港币0.55元)", (0.55, 0.0)),
    ("每股派人民币0.16元(相当于计算值港币0.182794元)", (0.182794, 0.0)),
    ("每股派人民币0.15元(相当于港币股0.1834元)", (0.1834, 0.0)),  # 源数据笔误「港币股」
    ("每股派人民币0.2元(相等于港币0.2065元)", (0.2065, 0.0)),
    ("每股派人民币0.25元(相等於每股0.3149港元)", (0.3149, 0.0)),  # 繁体「於」+ 金额在币种前
    ("每股派美元0.1元(相当于港币0.784812元(计算值))", (0.784812, 0.0)),
    ("每股派港币0.23元,每20股派送1股", (0.23, 0.05)),
    ("每10股转1股", (0.0, 0.1)),
    ("特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元)", (18.13, 0.0)),
]

#: 解析不出**港币数值**的方案 → None (warn + 跳过, 绝不当 0 处理)。
UNPARSEABLE_PLANS = [
    "每股派美元0.1元(可选择以股代息)",   # 美元无港币等值
    "每股派人民币0.05元",                # 人民币无港币等值
    "每12股派送1股红利认股权证",          # 权证非送股, 且无价值数据
    "每12股派送1份红利认股权证",          # 量词是「份」
    "特殊说明:每21股腾讯股份分派1股京东集团A类普通股股份",  # spin-off 无等值
    "每8.7股股份分派1股碧桂园服务控股有限公司股份",          # 非整数股比
    "未派发或宣派股息",                   # IS_BFP=1 占位行 (已被过滤, 双保险)
    "",
    None,
]


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------
class _FakeDivClient:
    """按 SECUCODE 返回预置分红事件; 记录调用参数; 可指定某些标的抛 EastmoneyError。"""

    def __init__(self, events_by_secucode=None, *, fail=()):
        self._events = dict(events_by_secucode or {})
        self._fail = set(fail)
        self.calls: list[dict] = []
        self.closed = False

    def datacenter_report(self, report_name, flt=None, columns="ALL", page_size=50,
                          page_number=1, sort_columns=None, sort_types=None):
        self.calls.append({
            "report": report_name, "flt": flt, "page_size": page_size,
            "page_number": page_number, "sort_columns": sort_columns,
            "sort_types": sort_types,
        })
        m = re.search(r'SECUCODE="([^"]+)"', flt or "")
        secucode = m.group(1) if m else None
        if secucode in self._fail:
            raise EastmoneyError(f"模拟取数失败: {secucode}")
        return list(self._events.get(secucode, []))

    def close(self):
        self.closed = True


def _ev(ex_date: str, plan: str) -> dict:
    """一条 F10 分红事件行 (只含解析真正用到的两个字段)。"""
    return {"EX_DIVIDEND_DATE": f"{ex_date} 00:00:00", "PLAN_EXPLAIN": plan}


def _closes(*pairs: tuple[dt.date, float | None]) -> dict[dt.date, float | None]:
    return dict(pairs)


def _seed_instruments(data_dir, symbols: list[str]) -> None:
    out = data_dir / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame([{
        "symbol": s, "name": s, "total_shares": 200_000_000, "float_shares": 200_000_000,
        "limit_up": None, "limit_down": None,   # 港股无涨跌停 → 哨兵列恒 None
        "listing_date": None, "is_cny_counter": False,
    } for s in symbols]), out)


def _seed_daily(repo, symbol: str, *, days: int = 30,
                start: dt.date = dt.date(2026, 9, 1)) -> None:
    """写入 days 个交易日的港股原始日K。第 i 天 close = 101 + i。

    volume/amount 均非零、open/high 均非零 —— 否则会被 filter_halt_days 当停牌日丢掉。
    """
    rows = []
    for i in range(days):
        px = 100.0 + i
        rows.append({
            "symbol": symbol, "date": start + dt.timedelta(days=i),
            "open": px, "high": px + 2.0, "low": px - 2.0, "close": px + 1.0,
            "volume": 1_000_000.0, "amount": 1_000_000.0 * px, "quote_ts": 0,
        })
    repo.append_hk_daily(pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date)))


def _seed_factor_store(data_dir, rows: list[dict] | None = None) -> None:
    """写因子库。``rows=None`` → **空库** (已同步、无除权事件), 不是「未同步」。"""
    out = sync_hk.hk_adj_factor_path(data_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    schema = {
        "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
        "dividend": pl.Float64, "bonus": pl.Float64, "allot": pl.Float64,
        "allot_price": pl.Float64, "prev_close": pl.Float64,
    }
    df = (pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
          if rows else pl.DataFrame(schema=schema))
    atomic_write_parquet(df, out)


def _read_factor_store(data_dir) -> pl.DataFrame:
    return pl.read_parquet(sync_hk.hk_adj_factor_path(data_dir))


def _read_enriched(data_dir) -> pl.DataFrame:
    return pl.read_parquet(data_dir / "kline_hk_enriched" / "**" / "*.parquet").sort("date")


# ---------------------------------------------------------------------------
# ① 方案文本解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("plan", "expected"), REAL_PLANS)
def test_parse_real_plan_texts(plan, expected):
    """实测方案文本 → 精确的 (港币现金, 送转比例)。"""
    got = hd.parse_hk_dividend_plan(plan)
    assert got is not None, f"应能解析: {plan!r}"
    assert (got.dividend_hkd, got.bonus_ratio) == pytest.approx(expected), plan


@pytest.mark.parametrize("plan", UNPARSEABLE_PLANS)
def test_parse_returns_none_when_no_hkd_value(plan):
    """拿不到港币数值 → None。绝不能当 0: 那会静默产出 ex_factor=1.0 的假因子。"""
    assert hd.parse_hk_dividend_plan(plan) is None


def test_parse_prefers_hkd_over_rmb_in_same_text():
    """🔴 陷阱: 同一句里既有港币又有人民币时, 必须取**港币**。

    取错会让因子偏小约 8% (汇率差), 且数值看起来完全正常 —— 典型的「看起来对但错」。
    """
    got = hd.parse_hk_dividend_plan("每股派港币1元(相当于人民币0.9元)")
    assert got is not None
    assert got.dividend_hkd == pytest.approx(1.0), "误取了人民币 0.9"


def test_parse_ignores_fx_rate_prefix():
    """🔴 陷阱: ``汇率人民币A元:港币B元`` 里也有「港币+数字」, 但缺「相当于」前缀,
    不能被当成派息额; 应取后面真正的等值港币。"""
    got = hd.parse_hk_dividend_plan(
        "每股派人民币0.5元(汇率人民币0.5元:港币0.55元)(相当于港币0.55元)"
    )
    assert got is not None
    assert got.dividend_hkd == pytest.approx(0.55)


def test_parse_scrip_dividend_counts_full_cash():
    """「可选择以股代息」按**全额现金**处理: 除净日的价格调整由宣派额决定,
    与股东最终选现金还是选股票无关。"""
    got = hd.parse_hk_dividend_plan("每股派港币0.5元(可选择以股代息)")
    assert got is not None
    assert got.dividend_hkd == pytest.approx(0.5)
    assert got.bonus_ratio == 0.0, "以股代息不是送转股, 不能计 bonus"


def test_parse_cash_and_bonus_together():
    """现金 + 送转同时出现时两者都要拿到, 供 ref=(P−D)/(1+B) 用。"""
    got = hd.parse_hk_dividend_plan("每股派港币0.23元,每20股派送1股")
    assert got is not None
    assert got.dividend_hkd == pytest.approx(0.23)
    assert got.bonus_ratio == pytest.approx(1 / 20)


# ---------------------------------------------------------------------------
# ② 因子构建 (固定样本数值断言)
# ---------------------------------------------------------------------------
def test_factor_matches_measured_hsic_dividend():
    """🔴 锚点: 汇丰 2026-11-05 实测除净, 与腾讯前复权序列独立测得的偏移逐位吻合。

    P = 135.500 (除净日前一交易日收盘), D = 0.784812 港币, B = 0
    ref = (135.500 − 0.784812) / 1 = 134.715188
    ex_factor = 135.500 / 134.715188 = 1.005825712836477
    """
    closes = _closes(
        (dt.date(2026, 11, 3), 133.700),
        (dt.date(2026, 11, 4), 135.500),   # ← 除净日前最后一个交易日
        (dt.date(2026, 11, 5), 136.000),   # 除净日当天, 不该被用作 P
    )
    f = hd.build_hk_adj_factors(
        "00005.HK", [_ev("2026-11-05", "每股派美元0.1元(相当于港币0.784812元(计算值))")], closes
    )
    assert f.height == 1
    assert f["ex_factor"][0] == pytest.approx(1.005825712836477, rel=1e-12)
    assert f["prev_close"][0] == pytest.approx(135.500), "P 取了除净日当天而非前一交易日"
    assert f["dividend"][0] == pytest.approx(0.784812)
    assert f["bonus"][0] == 0.0
    assert f["trade_date"][0] == dt.date(2026, 11, 5)


def test_factor_bonus_only():
    """纯送转: P=100, 每10股转1股 (B=0.1) → ref=100/1.1, ex_factor=1.1 整。"""
    closes = _closes((dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 91.0))
    f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-02", "每10股转1股")], closes)
    assert f["ex_factor"][0] == pytest.approx(1.1, rel=1e-12)
    assert f["dividend"][0] == 0.0
    assert f["bonus"][0] == pytest.approx(0.1)


def test_factor_cash_and_bonus():
    """现金 + 送转: P=100, D=2, B=0.05 → ref=98/1.05, ex_factor=1.05/0.98。"""
    closes = _closes((dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 93.0))
    f = hd.build_hk_adj_factors(
        "00700.HK", [_ev("2026-01-02", "每股派港币2元,每20股派送1股")], closes
    )
    assert f["ex_factor"][0] == pytest.approx(1.0714285714285714, rel=1e-12)


def test_same_ex_date_events_are_aggregated_not_deduped():
    """🔴 同一除净日多条方案 (末期息 + 特别息) 必须**先合并再算 ref**。

    不合并的话 _apply_adj_factor 的 unique(subset=[symbol, trade_date]) 会静默丢掉
    一条 —— 因子偏小, 且没有任何报错。
    P=100, D=1+2=3 → ex_factor = 100/97 = 1.0309278350515463
    (未合并会错成 100/99=1.0101 或 100/98=1.0204)
    """
    closes = _closes((dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 97.0))
    f = hd.build_hk_adj_factors("00700.HK", [
        _ev("2026-01-02", "每股派港币1元"),
        _ev("2026-01-02", "每股派港币2元"),
    ], closes)
    assert f.height == 1, "同日两条方案应合并成一行"
    assert f["ex_factor"][0] == pytest.approx(1.0309278350515463, rel=1e-12)
    assert f["dividend"][0] == pytest.approx(3.0)


def test_multiple_ex_dates_give_one_row_each():
    """不同除净日各自成行, 且 ex_factor 是**单次前后比价而非累计**
    (累计由 _apply_adj_factor 的 cum_prod 负责)。"""
    closes = _closes(
        (dt.date(2026, 1, 1), 100.0),
        (dt.date(2026, 6, 1), 200.0),
        (dt.date(2026, 6, 2), 199.0),
        (dt.date(2026, 12, 1), 300.0),
        (dt.date(2026, 12, 2), 299.0),
    )
    f = hd.build_hk_adj_factors("00700.HK", [
        _ev("2026-12-02", "每股派港币10元"),   # 源数据按公告日倒序, 故意乱序传入
        _ev("2026-06-02", "每股派港币10元"),
    ], closes)
    assert f.height == 2
    assert f["trade_date"].to_list() == [dt.date(2026, 6, 2), dt.date(2026, 12, 2)], "应按日期升序"
    assert f["ex_factor"][0] == pytest.approx(200.0 / 190.0, rel=1e-12)
    assert f["ex_factor"][1] == pytest.approx(300.0 / 290.0, rel=1e-12)


def test_prev_close_falls_back_over_halt_day():
    """除净日前一天停牌 (close=None) → 回退到更早的**有效**交易日定 P, 且不抛
    float(None) 的 TypeError (一只脏数据不该炸掉整批)。"""
    closes = _closes(
        (dt.date(2026, 1, 1), 100.0),
        (dt.date(2026, 1, 2), None),          # 停牌
        (dt.date(2026, 1, 3), 99.0),
    )
    f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-03", "每股派港币1元")], closes)
    assert f.height == 1
    assert f["prev_close"][0] == pytest.approx(100.0), "应回退到 1/1 的有效收盘"
    assert f["ex_factor"][0] == pytest.approx(100.0 / 99.0, rel=1e-12)


def test_event_before_local_history_is_skipped():
    """除净日早于本地全部日K → 定不出 P, 跳过 (不能拿除净日当天的价当 P)。"""
    closes = _closes((dt.date(2026, 6, 1), 100.0), (dt.date(2026, 6, 2), 101.0))
    f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-05", "每股派港币1元")], closes)
    assert f.is_empty()


def test_all_halted_closes_gives_empty_frame():
    """closes 全是 None → 空帧, 不抛异常。"""
    f = hd.build_hk_adj_factors(
        "00700.HK", [_ev("2026-01-03", "每股派港币1元")],
        _closes((dt.date(2026, 1, 1), None), (dt.date(2026, 1, 2), None)),
    )
    assert f.is_empty()


def test_nan_prev_close_is_skipped():
    """🔴 NaN 前收盘必须挡: NaN 穿过所有 > / <= 比较 (恒 False), 产出的 NaN 因子
    经 cum_prod 会污染该除净日之后**整条**价格序列, 比漏一个事件严重得多。"""
    closes = _closes((dt.date(2026, 1, 1), float("nan")), (dt.date(2026, 1, 2), 99.0))
    f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-02", "每股派港币1元")], closes)
    assert f.is_empty(), "NaN 前收盘应被跳过"


def test_absurd_dividend_ratio_is_skipped(caplog):
    """D/P > 0.9 → 跳过并告警。典型成因是把汇率或派息总额误当每股金额。"""
    closes = _closes((dt.date(2026, 1, 1), 10.0), (dt.date(2026, 1, 2), 1.0))
    with caplog.at_level(logging.WARNING, logger="app.plugins.eastmoney.hk_dividend"):
        f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-02", "每股派港币9.5元")], closes)
    assert f.is_empty()
    assert "超出合理上限" in caplog.text


def test_absurd_bonus_ratio_is_skipped(caplog):
    """B > 5.0 (每 1 股送 5 股以上) → 跳过并告警。"""
    closes = _closes((dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 20.0))
    with caplog.at_level(logging.WARNING, logger="app.plugins.eastmoney.hk_dividend"):
        f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-02", "每1股派送6股")], closes)
    assert f.is_empty()
    assert "送转比例" in caplog.text


def test_dividend_exceeding_price_is_skipped():
    """D > P → ref <= 0, 跳过 (不产出负因子)。"""
    closes = _closes((dt.date(2026, 1, 1), 10.0), (dt.date(2026, 1, 2), 1.0))
    f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-02", "每股派港币12元")], closes)
    assert f.is_empty()


def test_unparseable_plan_is_skipped_with_warning(caplog):
    """解析不出的方案 → warn + 跳过该事件, 其余事件照常产出。"""
    closes = _closes(
        (dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 99.0),
        (dt.date(2026, 6, 1), 120.0), (dt.date(2026, 6, 2), 119.0),
    )
    with caplog.at_level(logging.WARNING, logger="app.plugins.eastmoney.hk_dividend"):
        f = hd.build_hk_adj_factors("00700.HK", [
            _ev("2026-01-02", "每12股派送1股红利认股权证"),   # 解析不出
            _ev("2026-06-02", "每股派港币1元"),               # 正常
        ], closes)
    assert f.height == 1, "只该留下能解析的那条"
    assert f["trade_date"][0] == dt.date(2026, 6, 2)
    assert "无法解析" in caplog.text


def test_row_without_ex_date_is_skipped():
    """无除净日 = 尚未实施, 不影响历史价格 → 跳过, 不告警刷屏。"""
    closes = _closes((dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 99.0))
    f = hd.build_hk_adj_factors("00700.HK", [
        {"EX_DIVIDEND_DATE": None, "PLAN_EXPLAIN": "每股派港币1元"},
        {"PLAN_EXPLAIN": "每股派港币1元"},
    ], closes)
    assert f.is_empty()


def test_empty_events_gives_empty_frame():
    """无除权事件 → **空帧** (合法结果), 语义区别于「取数失败」。"""
    closes = _closes((dt.date(2026, 1, 1), 100.0))
    assert hd.build_hk_adj_factors("00700.HK", [], closes).is_empty()
    assert hd.build_hk_adj_factors("00700.HK", [], {}).is_empty()


def test_factor_schema_matches_ashare_contract():
    """输出 schema 必须与 A 股因子库同构, 否则 _merge_adj_factor_store /
    _apply_adj_factor 这两个共用函数会静默错列。"""
    closes = _closes((dt.date(2026, 1, 1), 100.0), (dt.date(2026, 1, 2), 99.0))
    f = hd.build_hk_adj_factors("00700.HK", [_ev("2026-01-02", "每股派港币1元")], closes)
    assert f.columns[:len(ADJ_FACTOR_COLS)] == ADJ_FACTOR_COLS
    assert f.columns[len(ADJ_FACTOR_COLS):] == list(ADJ_DETAIL_COLS)
    # 港股无配股 → 两列恒 null, 但**列必须存在** (diagonal concat 靠列名对齐)
    assert f["allot"].null_count() == f.height
    assert f["allot_price"].null_count() == f.height


# ---------------------------------------------------------------------------
# ③ 因子库读取的三态语义 (fail-closed 的基础)
# ---------------------------------------------------------------------------
def test_load_missing_store_returns_none(tmp_path):
    """库不存在 → None = *不知道有没有除权事件*, 不是「无事件」。"""
    assert sync_hk.load_hk_adj_factors(tmp_path) is None


def test_load_empty_store_returns_empty_frame(tmp_path):
    """库存在但无行 → **空帧** = 确实无除权事件, 可安全按原始价计算。"""
    _seed_factor_store(tmp_path)
    got = sync_hk.load_hk_adj_factors(tmp_path)
    assert got is not None, "空库不该被当成「未同步」"
    assert got.is_empty()


def test_load_corrupt_store_returns_none_and_logs(tmp_path, caplog):
    """文件损坏 → None + error。宁可中止, 也不拿半截因子去复权 —— 那会让价格序列
    在损坏点前后各错一半, 比完全不复权更难发现。"""
    out = sync_hk.hk_adj_factor_path(tmp_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"not a parquet file at all")
    with caplog.at_level(logging.ERROR, logger="app.services.kline_sync_hk"):
        assert sync_hk.load_hk_adj_factors(tmp_path) is None
    assert "读取失败" in caplog.text


def test_load_filters_by_symbols(tmp_path):
    """按标的过滤只作用于非空库; 空库直接返回, 不因缺列而报错。"""
    _seed_factor_store(tmp_path, [
        {"symbol": "00700.HK", "trade_date": dt.date(2026, 1, 2), "ex_factor": 1.1},
        {"symbol": "00005.HK", "trade_date": dt.date(2026, 1, 2), "ex_factor": 1.2},
    ])
    got = sync_hk.load_hk_adj_factors(tmp_path, symbols=["00700.HK"])
    assert got is not None
    assert got["symbol"].to_list() == ["00700.HK"]
    # 过滤后为空也是合法的空帧, 不是 None
    assert sync_hk.load_hk_adj_factors(tmp_path, symbols=["09988.HK"]).is_empty()


# ---------------------------------------------------------------------------
# ④ sync_hk_enriched 的 fail-closed 闸门
# ---------------------------------------------------------------------------
def test_enriched_aborts_without_factor_store(tmp_path, caplog):
    """🔴 本组测试存在的核心理由: 因子库缺失时必须**中止**, 不能静默用原始价算。

    用原始价算出来的 MA/MACD 会在每个除净日出现假跳空, 且数值看起来完全正常,
    属 CONTRIBUTING §3 的「看起来对但错」, 比直接报错危险得多。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK")
    (tmp_path / sync_hk.HK_ADJ_FACTOR_RELPATH).unlink(missing_ok=True)  # 模拟运维漏跑因子同步

    with caplog.at_level(logging.ERROR, logger="app.services.kline_sync_hk"):
        assert sync_hk.sync_hk_enriched(repo) == 0
    assert "复权因子库" in caplog.text and "不存在" in caplog.text
    assert not list((tmp_path / "kline_hk_enriched").glob("**/*.parquet")), "中止时不该写任何 enriched"


def test_enriched_proceeds_when_explicitly_opted_out(tmp_path, caplog):
    """显式传 require_adj_factor=False → 降级放行, 但必须 warning 说清代价。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK")
    (tmp_path / sync_hk.HK_ADJ_FACTOR_RELPATH).unlink(missing_ok=True)

    with caplog.at_level(logging.WARNING, logger="app.services.kline_sync_hk"):
        assert sync_hk.sync_hk_enriched(repo, require_adj_factor=False) == 30
    assert "原始价" in caplog.text and "失真" in caplog.text


# ---------------------------------------------------------------------------
# ⑤ 端到端: 复权真的作用到了 enriched 上
# ---------------------------------------------------------------------------
def test_enriched_close_is_adjusted_but_raw_close_is_not(tmp_path):
    """🔴 钉死「factors=None 导致 MA/MACD 用原始价算」这个洞已被堵上。

    30 天原始收盘 close_i = 101 + i; 在第 15 天 (2026-09-16) 放一个 ex_factor=1.25。
    前复权口径 adjusted = raw × cum_factor_at_D / total_cum_factor:
      i < 15 → raw × 1.00/1.25 = raw × 0.8   (历史价被压低, 消除除权缺口)
      i ≥ 15 → raw × 1.25/1.25 = raw          (最新价保持不变)
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=30)
    _seed_factor_store(tmp_path, [{
        "symbol": "00700.HK", "trade_date": dt.date(2026, 9, 16), "ex_factor": 1.25,
        "dividend": 25.0, "bonus": 0.0, "allot": None, "allot_price": None,
        "prev_close": 115.0,
    }])

    assert sync_hk.sync_hk_enriched(repo) == 30
    df = _read_enriched(tmp_path)

    for i, expected in ((0, 80.8), (14, 92.0), (15, 116.0), (29, 130.0)):
        row = df.row(i, named=True)
        raw = 101.0 + i
        assert row["raw_close"] == pytest.approx(raw), f"day{i} 原始价被改动了"
        assert row["close"] == pytest.approx(expected, rel=1e-9), (
            f"day{i} 前复权价错: 期望 {expected}, 实得 {row['close']}"
        )

    # 除净日之前 close < raw_close, 之后二者相等 —— 这正是「缺口被消除」的形状
    assert (df["close"] < df["raw_close"]).sum() == 15
    assert (df["close"] == df["raw_close"]).sum() == 15


def test_enriched_with_empty_factor_store_equals_raw(tmp_path):
    """空因子库 (无除权事件) → close 与 raw_close 逐行相等, 且**不**中止。

    这条与上一条配对: 证明 fail-closed 挡的是「未知」, 不是「已知无事件」。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=10)
    _seed_factor_store(tmp_path)   # 空库

    assert sync_hk.sync_hk_enriched(repo) == 10
    df = _read_enriched(tmp_path)
    assert (df["close"] == df["raw_close"]).all()


# ---------------------------------------------------------------------------
# ⑥ sync_hk_adj_factor
# ---------------------------------------------------------------------------
def test_sync_writes_factor_store_from_dividend_events(tmp_path):
    """正常路径: F10 事件 → 因子库, 数值与手算一致。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=30)   # close_i = 101 + i, 起 2026-09-01
    client = _FakeDivClient({"00700.HK": [_ev("2026-09-16", "每股派港币5元")]})

    added, changed = sync_hk.sync_hk_adj_factor(repo, client=client)

    assert added == 1
    assert changed == ["00700.HK"]
    f = _read_factor_store(tmp_path)
    assert f.height == 1
    # P = 第 14 天收盘 = 115.0, D = 5 → ex_factor = 115/110
    assert f["ex_factor"][0] == pytest.approx(115.0 / 110.0, rel=1e-12)
    assert f["prev_close"][0] == pytest.approx(115.0)
    assert f["trade_date"][0] == dt.date(2026, 9, 16)


def test_sync_uses_secucode_filter_and_is_bfp(tmp_path):
    """请求参数口径: SECUCODE 过滤 (复用 hk_fin_secucode, 不另起代码规范化分支)
    + IS_BFP="0" (实测能把腾讯 91 行降到 27 行, 剔掉「未派发或宣派股息」占位行)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=5)
    client = _FakeDivClient({"00700.HK": []})

    sync_hk.sync_hk_adj_factor(repo, client=client)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["report"] == hd.RPT_HK_DIVIDEND
    assert call["flt"] == '(SECUCODE="00700.HK")(IS_BFP="0")'
    assert call["page_size"] == hd.HK_DIV_PAGE_SIZE
    assert call["sort_columns"] == hd.HK_DIV_SORT_COLUMNS
    assert call["sort_types"] == hd.HK_DIV_SORT_TYPES


def test_sync_records_failure_and_continues(tmp_path, caplog):
    """🔴 单标的取数失败 → 记 warning + 进 failed_out + **继续**跑其余标的。

    绝不能把失败伪装成「该标的无除权事件」: 那会让上层以为数据是完整的,
    从而用缺了因子的价格序列算指标。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK", "00005.HK"])
    _seed_daily(repo, "00700.HK", days=30)
    _seed_daily(repo, "00005.HK", days=30)
    client = _FakeDivClient(
        {"00005.HK": [_ev("2026-09-16", "每股派港币1元")]},
        fail={"00700.HK"},
    )
    failed: list[str] = []

    with caplog.at_level(logging.WARNING, logger="app.services.kline_sync_hk"):
        added, changed = sync_hk.sync_hk_adj_factor(repo, client=client, failed_out=failed)

    assert failed == ["00700.HK"], "失败标的必须被记录, 供上层重跑"
    assert "拉取失败" in caplog.text
    assert changed == ["00005.HK"], "一只失败不该中断整轮"
    assert added == 1
    assert _read_factor_store(tmp_path)["symbol"].to_list() == ["00005.HK"]


def test_sync_no_events_writes_empty_store(tmp_path):
    """全部标的都无除权事件 → 写出**空库** (而非不写)。

    空库是「已同步、确认无事件」的凭据; 不写会让 sync_hk_enriched 一直 fail closed。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=5)
    client = _FakeDivClient({"00700.HK": []})

    added, changed = sync_hk.sync_hk_adj_factor(repo, client=client)

    assert (added, changed) == (0, [])
    got = sync_hk.load_hk_adj_factors(tmp_path)
    assert got is not None and got.is_empty(), "无事件也要落一个空库"


def test_sync_skips_when_no_local_daily(tmp_path, caplog):
    """本地无原始日K → 定不出参考价 P, 记 warning 后返回空。

    这不是「无除权事件」而是前置数据缺失, 调用方应先跑 sync_hk_daily。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    client = _FakeDivClient({"00700.HK": [_ev("2026-09-16", "每股派港币5元")]})

    with caplog.at_level(logging.WARNING, logger="app.services.kline_sync_hk"):
        added, changed = sync_hk.sync_hk_adj_factor(repo, client=client)

    assert (added, changed) == (0, [])
    assert "无法定参考价" in caplog.text
    assert client.calls == [], "没有原始日K时不该白发请求"


def test_sync_progress_callback_fires_per_symbol(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK", "00005.HK"])
    _seed_daily(repo, "00700.HK", days=5)
    _seed_daily(repo, "00005.HK", days=5)
    client = _FakeDivClient({})
    seen: list[tuple[int, int]] = []

    sync_hk.sync_hk_adj_factor(
        repo, client=client, on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen == [(1, 2), (2, 2)]


def test_sync_closes_only_self_created_client(tmp_path, monkeypatch):
    """自建 client 必须关 (连接池泄漏); 调用方传进来的绝不能关。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=5)

    injected = _FakeDivClient({"00700.HK": []})
    sync_hk.sync_hk_adj_factor(repo, client=injected)
    assert injected.closed is False, "不该关掉调用方传进来的 client"

    created = _FakeDivClient({"00700.HK": []})
    monkeypatch.setattr(sync_hk, "EastmoneyClient", lambda: created)
    sync_hk.sync_hk_adj_factor(repo)
    assert created.closed is True, "自建的 client 必须关闭"


def test_sync_closes_self_created_client_even_on_error(tmp_path, monkeypatch):
    """中途抛异常也要关 client (finally 保护), 否则熔断/连接池会泄漏。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=5)

    created = _FakeDivClient({"00700.HK": []})

    def _boom(*a, **k):
        # 刻意**不**在这里 close: 只有 sync 自己的 finally 关掉它, 断言才有意义。
        raise RuntimeError("模拟意外异常")

    monkeypatch.setattr(sync_hk, "EastmoneyClient", lambda: created)
    monkeypatch.setattr(sync_hk.hk_dividend, "fetch_hk_dividend_events", _boom)
    with pytest.raises(RuntimeError, match="模拟意外异常"):
        sync_hk.sync_hk_adj_factor(repo)
    assert created.closed is True, "异常路径也要经 finally 关掉自建 client"


def test_sync_is_idempotent(tmp_path):
    """同一批事件跑两遍 → 第二遍 added=0、changed 为空 (因子未变)。

    changed 为空很关键: 它是 enriched 局部重算的触发条件, 误报会导致每轮同步
    都全市场重算 enriched。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=30)
    events = {"00700.HK": [_ev("2026-09-16", "每股派港币5元")]}

    first = sync_hk.sync_hk_adj_factor(repo, client=_FakeDivClient(events))
    second = sync_hk.sync_hk_adj_factor(repo, client=_FakeDivClient(events))

    assert first == (1, ["00700.HK"])
    assert second == (0, []), "因子没变就不该报 changed"
    assert _read_factor_store(tmp_path).height == 1, "重跑不该产生重复行"


def test_sync_only_reads_universe_not_cny_counter(tmp_path):
    """人民币柜台不进 universe (同一公司双份拉取会翻倍请求数, 加剧封禁风险)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    out = tmp_path / HK_INSTRUMENT_RELPATH
    df = pl.read_parquet(out)
    df = pl.concat([df, df.with_columns(
        pl.lit("80700.HK").alias("symbol"), pl.lit(True).alias("is_cny_counter")
    )])
    atomic_write_parquet(df, out)
    _seed_daily(repo, "00700.HK", days=5)
    client = _FakeDivClient({"00700.HK": []})

    sync_hk.sync_hk_adj_factor(repo, client=client)

    requested = [re.search(r'SECUCODE="([^"]+)"', c["flt"]).group(1) for c in client.calls]
    assert requested == ["00700.HK"], f"人民币柜台不该被请求: {requested}"


def test_hk_factor_store_is_isolated_from_ashare(tmp_path):
    """港股因子落在 adj_factor_hk/all.parquet, 不污染 A 股的 adj_factor/all.parquet。

    断言针对**文件**而非目录: DataStore 会按注册清单预建全部数据目录 (含 A 股的
    adj_factor/), 空目录本身不代表写入。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=30)
    sync_hk.sync_hk_adj_factor(
        repo, client=_FakeDivClient({"00700.HK": [_ev("2026-09-16", "每股派港币5元")]})
    )

    assert (tmp_path / "adj_factor_hk" / "all.parquet").exists()
    assert not (tmp_path / "adj_factor" / "all.parquet").exists(), "写进了 A 股因子库"
    assert sync_hk.HK_ADJ_FACTOR_RELPATH != "adj_factor/all.parquet"


def test_sync_all_failed_does_not_write_empty_store(tmp_path):
    """🔴 全部标的都失败时**不落空库**。

    空库的语义是「已同步、确认无除权事件」, sync_hk_enriched 见它就放行。若失败
    也落空库, 等于用「数据齐全」的假象骗过 fail-closed 闸门 —— 比中止危险得多。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK"])
    _seed_daily(repo, "00700.HK", days=30)
    failed: list[str] = []

    added, changed = sync_hk.sync_hk_adj_factor(
        repo, client=_FakeDivClient(fail={"00700.HK"}), failed_out=failed
    )

    assert (added, changed) == (0, [])
    assert failed == ["00700.HK"]
    assert sync_hk.load_hk_adj_factors(tmp_path) is None, "失败的那轮不该留下空库"
    assert sync_hk.sync_hk_enriched(repo) == 0, "因子库缺失 → enriched 必须仍中止"


def test_sync_partial_failure_keeps_previous_store(tmp_path):
    """部分失败时不新建库, 但**已有**的库 (上一轮成功结果) 保持可用。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_instruments(tmp_path, ["00700.HK", "00005.HK"])
    _seed_daily(repo, "00700.HK", days=30)
    _seed_daily(repo, "00005.HK", days=30)

    ok = sync_hk.sync_hk_adj_factor(
        repo, client=_FakeDivClient({"00005.HK": [_ev("2026-09-16", "每股派港币1元")]})
    )
    assert ok == (1, ["00005.HK"])

    failed: list[str] = []
    sync_hk.sync_hk_adj_factor(
        repo, client=_FakeDivClient(fail={"00700.HK", "00005.HK"}), failed_out=failed
    )
    assert sorted(failed) == ["00005.HK", "00700.HK"]
    store = sync_hk.load_hk_adj_factors(tmp_path)
    assert store is not None and store.height == 1, "上一轮的结果不该被失败的那轮抹掉"


def test_market_hk_is_used_not_cn():
    """守卫: 港股链路必须显式带 Market.HK (走 CN 默认会让换手率高 100 倍)。"""
    assert Market.HK != Market.CN
    assert sync_hk.Market.HK is Market.HK
