"""TencentProvider 契约与单位标准化测试 (P1-12)。

不依赖真实网络: 用假 TencentClient 返回实测录制的样例快照/m1 分钟K,
验证字段映射 (含"收在第 3 列"的腾讯K线陷阱)、单位口径 (CONTRIBUTING §3.1:
百分数→小数、volume 手透传、amount 万元→元)、代码双向映射、五档解析、
维表股票池 (mtime 缓存/缺失告警)、批量分片软失败、GBK 解码与限速/熔断节奏。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.plugins.tencent import client as tc
from app.plugins.tencent import provider as tp
from app.plugins.tencent.provider import TencentProvider


# ---- 实测录制样例 (2026-11 冒烟, qt.gtimg.cn 快照 ~ 分隔字段) ----
def _snap_fields(**over) -> list[str]:
    """构造 52 字段快照行。over 用 f3/f37 等键覆盖指定索引。"""
    f = [""] * 52
    f[0] = "1"  # 市场类型 1=SH 51=SZ 62=BJ
    f[1] = "浦发银行"
    f[2] = "600000"
    f[3] = "10.02"  # 最新价
    f[4] = "9.99"  # 昨收
    f[5] = "9.98"  # 今开
    f[6] = "648865"  # 量(手)
    # 买一~买五: 9=价 10=量 交替
    for i, (p, v) in enumerate(
        [("10.01", 100), ("10.00", 200), ("9.99", 300), ("9.98", 400), ("9.97", 500)]
    ):
        f[9 + i * 2], f[10 + i * 2] = p, str(v)
    # 卖一~卖五: 19=价 20=量 交替
    for i, (p, v) in enumerate(
        [("10.02", 110), ("10.03", 210), ("10.04", 310), ("10.05", 410), ("10.06", 510)]
    ):
        f[19 + i * 2], f[20 + i * 2] = p, str(v)
    f[30] = "20261103150303"  # 时间 YYYYMMDDHHMMSS 北京墙钟
    f[31] = "0.03"  # 涨跌额
    f[32] = "0.30"  # 涨跌幅 %
    f[33] = "10.05"  # 高
    f[34] = "9.95"  # 低
    f[35] = "10.02/648865/647741483"  # 价/量(手)/额(元)
    f[36] = "648865"  # 量(手)
    f[37] = "64774.1483"  # 额(万元)
    f[38] = "0.22"  # 换手率 %
    f[43] = "1.00"  # 振幅 %
    f[44] = "2900.5"  # 流通市值(亿)
    f[45] = "2950.1"  # 总市值(亿)
    f[47] = "10.99"  # 涨停价
    f[48] = "8.99"  # 跌停价
    for k, v in over.items():
        f[int(k[1:])] = v
    return f


# m1 分钟K行(实测列序): [时间, 开, 收, 高, 低, 量(手), {}, ?] — 收在第 3 列!
_M1_ROWS = [
    ["202611030931", "10.00", "10.02", "10.03", "9.99", "1907.00", {}, "0.0573"],
    ["202611030932", "10.02", "10.01", "10.02", "10.00", "800.00", {}, "0.0240"],
]


class _FakeClient:
    """按预置数据应答, 记录调用供断言。与真实客户端同方法签名。"""

    def __init__(self, snapshots=None, m1=None, snap_error=None, m1_error=None):
        # snapshots: {tx_code: fields}; m1: {tx_code: rows}
        self.snapshots = snapshots or {}
        self.m1 = m1 or {}
        self.snap_error = snap_error
        self.m1_error = m1_error
        self.calls: list[tuple] = []

    def snapshot(self, codes):
        self.calls.append(("snapshot", tuple(codes)))
        if self.snap_error:
            raise self.snap_error
        return {c: self.snapshots[c] for c in codes if c in self.snapshots}

    def kline_m1(self, code, count=320):
        self.calls.append(("kline_m1", code, count))
        if self.m1_error:
            raise self.m1_error
        return list(self.m1.get(code, []))

    def close(self):
        pass


def _provider(fake: _FakeClient) -> TencentProvider:
    p = TencentProvider()
    p._client = fake
    return p


# ---- 代码双向映射 ----
@pytest.mark.parametrize(
    "symbol,tx_code",
    [("600000.SH", "sh600000"), ("000001.SZ", "sz000001"), ("920002.BJ", "bj920002"),
     ("000001.SH", "sh000001")],  # 指数同规则
)
def test_symbol_mapping_roundtrip(symbol, tx_code):
    assert tp.symbol_to_tx_code(symbol) == tx_code
    assert tp.tx_code_to_symbol(tx_code) == symbol


@pytest.mark.parametrize("bad", ["600000", "600000.US", "abc.SH", "", None])
def test_symbol_to_tx_code_rejects_bad(bad):
    assert tp.symbol_to_tx_code(bad) is None


# ---- 快照映射与单位口径 ----
def test_map_snapshot_units():
    """百分数→小数; amount 万元→元; volume 手透传; f30 北京墙钟→epoch 毫秒。"""
    rec = tp._map_snapshot_row("sh600000", _snap_fields(), fetched_ms=111)
    assert rec["symbol"] == "600000.SH"
    assert rec["name"] == "浦发银行"
    assert rec["last_price"] == 10.02
    assert rec["change_pct"] == pytest.approx(0.003)  # 0.30% → 0.003
    assert rec["amplitude"] == pytest.approx(0.01)  # 1.00% → 0.01
    assert rec["turnover_rate"] == pytest.approx(0.0022)
    assert rec["volume"] == 648865  # 手
    assert rec["amount"] == pytest.approx(647741483.0)  # 64774.1483 万元 → 元
    # 2026-11-03 15:03:03 北京 → epoch ms
    expected = int(datetime(2026, 11, 3, 15, 3, 3, tzinfo=tp._BJ_TZ).timestamp() * 1000)
    assert rec["timestamp"] == expected


def test_map_snapshot_suspended_kept_with_none_price():
    """停牌行最新价 0.00 → None 保留行 (与东财 "-" 同语义), 不伪造。"""
    rec = tp._map_snapshot_row("sz000001", _snap_fields(f3="0.00", f31="", f32=""), fetched_ms=1)
    assert rec is not None
    assert rec["last_price"] is None
    assert rec["change_pct"] is None  # 无法推导 (last 为 None)


def test_map_snapshot_derives_missing_change():
    """f31/f32 缺失 → 由 last-prev 推导 (与 quote_service 同口径, 小数制)。"""
    rec = tp._map_snapshot_row("sh600000", _snap_fields(f31="", f32=""), fetched_ms=1)
    assert rec["change_amount"] == pytest.approx(0.03)
    assert rec["change_pct"] == pytest.approx(0.03 / 9.99)


def test_map_snapshot_amount_falls_back_to_composite():
    """f37 缺失 → 退回 f35 组合串第 3 段 (精确元值)。"""
    rec = tp._map_snapshot_row("sh600000", _snap_fields(f37=""), fetched_ms=1)
    assert rec["amount"] == pytest.approx(647741483.0)


def test_map_snapshot_bad_time_falls_back_to_fetched():
    rec = tp._map_snapshot_row("sh600000", _snap_fields(f30=""), fetched_ms=12345)
    assert rec["timestamp"] == 12345


def test_map_snapshot_unknown_code_dropped():
    assert tp._map_snapshot_row("pv_none_match", _snap_fields(), 1) is None


# ---- 五档盘口 ----
def test_depth_entry_mapping():
    e = tp._depth_entry(_snap_fields(), fallback_ms=7)
    assert e["bid_prices"] == [10.01, 10.00, 9.99, 9.98, 9.97]
    assert e["bid_volumes"] == [100, 200, 300, 400, 500]  # 手, int
    assert e["ask_prices"] == [10.02, 10.03, 10.04, 10.05, 10.06]
    assert e["ask_volumes"] == [110, 210, 310, 410, 510]
    assert e["timestamp"] > 7


def test_depth_entry_zero_prices_skipped():
    """指数/停牌五档恒 0 → None 跳过, 不伪造盘口。"""
    f = _snap_fields()
    for i in list(range(9, 19)):
        f[i] = "0.00" if i % 2 == 1 else "0"
    assert tp._depth_entry(f, fallback_ms=1) is None


# ---- m1 分钟K行映射 (列序陷阱) ----
def test_map_m1_row_column_order():
    """收在第 3 列 (idx2), 高/低在 idx3/idx4 — 腾讯K线通用陷阱。"""
    rec = tp._map_m1_row("600000.SH", _M1_ROWS[0])
    assert rec == {
        "symbol": "600000.SH",
        "datetime": datetime(2026, 11, 3, 9, 31),  # naive 北京墙钟 (§3.3)
        "open": 10.00,
        "close": 10.02,  # idx2 = 收
        "high": 10.03,  # idx3 = 高
        "low": 9.99,  # idx4 = 低
        "volume": 1907.0,  # 手
    }
    assert "amount" not in rec  # 无可靠成交额, 不伪造


@pytest.mark.parametrize(
    "row",
    [None, [], ["202611030931"], ["bad_time", "1", "1", "1", "1", "1"],
     ["202611030931", "", "10.02", "10.03", "9.99", "100"]],
)
def test_map_m1_row_bad_rows(row):
    assert tp._map_m1_row("600000.SH", row) is None


# ---- 维表股票池 ----
def test_universe_reads_parquet_with_mtime_cache(tmp_path, monkeypatch):
    import os

    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    idir = tmp_path / "instruments"
    idir.mkdir()
    path = idir / "instruments.parquet"
    pl.DataFrame({"symbol": ["600000.SH", "000001.SZ", "BAD"]}).write_parquet(path)
    p = TencentProvider()
    uni = p._universe()
    assert uni == ["600000.SH", "000001.SZ"]  # 无法映射的剔除
    # 重写内容但恢复 mtime → 缓存命中, 不重读
    mtime = path.stat().st_mtime
    pl.DataFrame({"symbol": ["300750.SZ"]}).write_parquet(path)
    os.utime(path, (mtime, mtime))
    assert p._universe() == uni
    # mtime 变化 → 重读拿到新内容
    os.utime(path, (mtime + 10, mtime + 10))
    assert p._universe() == ["300750.SZ"]


def test_universe_missing_parquet_returns_empty(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert TencentProvider()._universe() == []  # 显式空 + 告警, 不伪造


# ---- get_realtime ----
def test_get_realtime_maps_and_chunks(tmp_path, monkeypatch):
    """股票池分片请求 (SNAPSHOT_BATCH/批), 映射为内部 record。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    idir = tmp_path / "instruments"
    idir.mkdir()
    pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"]}).write_parquet(
        idir / "instruments.parquet"
    )
    fake = _FakeClient(snapshots={
        "sh600000": _snap_fields(),
        "sz000001": _snap_fields(f1="平安银行", f2="000001"),
    })
    recs = _provider(fake).get_realtime()
    assert {r["symbol"] for r in recs} == {"600000.SH", "000001.SZ"}
    assert fake.calls[0][0] == "snapshot"


def test_get_realtime_empty_universe_no_request(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    fake = _FakeClient()
    assert _provider(fake).get_realtime() == []
    assert fake.calls == []  # 维表缺失不发请求


def test_get_realtime_partial_batch_failure_keeps_rest(tmp_path, monkeypatch):
    """单批失败只丢该批: 用超过一批的股票池验证 (第 1 批抛错, 第 2 批正常)。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    idir = tmp_path / "instruments"
    idir.mkdir()
    syms = [f"{600000 + i}.SH" for i in range(tc.SNAPSHOT_BATCH + 1)]
    pl.DataFrame({"symbol": syms}).write_parquet(idir / "instruments.parquet")

    class _PartialFake(_FakeClient):
        def snapshot(self, codes):
            self.calls.append(("snapshot", len(codes)))
            if len(codes) == tc.SNAPSHOT_BATCH:  # 第一批 (满批) 抛错
                raise tc.TencentError("boom")
            return {c: _snap_fields() for c in codes}

    fake = _PartialFake()
    recs = _provider(fake).get_realtime()
    assert len(recs) == 1  # 只剩第二批的 1 只
    assert len(fake.calls) == 2


def test_get_realtime_all_batches_fail_returns_empty(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    idir = tmp_path / "instruments"
    idir.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(idir / "instruments.parquet")
    fake = _FakeClient(snap_error=tc.TencentError("down"))
    assert _provider(fake).get_realtime() == []  # 软失败, 不抛


# ---- get_realtime_indices ----
def test_get_realtime_indices_maps():
    fake = _FakeClient(snapshots={"sh000001": _snap_fields(f1="上证指数", f2="000001")})
    recs = _provider(fake).get_realtime_indices(["000001.SH"])
    assert recs and recs[0]["symbol"] == "000001.SH"


def test_get_realtime_indices_none_on_error():
    """None = 本轮失败 (上游沿用缓存); [] = 无有效代码。"""
    fake = _FakeClient(snap_error=tc.TencentError("down"))
    assert _provider(fake).get_realtime_indices(["000001.SH"]) is None
    assert _provider(_FakeClient()).get_realtime_indices([]) == []


# ---- get_depth_batch ----
def test_get_depth_batch_mapping():
    fake = _FakeClient(snapshots={"sh600000": _snap_fields()})
    out = _provider(fake).get_depth_batch(["600000.SH", "000001.SZ"])
    assert set(out) == {"600000.SH"}  # 无数据的标的跳过
    assert out["600000.SH"]["bid_prices"][0] == 10.01


def test_get_depth_batch_error_continues():
    fake = _FakeClient(snap_error=tc.TencentError("down"))
    assert _provider(fake).get_depth_batch(["600000.SH"]) == {}  # 软失败


# ---- get_minute ----
def test_get_minute_maps_and_filters_window():
    fake = _FakeClient(m1={"sh600000": _M1_ROWS})
    df = _provider(fake).get_minute(
        ["600000.SH"],
        datetime(2026, 11, 3, 9, 31, 30),  # 只保留 09:32 那根
        datetime(2026, 11, 3, 15, 0),
    )
    assert df.height == 1
    assert df.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume"]
    assert df.row(0)[df.columns.index("close")] == 10.01


def test_get_minute_progress_and_per_symbol_soft_fail():
    """单标的失败只丢该标的; on_chunk_done 每标的回调一次。"""
    class _HalfFake(_FakeClient):
        def kline_m1(self, code, count=320):
            self.calls.append(("kline_m1", code))
            if code == "sz000001":
                raise tc.TencentError("boom")
            return list(_M1_ROWS)

    fake = _HalfFake()
    progress = []
    df = _provider(fake).get_minute(
        ["600000.SH", "000001.SZ"], None, None,
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    )
    assert df.height == 2  # 只剩 600000 的两根
    assert progress == [(1, 2), (2, 2)]


def test_get_minute_rejects_other_freq_and_asset():
    fake = _FakeClient(m1={"sh600000": _M1_ROWS})
    p = _provider(fake)
    assert p.get_minute(["600000.SH"], None, None, freq="5m").height == 0
    assert p.get_minute(["600000.SH"], None, None, asset_type="etf").height == 0
    assert fake.calls == []  # 不发请求


def test_get_minute_empty_symbols():
    assert _provider(_FakeClient()).get_minute([], None, None).height == 0


# ---- test_dataset ----
def test_test_dataset_realtime_and_unknown():
    fake = _FakeClient(snapshots={"sh600000": _snap_fields()})
    p = _provider(fake)
    out = p.test_dataset("realtime", ["600000.SH"])
    assert out["rows"] == 1 and out["preview"][0]["symbol"] == "600000.SH"
    unknown = p.test_dataset("daily")
    assert unknown["rows"] == 0 and "回退" in unknown["error"]


def test_test_dataset_minute_json_serializable():
    import json

    # test_dataset 的分钟窗口是 now-1d→now, 样例行须用近期时间才不被窗口过滤
    base = (datetime.now() - timedelta(minutes=5)).replace(second=0, microsecond=0)
    rows = [
        [base.strftime("%Y%m%d%H%M"), "10.00", "10.02", "10.03", "9.99", "100.00", {}, "0"],
        [(base + timedelta(minutes=1)).strftime("%Y%m%d%H%M"),
         "10.02", "10.01", "10.03", "10.00", "80.00", {}, "0"],
    ]
    fake = _FakeClient(m1={"sh600000": rows})
    out = _provider(fake).test_dataset("minute", ["600000.SH"])
    json.dumps(out)  # datetime 已转 ISO, 不抛即通过
    assert out["rows"] == 2


def test_test_dataset_depth5():
    fake = _FakeClient(snapshots={"sh600000": _snap_fields()})
    out = _provider(fake).test_dataset("depth5", ["600000.SH"])
    assert out["rows"] == 1
    assert "bid_prices" in out["preview"]["600000.SH"]


# ---- client 层: GBK 解码/批量上限/信封/限速/熔断 ----
class _VirtualTime:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _FakeResponse:
    def __init__(self, status=200, content=b"", text=None):
        self.status_code = status
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", errors="replace")


class _FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[tuple] = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _client_with(responses, vt) -> tuple[tc.TencentClient, _FakeHttp]:
    fake_http = _FakeHttp(responses)
    c = tc.TencentClient(
        qps=1000.0, jitter_s=(0.0, 0.0), clock=vt.now, sleeper=vt.sleep, http=fake_http
    )
    return c, fake_http


def test_snapshot_gbk_decode_and_parse():
    """GBK 字节流显式解码; v_pv_none_match 占位行 (字段不足) 被剔除。"""
    text = 'v_sh600000="1~浦发银行~600000~10.02~' + "~".join(["0"] * 48) + '";\nv_pv_none_match="1";'
    vt = _VirtualTime()
    c, http = _client_with([_FakeResponse(content=text.encode("gbk"))], vt)
    out = c.snapshot(["sh600000", "sh999999"])
    assert "sh600000" in out and out["sh600000"][1] == "浦发银行"
    assert "pv_none_match" not in out
    assert http.requests[0][1] == {"q": "sh600000,sh999999"}


def test_snapshot_batch_limit_raises():
    vt = _VirtualTime()
    c, _ = _client_with([], vt)
    with pytest.raises(tc.TencentError, match="分批"):
        c.snapshot([f"sh{600000 + i}" for i in range(tc.SNAPSHOT_BATCH + 1)])


def test_snapshot_empty_codes_no_request():
    vt = _VirtualTime()
    c, http = _client_with([], vt)
    assert c.snapshot([]) == {}
    assert http.requests == []


def test_kline_m1_envelope_and_cap():
    payload = {"code": 0, "data": {"sh600000": {"m1": _M1_ROWS}}}
    import json as _json

    vt = _VirtualTime()
    c, http = _client_with(
        [_FakeResponse(content=_json.dumps(payload).encode("utf-8"))], vt
    )
    rows = c.kline_m1("sh600000", count=1400)  # 请求超上限被钳到 320
    assert rows == _M1_ROWS
    assert http.requests[0][1]["param"] == f"sh600000,m1,,{tc.MKLINE_M1_MAX}"


def test_kline_m1_bad_code_raises():
    import json as _json

    vt = _VirtualTime()
    payload = {"code": 1, "msg": "param error"}
    c, _ = _client_with([_FakeResponse(content=_json.dumps(payload).encode())], vt)
    with pytest.raises(tc.TencentError, match="code=1"):
        c.kline_m1("bad")


def test_get_text_retries_empty_body_then_succeeds():
    """空响应体 (限流典型症状) → 退避重试, 第三次成功。"""
    vt = _VirtualTime()
    c, http = _client_with(
        [_FakeResponse(content=b"  "), _FakeResponse(content=b""),
         _FakeResponse(content=b'{"code":0,"data":{}}')],
        vt,
    )
    out = c._get_text(tc.IFZQ_BASE, "/x", {})
    assert "code" in out
    assert len(http.requests) == 3
    assert vt.t > 0  # 退避确实等待过


def test_get_text_4xx_no_retry():
    vt = _VirtualTime()
    c, http = _client_with([_FakeResponse(status=404, content=b"nf")], vt)
    with pytest.raises(tc.TencentError, match="404"):
        c._get_text(tc.QT_BASE, "/x", {})
    assert len(http.requests) == 1


def test_token_bucket_paces_requests():
    """容量 2、速率 2/s: 前 2 次立即通过, 后 2 次各等 0.5s。"""
    vt = _VirtualTime()
    bucket = tc._TokenBucket(rate=2.0, capacity=2, clock=vt.now, sleeper=vt.sleep)
    for _ in range(4):
        bucket.acquire()
    assert vt.t == pytest.approx(1.0, abs=0.01)


def test_breaker_opens_and_recovers():
    vt = _VirtualTime()
    b = tc._Breaker(fail_threshold=3, cooldown_s=100.0, clock=vt.now)
    b.on_failure()
    b.on_failure()
    b.check()  # 未到阈值不抛
    b.on_failure()  # 第 3 次 → 熔断
    with pytest.raises(tc.TencentError, match="熔断"):
        b.check()
    vt.sleep(101.0)
    b.check()  # 冷却期满自动放行


def test_breaker_success_resets_streak():
    vt = _VirtualTime()
    b = tc._Breaker(fail_threshold=3, cooldown_s=100.0, clock=vt.now)
    b.on_failure()
    b.on_failure()
    b.on_success()  # 连续计数清零
    b.on_failure()
    b.on_failure()
    b.check()  # 仍不熔断


# ---- 插件注册 (loader 隔离) ----
def test_plugin_yaml_loadable():
    """plugin.yaml 可被 loader 解析, datasets 与 provider 声明一致。"""
    import yaml

    meta = yaml.safe_load(
        (Path(tp.__file__).parent / "plugin.yaml").read_text(encoding="utf-8")
    )
    assert meta["name"] == "tencent"
    assert meta["runtime"] == "python"
    assert set(k for k, v in meta["datasets"].items() if v) == set(tp._DATASETS)
    assert meta["api_key_env"] is None  # 免 Key


def test_availability_always_true():
    ok, msg = tp.availability()
    assert ok and msg


# ---- P1-13 第三批: 分时逐笔 (stock.gtimg.cn detail 分页) ----

def test_tick_trades_tail_pages():
    """bigdata 索引段数 = 总页数; 取尾部 pages 页拼接返回原始 tick 串。"""
    vt = _VirtualTime()
    idx = 'v_detail_time_sz000651=[20261030,"09:25:00~09:33:24|09:33:27~09:36:54|09:36:57~09:40:21"];'
    p1 = 'v_detail_data_sz000651=[1,"70/09:36:57/38.04/0.01/58/220586/B|71/09:37:00/38.03/-0.01/150/570527/S"];'
    p2 = 'v_detail_data_sz000651=[2,"140/09:40:18/38.05/0.02/30/114150/M"];'
    c, http = _client_with(
        [_FakeResponse(content=idx.encode("gbk")),
         _FakeResponse(content=p1.encode("gbk")),
         _FakeResponse(content=p2.encode("gbk"))], vt)
    ticks = c.tick_trades("sz000651", pages=2)
    assert len(ticks) == 3 and ticks[0].startswith("70/") and ticks[-1].endswith("/M")
    # 请求序列: 先 bigdata 索引定位总页数(3), 再拉尾部 2 页(p=1,2)
    assert http.requests[0][1]["action"] == "bigdata"
    assert [r[1]["p"] for r in http.requests[1:]] == [1, 2]
    assert all(r[1]["c"] == "sz000651" for r in http.requests)
    assert all("stock.gtimg.cn" in r[0] for r in http.requests)


def test_tick_trades_empty_index_returns_empty():
    """索引为空(如新股尚无逐笔) → 返回 [], 不再发数据页请求。"""
    vt = _VirtualTime()
    c, http = _client_with([_FakeResponse(content=b"v_detail_time_sz000651=[];")], vt)
    assert c.tick_trades("sz000651") == []
    assert len(http.requests) == 1


def test_tick_trades_pages_capped_at_total():
    """总页数不足请求页数时从第 0 页拉起 — 不能越界(越界页返回空响应体会被当限流重试)。"""
    vt = _VirtualTime()
    idx = 'v_detail_time_sz000651=[20261030,"09:25:00~09:33:24"];'
    p0 = 'v_detail_data_sz000651=[0,"0/09:25:00/37.52/-0.02/1191/4467206/S"];'
    c, http = _client_with(
        [_FakeResponse(content=idx.encode("gbk")), _FakeResponse(content=p0.encode("gbk"))], vt)
    ticks = c.tick_trades("sz000651", pages=3)
    assert len(ticks) == 1
    assert [r[1].get("p") for r in http.requests[1:]] == [0]
