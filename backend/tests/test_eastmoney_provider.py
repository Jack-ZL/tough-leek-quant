"""EastmoneyProvider 契约与单位标准化测试 (P1-12)。

不依赖真实网络: 用假 EastmoneyClient 返回实测录制的样例快照/K线/分红事件/盘口,
验证字段映射、单位口径 (CONTRIBUTING §3.1: 百分数→小数、volume 手/amount 元透传)、
secid 双向映射、除权因子推导 (交易所公式 + 同日合并 + 涨跌停自检 + 窗口未覆盖跳过)、
限速令牌桶/熔断器虚拟时钟节奏、空响应体重试退避与插件注册 (loader 隔离)。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.plugins.eastmoney import client as ec
from app.plugins.eastmoney import provider as ep
from app.plugins.eastmoney.provider import EastmoneyProvider

# ---- 实测录制样例 (2026-11 冒烟) ----
# clist 快照行: f2最新价 f3涨跌幅% f4涨跌额 f5量(手) f6额(元) f7振幅% f8换手% f12代码
# f13市场(1沪/0深) f14名称 f15高 f16低 f17开 f18昨收 f20总市值 f21流通市值 f124时间戳(秒)
def _spot_row(code: str = "688066", market: int = 1, **over) -> dict:
    row = {
        "f2": 5.57, "f3": 20.04, "f4": 0.93, "f5": 107158, "f6": 59632886.0,
        "f7": 2.59, "f8": 3.49, "f12": code, "f13": market, "f14": "*ST航图",
        "f15": 5.57, "f16": 5.45, "f17": 5.57, "f18": 4.64,
        "f20": 5_000_000_000.0, "f21": 4_000_000_000.0, "f124": 1761930000,
    }
    row.update(over)
    return row


# kline CSV 行(实测列序): 日期,开,收,高,低,量(手),额(元),振幅%,涨跌幅%,涨跌额,换手%
_KLINE_600000 = [
    "2026-01-05,12.47,11.82,12.48,11.80,1222843,1459886350.00,5.47,-4.98,-0.62,0.37",
    "2026-01-06,11.78,12.00,12.03,11.71,1213754,1439780652.00,2.71,1.52,0.18,0.36",
]


class _FakeClient:
    """按预置数据应答, 记录调用供断言。与真实客户端同方法签名。"""

    def __init__(
        self,
        spot: list[dict] | None = None,
        spot_total: int = 0,
        klines: dict[str, list[str]] | None = None,
        indices: list[dict] | None = None,
        dividends: list[dict] | None = None,
        depth: dict[str, dict | None] | None = None,
        error: Exception | None = None,
    ):
        self.spot = spot or []
        self.spot_total = spot_total
        self.klines = klines or {}
        self.indices = indices if indices is not None else []
        self.dividends = dividends or []
        self.depth = depth or {}
        self.error = error
        self.calls: list[tuple] = []

    def _maybe_raise(self, tag):
        self.calls.append((tag,))
        if self.error:
            raise self.error

    def spot_all(self):
        self._maybe_raise("spot_all")
        if not self.spot:
            raise ec.EastmoneyError("全市场快照为空")
        return list(self.spot)

    def spot_page(self, pn=1, pz=1000, fs=None):
        self.calls.append(("spot_page", pn, pz))
        if self.error:
            raise self.error
        return list(self.spot[:pz]), self.spot_total or len(self.spot)

    def index_spot(self, secids):
        self.calls.append(("index_spot", tuple(secids)))
        if self.error:
            raise self.error
        return list(self.indices)

    def kline(self, secid, beg, end, *, klt=101, fqt=0):
        self.calls.append(("kline", secid, beg, end, klt, fqt))
        if self.error:
            raise self.error
        return list(self.klines.get(secid, []))

    def quote_depth(self, secid):
        self.calls.append(("quote_depth", secid))
        if self.error:
            raise self.error
        return self.depth.get(secid)

    def dividend_events(self, start, end):
        self.calls.append(("dividend_events", start, end))
        if self.error:
            raise self.error
        return list(self.dividends)

    def close(self):
        pass


def _provider(fake: _FakeClient) -> EastmoneyProvider:
    p = EastmoneyProvider()
    p._client = fake
    return p


# ---- 快照行映射与单位口径 ----
def test_map_spot_row_units():
    """百分数字段 → 小数; volume 手/amount 元透传; f124 秒 → 毫秒时间戳。"""
    rec = ep._map_spot_row(_spot_row(), fetched_ms=111)
    assert rec["symbol"] == "688066.SH"
    assert rec["name"] == "*ST航图"
    assert rec["last_price"] == 5.57 and rec["prev_close"] == 4.64
    assert rec["open"] == 5.57 and rec["high"] == 5.57 and rec["low"] == 5.45
    assert rec["change_pct"] == pytest.approx(0.2004)  # 20.04% → 0.2004
    assert rec["change_amount"] == 0.93
    assert rec["amplitude"] == pytest.approx(0.0259)
    assert rec["turnover_rate"] == pytest.approx(0.0349)
    assert rec["volume"] == 107158  # 东财原生手, 不换算
    assert rec["amount"] == 59632886.0
    assert rec["timestamp"] == 1761930000 * 1000


def test_map_spot_row_suspended_kept_with_nones():
    """停牌行价格字段为 "-": 保留记录但值为 None, 不伪造 0。"""
    rec = ep._map_spot_row(
        _spot_row(f2="-", f3="-", f4="-", f5="-", f6="-", f7="-", f8="-", f124="-"),
        fetched_ms=222,
    )
    assert rec is not None and rec["symbol"] == "688066.SH"
    assert rec["last_price"] is None and rec["change_pct"] is None
    assert rec["volume"] is None and rec["turnover_rate"] is None
    assert rec["timestamp"] == 222  # f124 无效时退回本地拉取时刻


def test_map_spot_row_derives_missing_change():
    """f3/f4 缺失 → 用最新价与昨收推导涨跌额/涨跌幅 (小数制, 与 quote_service 同口径)。"""
    rec = ep._map_spot_row(_spot_row(f3=None, f4=None, f2=10.5, f18=10.0), fetched_ms=1)
    assert rec["change_amount"] == pytest.approx(0.5)
    assert rec["change_pct"] == pytest.approx(0.05)


def test_map_spot_row_bad_timestamp_falls_back():
    """f124 缺失/非法 ("-") → 退回本地拉取时刻。"""
    assert ep._map_spot_row(_spot_row(f124="-"), 333)["timestamp"] == 333
    assert ep._map_spot_row(_spot_row(f124=None), 334)["timestamp"] == 334


def test_map_spot_row_no_code_dropped():
    assert ep._map_spot_row({"f13": 1}, 1) is None


@pytest.mark.parametrize(
    "code,market,suffix",
    [
        ("688066", 1, ".SH"),
        ("300030", 0, ".SZ"),
        ("000001", 0, ".SZ"),
        ("920002", 0, ".BJ"),  # 北交所新代码段挂深市市场码
        ("430047", 0, ".BJ"),
        ("830799", 0, ".BJ"),
    ],
)
def test_suffix_mapping(code, market, suffix):
    assert ep._suffix_of(code, market) == suffix
    rec = ep._map_spot_row(_spot_row(code=code, f13=market), 1)
    assert rec["symbol"] == code + suffix


# ---- kline CSV 解析 (列序陷阱: 开收高低, 非常见 OHLC) ----
def test_map_kline_row_column_order():
    row = ep._map_kline_row("600000.SH", _KLINE_600000[0])
    assert row == {
        "symbol": "600000.SH", "date": date(2026, 1, 5),
        "open": 12.47, "close": 11.82, "high": 12.48, "low": 11.80,
        "volume": 1222843.0, "amount": 1459886350.0,
    }


def test_map_kline_row_bad_lines():
    assert ep._map_kline_row("600000.SH", "garbage") is None
    assert ep._map_kline_row("600000.SH", "2026-01-05,12.47,-,12.48,11.80,1,2") is None
    assert ep._map_kline_row("600000.SH", "not-a-date,1,2,3,4,5,6") is None


# ---- secid 映射 ----
def test_symbol_to_secid():
    assert ep.symbol_to_secid("600000.SH") == "1.600000"
    assert ep.symbol_to_secid("000001.SZ") == "0.000001"
    assert ep.symbol_to_secid("920002.BJ") == "0.920002"
    assert ep.symbol_to_secid("000001.SH") == "1.000001"  # 上证指数与深市同码不同前缀
    assert ep.symbol_to_secid("399001.SZ") == "0.399001"
    assert ep.symbol_to_secid("600000") is None  # 裸码拒绝 (上游已归一化)
    assert ep.symbol_to_secid("abc.SH") is None
    assert ep.symbol_to_secid("") is None


# ---- 交易所公式与板限 ----
def test_ref_price_formula():
    # X = (10 - 0.35) / (1 + 0.2) = 8.0416… → 0.01 half-up → 8.04
    assert ep._ref_price(10.0, 0.35, 0.2, 0.0, 0.0) == 8.04
    assert ep._ref_price(None, 0.35, 0.2, 0.0, 0.0) is None
    assert ep._ref_price(10.0, 0.0, -1.5, 0.0, 0.0) is None  # 分母 ≤ 0


def test_price_limit_by_board():
    assert ep._price_limit("300030.SZ") == 0.20
    assert ep._price_limit("688066.SH") == 0.20
    assert ep._price_limit("920002.BJ") == 0.30
    assert ep._price_limit("600000.SH") == 0.10


# ---- realtime ----
def test_get_realtime_maps_records():
    fake = _FakeClient(spot=[_spot_row(), _spot_row(code="300030", market=0)])
    recs = _provider(fake).get_realtime()
    assert [r["symbol"] for r in recs] == ["688066.SH", "300030.SZ"]


def test_get_realtime_soft_fail_returns_empty():
    """接口失败软返回 [] (不阻断轮询, 上游沿用缓存)。"""
    fake = _FakeClient(error=ec.EastmoneyError("熔断中"))
    assert _provider(fake).get_realtime() == []


def test_get_realtime_schema_change_warning():
    """整页都缺 f12 → 判定接口结构变化, 返回 [] (日志告警路径)。"""
    fake = _FakeClient(spot=[{"f13": 1}, {"f13": 0}])
    assert _provider(fake).get_realtime() == []


def test_get_realtime_indices_maps_secids():
    """ wanted 指数 symbol → secid 单次 clist; 失败 None(沿用缓存), 空 []。"""
    fake = _FakeClient(indices=[_spot_row(code="000001", market=1, f14="上证指数")])
    p = _provider(fake)
    recs = p.get_realtime_indices(["000001.SH", "399001.SZ", "899050.BJ"])
    assert ("index_spot", ("1.000001", "0.399001", "0.899050")) in fake.calls
    assert recs and recs[0]["symbol"] == "000001.SH"

    fake_err = _FakeClient(error=ec.EastmoneyError("boom"))
    assert _provider(fake_err).get_realtime_indices(["000001.SH"]) is None

    fake_empty = _FakeClient(indices=[])
    assert _provider(fake_empty).get_realtime_indices(["000001.SH"]) == []
    assert _provider(fake_empty).get_realtime_indices([]) == []


# ---- daily ----
def test_iter_daily_normalized_frames_and_progress():
    fake = _FakeClient(klines={"1.600000": _KLINE_600000, "0.000001": []})
    progress: list[tuple[int, int]] = []
    frames = list(
        _provider(fake).iter_daily(
            ["600000.SH", "000001.SZ"],
            start_time=datetime(2026, 1, 1),
            end_time=datetime(2026, 1, 31),
            on_chunk_done=lambda d, t: progress.append((d, t)),
        )
    )
    assert len(frames) == 1  # 空标的不出帧
    df = frames[0]
    assert {"symbol", "date", "open", "high", "low", "close", "volume", "amount"} <= set(df.columns)
    assert df.height == 2 and df["symbol"][0] == "600000.SH"
    assert progress == [(1, 2), (2, 2)]
    # 原始价契约: fqt=0 不复权
    assert ("kline", "1.600000", "20260101", "20260131", 101, 0) in fake.calls


def test_iter_daily_soft_fail_per_symbol():
    """单标的失败只跳过该标的, 进度回调照常推进。"""
    class _Half(_FakeClient):
        def kline(self, secid, beg, end, *, klt=101, fqt=0):
            if secid == "1.600000":
                raise ec.EastmoneyError("限流")
            return super().kline(secid, beg, end, klt=klt, fqt=fqt)

    fake = _Half(klines={"0.000001": _KLINE_600000})
    progress: list[tuple[int, int]] = []
    frames = list(
        _provider(fake).iter_daily(
            ["600000.SH", "000001.SZ"], None, None,
            on_chunk_done=lambda d, t: progress.append((d, t)),
        )
    )
    assert len(frames) == 1 and frames[0]["symbol"][0] == "000001.SZ"
    assert progress == [(1, 2), (2, 2)]


def test_iter_daily_empty_symbols():
    progress: list[tuple[int, int]] = []
    out = list(_provider(_FakeClient()).iter_daily([], None, None, on_chunk_done=lambda d, t: progress.append((d, t))))
    assert out == [] and progress == [(0, 0)]


def test_get_daily_aggregates():
    fake = _FakeClient(klines={"1.600000": _KLINE_600000})
    df = _provider(fake).get_daily(["600000.SH"], datetime(2026, 1, 1), datetime(2026, 1, 31))
    assert df.height == 2
    assert _provider(_FakeClient()).get_daily([], None, None).is_empty()


# ---- adj_factor ----
def _div_event(secucode="600000.SH", ex="2026-05-11 00:00:00", bonus_rmb=3.5, it_ratio=2.0, **over):
    ev = {
        "SECUCODE": secucode,
        "SECURITY_CODE": secucode.split(".")[0],
        "EX_DIVIDEND_DATE": ex,
        "PRETAX_BONUS_RMB": bonus_rmb,  # 每 10 股派息
        "BONUS_IT_RATIO": it_ratio,  # 每 10 股送转合计
        "BONUS_RATIO": None,
        "IT_RATIO": None,
        "ASSIGN_PROGRESS": "实施分配",
    }
    ev.update(over)
    return ev


# 配价K线: 除权日前收盘 10.0, 除权日收盘 8.10 (参考价 8.04 附近, 自检可通过)
_ADJ_KLINES = {
    "1.600000": [
        "2026-05-07,10.10,10.05,10.20,9.98,100000,100500000.0,2.0,0.5,0.05,1.0",
        "2026-05-08,10.05,10.00,10.10,9.95,100000,100000000.0,1.5,-0.5,-0.05,1.0",
        "2026-05-11,8.05,8.10,8.20,8.00,120000,97000000.0,2.5,0.7,0.06,1.2",
    ],
}


def test_get_adj_factors_derivation():
    """10派3.5转2 → D=0.35 B=0.2: ref=(10-0.35)/1.2=8.04, factor=10/8.04。"""
    fake = _FakeClient(dividends=[_div_event()], klines=_ADJ_KLINES)
    df = _provider(fake).get_adj_factors(
        ["600000.SH"], datetime(2026, 5, 1), datetime(2026, 5, 31)
    )
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600000.SH" and row["trade_date"] == date(2026, 5, 11)
    assert row["ex_factor"] == pytest.approx(10.0 / 8.04)
    assert row["dividend"] == pytest.approx(0.35) and row["bonus"] == pytest.approx(0.2)
    assert row["allot"] is None and row["prev_close"] == 10.0


def test_get_adj_factors_same_day_merge():
    """同日两事件 (派息与送转分开公告) 成分求和合并为一行。"""
    fake = _FakeClient(
        dividends=[
            _div_event(bonus_rmb=1.0, it_ratio=None, BONUS_RATIO=None, IT_RATIO=None),
            _div_event(bonus_rmb=None, it_ratio=None, BONUS_RATIO=1.0, IT_RATIO=1.0),
        ],
        klines=_ADJ_KLINES,
    )
    df = _provider(fake).get_adj_factors(["600000.SH"], datetime(2026, 5, 1), datetime(2026, 5, 31))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["dividend"] == pytest.approx(0.1) and row["bonus"] == pytest.approx(0.2)
    # ref = (10 - 0.1)/1.2 = 8.25, factor = 10/8.25; 自检 ret=8.10/8.25-1 在板限内
    assert row["ex_factor"] == pytest.approx(10.0 / 8.25)


def test_get_adj_factors_self_check_rejects_bad_factor():
    """除权日收盘远离参考价 → 复权后涨跌幅超板限, 自检剔除该因子。"""
    bad_klines = {
        "1.600000": [
            "2026-05-08,10.05,10.00,10.10,9.95,100000,100000000.0,1.5,-0.5,-0.05,1.0",
            "2026-05-11,9.50,9.50,9.60,9.40,120000,114000000.0,1.0,-5.0,-0.5,1.2",
        ],
    }
    fake = _FakeClient(dividends=[_div_event()], klines=bad_klines)
    df = _provider(fake).get_adj_factors(["600000.SH"], datetime(2026, 5, 1), datetime(2026, 5, 31))
    assert df.is_empty() and "ex_factor" in df.columns  # 空帧仍带 schema


def test_get_adj_factors_skips_uncovered_and_foreign():
    """日K窗口未覆盖的远古事件跳过; 非请求标的的事件被过滤。"""
    fake = _FakeClient(
        dividends=[
            _div_event(ex="2026-04-01 00:00:00"),  # 早于配价K线起点 → prev_days 为空
            _div_event(secucode="000001.SZ"),  # 不在请求集
        ],
        klines=_ADJ_KLINES,
    )
    df = _provider(fake).get_adj_factors(["600000.SH"], datetime(2026, 3, 1), datetime(2026, 5, 31))
    assert df.is_empty()


def test_get_adj_factors_no_kline_skips_symbol():
    fake = _FakeClient(dividends=[_div_event()], klines={})
    df = _provider(fake).get_adj_factors(["600000.SH"], datetime(2026, 5, 1), datetime(2026, 5, 31))
    assert df.is_empty()


def test_get_adj_factors_etf_and_empty_symbols():
    """ETF/空标的 → 空帧 (上层回退 TickFlow), 不触发任何请求。"""
    fake = _FakeClient(dividends=[_div_event()])
    p = _provider(fake)
    assert p.get_adj_factors(["510300.SH"], None, None, asset_type="etf").is_empty()
    assert p.get_adj_factors([], None, None).is_empty()
    assert fake.calls == []


def test_get_adj_factors_progress_and_error():
    progress: list[tuple[int, int]] = []
    fake = _FakeClient(dividends=[_div_event()], klines=_ADJ_KLINES)
    _provider(fake).get_adj_factors(
        ["600000.SH"], datetime(2026, 5, 1), datetime(2026, 5, 31),
        on_chunk_done=lambda d, t: progress.append((d, t)),
    )
    assert progress == [(1, 1)]
    fake_err = _FakeClient(error=ec.EastmoneyError("boom"))
    df = _provider(fake_err).get_adj_factors(["600000.SH"], None, None)
    assert df.is_empty() and "ex_factor" in df.columns


# ---- instruments ----
def test_get_instruments_rows():
    fake = _FakeClient(spot=[
        _spot_row(code="688066", market=1, f2=5.0, f20=1_000_000_000.0, f21=800_000_000.0),
        _spot_row(code="920002", market=0, f14="万达轴承", f2="-"),  # 停牌: 股本反推不可用
    ])
    items = _provider(fake).get_instruments("stock")
    assert len(items) == 2
    a = items[0]
    assert a["symbol"] == "688066.SH" and a["exchange"] == "SH"
    assert a["region"] == "CN" and a["type"] == "stock" and a["code"] == "688066"
    assert a["ext"]["total_shares"] == 200_000_000  # f20/f2
    assert a["ext"]["float_shares"] == 160_000_000
    assert a["ext"]["listing_date"] is None  # 接口不提供 → 旧维表保留
    b = items[1]
    assert b["symbol"] == "920002.BJ" and b["exchange"] == "BJ"
    assert b["ext"]["total_shares"] is None  # 停牌无价格, 不伪造股本
    assert _provider(fake).get_instruments("etf") == []


def test_get_instruments_soft_fail():
    fake = _FakeClient(error=ec.EastmoneyError("boom"))
    assert _provider(fake).get_instruments("stock") == []


# ---- depth5 ----
_DEPTH_ROW = {
    "f19": 9.90, "f20": 100, "f17": 9.89, "f18": 200, "f15": 9.88, "f16": 300,
    "f13": 9.87, "f14": 400, "f11": 9.86, "f12": 500,
    "f31": 9.91, "f32": 110, "f33": 9.92, "f34": 220, "f35": 9.93, "f36": 330,
    "f37": 9.94, "f38": 440, "f39": 9.95, "f40": 550,
}


def test_get_depth_batch_mapping():
    fake = _FakeClient(depth={"1.600000": _DEPTH_ROW, "0.000001": None})
    out = _provider(fake).get_depth_batch(["600000.SH", "000001.SZ"])
    assert set(out) == {"600000.SH"}  # data=null (无效/停牌) 跳过
    e = out["600000.SH"]
    assert e["bid_prices"] == [9.90, 9.89, 9.88, 9.87, 9.86]  # 买一→买五
    assert e["bid_volumes"] == [100, 200, 300, 400, 500]
    assert e["ask_prices"] == [9.91, 9.92, 9.93, 9.94, 9.95]
    assert e["ask_volumes"] == [110, 220, 330, 440, 550]
    assert isinstance(e["timestamp"], int)


def test_get_depth_batch_partial_fields_skipped():
    """任一档价格缺失 ("-") → 整个标的跳过, 不伪造盘口。"""
    row = dict(_DEPTH_ROW, f15="-")
    fake = _FakeClient(depth={"1.600000": row})
    assert _provider(fake).get_depth_batch(["600000.SH"]) == {}


def test_get_depth_batch_error_continues():
    class _Half(_FakeClient):
        def quote_depth(self, secid):
            if secid == "1.600000":
                raise ec.EastmoneyError("限流")
            return super().quote_depth(secid)

    fake = _Half(depth={"0.000001": _DEPTH_ROW})
    out = _provider(fake).get_depth_batch(["600000.SH", "000001.SZ"])
    assert set(out) == {"000001.SZ"}


# ---- 设置页试拉 ----
def test_test_dataset_realtime_and_unknown():
    fake = _FakeClient(spot=[_spot_row()], spot_total=5926)
    p = _provider(fake)
    out = p.test_dataset("realtime")
    assert out["rows"] == 5926 and out["provider"] == "eastmoney"
    assert out["preview"][0]["symbol"] == "688066.SH"
    unknown = p.test_dataset("minute")
    assert unknown["rows"] == 0 and "未接入" in unknown["error"]


def test_test_dataset_daily_json_serializable():
    fake = _FakeClient(klines={"1.600000": _KLINE_600000})
    out = _provider(fake).test_dataset("daily", ["600000.SH"])
    assert out["rows"] == 2
    json.dumps(out)  # date → ISO 字符串, 不抛异常


def test_test_dataset_depth5():
    fake = _FakeClient(depth={"0.000001": _DEPTH_ROW})
    out = _provider(fake).test_dataset("depth5", ["000001.SZ"])
    assert out["rows"] == 1 and "000001.SZ" in out["preview"]


# ---- 限速/熔断 (虚拟时钟) ----
class _VirtualTime:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_token_bucket_paces_requests():
    """容量 2、速率 2/s: 前 2 次立即通过, 后 2 次各等 0.5s (虚拟时钟验证节奏)。"""
    vt = _VirtualTime()
    bucket = ec._TokenBucket(rate=2.0, capacity=2, clock=vt.now, sleeper=vt.sleep)
    for _ in range(4):
        bucket.acquire()
    assert vt.t == pytest.approx(1.0, abs=0.01)


def test_breaker_opens_and_recovers():
    vt = _VirtualTime()
    b = ec._Breaker(fail_threshold=3, cooldown_s=100.0, clock=vt.now)
    b.on_failure()
    b.on_failure()
    b.check()  # 未到阈值, 不抛
    b.on_failure()  # 第 3 次 → 熔断开启
    with pytest.raises(ec.EastmoneyError):
        b.check()
    vt.sleep(101.0)
    b.check()  # 冷却期满自动放行


def test_breaker_success_resets_streak():
    vt = _VirtualTime()
    b = ec._Breaker(fail_threshold=3, cooldown_s=100.0, clock=vt.now)
    b.on_failure()
    b.on_failure()
    b.on_success()  # 连续计数清零
    b.on_failure()
    b.on_failure()
    b.check()  # 仍不熔断


# ---- HTTP 层: 重试/退避/信封 ----
class _FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _client_with(responses, vt) -> tuple[ec.EastmoneyClient, _FakeHttp]:
    c = ec.EastmoneyClient(qps=1000.0, jitter_s=(0.0, 0.0), clock=vt.now, sleeper=vt.sleep)
    fake_http = _FakeHttp(responses)
    c._http = fake_http
    return c, fake_http


def test_get_json_retries_empty_body_then_succeeds():
    """空响应体 (东财限流典型症状) → 退避重试, 第三次成功。"""
    vt = _VirtualTime()
    c, http = _client_with(
        [_FakeResponse(text="  "), _FakeResponse(text=""), _FakeResponse(payload={"rc": 0, "data": {"ok": 1}})],
        vt,
    )
    out = c._get_json(ec.PUSH2_BASE, "/api/qt/clist/get", {})
    assert out == {"rc": 0, "data": {"ok": 1}}
    assert len(http.requests) == 3
    assert vt.t > 0  # 退避确实等待过 (虚拟时间前进)


def test_get_json_4xx_no_retry():
    """4xx 确定性错误立即抛出, 不浪费重试配额。"""
    vt = _VirtualTime()
    c, http = _client_with([_FakeResponse(status=404, text="nf")], vt)
    with pytest.raises(ec.EastmoneyError, match="404"):
        c._get_json(ec.PUSH2_BASE, "/x", {})
    assert len(http.requests) == 1


def test_get_json_exhausts_retries_and_trips_breaker():
    """重试耗尽抛错; 连续失败累计触发熔断后快速失败 (不再发请求)。"""
    vt = _VirtualTime()
    c, http = _client_with([_FakeResponse(status=503, text="busy")] * 30, vt)
    with pytest.raises(ec.EastmoneyError, match="重试"):
        c._get_json(ec.PUSH2_BASE, "/x", {})
    n_first = len(http.requests)
    assert n_first == 3  # max_retries=3
    # 再来两轮 (每轮 3 次失败) → 累计 ≥5 次连续失败, 熔断开启
    for _ in range(2):
        with pytest.raises(ec.EastmoneyError):
            c._get_json(ec.PUSH2_BASE, "/x", {})
    before = len(http.requests)
    with pytest.raises(ec.EastmoneyError, match="熔断"):
        c._get_json(ec.PUSH2_BASE, "/x", {})
    assert len(http.requests) == before  # 熔断期快速失败, 未发请求


def test_push2_envelope_rc_and_null_data():
    vt = _VirtualTime()
    c, _ = _client_with([_FakeResponse(payload={"rc": 4001, "data": None})], vt)
    with pytest.raises(ec.EastmoneyError, match="rc=4001"):
        c._push2_data(ec.PUSH2_BASE, "/x", {})
    c2, _ = _client_with([_FakeResponse(payload={"rc": 0, "data": None})], vt)
    assert c2._push2_data(ec.PUSH2HIS_BASE, "/x", {}) is None  # 无效 secid → 空结果非错误


def test_spot_page_diff_dict_compat():
    """老版本接口 diff 为 {序号: 行} 字典 — 兼容转列表。"""
    payload = {"rc": 0, "data": {"total": 2, "diff": {"0": {"f12": "600000", "f13": 1}, "1": {"f12": "000001", "f13": 0}}}}
    vt = _VirtualTime()
    c, _ = _client_with([_FakeResponse(payload=payload)], vt)
    rows, total = c.spot_page()
    assert total == 2 and [r["f12"] for r in rows] == ["600000", "000001"]


def test_dividend_events_pagination_and_failure():
    p1 = {"success": True, "result": {"pages": 2, "data": [{"SECUCODE": "600000.SH"}]}}
    p2 = {"success": True, "result": {"pages": 2, "data": [{"SECUCODE": "000001.SZ"}]}}
    vt = _VirtualTime()
    c, http = _client_with([_FakeResponse(payload=p1), _FakeResponse(payload=p2)], vt)
    rows = c.dividend_events(date(2026, 5, 1), date(2026, 5, 31))
    assert len(rows) == 2 and len(http.requests) == 2
    assert http.requests[0][1]["pageNumber"] == 1 and http.requests[1][1]["pageNumber"] == 2

    c2, _ = _client_with([_FakeResponse(payload={"success": False, "message": "ALLOT_RATIO返回字段不存在"})], vt)
    with pytest.raises(ec.EastmoneyError, match="分红事件接口失败"):
        c2.dividend_events(date(2026, 5, 1), date(2026, 5, 31))


# ---- 插件注册 (loader 集成, 不触网) ----
def test_plugin_registered_by_loader():
    """内置插件扫描: eastmoney 免 Key 恒可用, datasets 声明驱动路由与设置页卡片。"""
    from app.data_providers import custom as custom_sources

    plugins = {p["name"]: p for p in custom_sources.list_plugins()}
    em = plugins.get("eastmoney")
    assert em is not None and em["available"] is True
    assert em["api_key_env"] == ""  # 免 Key → 卡片即装即亮
    assert set(em["datasets"]) == {"realtime", "daily", "adj_factor", "depth5"}
    assert "eastmoney" in custom_sources.names()
    assert custom_sources.is_builtin("eastmoney")
    for ds in ("realtime", "daily", "adj_factor", "depth5"):
        assert custom_sources.provider_has_dataset("eastmoney", ds)
    assert not custom_sources.provider_has_dataset("eastmoney", "minute")  # 未声明 → 回退 tickflow
    provider = custom_sources.get_provider("eastmoney")
    assert isinstance(provider, EastmoneyProvider) and provider.builtin is True


def test_capability_matrix_includes_eastmoney():
    """能力矩阵自动注入: 偏好切到 eastmoney 后 4 个能力生效源即 eastmoney。"""
    from app.data_providers.capabilities import build_capability_matrix

    current = {
        "daily_data_provider": "eastmoney",
        "adj_factor_provider": "eastmoney",
        "minute_data_provider": "tickflow",
        "full_minute_data_provider": "tickflow",
        "depth5_data_provider": "eastmoney",
        "realtime_data_provider": "eastmoney",
        "financial_data_provider": "tickflow",
    }
    matrix = build_capability_matrix(current, tickflow_tier="expert")
    by_id = {c["id"]: c for c in matrix["capabilities"]}
    for cap in ("realtime", "daily", "adj_factor", "depth5"):
        assert by_id[cap]["effective"] == "eastmoney"
        assert by_id[cap]["usable"] is True
        names = [c["name"] for c in by_id[cap]["candidates"]]
        assert "eastmoney" in names
    # minute/financial 未声明 → 生效源仍为 tickflow
    assert by_id["minute"]["effective"] == "tickflow"
    assert by_id["financial"]["effective"] == "tickflow"
