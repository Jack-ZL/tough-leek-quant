"""腾讯港股快照归一化测试 (方案 §7 / PR3 剩余项)。

不依赖真实网络: 用 2026-11-13 实测录制的 ``hk00700`` / ``hk09988`` / ``hkHSI``
三行 78 字段快照做样例, 验证:

- 代码双向映射与 fail-closed 过滤 (非港股 / 非 5 位 / 窝轮牛熊证 / 指数);
- 🔴 港股与 A 股**字段布局不同**的三处单位陷阱: volume 是股不是手、
  amount 是元不是万元、时间戳是 ``YYYY/MM/DD HH:MM:SS`` 不是 14 位数字串;
- 🔴 换手率在字段 59 而不是 A 股的 38 (港股 38 恒 0);
- 输出键与 ``eastmoney.hk.map_hk_spot_row`` **逐键相同** (两源可互换的前提);
- 停牌/指数/缺价行丢弃而不伪造;
- ``fetch_hk_spot`` 的分批、单批软失败、**全批失败必须抛错** (不返回 ``[]``)。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from app.plugins.eastmoney import hk as em_hk
from app.plugins.tencent import hk_spot
from app.plugins.tencent.client import SNAPSHOT_BATCH, TencentError

# ---- 实测录制样例 (2026-11-13 收盘后, qt.gtimg.cn, ~ 分隔, 港股 78 字段) ----
#: 腾讯控股。字段 9-28 (五档) 恒 0 —— 港股该端点无盘口。
HK_00700 = [
    "100", "腾讯控股", "00700", "386.600", "398.800", "393.000", "31493353.0",
    "0", "0",
    "386.600", "0", "0", "0", "0", "0", "0", "0", "0", "0",
    "386.600", "0", "0", "0", "0", "0", "0", "0", "0", "0",
    "31493353.0", "2026/11/13 16:08:22", "-12.200", "-3.06", "395.200", "385.600",
    "386.600", "31493353.0", "12251162809.622", "0", "14.12", "", "0", "0", "2.41",
    "35152.1649", "35152.1649", "TENCENT", "1.37", "653.700", "375.200", "1.45",
    "8.41", "0", "0", "0", "0", "0", "12.96", "2.70", "0.35", "100", "-34.88",
    "-7.91", "GP", "20.41", "11.00", "0.78", "-7.47", "-13.55", "9092644829.00",
    "9092644829.00", "13.38", "5.315", "389.008", "-27.43", "HKD", "1", "50",
]

#: 阿里巴巴-W (人民币柜台代码 09988, 港币柜台)。用于交叉验证换手率字段 59。
HK_09988 = [
    "100", "阿里巴巴-W", "09988", "90.550", "93.800", "93.350", "158411438.0",
    "0", "0",
    "90.550", "0", "0", "0", "0", "0", "0", "0", "0", "0",
    "90.550", "0", "0", "0", "0", "0", "0", "0", "0", "0",
    "158411438.0", "2026/11/13 16:08:24", "-3.250", "-3.46", "94.450", "89.600",
    "90.550", "158411438.0", "14487493899.130", "0", "15.36", "", "0", "0", "5.17",
    "18021.2243", "18021.2243", "BABA-W", "1.10", "173.173", "88.650", "1.52",
    "-63.21", "0", "0", "0", "0", "0", "21.35", "1.40", "0.80", "100", "-36.13",
    "-13.68", "GP", "7.04", "3.60", "-6.70", "-14.90", "-27.09", "19901959514.00",
    "19901959514.00", "36.87", "0.992", "91.455", "-40.10", "HKD", "1", "50",
]

#: 恒生指数。字段 2 是 ``HSI`` (非 5 位数字), 字段 63 是 ``ZS`` —— 双重信号都应拒收。
HK_HSI = [
    "100", "恒生指数", "HSI", "23637.600", "24008.550", "23871.910", "19585700.7140",
    "0", "0",
    "23637.600", "0", "0", "0", "0", "0", "0", "0", "0", "0",
    "23637.600", "0", "0", "0", "0", "0", "0", "0", "0", "0",
    "0.0", "2026/11/13 18:31:15", "-370.950", "-1.55", "23930.130", "23595.360",
    "23637.600", "19585700.7140", "19585700.714", "0", "0", "", "0", "0", "1.39",
    "0", "0", "Hang Seng Index", "0", "28056.100", "22518.000", "1.06",
    "-18.52", "0", "0", "0", "0", "0", "0.00", "0.00", "0.00", "0", "-7.78",
    "-3.45", "ZS", "", "", "1.47", "-1.77", "-7.29", "0.00",
    "0.00", "0.00", "0.000", "", "-12.72", "HKD", "1", "",
]

#: 2026-11-13 16:08:22 HKT (= 08:08:22 UTC) 的 epoch 毫秒。
TS_00700_MS = 1794557302000
FETCHED_MS = 1700000000000


def _fields(base: list[str], **over) -> list[str]:
    """复制录制行并按索引覆盖。``over`` 用 ``f3="0.000"`` 形式指定。"""
    out = list(base)
    for key, value in over.items():
        assert key.startswith("f"), key
        out[int(key[1:])] = value
    return out


class _FakeClient:
    """记录 snapshot 调用的假客户端。

    ``rows``: {腾讯代码: 字段列表}, 只返回**被请求到**的代码 (与真实端点一致)。
    ``extra``: 无论请求什么都混进响应的行 —— 用于模拟上游污染
        (同端点会混返指数行, 新浪港股指数链路也有同款问题)。
    ``errors``: {批次序号: 异常}, 该批 ``snapshot`` 抛错。
    """

    def __init__(
        self,
        rows: dict[str, list[str]] | None = None,
        *,
        extra: dict[str, list[str]] | None = None,
        errors: dict[int, Exception] | None = None,
    ):
        self.rows = rows or {}
        self.extra = extra or {}
        self.errors = errors or {}
        self.chunks: list[list[str]] = []

    def snapshot(self, codes: list[str]) -> dict[str, list[str]]:
        idx = len(self.chunks)
        self.chunks.append(list(codes))
        if idx in self.errors:
            raise self.errors[idx]
        out = {c: self.rows[c] for c in codes if c in self.rows}
        out.update(self.extra)
        return out


# ---------------------------------------------------------------------------
# 代码双向映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,expected", [
    ("00700.HK", "hk00700"),
    ("09988.HK", "hk09988"),
    ("89988.HK", "hk89988"),  # 人民币柜台 (8 开头) 也是股票, 收
    ("08001.HK", "hk08001"),  # GEM (08xxx) 收
])
def test_hk_spot_code_valid(symbol, expected):
    assert hk_spot.hk_spot_code(symbol) == expected


@pytest.mark.parametrize("symbol", [
    "600000.SH",   # 非港股后缀
    "000001.SZ",
    "700.HK",      # 🔴 非 5 位: 放过会拼出 hk700, 可能解析到另一只标的
    "000700.HK",   # 6 位
    "00700",       # 无后缀
    "",
    None,
    "10000.HK",    # 窝轮 (1xxxx)
    "60000.HK",    # 牛熊证 (6xxxx)
])
def test_hk_spot_code_rejects(symbol):
    assert hk_spot.hk_spot_code(symbol) is None


def test_hk_spot_code_normalizes_case():
    assert hk_spot.hk_spot_code("00700.hk") == "hk00700"
    assert hk_spot.hk_spot_code("  00700.HK  ") == "hk00700"


@pytest.mark.parametrize("tx_code,expected", [
    ("hk00700", "00700.HK"),
    ("HK00700", "00700.HK"),
    ("hk89988", "89988.HK"),
])
def test_hk_spot_symbol_valid(tx_code, expected):
    assert hk_spot.hk_spot_symbol(tx_code) == expected


@pytest.mark.parametrize("tx_code", [
    "hkHSI",       # 🔴 指数: 代码段非数字 → 拒收 (指数走新浪链路)
    "hkhsi",
    "sh600000",    # A 股前缀
    "sz000001",
    "hk700",       # 非 5 位
    "hk000700",    # 6 位
    "hk10000",     # 窝轮
    "00700",       # 无前缀
    "",
    None,
])
def test_hk_spot_symbol_rejects(tx_code):
    assert hk_spot.hk_spot_symbol(tx_code) is None


@pytest.mark.parametrize("symbol", ["00700.HK", "09988.HK", "89988.HK", "08001.HK"])
def test_code_symbol_roundtrip(symbol):
    assert hk_spot.hk_spot_symbol(hk_spot.hk_spot_code(symbol)) == symbol


# ---------------------------------------------------------------------------
# 🔴 时间戳格式 (港股与 A 股不同)
# ---------------------------------------------------------------------------

def test_timestamp_parses_slash_format_from_server():
    """实测港股字段 30 用斜杠分隔 (ord('/')==47 逐字节确认过)。"""
    raw = HK_00700[30]
    assert raw[4] == "/" and raw[7] == "/", "录制样例的分隔符应仍是斜杠"
    assert hk_spot.hk_timestamp_ms(raw, FETCHED_MS) == TS_00700_MS


def test_timestamp_also_accepts_dash_format():
    """上游偶有混用横杠写法 → 两种都解析, 而不是只认一种后静默退回。"""
    assert hk_spot.hk_timestamp_ms("2026-11-13 16:08:22", FETCHED_MS) == TS_00700_MS


def test_timestamp_rejects_a_share_format_and_falls_back():
    """🔴 A 股的 14 位数字串在港股行里解析不了 → 退回拉取时刻, 不抛错。"""
    assert hk_spot.hk_timestamp_ms("20261113161444", FETCHED_MS) == FETCHED_MS


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-time", "2026-13-45 99:99:99"])
def test_timestamp_falls_back_on_garbage(raw):
    assert hk_spot.hk_timestamp_ms(raw, FETCHED_MS) == FETCHED_MS


def test_timestamp_is_hkt_wall_clock():
    """16:08:22 HKT == 08:08:22 UTC (UTC+8), 钉住时区没被当成本地时区。"""
    ms = hk_spot.hk_timestamp_ms("2026/11/13 16:08:22", FETCHED_MS)
    utc = datetime.fromtimestamp(ms / 1000, tz=UTC)
    assert (utc.year, utc.month, utc.day, utc.hour, utc.minute) == (2026, 11, 13, 8, 8)


# ---------------------------------------------------------------------------
# 行映射: 值与单位口径
# ---------------------------------------------------------------------------

def test_map_produces_same_keys_as_eastmoney_hk_spot():
    """🔴 两个港股实时源必须可互换 → 输出键逐键相同 (quote_service 契约)。"""
    tx_keys = set(hk_spot.map_hk_spot_fields("hk00700", HK_00700, FETCHED_MS))
    em_keys = set(em_hk.map_hk_spot_row(
        {"f12": "00700", "f2": "386.6", "f18": "398.8"}, FETCHED_MS,
    ))
    assert tx_keys == em_keys


def test_map_values():
    rec = hk_spot.map_hk_spot_fields("hk00700", HK_00700, FETCHED_MS)
    assert rec["symbol"] == "00700.HK"
    assert rec["name"] == "腾讯控股"
    assert rec["last_price"] == 386.6
    assert rec["prev_close"] == 398.8
    assert rec["open"] == 393.0
    assert rec["high"] == 395.2
    assert rec["low"] == 385.6
    assert rec["change_amount"] == -12.2
    assert rec["timestamp"] == TS_00700_MS


def test_map_volume_is_shares_not_lots():
    """🔴 港股字段 36 是**股**; A 股同索引是手。乘 100 会让成交额/换手差两个量级。

    交叉验证: amount / volume = 12251162809.622 / 31493353 ≈ 389.0 HKD,
    落在当日 385.6~395.2 区间内 → 量确实是股。若当成手, 均价会变成 3.89。
    """
    rec = hk_spot.map_hk_spot_fields("hk00700", HK_00700, FETCHED_MS)
    assert rec["volume"] == 31493353.0
    assert rec["amount"] == 12251162809.622
    vwap = rec["amount"] / rec["volume"]
    assert rec["low"] <= vwap <= rec["high"]


def test_map_amount_is_yuan_not_wan():
    """🔴 港股字段 37 已是**元 (HKD)**; A 股同索引是万元需 ×10000。"""
    rec = hk_spot.map_hk_spot_fields("hk09988", HK_09988, FETCHED_MS)
    assert rec["amount"] == 14487493899.130
    # 若误按 A 股口径 ×10000, 阿里单日成交额会变成 144 万亿 HKD (港股全年都没这么多)
    assert rec["amount"] < 1e12


def test_map_percent_fields_are_fractions():
    """CONTRIBUTING §3.1: 源给百分数 → 内部产出小数。"""
    rec = hk_spot.map_hk_spot_fields("hk00700", HK_00700, FETCHED_MS)
    assert rec["change_pct"] == pytest.approx(-0.0306)
    assert rec["amplitude"] == pytest.approx(0.0241)
    assert rec["turnover_rate"] == pytest.approx(0.0035)
    # 振幅自洽: (高-低)/昨收
    assert rec["amplitude"] == pytest.approx((395.2 - 385.6) / 398.8, abs=1e-4)
    # 涨跌幅自洽: 涨跌额/昨收
    assert rec["change_pct"] == pytest.approx(rec["change_amount"] / 398.8, abs=1e-4)


def test_map_turnover_uses_field_59_not_38():
    """🔴 港股换手率在字段 59; 字段 38 恒 0 (A 股才在 38)。

    交叉验证两只标的: 成交量 / 字段 69 (总股本) ≈ 字段 59 / 100。
    """
    for code, row in (("hk00700", HK_00700), ("hk09988", HK_09988)):
        assert row[38] == "0", f"{code} 的字段 38 应恒为 0"
        rec = hk_spot.map_hk_spot_fields(code, row, FETCHED_MS)
        expected = float(row[36]) / float(row[69])
        assert rec["turnover_rate"] == pytest.approx(expected, abs=1e-4)


def test_map_uses_chinese_name_not_english():
    """字段 1 是中文名, 字段 46 是英文名 (TENCENT) —— 取前者。"""
    rec = hk_spot.map_hk_spot_fields("hk00700", HK_00700, FETCHED_MS)
    assert rec["name"] == "腾讯控股"
    assert rec["name"] != HK_00700[46]


# ---------------------------------------------------------------------------
# 行映射: fail-closed 拒收
# ---------------------------------------------------------------------------

def test_map_rejects_index_row():
    """指数行双重拒收: 代码段非数字 + 字段 63 是 ZS。指数走新浪链路, 不做双份真相。"""
    assert hk_spot.map_hk_spot_fields("hkHSI", HK_HSI, FETCHED_MS) is None


def test_map_rejects_non_equity_kind_even_with_numeric_code():
    """字段 63 不是 GP → 丢弃 (即使代码看起来像股票)。"""
    row = _fields(HK_00700, f63="ZS")
    assert hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS) is None


def test_map_tolerates_missing_kind_field():
    """字段 63 越界/为空时不按类别拒收 —— 类别只是二次信号, 代码校验才是主闸门。"""
    row = HK_00700[:63]  # 截断到不含字段 63
    assert hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS) is not None


@pytest.mark.parametrize("last", ["0.000", "0", "", None])
def test_map_drops_suspended_or_priceless_row(last):
    """停牌/收盘清算占位价为 0 → **整行丢弃** (同 sina.hk_index 的处理)。

    保留缺价行会让下游 change_pct 补算除零, 并污染自选的空态判定。
    """
    row = _fields(HK_00700, f3=last if last is not None else "")
    if last is None:
        row = HK_00700[:3]  # 字段 3 越界
    assert hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS) is None


@pytest.mark.parametrize("tx_code", ["hkHSI", "sh600000", "hk10000", "hk700"])
def test_map_rejects_unmappable_code(tx_code):
    assert hk_spot.map_hk_spot_fields(tx_code, HK_00700, FETCHED_MS) is None


def test_map_derives_change_when_fields_missing():
    """字段 31/32 缺失 → 从最新价与昨收推导, 口径与 quote_service 一致 (小数)。"""
    row = _fields(HK_00700, f31="", f32="")
    rec = hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS)
    assert rec["change_amount"] == pytest.approx(386.6 - 398.8)
    assert rec["change_pct"] == pytest.approx((386.6 - 398.8) / 398.8)


def test_map_derives_change_amount_only_when_pct_present():
    row = _fields(HK_00700, f31="")
    rec = hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS)
    assert rec["change_amount"] == pytest.approx(-12.2)
    assert rec["change_pct"] == pytest.approx(-0.0306)


def test_map_falls_back_to_fetched_ms():
    row = _fields(HK_00700, f30="")
    assert hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS)["timestamp"] == FETCHED_MS


def test_map_blank_name_becomes_none():
    row = _fields(HK_00700, f1="   ")
    assert hk_spot.map_hk_spot_fields("hk00700", row, FETCHED_MS)["name"] is None


def test_map_truncated_row_does_not_raise():
    """上游可能截尾 (指数/停牌行实测缺尾部字段) → 越界取 None, 不 IndexError。"""
    rec = hk_spot.map_hk_spot_fields("hk00700", HK_00700[:44], FETCHED_MS)
    assert rec["last_price"] == 386.6
    assert rec["turnover_rate"] is None  # 字段 59 越界


# ---------------------------------------------------------------------------
# fetch_hk_spot: 分批 / 软失败 / 硬失败
# ---------------------------------------------------------------------------

def test_fetch_maps_and_returns_records():
    client = _FakeClient({"hk00700": HK_00700, "hk09988": HK_09988})
    recs = hk_spot.fetch_hk_spot(client, ["00700.HK", "09988.HK"])
    assert {r["symbol"] for r in recs} == {"00700.HK", "09988.HK"}
    assert client.chunks == [["hk00700", "hk09988"]]


def test_fetch_dedupes_and_sorts_symbols():
    """入参去重 + 排序 → 请求 URL 稳定, 便于测试与缓存键复现。"""
    client = _FakeClient({"hk09988": HK_09988, "hk00700": HK_00700})
    hk_spot.fetch_hk_spot(client, ["09988.HK", "00700.HK", "09988.HK"])
    assert client.chunks == [["hk00700", "hk09988"]]


def test_fetch_raises_on_empty_symbols():
    with pytest.raises(TencentError, match="为空"):
        hk_spot.fetch_hk_spot(_FakeClient(), [])


def test_fetch_raises_when_all_symbols_unmappable():
    """🔴 全部无法映射 → 抛错, 不静默返回 [] (调用方会误判成「港股没行情」)。"""
    client = _FakeClient()
    with pytest.raises(TencentError, match="全部无法映射"):
        hk_spot.fetch_hk_spot(client, ["600000.SH", "HSI"])
    assert client.chunks == []


def test_fetch_skips_unmappable_and_warns(caplog):
    client = _FakeClient({"hk00700": HK_00700})
    with caplog.at_level(logging.WARNING):
        recs = hk_spot.fetch_hk_spot(client, ["00700.HK", "600000.SH"])
    assert [r["symbol"] for r in recs] == ["00700.HK"]
    assert client.chunks == [["hk00700"]]
    assert "600000.SH" in caplog.text


def test_fetch_tolerates_missing_rows():
    """无效代码上游不报错也不返回行 (实测 60 只返回 47 行) → 少行是常态, 不抛错。"""
    client = _FakeClient({"hk00700": HK_00700})
    recs = hk_spot.fetch_hk_spot(client, ["00700.HK", "09988.HK"])
    assert [r["symbol"] for r in recs] == ["00700.HK"]


def test_fetch_batches_at_snapshot_limit():
    symbols = [f"{i:05d}.HK" for i in range(SNAPSHOT_BATCH + 5)]
    client = _FakeClient()
    hk_spot.fetch_hk_spot(client, symbols)
    assert len(client.chunks) == 2
    assert len(client.chunks[0]) == SNAPSHOT_BATCH
    assert len(client.chunks[1]) == 5


def test_fetch_single_batch_failure_is_soft():
    """单批失败只丢该批 (记 warning 继续), 与 A 股 get_realtime 同策略。"""
    client = _FakeClient(
        {"hk00700": HK_00700},
        errors={0: TencentError("批 0 熔断中")},
    )
    symbols = [f"{i:05d}.HK" for i in range(SNAPSHOT_BATCH)] + ["00700.HK"]
    recs = hk_spot.fetch_hk_spot(client, symbols)
    assert [r["symbol"] for r in recs] == ["00700.HK"]
    assert len(client.chunks) == 2


def test_fetch_raises_when_every_batch_fails():
    """🔴 全批失败**必须抛错**而不是返回 []: 上游靠异常区分「源挂了」与「确实没数据」。

    返回 [] 会让调用方把空结果写进缓存, 重蹈「空态被当成真相」的覆辙。
    """
    client = _FakeClient(errors={0: TencentError("网络失败"), 1: TencentError("网络失败")})
    symbols = [f"{i:05d}.HK" for i in range(SNAPSHOT_BATCH + 1)]
    with pytest.raises(TencentError, match="全部 2 批失败"):
        hk_spot.fetch_hk_spot(client, symbols)


def test_fetch_drops_index_rows_from_mixed_response():
    """🔴 同端点会混返未请求的指数行 → 映射阶段丢弃, 不混进股票行情。

    新浪港股指数链路有同款污染 (``HK_INDEX_A_SHARE_CONTAMINANTS``), 这里靠
    「代码段必须是 5 位数字 + 字段 63 必须是 GP」双重闸门挡住, 不需要污染黑名单。
    """
    client = _FakeClient({"hk00700": HK_00700}, extra={"hkHSI": HK_HSI})
    recs = hk_spot.fetch_hk_spot(client, ["00700.HK"])
    assert [r["symbol"] for r in recs] == ["00700.HK"]
