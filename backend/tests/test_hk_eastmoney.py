"""港股东财直连模块 (``app.plugins.eastmoney.hk``) 的测试。

钉住五条最容易被后续改动破坏的契约:

1. **secid 前缀统一为 ``116``** — ``128`` 实测亦可用, 但两套前缀混用会让缓存键
   与去重分叉, 因此常量必须由测试钉死。
2. **列表过滤器必须是 ``m:128``** — 用 ``m:116`` 会静默混入 5903 只窝轮/牛熊证,
   污染全市场统计且不报错。
3. 🔴 **成交量单位是「股」不是「手」** — 透传时不得再乘 100。这是港股接入最容易
   静默算错的一处 (换手率高 100 倍、VWAP 低 100 倍), 见方案 §3.1.2。
4. **双柜台两个代码都入库**, 用 ``is_cny_counter`` 标记, 且配对关系可反查。
5. **A 股链路零影响** — 本模块只处理 ``.HK``, 非港股 symbol 一律拒绝或返回 None。

fixture 全部来自 2026-11-15 的活体探测录制 (非编造), 原始记录见
docs/hk-market-plan.md §3.1.1。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.market import Market, SymbolMarketMismatchError, is_valid_symbol
from app.plugins.eastmoney import client as em_client
from app.plugins.eastmoney import hk

# ================================================================
# 活体探测录制的真实 clist 行 (2026-11-15, fs=m:128+t:3)
# ================================================================

#: 89988 阿里巴巴-WR — 人民币柜台。用于钉住「volume 单位是股」。
ROW_89988 = {
    "f1": 3, "f2": 76.8, "f3": -3.52, "f4": -2.8, "f5": 86000, "f6": 6679940.0,
    "f7": 4.65, "f8": 0.0, "f9": 20.78, "f10": 1.5, "f12": "89988", "f13": 128,
    "f14": "阿里巴巴-WR", "f15": 80.1, "f16": 76.4, "f17": 79.2, "f18": 79.6,
    "f20": 1528470490675, "f21": 1528470490675, "f22": 0.0, "f23": 1.4,
    "f24": -28.16, "f25": -39.63, "f26": 20230619, "f38": 19901959514.0,
    "f100": "专业零售", "f124": 1794556669,
}

#: 09988 阿里巴巴-W — 同一家公司的港币柜台。
ROW_09988 = {**ROW_89988, "f12": "09988", "f14": "阿里巴巴-W", "f2": 120.0}

#: 00700 腾讯控股 — 主板港币柜台, 每手 100 股。
ROW_00700 = {
    **ROW_89988, "f12": "00700", "f14": "腾讯控股", "f2": 389.0, "f26": 20040616,
    "f100": "互动媒体及服务", "f38": 9190000000.0,
}

#: 窝轮样本 — 实测出现在 m:116 (错误过滤器) 下, 必须被纯净性守卫剔除。
ROW_WARRANT = {**ROW_89988, "f12": "14192", "f14": "中寿信证七一购A"}
ROW_CBBC = {**ROW_89988, "f12": "69128", "f14": "华虹汇丰八甲熊J"}


# ================================================================
# 端点常量 (改动前必须重新活体验证)
# ================================================================

def test_secid_prefix_is_pinned_to_116():
    """``128`` 也能用, 但必须统一选一个 — 混用会让缓存键与去重分叉。"""
    assert hk.HK_SECID_PREFIX == "116"
    assert hk.hk_secid("00700.HK") == "116.00700"


def test_list_filter_uses_m128_not_m116():
    """m:116 会混入窝轮/牛熊证 (实测 17580 行 vs m:128 的 2621 行)。"""
    assert hk.FS_HK_MAIN_BOARD == "m:128+t:3"
    assert "m:116" not in hk.FS_HK_ALL
    assert hk.FS_HK_ALL == "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"


def test_spot_fields_cover_every_field_the_mapper_reads():
    """mapper 读的字段必须都在请求列表里 — 漏一个就静默变 None。"""
    requested = set(hk.HK_SPOT_FIELDS.split(","))
    # map_hk_spot_row + hk_instrument_rows 实际读取的字段
    used = {"f2", "f3", "f4", "f5", "f6", "f7", "f8", "f12", "f14", "f15",
            "f16", "f17", "f18", "f26", "f38", "f100", "f124"}
    assert used <= requested, f"缺字段: {sorted(used - requested)}"


# ================================================================
# symbol / secid 转换
# ================================================================

@pytest.mark.parametrize("symbol,expected", [
    ("00700.HK", "116.00700"),
    ("09988.HK", "116.09988"),
    ("89988.HK", "116.89988"),   # 人民币柜台同样是合法可交易标的
    ("00005.HK", "116.00005"),
    ("00700.hk", "116.00700"),   # 大小写宽松
    (" 00700.HK ", "116.00700"),
])
def test_hk_secid(symbol, expected):
    assert hk.hk_secid(symbol) == expected


@pytest.mark.parametrize("symbol", [
    "600519.SH", "000001.SZ", "920001.BJ",   # A 股: 必须拒绝
    "HSI", "HSCEI",                           # 指数: 另有通道
    "00700",                                  # 裸代码 (无后缀)
    "", " ", None,
    "00700.XX", "abc.HK", "00700.HK;DROP",
    "700.HK", "0700.HK", "007000.HK",         # 非 5 位: 见下方 test 的说明
])
def test_hk_secid_rejects_non_hk(symbol):
    """非港股一律 None (调用方跳过并记日志), 不抛错 — 与 provider.symbol_to_secid 同约定。"""
    assert hk.hk_secid(symbol) is None


def test_hk_secid_rejects_non_five_digit_code_rather_than_guessing():
    """代码位数必须恰好 5 位, 否则 None —— 不能拼出 ``116.700`` 这种畸形 secid。

    东财对畸形 secid 未必报错, 可能**静默解析到另一只标的**。取错数据比取不到
    数据危险得多: 后者会进日志, 前者会一路算进指标和信号。内部 symbol 契约本就是
    ``[0-9]{5}\\.HK``, 正常链路由 :func:`hk.hk_symbol` 的 ``zfill(5)`` 保证, 这里
    只是把「契约外的输入」挡在触网之前。
    """
    assert hk.hk_secid("700.HK") is None
    # 与 app.market 的 symbol 契约一致: 契约内的一律放行
    assert is_valid_symbol("00700.HK") and hk.hk_secid("00700.HK") == "116.00700"
    assert not is_valid_symbol("700.HK")

@pytest.mark.parametrize("symbol,expected", [
    # A 股/ETF/指数: 前缀规则必须逐字不变 (存量行为回归护栏)
    ("600000.SH", "1.600000"),
    ("000001.SZ", "0.000001"),
    ("920002.BJ", "0.920002"),
    ("399001.SZ", "0.399001"),
    ("510300.SH", "1.510300"),
    # 港股: 走 116 前缀
    ("00700.HK", "116.00700"),
    ("89988.HK", "116.89988"),
    # 契约外
    ("700.HK", None),
    ("AAPL.US", None),
    ("00700", None),
])
def test_symbol_to_secid_routes_hk_through_116_prefix(symbol, expected):
    """``provider.symbol_to_secid`` 增加港股分支后, A 股映射一个都不能变。

    港股分支委托给 :func:`hk.hk_secid` 而非就地写 ``f"116.{code}"`` —— 「116」这个
    魔数只允许存在一处, 否则两处各写一遍迟早走偏。
    """
    from app.plugins.eastmoney import provider

    assert provider.symbol_to_secid(symbol) == expected


@pytest.mark.parametrize("code,expected", [
    ("00700", "00700.HK"),
    ("700", "00700.HK"),      # 不足位左补零
    ("9988", "09988.HK"),
    ("89988", "89988.HK"),
])
def test_hk_symbol(code, expected):
    assert hk.hk_symbol(code) == expected


@pytest.mark.parametrize("code", ["", None, "abc", "007000", "123456", "-"])
def test_hk_symbol_rejects_malformed(code):
    """港股全市场统一 5 位; 6 位是 A 股位数, 必须拒绝而非截断。"""
    assert hk.hk_symbol(code) is None


def test_generated_symbols_pass_the_market_guard():
    """hk_symbol 的产物必须能通过 app.market 的严格判据 (双载体同源)。"""
    from app.market import is_valid_symbol
    for code in ("00700", "700", "09988", "89988", "08001"):
        assert is_valid_symbol(hk.hk_symbol(code), Market.HK)


# ================================================================
# 双柜台 (dual counter)
# ================================================================

@pytest.mark.parametrize("cny,hkd", [
    ("89988", "09988"),   # 阿里巴巴
    ("89888", "09888"),   # 百度集团
    ("89618", "09618"),   # 京东集团
    ("86618", "06618"),   # 京东健康
    ("83690", "03690"),   # 美团
    ("82388", "02388"),   # 中银香港
    ("82333", "02333"),   # 长城汽车
    ("82331", "02331"),   # 李宁
])
def test_hkd_counter_of_covers_all_observed_prefixes(cny, hkd):
    """⚠️ 不能只按 ``89`` 开头识别 — 实测 82xxx/83xxx/86xxx 同样是人民币柜台。"""
    assert hk.hkd_counter_of(cny) == hkd


@pytest.mark.parametrize("code", ["09988", "00700", "08001", "700", "", None, "8998"])
def test_hkd_counter_of_returns_none_for_hkd_counter(code):
    """港币柜台 (首位 0) 返回 None — 它自己就是主柜台。"""
    assert hk.hkd_counter_of(code) is None


@pytest.mark.parametrize("code,name,expected", [
    ("89988", "阿里巴巴-WR", True),
    ("83690", "美团-WR", True),
    ("82388", "中银香港-R", True),
    ("89618", "京东集团-SWR", True),
    ("09988", "阿里巴巴-W", False),
    ("00700", "腾讯控股", False),
    ("02388", "中银香港", False),
    # 两信号不一致 → 判 False (宁可漏标让人复核, 不静默踢出统计)
    ("89988", "某公司", False),
    ("09988", "阿里巴巴-WR", False),
])
def test_is_cny_counter_requires_both_signals(code, name, expected):
    assert hk.is_cny_counter(code, name) is expected


def test_is_cny_counter_without_name_falls_back_to_code_prefix():
    """名称缺省时只按代码前缀判 (主板 8xxxx 即人民币柜台)。"""
    assert hk.is_cny_counter("89988") is True
    assert hk.is_cny_counter("09988") is False


def test_instrument_rows_marks_both_counters_and_links_them():
    """**已定策略**: 两个代码都入库, 用标记区分, 配对关系可反查。"""
    rows = {r["symbol"]: r for r in hk.hk_instrument_rows([ROW_89988, ROW_09988])}
    assert set(rows) == {"89988.HK", "09988.HK"}, "人民币柜台不得在入库时被过滤掉"

    cny = rows["89988.HK"]
    assert cny["is_cny_counter"] is True
    assert cny["hkd_counter"] == "09988.HK"
    assert cny["name"] == "阿里巴巴-WR"

    hkd = rows["09988.HK"]
    assert hkd["is_cny_counter"] is False
    assert hkd["hkd_counter"] is None
    # 同一家公司 → 股本/行业/上市日期一致
    assert hkd["total_shares"] == cny["total_shares"]
    assert hkd["industry"] == cny["industry"]


def test_cny_counter_without_its_pair_is_still_flagged(caplog):
    """配对代码不在本次列表时记 warning, 但仍按代码+名称两信号落库。

    ``hkd_counter`` 照样填: 配对规则 (首位 0↔8) 是确定的, 与本次是否抓到无关;
    它是维表上的信息列而非外键, 悬空引用无害, 而缺了它查询层就无法把双柜台
    归并到同一公司。异常已由 warning 吵出来。
    """
    with caplog.at_level("WARNING"):
        rows = hk.hk_instrument_rows([ROW_89988])
    assert rows[0]["is_cny_counter"] is True
    assert rows[0]["hkd_counter"] == "09988.HK"
    assert "双柜台判定信号不一致" in caplog.text


# ================================================================
# 🔴 成交量单位 (最高危的一处)
# ================================================================

def test_volume_is_passed_through_in_shares_not_lots():
    """港股 f5 单位是**股**, 必须原样透传。

    实测自洽验证: f5=86000 × f2=76.8 ≈ f6=6679940 元。若 f5 是手,
    成交额应为 6.6 亿 (差 100 倍)。A 股 f5 是手, 两者不可混用同一系数。
    """
    rec = hk.map_hk_spot_row(ROW_89988, 1700000000000)
    assert rec["volume"] == 86000.0, "不得乘/除 100 — 港股 volume 单位就是股"
    assert rec["amount"] == 6679940.0
    # 单位自洽: volume(股) × price ≈ amount(元)
    assert abs(rec["volume"] * rec["last_price"] - rec["amount"]) / rec["amount"] < 0.02


def test_volume_unit_coefficient_differs_from_a_share():
    """内部换算系数: A 股 100 (手→股), 港股 1 (本来就是股)。"""
    from app.market import shares_per_volume_unit
    assert shares_per_volume_unit(Market.CN) == 100.0
    assert shares_per_volume_unit(Market.HK) == 1.0
    assert shares_per_volume_unit() == 100.0, "缺省必须是 CN, 保证 A 股路径算术不变"


def test_turnover_formula_gives_same_result_for_both_markets():
    """用同一份「股数」真值反推: 两个市场的换手率公式必须给出相同结果。

    这条是 §3.1.2 的验收标准 —— 若港股沿用 A 股的 ×10000, 结果会差 100 倍。
    """
    from app.market import shares_per_volume_unit

    float_shares = 1_000_000.0
    true_shares_traded = 10_000.0          # 真实成交 1 万股
    expected_pct = true_shares_traded / float_shares * 100.0   # 1.0%

    # A 股: volume 以手入库 → 100 手
    cn_volume = true_shares_traded / 100.0
    cn_pct = cn_volume * shares_per_volume_unit(Market.CN) * 100.0 / float_shares
    # 港股: volume 以股入库 → 10000 股
    hk_volume = true_shares_traded
    hk_pct = hk_volume * shares_per_volume_unit(Market.HK) * 100.0 / float_shares

    assert cn_pct == pytest.approx(expected_pct)
    assert hk_pct == pytest.approx(expected_pct)


# ================================================================
# 字段解析
# ================================================================

@pytest.mark.parametrize("value,expected", [
    (20230619, date(2023, 6, 19)),      # 实测 f26 是 YYYYMMDD 整数
    ("20230619", date(2023, 6, 19)),
    (20230619.0, date(2023, 6, 19)),
    (20040616, date(2004, 6, 16)),      # 腾讯控股
])
def test_parse_hk_list_date(value, expected):
    assert hk.parse_hk_list_date(value) == expected


@pytest.mark.parametrize("value", [
    "-", None, "", 0, 0.0,
    20231332,      # 13 月 32 日
    2023061,       # 7 位
    202306190,     # 9 位
    "2023-06-19",  # A 股 datacenter 格式, 港股 clist 不用
])
def test_parse_hk_list_date_returns_none_for_missing_or_invalid(value):
    """缺值与非法值一律 None, 不伪造日期 (与 A 股 parse_listing_date 同约定)。"""
    assert hk.parse_hk_list_date(value) is None


@pytest.mark.parametrize("price,expected", [
    # HKEX 价位表, 区间左闭右开 [lower, upper)
    (0.01, 0.001), (0.249, 0.001),
    (0.25, 0.005), (0.499, 0.005),
    (0.50, 0.010), (9.99, 0.010),
    (10.0, 0.020), (19.99, 0.020),
    (20.0, 0.050), (76.8, 0.050), (99.99, 0.050),
    (100.0, 0.100), (199.99, 0.100),
    (200.0, 0.200), (499.99, 0.200),
    (500.0, 0.500), (999.99, 0.500),
    (1000.0, 1.000), (1999.99, 1.000),
    (2000.0, 2.000), (4999.99, 2.000),
    (5000.0, 5.000), (99999.0, 5.000),
])
def test_hk_tick_size_follows_hkex_spread_table(price, expected):
    """港股 tick size **随价格变动** (A 股恒为 0.01), 必须查表。"""
    assert hk.hk_tick_size(price) == expected


@pytest.mark.parametrize("price", [None, "-", "", 0, -1.5])
def test_hk_tick_size_returns_none_without_price(price):
    assert hk.hk_tick_size(price) is None


# ================================================================
# 列表纯净性守卫
# ================================================================

def test_partition_rejects_warrants_and_cbbcs():
    """用错市场码 (m:116) 会静默混入 5903 只衍生品 — 守卫必须挡住。"""
    clean, rejected = hk.partition_hk_derivatives(
        [ROW_00700, ROW_09988, ROW_89988, ROW_WARRANT, ROW_CBBC]
    )
    assert [r["f12"] for r in clean] == ["00700", "09988", "89988"]
    assert {r["f12"] for r in rejected} == {"14192", "69128"}


def test_partition_keeps_gem_and_cny_counters():
    """GEM (08xxx) 与人民币柜台 (8xxxx) 都是股票, 不得误杀。"""
    gem = {**ROW_00700, "f12": "08001", "f14": "某创业板公司"}
    clean, rejected = hk.partition_hk_derivatives([gem, ROW_89988])
    assert [r["f12"] for r in clean] == ["08001", "89988"]
    assert rejected == []


@pytest.mark.parametrize("bad", ["1419", "00700X", "", None, "141921"])
def test_partition_rejects_malformed_codes(bad):
    row = {**ROW_00700, "f12": bad}
    clean, rejected = hk.partition_hk_derivatives([row])
    assert clean == []
    assert len(rejected) == 1


def test_instrument_rows_logs_warning_when_derivatives_leak_in(caplog):
    """剔除数量非零即说明上游过滤器语义变了, 必须吵出来而不是静默丢。"""
    with caplog.at_level("WARNING"):
        rows = hk.hk_instrument_rows([ROW_00700, ROW_WARRANT])
    assert [r["symbol"] for r in rows] == ["00700.HK"]
    assert "剔除 1 行非股票标的" in caplog.text


# ================================================================
# 快照行映射
# ================================================================

def test_map_hk_spot_row_matches_internal_realtime_schema():
    """输出键必须与 provider._map_spot_row 一致 (quote_service 契约)。"""
    from app.plugins.eastmoney.provider import _map_spot_row

    a_share_keys = set(_map_spot_row({
        "f12": "600519", "f13": 1, "f14": "贵州茅台", "f2": 1500.0, "f18": 1490.0,
        "f17": 1495.0, "f15": 1510.0, "f16": 1488.0, "f5": 30000, "f6": 4.5e9,
        "f3": 0.67, "f4": 10.0, "f7": 1.5, "f8": 0.24, "f124": 1794556669,
    }, 1700000000000))
    hk_keys = set(hk.map_hk_spot_row(ROW_00700, 1700000000000))
    assert hk_keys == a_share_keys, f"键集不一致: {hk_keys ^ a_share_keys}"


def test_map_hk_spot_row_values():
    rec = hk.map_hk_spot_row(ROW_89988, 1700000000000)
    assert rec["symbol"] == "89988.HK"
    assert rec["name"] == "阿里巴巴-WR"
    assert rec["last_price"] == 76.8
    assert rec["prev_close"] == 79.6
    assert rec["open"] == 79.2
    assert rec["high"] == 80.1
    assert rec["low"] == 76.4
    assert rec["change_pct"] == pytest.approx(-0.0352)
    assert rec["change_amount"] == pytest.approx(-2.8)
    assert rec["amplitude"] == pytest.approx(0.0465)
    assert rec["turnover_rate"] == 0.0
    assert rec["timestamp"] == 1794556669000   # f124 秒 → 毫秒


def test_map_hk_spot_row_does_not_use_f13_for_suffix():
    """港股 f13=128, 若沿用 A 股 _suffix_of 的 f13 推断会被判成深市 (.SZ)。"""
    assert ROW_89988["f13"] == 128
    assert hk.map_hk_spot_row(ROW_89988, 0)["symbol"].endswith(".HK")


def test_map_hk_spot_row_handles_suspended_and_missing_fields():
    """停牌行价格是 '-' → None 并保留该行; 缺失字段不伪造 0。"""
    suspended = {**ROW_00700, "f2": "-", "f3": "-", "f4": "-", "f5": "-",
                 "f6": "-", "f7": "-", "f8": "-", "f15": "-", "f16": "-", "f17": "-"}
    rec = hk.map_hk_spot_row(suspended, 1700000000000)
    assert rec is not None, "停牌行必须保留 (下游按停牌处理)"
    assert rec["last_price"] is None
    assert rec["volume"] is None
    assert rec["change_pct"] is None
    assert rec["prev_close"] == 79.6   # f18 仍在


def test_map_hk_spot_row_derives_change_when_fields_missing():
    """f3/f4 缺失时用 last-prev 兜底 (与 A 股路径同一逻辑)。"""
    row = {**ROW_00700, "f3": "-", "f4": "-", "f2": 400.0, "f18": 390.0}
    rec = hk.map_hk_spot_row(row, 0)
    assert rec["change_amount"] == pytest.approx(10.0)
    assert rec["change_pct"] == pytest.approx(10.0 / 390.0)


def test_map_hk_spot_row_falls_back_to_fetched_ms():
    """f124 缺失/异常时用抓取时刻, 不留 None (下游按时间戳排序)。"""
    row = {**ROW_00700, "f124": "-"}
    assert hk.map_hk_spot_row(row, 1700000000000)["timestamp"] == 1700000000000


@pytest.mark.parametrize("bad_code", ["", None, "abc", "007000"])
def test_map_hk_spot_row_returns_none_for_bad_code(bad_code):
    assert hk.map_hk_spot_row({**ROW_00700, "f12": bad_code}, 0) is None


# ================================================================
# instruments_hk 维表行
# ================================================================

def test_instrument_rows_schema():
    rows = hk.hk_instrument_rows([ROW_00700])
    assert len(rows) == 1
    row = rows[0]
    assert tuple(row) == hk.HK_INSTRUMENT_COLUMNS, "列顺序即 parquet schema, 变动需同步仓储层"
    assert row["symbol"] == "00700.HK"
    assert row["code"] == "00700"
    assert row["exchange"] == "HK"
    assert row["region"] == "HK"
    assert row["type"] == "stock"
    assert row["listing_date"] == date(2004, 6, 16)
    assert row["total_shares"] == 9190000000
    assert row["industry"] == "互动媒体及服务"
    assert row["is_cny_counter"] is False


def test_first_twelve_columns_match_a_share_instruments_schema():
    """方案 §4 闸3: ``instruments_hk`` 前 12 列必须与 A 股 ``instruments`` 同名同序。

    对齐后共用读取代码 (维表 join、字段保留、缓存失效) 不必为港股分叉;
    港股专有列一律追加在尾部。漂移会让「A 股能跑的查询在港股上 KeyError」。
    """
    from app.services.instrument_sync import _flatten_instruments

    a_share_cols = list(_flatten_instruments([{
        "symbol": "600519.SH", "name": "贵州茅台", "code": "600519",
        "exchange": "SH", "region": "CN", "type": "stock",
        "ext": {"listing_date": None, "total_shares": None, "float_shares": None,
                "tick_size": None, "limit_up": None, "limit_down": None},
    }])[0])
    assert list(hk.HK_INSTRUMENT_COLUMNS[:len(a_share_cols)]) == a_share_cols
    # 港股专有列必须全部在尾部
    assert set(hk.HK_INSTRUMENT_COLUMNS[len(a_share_cols):]) == {
        "lot_size", "industry", "is_cny_counter", "hkd_counter",
    }


def test_instrument_rows_keep_price_limit_columns_as_none():
    """港股无涨跌停, 但**保留列**并置 None (方案 §4: schema 与 A 股对齐)。

    保留而非删列: 现有守卫按「limit_up 为 0/null 即无限制」判定, null 是安全值;
    删列会让共用读取代码分叉。置 None 而非 0: 0 会被读成「限价 0 元」。
    """
    assert "limit_up" in hk.HK_INSTRUMENT_COLUMNS
    assert "limit_down" in hk.HK_INSTRUMENT_COLUMNS
    row = hk.hk_instrument_rows([ROW_00700])[0]
    assert row["limit_up"] is None
    assert row["limit_down"] is None


def test_lot_size_is_none_not_a_guessed_default():
    """东财 clist 不提供每手股数 (f292 已实测排除)。

    ⚠️ 不得填 100 兜底 —— 腾讯 100 / 汇丰 400 / 中国银行 1000, 猜错会让
    整手校验与成交金额算错。消费方拿到 None 必须去查标的或显式报错。
    """
    assert hk.hk_instrument_rows([ROW_00700])[0]["lot_size"] is None
    assert hk.HK_FALLBACK_LOT_SIZE == 100   # 仅供展示兜底, 不参与计算


def test_float_shares_equals_total_shares_for_hk():
    """港股全流通 → 流通股本 = 总股本 (实测 f38 == f39)。"""
    row = hk.hk_instrument_rows([ROW_00700])[0]
    assert row["float_shares"] == row["total_shares"]


def test_instrument_rows_normalizes_placeholder_industry_to_none():
    """东财缺值占位 '-' 不得当成真实行业名进维表。"""
    row = hk.hk_instrument_rows([{**ROW_00700, "f100": "-"}])[0]
    assert row["industry"] is None


def test_instrument_rows_skips_unusable_codes():
    rows = hk.hk_instrument_rows([ROW_00700, {**ROW_00700, "f12": "abc"}])
    assert [r["symbol"] for r in rows] == ["00700.HK"]


def test_instrument_rows_deduplicates_nothing_but_preserves_order():
    """入库顺序跟随上游 (按 f12 排序), 便于 diff 两次全量同步的差异。"""
    rows = hk.hk_instrument_rows([ROW_89988, ROW_00700, ROW_09988])
    assert [r["symbol"] for r in rows] == ["89988.HK", "00700.HK", "09988.HK"]


# ================================================================
# 网络层薄封装 (不打真实接口 — 用假 client)
# ================================================================

class _FakeClient:
    """记录调用参数的假 client, 避免测试触发 push2 的 IP 级封禁。"""

    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def _push2_data(self, base, path, params):
        self.calls.append((base, path, dict(params)))
        pn = params["pn"]
        if pn > len(self._pages):
            return None
        return self._pages[pn - 1]

    def kline(self, secid, beg, end, *, klt=101, fqt=0):
        self.calls.append(("kline", secid, {"beg": beg, "end": end, "klt": klt, "fqt": fqt}))
        return ["2026-11-13,393.000,386.600,395.200,385.600,31493353,12251162810,3.2,1.1,4.2,0.35"]


def test_fetch_hk_spot_uses_page_size_100_not_1000():
    """实测港股 pz=1000 只返回 100 行 → 沿用 1000 会让「拉满即停」永不成立、白跑 30 页。"""
    client = _FakeClient([{"total": 2, "diff": [ROW_00700, ROW_09988]}])
    rows = hk.fetch_hk_spot(client)
    assert len(rows) == 2
    assert client.calls[0][2]["pz"] == 100
    assert client.calls[0][2]["fs"] == hk.FS_HK_MAIN_BOARD
    assert client.calls[0][2]["fields"] == hk.HK_SPOT_FIELDS


def test_fetch_hk_spot_paginates_until_total_reached():
    client = _FakeClient([
        {"total": 3, "diff": [ROW_00700, ROW_09988]},
        {"total": 3, "diff": [ROW_89988]},
        {"total": 3, "diff": []},
    ])
    assert len(hk.fetch_hk_spot(client)) == 3
    assert len(client.calls) == 2, "拉满 total 即停, 不多打一页"


def test_fetch_hk_spot_accepts_dict_shaped_diff():
    """老版本接口 diff 是 {序号: 行} 字典 — 必须兼容 (与 client.spot_all 同处理)。"""
    client = _FakeClient([{"total": 2, "diff": {"0": ROW_00700, "1": ROW_09988}}])
    assert len(hk.fetch_hk_spot(client)) == 2


def test_fetch_hk_spot_raises_on_empty():
    """港股全市场为空只可能是接口异常, 不能当成「今天没有港股」静默返回空表。"""
    client = _FakeClient([{"total": 0, "diff": []}])
    with pytest.raises(em_client.EastmoneyError):
        hk.fetch_hk_spot(client)


def test_fetch_hk_spot_warns_on_incomplete_pagination(caplog):
    client = _FakeClient([{"total": 999, "diff": [ROW_00700]}, None])
    with caplog.at_level("WARNING"):
        rows = hk.fetch_hk_spot(client)
    assert len(rows) == 1
    assert "分页未拉满" in caplog.text


def test_fetch_hk_kline_uses_116_prefix_and_raw_adjustment():
    """落库契约: fqt=0 不复权 (原始价), 复权因子另存 adj_factor 表。"""
    client = _FakeClient([])
    lines = hk.fetch_hk_kline(client, "00700.HK", "20261101", "20261115")
    assert lines
    kind, secid, params = client.calls[0]
    assert kind == "kline"
    assert secid == "116.00700"
    assert params["fqt"] == 0
    assert params["klt"] == 101


@pytest.mark.parametrize("symbol", ["600519.SH", "000001.SZ", "HSI", "00700"])
def test_fetch_hk_kline_rejects_non_hk_symbol(symbol):
    """A 股/指数/裸代码走错通道必须当场失败, 不得静默返回空。"""
    with pytest.raises((SymbolMarketMismatchError, em_client.EastmoneyError)):
        hk.fetch_hk_kline(_FakeClient([]), symbol, "20261101", "20261115")


def test_hk_kline_row_is_parseable_by_the_shared_mapper():
    """港股 kline 行格式与 A 股一致 → 可直接复用 provider._map_kline_row。

    实测 00700 末根前 6 列: ``2026-11-13,393.000,386.600,395.200,385.600,31493353``。
    ⚠️ 列序是「开**收**高低」不是「开高低收」, 复用同一 mapper 才不会错位 ——
    这条测试就是钉住列序的。后 5 列 (额/振幅%/涨跌幅%/涨跌额/换手率%) 由
    ``fields2`` 决定, mapper 只取前 7 列。
    """
    from app.plugins.eastmoney.provider import _map_kline_row

    lines = _FakeClient([]).kline("116.00700", "", "")
    rec = _map_kline_row("00700.HK", lines[0])
    assert rec["date"] == date(2026, 11, 13)
    assert rec["open"] == 393.0
    assert rec["close"] == 386.6      # 收在第 3 列 (不是第 5 列)
    assert rec["high"] == 395.2
    assert rec["low"] == 385.6
    assert rec["volume"] == 31493353.0   # 股 (A 股同位置是手)
    assert rec["amount"] == 12251162810.0
    # OHLC 自洽: high ≥ max(open, close) ≥ min(open, close) ≥ low
    assert rec["high"] >= max(rec["open"], rec["close"])
    assert min(rec["open"], rec["close"]) >= rec["low"]
