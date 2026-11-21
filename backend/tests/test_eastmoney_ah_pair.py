"""``app.plugins.eastmoney.ah_pair`` 单测 —— AH 配对候选。

夹具的**结构**全部来自 2026-11 的真实探测行, 覆盖上游在这张表里已知的全部脏数据
形态:

- 占位 A 股代码 (``A04021.SZ`` 等 9 个, 字母开头);
- 多柜台 (``601899.SH 紫金矿业 → '02958.HK,02899.HK'``, 逗号连接在同一列里);
- 名称里带每手股数后缀 (``紫金矿业(二千)`` / ``紫金矿业(五百)``);
- A/H 简称不同 (``万科A`` vs ``万科企业``、``南京熊猫`` vs ``南京熊猫电子股份``);
- ST / 退市股 (``000488.SZ ST晨鸣``、``000585.SZ 东电退``) —— **刻意保留**;
- 无 H 股的普通 A 股 (``STR_CODEH`` 为空), 占 8387 行里的绝大多数。

⚠️ 代码、名称、多柜台结构、``02607.HK`` 的上市日并列 (1994-03-24) 都是实测值;
少数 ``LISTING_DATE`` 是**合成占位** (探测时未逐行记录真值), 它们只用于验证解析
与排序, 不参与任何业务断言。真实上市日由本地 ``instruments_hk`` 维表提供, 见
:mod:`tests.test_ah_premium`。

🔴 本插件**只拆分多柜台候选, 不做消歧**: 消歧需要 ``instruments_hk.listing_date``,
而插件层不碰数据存储 (分层纪律)。消歧规则由
:mod:`tests.test_ah_premium` 覆盖。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.plugins.eastmoney import ah_pair
from app.plugins.eastmoney.client import EastmoneyError

# ---------------------------------------------------------------------------
# 真实探测夹具
# ---------------------------------------------------------------------------

#: 单柜台的标准形态。``STR_CODEH`` 已经是规范的 ``01398.HK``, 无需任何归一化。
ROW_ICBC = {
    "SECUCODE": "601398.SH",
    "SECURITY_NAME_ABBR": "工商银行",
    "STR_CODEH": "01398.HK",
    "STR_NAMEH": "工商银行",
    "LISTING_DATE": "1994-01-01",
}

#: 🔴 多柜台: 上游把两个 H 股代码用**半角逗号**塞在同一列里。
#: ``(二千)`` / ``(五百)`` 是每手股数 (TRADE_UNIT), 已由
#: ``RPT_HKF10_INFO_ORGPROFILE`` 交叉验证。``02958.HK`` 是 2026-11-13 才开的
#: 第二柜台, ``02899.HK`` 是 2003-12-23 的原始柜台。
ROW_ZIJIN = {
    "SECUCODE": "601899.SH",
    "SECURITY_NAME_ABBR": "紫金矿业",
    "STR_CODEH": "02958.HK,02899.HK",
    "STR_NAMEH": "紫金矿业(二千),紫金矿业(五百)",
    "LISTING_DATE": "1998-01-01",
}

ROW_LUOYANG = {
    "SECUCODE": "603993.SH",
    "SECURITY_NAME_ABBR": "洛阳钼业",
    "STR_CODEH": "02957.HK,03993.HK",
    "STR_NAMEH": "洛阳钼业(三千),洛阳钼业(五百)",
    "LISTING_DATE": "2000-01-01",
}

#: 🔴 同一个 H 股被两只 A 股认领, 且**两者 A 股上市日完全相同** (1994-03-24)。
#: 这是消歧最难的一组: 上市日规则失效, 只能靠「A 简称与 H 简称精确相等」破局
#: (H 名是 ``上海医药``, 与 601607.SH 相等)。两条都在 ``test_ah_premium.py`` 里钉。
ROW_SHANGYAO_ZHUANHUAN = {
    "SECUCODE": "600849.SH",
    "SECURITY_NAME_ABBR": "上药转换",
    "STR_CODEH": "02607.HK",
    "STR_NAMEH": "上海医药",
    "LISTING_DATE": "1994-03-24",
}
ROW_SHANGHAI_YIYAO = {
    "SECUCODE": "601607.SH",
    "SECURITY_NAME_ABBR": "上海医药",
    "STR_CODEH": "02607.HK",
    "STR_NAMEH": "上海医药",
    "LISTING_DATE": "1994-03-24",
}

#: A/H 简称**不同**的真实例子 —— 这就是为什么名称相等不能当通用消歧规则。
ROW_VANKE = {
    "SECUCODE": "000002.SZ",
    "SECURITY_NAME_ABBR": "万科A",
    "STR_CODEH": "02202.HK",
    "STR_NAMEH": "万科企业",
    "LISTING_DATE": "1991-01-29",
}
ROW_PANDA = {
    "SECUCODE": "600775.SH",
    "SECURITY_NAME_ABBR": "南京熊猫",
    "STR_CODEH": "00553.HK",
    "STR_NAMEH": "南京熊猫电子股份",
    "LISTING_DATE": "1996-01-01",
}

#: ST / 退市股: **刻意不过滤**。名称关键词过滤是脆弱的猜测 (§13); 它们若缺本地
#: 价格会在服务层自然掉出, 而不是在这里被主观判断掉。
ROW_ST_CHENMING = {
    "SECUCODE": "000488.SZ",
    "SECURITY_NAME_ABBR": "ST晨鸣",
    "STR_CODEH": "01812.HK",
    "STR_NAMEH": "晨鸣纸业",
    "LISTING_DATE": "1997-01-01",
}
ROW_DONGDIAN_TUI = {
    "SECUCODE": "000585.SZ",
    "SECURITY_NAME_ABBR": "东电退",
    "STR_CODEH": "00042.HK",
    "STR_NAMEH": "东北电气",
    "LISTING_DATE": "1993-01-01",
}

#: 占位 A 股代码 (实测 9 个)。``STR_CODEA`` 在这些行上是占位值, 所以插件用的是
#: ``SECUCODE``; 但 ``SECUCODE`` 自己也有占位形态, 必须靠正则挡掉。
PLACEHOLDER_A_CODES = [
    "A04021.SZ",
    "A11047.SH",
    "A11283.SZ",
    "A12031.SZ",
    "A12318.SZ",
    "A14287.SH",
    "A15154.SH",
    "A15241.SH",
    "A22212.SZ",
]

#: 无 H 股的普通 A 股 (占 8387 行里的绝大多数)。
ROW_NO_H = {
    "SECUCODE": "600519.SH",
    "SECURITY_NAME_ABBR": "贵州茅台",
    "STR_CODEH": None,
    "STR_NAMEH": None,
    "LISTING_DATE": "2001-08-27",
}
ROW_EMPTY_H = {
    "SECUCODE": "000001.SZ",
    "SECURITY_NAME_ABBR": "平安银行",
    "STR_CODEH": "",
    "STR_NAMEH": "",
    "LISTING_DATE": "1991-04-03",
}


class _FakeClient:
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


# ---------------------------------------------------------------------------
# 代码形状正则
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "symbol",
    ["601398.SH", "000002.SZ", "300750.SZ", "603993.SH", "830799.BJ", "000488.SZ"],
)
def test_a_symbol_re_accepts_real_a_share_shapes(symbol):
    assert ah_pair.A_SYMBOL_RE.match(symbol)


@pytest.mark.parametrize("symbol", PLACEHOLDER_A_CODES)
def test_a_symbol_re_rejects_placeholder_codes(symbol):
    """🔴 占位码必须挡掉。

    实测 280 行原始候选里有 67 行不在本地 ``instruments`` 维表中, 绝大部分就是这些
    字母开头的占位码。放进来会让 AH 溢价表多出一堆查无此股的行。
    """
    assert not ah_pair.A_SYMBOL_RE.match(symbol)


@pytest.mark.parametrize("symbol", ["601398", "601398.HK", "60139.SH", "6013999.SH", "601398.ss"])
def test_a_symbol_re_rejects_malformed(symbol):
    assert not ah_pair.A_SYMBOL_RE.match(symbol)


@pytest.mark.parametrize("symbol", ["01398.HK", "00042.HK", "02958.HK", "0700.HK", "3750.HK"])
def test_h_symbol_re_accepts_real_hk_shapes(symbol):
    assert ah_pair.H_SYMBOL_RE.match(symbol)


@pytest.mark.parametrize(
    "symbol",
    # 实测同一列里混进来的权证/牛熊证/虚拟代码前缀
    ["H20001.HK", "H50002.HK", "BC1234.HK", "UB8888.HK", "BN0001.HK", "AB1234.HK", "DB9999.HK"],
)
def test_h_symbol_re_rejects_warrant_and_virtual_codes(symbol):
    """上游把权证/CBBC/虚拟代码混进 ``STR_CODEH`` 是已知行为, 必须挡掉。"""
    assert not ah_pair.H_SYMBOL_RE.match(symbol)


# ---------------------------------------------------------------------------
# 多值列拆分
# ---------------------------------------------------------------------------

def test_split_multi_handles_halfwidth_comma():
    """实测形态: 半角逗号连接。"""
    assert ah_pair._split_multi("02958.HK,02899.HK") == ["02958.HK", "02899.HK"]


def test_split_multi_tolerates_fullwidth_comma():
    """同时容全角逗号 —— 同一报表的中文名称列里全角标点很常见, 不值得赌它一致。"""
    assert ah_pair._split_multi("02958.HK，02899.HK") == ["02958.HK", "02899.HK"]


def test_split_multi_strips_whitespace_and_drops_empty_parts():
    assert ah_pair._split_multi(" 02958.HK , ,02899.HK ") == ["02958.HK", "02899.HK"]
    assert ah_pair._split_multi(",,,") == []


@pytest.mark.parametrize("value", [None, "", "   "])
def test_split_multi_returns_empty_for_missing(value):
    assert ah_pair._split_multi(value) == []


def test_split_multi_keeps_single_value_as_one_item():
    assert ah_pair._split_multi("01398.HK") == ["01398.HK"]


# ---------------------------------------------------------------------------
# 上市日解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1994-03-24", dt.date(1994, 3, 24)),
        ("1994-03-24 00:00:00", dt.date(1994, 3, 24)),
        ("1994/03/24", dt.date(1994, 3, 24)),
    ],
)
def test_parse_listing_date_accepts_known_shapes(raw, expected):
    assert ah_pair._parse_listing_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "无", "1994-13-45"])
def test_parse_listing_date_returns_none_on_garbage(raw):
    """解析失败 → ``None`` 并告警。上市日只用于消歧, 缺了顶多计入 ambiguous。"""
    assert ah_pair._parse_listing_date(raw) is None


def test_clean_str_normalizes_blank_to_none():
    assert ah_pair._clean_str(None) is None
    assert ah_pair._clean_str("") is None
    assert ah_pair._clean_str("   ") is None
    assert ah_pair._clean_str(" 工商银行 ") == "工商银行"


# ---------------------------------------------------------------------------
# 解析: 正常路径
# ---------------------------------------------------------------------------

def test_parse_single_counter_row():
    got = ah_pair.parse_ah_pair_candidates([ROW_ICBC])
    assert len(got) == 1
    cand = got[0]
    assert cand.a_symbol == "601398.SH"
    assert cand.a_name == "工商银行"
    assert cand.a_listing_date == dt.date(1994, 1, 1)
    assert cand.h_candidates == ("01398.HK",)
    assert cand.h_names == ("工商银行",)


def test_parse_multi_counter_row_splits_codes_and_aligns_names():
    """多柜台**只拆分不消歧**, 名称按位置与代码对齐。"""
    got = ah_pair.parse_ah_pair_candidates([ROW_ZIJIN])
    assert len(got) == 1
    cand = got[0]
    assert cand.a_symbol == "601899.SH"
    assert cand.h_candidates == ("02958.HK", "02899.HK")
    assert cand.h_names == ("紫金矿业(二千)", "紫金矿业(五百)")
    # 位置对齐是消歧的前提: 名称个数必须等于代码个数
    assert len(cand.h_names) == len(cand.h_candidates)


def test_parse_pads_missing_names_with_none_instead_of_guessing():
    """上游名称个数少于代码个数时补 ``None``, **不猜测**、不复用前一个名称。"""
    row = {**ROW_ZIJIN, "STR_NAMEH": "紫金矿业(二千)"}
    cand = ah_pair.parse_ah_pair_candidates([row])[0]
    assert cand.h_candidates == ("02958.HK", "02899.HK")
    assert cand.h_names == ("紫金矿业(二千)", None)


def test_parse_keeps_extra_names_truncated_to_code_count():
    """名称多于代码时只取前 N 个 —— 位置对应关系不能错位。"""
    row = {**ROW_ZIJIN, "STR_NAMEH": "甲,乙,丙"}
    cand = ah_pair.parse_ah_pair_candidates([row])[0]
    assert cand.h_names == ("甲", "乙")


def test_parse_sorts_by_a_symbol_ascending():
    rows = [ROW_VANKE, ROW_ICBC, ROW_ZIJIN]  # 000002.SZ / 601398.SH / 601899.SH
    got = ah_pair.parse_ah_pair_candidates(rows)
    assert [c.a_symbol for c in got] == ["000002.SZ", "601398.SH", "601899.SH"]


def test_parse_preserves_a_h_name_differences():
    """A/H 简称不同是常态, 原样保留 —— 这是「名称相等不能当通用规则」的证据。"""
    got = {c.a_symbol: c for c in ah_pair.parse_ah_pair_candidates([ROW_VANKE, ROW_PANDA])}
    assert got["000002.SZ"].a_name == "万科A"
    assert got["000002.SZ"].h_names == ("万科企业",)
    assert got["600775.SH"].a_name == "南京熊猫"
    assert got["600775.SH"].h_names == ("南京熊猫电子股份",)


def test_parse_keeps_st_and_delisted_names():
    """🔴 ST / 退市股**刻意保留**。

    名称关键词过滤是脆弱的猜测 (§13): 「ST」今天是退市风险标记, 但上游也可能给出
    「*ST」「退」「S」等变体, 逐个枚举永远漏。它们若缺本地价格会在服务层自然掉出。
    """
    got = ah_pair.parse_ah_pair_candidates([ROW_ST_CHENMING, ROW_DONGDIAN_TUI])
    assert [c.a_symbol for c in got] == ["000488.SZ", "000585.SZ"]
    assert got[0].a_name == "ST晨鸣"
    assert got[1].a_name == "东电退"


def test_parse_keeps_duplicate_h_claims_as_separate_candidates():
    """同一个 H 被两只 A 认领时, 插件**照实输出两条**, 不在这里取舍。

    去重是服务层的职责 (它才有本地维表与名称可比)。插件擅自去重会丢掉
    「上游存在歧义」这个信号。
    """
    got = ah_pair.parse_ah_pair_candidates([ROW_SHANGYAO_ZHUANHUAN, ROW_SHANGHAI_YIYAO])
    assert [c.a_symbol for c in got] == ["600849.SH", "601607.SH"]
    assert all(c.h_candidates == ("02607.HK",) for c in got)
    assert all(c.a_listing_date == dt.date(1994, 3, 24) for c in got)


# ---------------------------------------------------------------------------
# 解析: 丢弃路径
# ---------------------------------------------------------------------------

def test_parse_drops_rows_without_h_code():
    """``STR_CODEH`` 为 None 或空串 → 丢弃 (绝大多数 A 股没有 H 股)。"""
    assert ah_pair.parse_ah_pair_candidates([ROW_NO_H, ROW_EMPTY_H]) == []


@pytest.mark.parametrize("code", PLACEHOLDER_A_CODES)
def test_parse_drops_placeholder_a_codes(code):
    row = {**ROW_ICBC, "SECUCODE": code}
    assert ah_pair.parse_ah_pair_candidates([row]) == []


def test_parse_drops_row_when_secucode_missing():
    row = {k: v for k, v in ROW_ICBC.items() if k != "SECUCODE"}
    assert ah_pair.parse_ah_pair_candidates([row]) == []


def test_parse_uses_secucode_not_str_codea():
    """🔴 必须用 ``SECUCODE`` 而不是 ``STR_CODEA``。

    实测 ``STR_CODEA`` 在部分行上是占位值 (``A04021.SZ``), 而 ``SECUCODE`` 是真实
    代码。这条测试钉住「即使 ``STR_CODEA`` 是垃圾也不受影响」。
    """
    row = {**ROW_ICBC, "STR_CODEA": "A04021.SZ"}
    cand = ah_pair.parse_ah_pair_candidates([row])[0]
    assert cand.a_symbol == "601398.SH"


def test_parse_drops_row_when_all_h_candidates_malformed():
    row = {**ROW_ICBC, "STR_CODEH": "H20001.HK,BC1234.HK", "STR_NAMEH": "权证,牛熊证"}
    assert ah_pair.parse_ah_pair_candidates([row]) == []


def test_parse_keeps_valid_h_candidates_when_some_are_malformed():
    """部分候选不合形状时**保留合形状的那些**并 warn。

    整行丢掉会连带损失一个有效配对 —— 上游把权证混进同一列是已知行为, 不是异常。
    """
    row = {**ROW_ZIJIN, "STR_CODEH": "02899.HK,H20001.HK", "STR_NAMEH": "紫金矿业(五百),权证"}
    cand = ah_pair.parse_ah_pair_candidates([row])[0]
    assert cand.h_candidates == ("02899.HK",)
    assert cand.h_names == ("紫金矿业(五百)",)


def test_parse_handles_empty_input():
    assert ah_pair.parse_ah_pair_candidates([]) == []


# ---------------------------------------------------------------------------
# fetch: 参数传递与错误传播
# ---------------------------------------------------------------------------

def test_fetch_passes_report_filter_columns_and_sort():
    client = _FakeClient([ROW_ICBC])
    ah_pair.fetch_ah_pair_candidates(client)
    call = client.calls[0]
    assert call["report_name"] == "RPT_F10_BASIC_ORGINFO"
    # 🔴 只能按「A 股」过滤 (8387 行), **无法**在服务端过滤「有 H 股」。
    # 实测所有形式都失败: (STR_CODEH!="") → 9201 空; (STR_CODEH!='') → 被静默
    # 忽略仍返回 8387 行 (最危险: 不报错也不生效); is not null / like → 9501。
    assert call["flt"] == '(SECURITY_TYPE_CODE="058001001")'
    assert call["columns"] == ah_pair.AH_PAIR_COLUMNS
    assert call["page_size"] == ah_pair.AH_PAIR_PAGE_SIZE
    assert call["max_pages"] == ah_pair.AH_PAIR_MAX_PAGES
    # SECUCODE 天然唯一, 单键排序即满足翻页所需的全序稳定
    assert call["sort_columns"] == "SECUCODE"
    assert call["sort_types"] == "1"


def test_fetch_page_size_is_large_enough_for_few_round_trips():
    """``pageSize`` 必须够大: 实测 9000 可单页取全 8387 行 / 0.67s。

    取 5000 是常态 2 页 ≈1.3s 的折中 —— 既不给上游单次请求过大压力, 也不至于
    翻 17 页。这条测试挡住「顺手改回默认 500」(那会变成 17 个请求)。
    """
    assert ah_pair.AH_PAIR_PAGE_SIZE >= 1000
    assert ah_pair.AH_PAIR_MAX_PAGES * ah_pair.AH_PAIR_PAGE_SIZE >= 8387


def test_columns_whitelist_is_minimal():
    """只取需要的 5 列 (整表 57 列) —— 少传就少一份误用与带宽。"""
    assert ah_pair.AH_PAIR_COLUMNS == "SECUCODE,SECURITY_NAME_ABBR,STR_CODEH,STR_NAMEH,LISTING_DATE"


def test_fetch_returns_empty_list_when_upstream_empty():
    """整表返回空 → ``[]``。

    ⚠️ 对本报表而言 8387 行的表返回空意味着上游出问题了, 但插件层无法区分
    「真的没有 AH 股」与「上游异常」; 服务层据 ``candidate_count == 0`` fail closed。
    """
    assert ah_pair.fetch_ah_pair_candidates(_FakeClient([])) == []


def test_fetch_propagates_eastmoney_error():
    """上游失败必须上抛, 不降级成 ``[]`` (§4)。"""
    client = _FakeClient(EastmoneyError("9501 参数预处理错误"))
    with pytest.raises(EastmoneyError, match="9501"):
        ah_pair.fetch_ah_pair_candidates(client)


def test_candidate_field_count_is_pinned():
    """字段数钉死: ``h_candidates`` 与 ``h_names`` 必须成对存在。"""
    assert ah_pair.AhPairCandidate._fields == (
        "a_symbol",
        "a_name",
        "a_listing_date",
        "h_candidates",
        "h_names",
    )


def test_source_constant_is_stable():
    """``source`` 会进 API 响应, 改动等于改契约。"""
    assert ah_pair.AH_PAIR_SOURCE == "eastmoney_datacenter_f10_orginfo"
