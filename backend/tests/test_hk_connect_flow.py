"""``app.services.hk_connect_flow`` + ``GET /api/hk/connect-flow`` 测试。

服务层测试直接把 ``_FakeEmClient`` 当 ``client=`` 传进去, 因此**整条链**
(插件透视 → 单位换算 → 服务聚合) 都被真实执行, 只有 HTTP 那一层是假的。

重点回归闸:

1. 🔴 ``net_buy_5d`` / ``net_buy_20d`` 在窗口不足或含 null 时必须是 ``None``,
   **不是**部分和 —— 3 天的和冒充「近 5 日」在卡片上看不出来 (§4)。
2. 🔴 上游失败必须 502, **不得**降级成空列表 —— 「拉不到」和「这段时间没有
   资金流」是两种状态。
3. 🔴 窗口内确实没有交易日 (整段假期) 是**合法答案**: 200 + ``total=0`` +
   全 null 汇总, 不抛错。
4. 金额单位是**元**, 且服务层不做任何单位运算 (换算在插件层一次做完)。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.plugins.eastmoney.client import EastmoneyError
from app.services import hk_connect_flow as svc
from app.tickflow.repository import DataStore, KlineRepository
from tests.test_eastmoney_hsgt import DAY_2026_11_19, DAY_2026_11_20, _FakeClient


def _day(offset: int, net: float) -> list[dict]:
    """造一个只含 ``006`` 合计行的交易日 (用于滚动窗口测试)。

    ``BUY_AMT``/``SELL_AMT`` 按恒等式配平, 保证夹具自洽。
    """
    day = dt.date(2026, 11, 20) - dt.timedelta(days=offset)
    buy = (1000.0 + net) / 2
    sell = (1000.0 - net) / 2
    return [
        {
            "TRADE_DATE": f"{day.isoformat()} 00:00:00",
            "MUTUAL_TYPE": "006",
            "BUY_AMT": buy,
            "SELL_AMT": sell,
            "NET_DEAL_AMT": net,
            "DEAL_AMT": 1000.0,
            "ACCUM_DEAL_AMT": 5000000.0 + net,
            "INDEX_CLOSE_PRICE": 22410.8,
            "INDEX_CHANGE_RATE": -1.89,
        }
    ]


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------

def test_service_pivots_to_wide_frame_with_pinned_column_order():
    """列顺序由 ``HK_CONNECT_FLOW_COLS`` 唯一决定 (API 直接 ``to_dicts()``)。"""
    got = svc.hk_connect_flow(client=_FakeClient(DAY_2026_11_20 + DAY_2026_11_19))
    assert got.items.columns == list(svc.HK_CONNECT_FLOW_COLS)
    assert got.items.height == 2
    assert got.trading_days == 2


def test_service_sorts_ascending_so_frontend_needs_no_reversal():
    got = svc.hk_connect_flow(client=_FakeClient(DAY_2026_11_20 + DAY_2026_11_19))
    dates = got.items["trade_date"].to_list()
    assert dates == [dt.date(2026, 11, 19), dt.date(2026, 11, 20)]


def test_service_amounts_are_in_yuan_not_millions():
    """🔴 单位闸: 服务层不做单位运算, 但必须确认插件已经换算过。

    上游给的是「百万元」, 11811.50 百万元 = 11811500000 元。若哪天有人把插件的
    ``HSGT_AMT_UNIT`` 改掉而服务层照抄, 这条会失败。
    """
    got = svc.hk_connect_flow(client=_FakeClient(DAY_2026_11_20))
    row = got.items.row(0, named=True)
    assert row["net_buy"] == 11811500000.0
    assert row["turnover"] == 97681640000.0
    assert got.currency == "CNY"


def test_service_summary_comes_from_latest_row():
    got = svc.hk_connect_flow(client=_FakeClient(DAY_2026_11_20 + DAY_2026_11_19))
    assert got.latest_trade_date == dt.date(2026, 11, 20)
    assert got.latest_net_buy == 11811500000.0
    # 历史累计净流入取最新一行 (上游逐日维护), 不是窗口内求和
    assert got.accum_net_inflow == 5534878670000.0
    assert got.index_close == pytest.approx(22410.8)
    assert got.index_change_pct == pytest.approx(-1.89)


def test_service_picks_latest_by_date_not_by_list_position():
    """乱序输入也要取到真正最新的那天。

    插件已按 ``TRADE_DATE`` 升序返回, 但服务层不依赖这个契约 (显式 ``max``)。
    """
    got = svc.hk_connect_flow(client=_FakeClient(DAY_2026_11_20 + DAY_2026_11_19))
    assert got.latest_trade_date == dt.date(2026, 11, 20)
    got_rev = svc.hk_connect_flow(client=_FakeClient(DAY_2026_11_19 + DAY_2026_11_20))
    assert got_rev.latest_trade_date == dt.date(2026, 11, 20)


def test_roll_sums_use_trailing_window():
    """近 5 日 = 最近 5 个交易日的净买入合计 (单位: 元)。"""
    rows = []
    for i in range(7):
        rows.extend(_day(i, net=100.0 + i))  # 百万元
    got = svc.hk_connect_flow(client=_FakeClient(rows))
    assert got.trading_days == 7
    # 升序排列后最近 5 个交易日 = 11-16..11-20, 净买入 104+103+102+101+100
    # = 510 百万元 = 5.1e8 元 (注意不是最早的 5 天 106..102)
    assert got.net_buy_5d == pytest.approx(510.0 * 1e6)
    assert got.net_buy_20d is None  # 只有 7 天


def test_roll_sum_is_none_when_window_too_short():
    """🔴 窗口不足 → ``None``, **不是**把现有的加起来。

    3 天的和标成「近 5 日净买入」, 数字看着完全正常, 没人会发现少了 2 天 (§4)。
    """
    rows = []
    for i in range(3):
        rows.extend(_day(i, net=100.0))
    got = svc.hk_connect_flow(client=_FakeClient(rows))
    assert got.trading_days == 3
    assert got.net_buy_5d is None
    assert got.net_buy_20d is None
    # 但最新值与累计仍可用 —— 它们不依赖窗口长度
    assert got.latest_net_buy == pytest.approx(100.0 * 1e6)


def test_roll_sum_is_none_when_any_day_missing():
    """🔴 窗口内任一日缺数 → ``None``。

    ``sum()`` 会把 null 当 0 跳过, 于是「某天上游缺数」会静默变成一个偏小的合计。
    缺数与「那天净买入恰好为 0」是两回事。

    构造方式: 让 2026-11-18 那天**只有分通道行、没有 006 合计行** —— 该交易日仍然
    存在 (窗口长度够 5 天), 但 ``net_buy`` 为 ``None`` (插件刻意不用 002+004 兜底)。
    这样测的才是「含 null」这条规则, 而不是被「窗口不足」那条遮蔽。
    """
    rows = []
    for i in range(5):
        rows.extend(_day(i, net=100.0))
    # 抽掉 2026-11-18 的合计行, 换成一条分通道行, 保证那天仍在序列里
    rows = [r for r in rows if not (r["MUTUAL_TYPE"] == "006" and "2026-11-18" in r["TRADE_DATE"])]
    rows.append(
        {
            "TRADE_DATE": "2026-11-18 00:00:00",
            "MUTUAL_TYPE": "002",
            "BUY_AMT": 550.0,
            "SELL_AMT": 450.0,
            "NET_DEAL_AMT": 100.0,
            "DEAL_AMT": 1000.0,
            "ACCUM_DEAL_AMT": 3000000.0,
            "INDEX_CLOSE_PRICE": 22410.8,
            "INDEX_CHANGE_RATE": -1.89,
        }
    )
    got = svc.hk_connect_flow(client=_FakeClient(rows))
    # 前提校验: 5 个交易日都在, 且其中一天 net_buy 为 None
    assert got.trading_days == 5
    net_buys = got.items["net_buy"].to_list()
    assert net_buys.count(None) == 1
    assert got.net_buy_5d is None
    assert got.net_buy_20d is None


def test_roll_sum_helper_rejects_short_and_null_inputs():
    """直接钉住 ``_roll_sum`` 的两条 fail-closed 规则。"""
    assert svc._roll_sum([1.0, 2.0, 3.0], 5) is None  # 不足窗口
    assert svc._roll_sum([1.0, 2.0, None, 4.0, 5.0], 5) is None  # 含 null
    assert svc._roll_sum([1.0, 2.0, 3.0, 4.0, 5.0], 5) == 15.0
    # 只取尾部窗口, 不是全量求和
    assert svc._roll_sum([999.0, 1.0, 2.0, 3.0, 4.0, 5.0], 5) == 15.0
    assert svc._roll_sum([], 5) is None


def test_service_empty_window_returns_empty_frame_not_error():
    """窗口内无交易日 (整段假期) 是**合法答案**, 不抛错。"""
    got = svc.hk_connect_flow(client=_FakeClient([]))
    assert got.items.is_empty()
    assert got.items.columns == list(svc.HK_CONNECT_FLOW_COLS)
    assert got.trading_days == 0
    assert got.latest_net_buy is None
    assert got.latest_trade_date is None
    assert got.accum_net_inflow is None
    assert got.net_buy_5d is None
    assert got.net_buy_20d is None
    assert got.index_close is None
    # 口径元信息即使空窗也要给出 (前端要显示数据源)
    assert got.currency == "CNY"
    assert got.source == "eastmoney_datacenter_hsgt"


def test_service_propagates_eastmoney_error():
    """🔴 上游失败必须上抛, 由 API 层转 502。

    降级成空表会把「拉不到」伪装成「这段时间没有资金流」, 而资金流为 0 与资金流
    未知是完全不同的两件事 (§4)。
    """
    client = _FakeClient(EastmoneyError("9501 filter字段中日期参数格式错误"))
    with pytest.raises(EastmoneyError, match="9501"):
        svc.hk_connect_flow(client=client)


def test_service_passes_window_through_to_plugin():
    client = _FakeClient(DAY_2026_11_20)
    got = svc.hk_connect_flow(
        start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 20), client=client
    )
    flt = client.calls[0]["flt"]
    assert "(TRADE_DATE>='2026-11-01')" in flt
    assert "(TRADE_DATE<='2026-11-20')" in flt
    # 窗口原样回显进结果, 让「这是哪段时间的数据」可见
    assert got.start == dt.date(2026, 11, 1)
    assert got.end == dt.date(2026, 11, 20)


def test_service_closes_self_built_client(monkeypatch):
    """``client=None`` 时自建自关, 不泄漏连接。"""
    client = _FakeClient(DAY_2026_11_20)
    monkeypatch.setattr(svc, "EastmoneyClient", lambda: client)
    svc.hk_connect_flow()
    assert client.closed is True


def test_service_closes_self_built_client_even_on_error(monkeypatch):
    """上游抛错时也要关掉自建的 client (``try/finally``)。"""
    client = _FakeClient(EastmoneyError("限流"))
    monkeypatch.setattr(svc, "EastmoneyClient", lambda: client)
    with pytest.raises(EastmoneyError):
        svc.hk_connect_flow()
    assert client.closed is True


def test_service_does_not_close_injected_client():
    """调用方传进来的 client 由调用方负责生命周期, 服务层不得关它。"""
    client = _FakeClient(DAY_2026_11_20)
    svc.hk_connect_flow(client=client)
    assert client.closed is False


# ---------------------------------------------------------------------------
# API 层: GET /api/hk/connect-flow
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, monkeypatch):
    """最小 FastAPI app + **不打网络**的取数打桩。

    端点走 ``client=None`` 分支会自建 client, 所以直接替换服务模块里的
    ``EastmoneyClient`` 工厂。
    """
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = KlineRepository(DataStore(tmp_path))
    app.state.quote_service = None
    return TestClient(app), monkeypatch


def _patch_rows(monkeypatch, rows: list[dict] | Exception) -> _FakeClient:
    client = _FakeClient(rows)
    monkeypatch.setattr(svc, "EastmoneyClient", lambda: client)
    return client


def test_endpoint_shape_and_serialization(api):
    """响应形状 + ``date`` 已序列化成 ISO 串 (JSON 不能直接放 date)。"""
    client, monkeypatch = api
    _patch_rows(monkeypatch, DAY_2026_11_20 + DAY_2026_11_19)
    r = client.get("/api/hk/connect-flow", params={"start": "2026-11-01", "end": "2026-11-20"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 2
    assert d["trading_days"] == 2
    assert d["currency"] == "CNY"
    assert d["source"] == "eastmoney_datacenter_hsgt"
    assert d["market"] == "HK"
    assert d["start"] == "2026-11-01"
    assert d["end"] == "2026-11-20"
    assert d["latest_trade_date"] == "2026-11-20"
    assert d["latest_net_buy"] == 11811500000.0
    assert d["accum_net_inflow"] == 5534878670000.0
    assert d["index_close"] == pytest.approx(22410.8)
    # 逐行日期也必须是字符串
    assert all(isinstance(it["trade_date"], str) for it in d["items"])
    assert list(d["items"][0]) == list(svc.HK_CONNECT_FLOW_COLS)


def test_endpoint_default_window_is_90_days(api):
    """缺省窗口 = 今天−90 天 ~ 今天。"""
    client, monkeypatch = api
    fake = _patch_rows(monkeypatch, DAY_2026_11_20)
    r = client.get("/api/hk/connect-flow")
    assert r.status_code == 200
    flt = fake.calls[0]["flt"]
    today = dt.date.today()
    assert f"(TRADE_DATE<='{today.isoformat()}')" in flt
    assert f"(TRADE_DATE>='{(today - dt.timedelta(days=90)).isoformat()}')" in flt
    assert hk_api.HK_CONNECT_FLOW_DEFAULT_DAYS == 90


def test_endpoint_rejects_reversed_window(api):
    client, monkeypatch = api
    _patch_rows(monkeypatch, [])
    r = client.get(
        "/api/hk/connect-flow", params={"start": "2026-11-20", "end": "2026-11-01"}
    )
    assert r.status_code == 400
    assert "不能晚于" in r.json()["detail"]


def test_endpoint_rejects_overlong_window(api):
    """🔴 硬闸: 5 年以上窗口直接 400。

    上游 3 行/交易日, 再宽就会让一次 HTTP 请求拖到数秒并占满令牌桶。而全史累计已由
    ``accum_net_inflow`` 免费给出, 前端没有理由拉长窗口去自己加总。
    """
    client, monkeypatch = api
    _patch_rows(monkeypatch, [])
    r = client.get(
        "/api/hk/connect-flow", params={"start": "2015-01-01", "end": "2026-11-20"}
    )
    assert r.status_code == 400
    assert str(hk_api.HK_CONNECT_FLOW_MAX_WINDOW_DAYS) in r.json()["detail"]


def test_endpoint_accepts_window_at_the_limit(api):
    """刚好等于上限应当放行 (上限是「超过才拒」, 不是「达到就拒」)。"""
    client, monkeypatch = api
    _patch_rows(monkeypatch, [])
    end = dt.date(2026, 11, 20)
    start = end - dt.timedelta(days=hk_api.HK_CONNECT_FLOW_MAX_WINDOW_DAYS)
    r = client.get(
        "/api/hk/connect-flow", params={"start": start.isoformat(), "end": end.isoformat()}
    )
    assert r.status_code == 200


def test_endpoint_empty_window_is_200_with_null_summary(api):
    """整段假期 → 200 + ``total=0`` + 全 null 汇总, **不是** 502 也不是 404。"""
    client, monkeypatch = api
    _patch_rows(monkeypatch, [])
    r = client.get(
        "/api/hk/connect-flow", params={"start": "2026-12-25", "end": "2026-12-31"}
    )
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 0
    assert d["items"] == []
    assert d["latest_trade_date"] is None
    assert d["latest_net_buy"] is None
    assert d["accum_net_inflow"] is None
    assert d["net_buy_5d"] is None
    assert d["net_buy_20d"] is None


def test_endpoint_returns_502_on_upstream_failure(api):
    """🔴 上游失败 → 502, **不得**降级成空列表 (§4 fail closed)。"""
    client, monkeypatch = api
    _patch_rows(monkeypatch, EastmoneyError("9501 filter字段中日期参数格式错误"))
    r = client.get("/api/hk/connect-flow")
    assert r.status_code == 502
    assert "港股通资金流数据源不可用" in r.json()["detail"]


def test_endpoint_exposes_short_window_roll_sums_as_null(api):
    """窗口只有 2 天时 ``net_buy_5d``/``net_buy_20d`` 必须是 null, 不是部分和。"""
    client, monkeypatch = api
    _patch_rows(monkeypatch, DAY_2026_11_20 + DAY_2026_11_19)
    r = client.get(
        "/api/hk/connect-flow", params={"start": "2026-11-19", "end": "2026-11-20"}
    )
    d = r.json()
    assert d["total"] == 2
    assert d["net_buy_5d"] is None
    assert d["net_buy_20d"] is None


def test_endpoint_does_not_expose_hold_market_cap(api):
    """🔴 响应里不得出现持股市值字段。

    上游 ``HOLD_MARKET_CAP`` 口径无法定性 (两种解释相差整整 2 倍) 且无独立数据源
    可验证, 已在插件层排除。这条测试挡住「日后有人图省事把它加回来」。
    """
    client, monkeypatch = api
    _patch_rows(monkeypatch, DAY_2026_11_20)
    d = client.get("/api/hk/connect-flow").json()
    body = str(d)
    assert "hold_market_cap" not in body.lower()
    assert "HOLD_MARKET_CAP" not in body
    for item in d["items"]:
        assert "hold_market_cap" not in item
