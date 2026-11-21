"""港股 F10 概况 (``RPT_HKF10_INFO_ORGPROFILE``) → 标的维表行的映射与翻页。

PR6 维表换源的新增源。全部脱网: 东财 push2 集群对本机 IP 级封禁, datacenter-web
虽未被封, 但测试触网会让结果不可复现 (上游随时改列), 故一律用假 client。

实测口径 (2026-11-21 活体, 详见 ``app/plugins/eastmoney/hk_profile.py`` 模块
docstring): 全量 6920 行 / 14 页 (pageSize 500), 其中 5 位 ``0``/``8`` 开头 5122 行,
经腾讯在市闸门后 2862 行。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.plugins.eastmoney import hk, hk_profile
from app.plugins.eastmoney.client import EastmoneyError


def _profile(code: str, name: str, **over) -> dict:
    """最小可用的 F10 概况行 (35 列里只填映射用得到的)。"""
    row = {
        "SECUCODE": f"{code}.HK",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name,
        "BELONG_INDUSTRY": "软件服务",
        "INDUSTRY_TYPE": "通讯服务-媒体与娱乐-互动媒体与服务-互动媒体与服务",
        "TRADE_UNIT": 100,
        "HK_SHARES": 9_092_644_500,
        "LISTING_DATE": "2004-06-16 00:00:00",
        "BELONG_MARKET": "香港主板",
    }
    row.update(over)
    return row


class _FakeEmClient:
    """假 EastmoneyClient: 记录 ``datacenter_report_all`` 调用并按页返回。"""

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.calls: list[tuple] = []
        self.closed = False

    def datacenter_report_all(self, report_name, **kwargs):
        self.calls.append((report_name, kwargs))
        out: list[dict] = []
        for page in self._pages:
            out.extend(page)
        return out

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# 上市日期解析: 🔴 与 clist 的 f26 格式不同, 不可互换
# ---------------------------------------------------------------------------

def test_parse_profile_list_date_accepts_datacenter_datetime_string():
    """F10 给的是 ``'2004-06-16 00:00:00'`` 带时间字符串, 不是 clist 的 YYYYMMDD 整数。"""
    assert hk_profile.parse_hk_profile_list_date("2004-06-16 00:00:00") == date(2004, 6, 16)
    assert hk_profile.parse_hk_profile_list_date("1972-11-01 00:00:00") == date(1972, 11, 1)
    # 只有日期部分也应能解析 (上游若哪天去掉时间后缀, 不该整列变空)
    assert hk_profile.parse_hk_profile_list_date("2004-06-16") == date(2004, 6, 16)


@pytest.mark.parametrize("value", [None, "", "   ", "-", "0000-00-00 00:00:00", "不是日期"])
def test_parse_profile_list_date_never_fabricates(value):
    """缺值与非法值一律 None, 不伪造日期 (fail closed)。"""
    assert hk_profile.parse_hk_profile_list_date(value) is None


def test_the_two_list_date_parsers_are_not_interchangeable():
    """🔴 clist 的解析器吃不下 F10 的字符串, 且**静默**返回 None。

    这是换源最容易踩的坑: 把 F10 的 ``'2004-06-16 00:00:00'`` 喂给
    ``hk.parse_hk_list_date`` (它走 ``_to_float`` → 失败 → None), 整张维表的
    上市日期会全空却**不报任何错**。

    反方向不成立: ``date.fromisoformat`` 自 py3.11 起也接受 ``YYYYMMDD`` 基本格式,
    所以 F10 解析器**两种格式都吃**。这是有意的宽容 —— 上游哪天改回整数,
    不会整列变空。
    """
    assert hk.parse_hk_list_date("2004-06-16 00:00:00") is None, "clist 解析器不该吃字符串"
    assert hk.parse_hk_list_date(20040616) == date(2004, 6, 16), "clist 那一路仍需可用"
    assert hk_profile.parse_hk_profile_list_date(20040616) == date(2004, 6, 16)


# ---------------------------------------------------------------------------
# 行映射: 契约必须与 clist 那一路逐键相同
# ---------------------------------------------------------------------------

def test_output_keys_match_hk_instrument_columns_exactly():
    """换源的前提: 输出键**逐键同序**等于 ``HK_INSTRUMENT_COLUMNS``, 读端零改动。"""
    rows = hk_profile.hk_profile_instrument_rows([_profile("00700", "腾讯控股")])

    assert len(rows) == 1
    assert list(rows[0].keys()) == list(hk.HK_INSTRUMENT_COLUMNS)


def test_field_mapping():
    rows = hk_profile.hk_profile_instrument_rows(
        [_profile("00700", "腾讯控股")], live_prices={"00700.HK": 344.0},
    )
    r = rows[0]

    assert r["symbol"] == "00700.HK"
    assert r["code"] == "00700"
    assert r["name"] == "腾讯控股"
    assert r["exchange"] == r["region"] == "HK"
    assert r["type"] == "stock"
    assert r["listing_date"] == date(2004, 6, 16)
    assert r["industry"] == "软件服务"
    assert r["lot_size"] == 100
    assert r["total_shares"] == r["float_shares"] == 9_092_644_500
    # 344.0 落在 HKEX 价位表 [200, 500) → 0.2
    assert r["tick_size"] == 0.2
    # 港股无涨跌停 → None 而不是 0 (0 会被读成「限价 0 元」)
    assert r["limit_up"] is None and r["limit_down"] is None
    assert r["is_cny_counter"] is False and r["hkd_counter"] is None


def test_total_shares_uses_hk_shares_not_reg_capital():
    """🔴 ``REG_CAPITAL`` **不可**当总股本, 即使它看起来更像。

    实测 ``00700 腾讯`` 的 REG_CAPITAL 是字符串 ``"1000000 HKD"`` (注册资本名义值),
    与真实 91.9 亿股本差四个数量级; ``02318 中国平安`` 的是 ``"18107641995 CNY"``
    (集团总股本), 但带币种后缀没法直接当整数用。``HK_SHARES`` 是**港股 (H股) 股本**
    (平安 74.5 亿 = H股, 小于集团 182 亿), 语义明确且是纯整数, 故取它。
    """
    row = _profile("02318", "中国平安", HK_SHARES=7_447_576_912,
                   REG_CAPITAL="18107641995 CNY")
    r = hk_profile.hk_profile_instrument_rows([row])[0]

    assert r["total_shares"] == 7_447_576_912
    assert r["float_shares"] == 7_447_576_912, "港股全流通, 流通 = 港股股本"


def test_lot_size_comes_from_trade_unit():
    """⭐ 换源收益: 每手股数直接可得 (clist 的 ``f292`` 已实测排除, 拿不到)。

    取值逐只核对过港交所真实每手股数。
    """
    rows = hk_profile.hk_profile_instrument_rows([
        _profile("00005", "汇丰控股", TRADE_UNIT=400),
        _profile("02318", "中国平安", TRADE_UNIT=500),
        _profile("03988", "中国银行", TRADE_UNIT=1000),
        _profile("01485", "TENNESSEE B2025", TRADE_UNIT=None),
    ])
    got = {r["symbol"]: r["lot_size"] for r in rows}

    assert got == {"00005.HK": 400, "02318.HK": 500, "03988.HK": 1000, "01485.HK": None}


# ---------------------------------------------------------------------------
# 在市闸门 (live_prices)
# ---------------------------------------------------------------------------

def test_live_gate_drops_symbols_absent_from_tencent():
    """🔴 不在腾讯在市集合里的整行丢弃 —— F10 概况**没有在市状态列**。

    实测全量 6920 行含约 2200 个退市/长期停牌代码 (``00011 恒生银行`` 腾讯日K
    最后一根停在 2026-01-14, 新浪亦返回空串, 两源一致), 不过滤就会把死代码
    当在市标的落库。
    """
    rows = hk_profile.hk_profile_instrument_rows(
        [_profile("00700", "腾讯控股"), _profile("00011", "恒生银行", BELONG_INDUSTRY="银行")],
        live_prices={"00700.HK": 344.0},
    )

    assert [r["symbol"] for r in rows] == ["00700.HK"]


def test_without_gate_rows_are_kept_but_tick_size_is_none():
    """``live_prices=None`` 仅供脱网单测映射: 不过滤, 且 tick_size 无从算起。

    港股价位表按价格分档, F10 概况没有任何价格列 ⇒ 没有闸门就没有 tick_size。
    生产链路必须传闸门 (见 ``instrument_sync_hk.fetch_hk_instrument_rows``)。
    """
    rows = hk_profile.hk_profile_instrument_rows([_profile("00011", "恒生银行")])

    assert len(rows) == 1
    assert rows[0]["tick_size"] is None


def test_tick_size_follows_spread_table():
    """tick_size 必须按最新价查 HKEX 价位表, 不是常量 (A 股才恒为 0.01)。"""
    prices = {"00405.HK": 0.605, "00003.HK": 7.33, "00005.HK": 129.2, "00700.HK": 344.0}
    rows = hk_profile.hk_profile_instrument_rows(
        [_profile(c[:5], c) for c in prices], live_prices=prices,
    )
    got = {r["symbol"]: r["tick_size"] for r in rows}

    assert got == {"00405.HK": 0.01, "00003.HK": 0.01, "00005.HK": 0.1, "00700.HK": 0.2}


# ---------------------------------------------------------------------------
# 非股票代码过滤
# ---------------------------------------------------------------------------

def test_non_equity_codes_are_rejected_with_warning(caplog):
    """债务证券 / 待上市新股在映射前剔掉, 且**必须记 warning** (数量非零即口径变了)。

    实测被剔的三类: ``4xxxx`` 债务证券 (``40001 CCB N2611C``)、字母开头债券
    (``ABOCFB11008``, 11 位)、待上市新股 (``H2001 Aqara`` —— 递表未挂牌却已带行业)。
    判据与 ``hk.partition_hk_derivatives`` 同一个 (5 位数字且首位 0/8)。
    """
    rows = [
        _profile("00700", "腾讯控股"),
        _profile("40001", "CCB N2611C", BELONG_INDUSTRY=None),
        _profile("ABOCFB11008", "中国诚通发展集团债券2014", BELONG_INDUSTRY=None),
        _profile("H2001", "Aqara"),
    ]
    with caplog.at_level("WARNING"):
        got = hk_profile.hk_profile_instrument_rows(rows)

    assert [r["symbol"] for r in got] == ["00700.HK"]
    assert "剔除 3 行非股票代码" in caplog.text


def test_hk_profile_symbols_uses_the_same_filter():
    """闸门候选列表与映射用**同一个**判据, 两处不会分叉。"""
    rows = [_profile("00700", "腾讯控股"), _profile("89988", "阿里巴巴-WR"),
            _profile("40001", "CCB N2611C"), _profile("H2001", "Aqara")]

    assert hk_profile.hk_profile_symbols(rows) == ["00700.HK", "89988.HK"]


def test_null_industry_rows_are_kept_but_warned(caplog):
    """🔴 无行业的行**保留**, 只告警 —— 刻意不按「无行业」过滤。

    实测在市 2862 行里 7 行无行业, 全是债券 (``04286 政府基债二七一二`` + 6 只
    ``-T`` 后缀)。但东财行业字段偶发返回 ``'-'`` (clist 的 f100 有此毛病,
    ``industry`` 因此进了 ``_HK_PRESERVE_FIELDS``), 拿它当判据会在上游抽风时把
    真实股票**整行删掉** —— 丢真股票的代价远大于多 7 行债券, 且无行业的行不会
    落进任何行业分桶, 对行业页/轮动矩阵无害。
    """
    rows = [_profile("00700", "腾讯控股"),
            _profile("04286", "政府基债二七一二", BELONG_INDUSTRY=None),
            _profile("04338", "微软-T", BELONG_INDUSTRY="-")]
    with caplog.at_level("WARNING"):
        got = hk_profile.hk_profile_instrument_rows(rows)

    assert len(got) == 3, "无行业的行不该被丢掉"
    assert [r["industry"] for r in got] == ["软件服务", None, None]
    assert "无恒生行业" in caplog.text


def test_placeholder_dash_is_cleaned_to_none():
    """东财缺值占位 ``'-'`` 不得进维表当成真实行业名/公司名。"""
    r = hk_profile.hk_profile_instrument_rows(
        [_profile("00700", "-", BELONG_INDUSTRY="-")],
    )[0]

    assert r["name"] is None
    assert r["industry"] is None


# ---------------------------------------------------------------------------
# 双柜台: 三信号判定沿用 clist 那一路的口径
# ---------------------------------------------------------------------------

def test_dual_counter_three_signal_detection(caplog):
    """代码 8 开头 + 名称 R 结尾 + 配对港币代码确实在本次列表里。"""
    rows = [_profile("09988", "阿里巴巴-W"), _profile("89988", "阿里巴巴-WR")]
    with caplog.at_level("WARNING"):
        got = {r["symbol"]: r for r in hk_profile.hk_profile_instrument_rows(rows)}

    assert got["89988.HK"]["is_cny_counter"] is True
    assert got["89988.HK"]["hkd_counter"] == "09988.HK"
    assert got["09988.HK"]["is_cny_counter"] is False
    assert got["09988.HK"]["hkd_counter"] is None
    assert "信号不一致" not in caplog.text


def test_dual_counter_signal_disagreement_is_warned_not_silenced(caplog):
    """三信号打架时记 warning 并按「代码+名称」落库 —— 宁可标出来让人复核。

    构造: 只给 ``89988 阿里巴巴-WR`` 一行, 配对的港币柜台 ``09988`` 不在本次列表里
    ⇒ 「代码+名称」说是人民币柜台, 「配对存在」说不是。此时**不得**静默二选一。
    (反向情形构造不出来: ``hkd_counter_of`` 对非 8 开头代码恒返回 None。)
    """
    with caplog.at_level("WARNING"):
        got = hk_profile.hk_profile_instrument_rows([_profile("89988", "阿里巴巴-WR")])

    assert got[0]["is_cny_counter"] is True, "应按代码+名称两信号落库"
    assert got[0]["hkd_counter"] == "09988.HK"
    assert "信号不一致" in caplog.text


def test_name_ending_in_r_without_8_prefix_is_not_cny_counter():
    """实测存在 ``FORTIOR`` / ``TRUE PARTNER`` 这类 R 结尾的普通股, 不得误判。

    误判会把真实标的踢出统计 (漏判只是让同一家公司占两席), 代价不对称。
    """
    got = hk_profile.hk_profile_instrument_rows([_profile("02135", "FORTIOR")])[0]

    assert got["is_cny_counter"] is False


# ---------------------------------------------------------------------------
# 触网层: 全市场口径 + 空结果语义
# ---------------------------------------------------------------------------

def test_fetch_uses_full_market_org_profile_without_filter():
    """🔴 必须是**全市场** F10 概况报表, 且不加 filter。

    方案 §6: 港股板块/轮动必须全市场口径 —— QuantMind 早期用 141 只港股通子集,
    行业排名与轮动矩阵全部失真, 后来返工。加 filter 就是重新引入这个坑。
    """
    client = _FakeEmClient([[{"SECURITY_CODE": "00700"}]])

    rows = hk_profile.fetch_hk_org_profiles(client)

    assert rows == [{"SECURITY_CODE": "00700"}]
    report_name, kwargs = client.calls[0]
    assert report_name == "RPT_HKF10_INFO_ORGPROFILE"
    assert kwargs.get("flt") is None, "加 filter 会退化成子集口径"
    assert kwargs.get("page_size") == 500


def test_fetch_raises_instead_of_returning_empty():
    """0 行只可能是上游语义变了 → 抛错, 不返回 ``[]``。

    空列表与「港股真的没有标的」在调用方不可区分, 而后者会覆盖写盘掏空维表。
    """
    with pytest.raises(EastmoneyError, match="0 行"):
        hk_profile.fetch_hk_org_profiles(_FakeEmClient([[]]))
