"""港股东财财务接口契约测试 (插件层, 全离线)。

钉住的都是**实测得来、代价高昂**的口径, 一旦上游改动或本地重构走样就会红:

- ``hk_fin_secucode`` 的双后缀回归 —— 服务端对畸形 ``SECUCODE`` **静默返回空**
  (``code=9201``) 而非报错, 症状是"同步成功但 0 行", 极难定位;
- ``filter`` 多条件并列即 AND, 且**不支持 ``between``**;
- ``pageSize`` 硬上限 500、``in`` 批量上限 200 (超过 HTTP 414);
- ``PER_SHARES`` = 每手股数 (实测 8/8 匹配真值);
- 币种双轨: 宽表 ``CURRENCY`` 是**计价币种**, 长表 ``CURRENCY_CODE`` 才是
  **编报币种**, 且长表 ``CURRENCY`` 是中文名 (``"人民币"``) 而非 ISO 码;
- ``period_name`` 必须由 ``DATE_TYPE_CODE`` 推导, 不能用 ``REPORT_TYPE``
  (宽表带年份前缀 ``"2026年中报"``, 长表是裸值 ``"中报"``, 跨表不可比)。
"""

from __future__ import annotations

import logging

import pytest

from app.plugins.eastmoney import hk_financials as hf


# ---------------------------------------------------------------------------
# 测试替身: 只实现 datacenter_report
# ---------------------------------------------------------------------------
class FakeClient:
    """按 (report, filter, page_number) 返回预设页; 记录调用参数供断言。"""

    def __init__(self, pages: dict[int, list[dict]], report: str = hf.RPT_HK_MAIN_INDICATOR):
        self._pages = pages
        self._report = report
        self.calls: list[dict] = []

    def datacenter_report(self, report_name, flt=None, **kw):
        self.calls.append({"report": report_name, "flt": flt, **kw})
        assert report_name == self._report, f"意外报表名 {report_name}"
        return list(self._pages.get(kw.get("page_number", 1), []))


def _mi(code="00700", **over) -> dict:
    """构造一行主指标 payload (只填被测字段, 其余留空)。"""
    row = {
        "SECUCODE": f"{code}.HK",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "腾讯控股",
        "ORG_CODE": "10009066",
        "ORG_TYPE": "一般企业",
        "REPORT_DATE": "2026-06-30 00:00:00",
        "STD_REPORT_DATE": "2026-06-30 00:00:00",
        "START_DATE": "2026-01-01 00:00:00",
        "FISCAL_YEAR": "12-31",
        "DATE_TYPE_CODE": "002",
        "REPORT_TYPE": "2026年中报",
        "ACCOUNT_STANDARD": "国际会计准则",
        "CURRENCY": "HKD",
        "IS_CNY_CODE": 0,
        "PER_SHARES": 100,
        "PE_TTM": 13.171,
        "PB_TTM": 2.688,
        "ROE_AVG": 9.966,
        "OPERATE_INCOME": 401243000000,
        "HOLDER_PROFIT": 114115000000,
        "NETCASH_OPERATE": 154060000000,
        "TOTAL_ASSETS": 2159394000000,
        "LOAN_ASSETS": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# hk_fin_secucode: 规范化与双后缀回归
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw", ["00700", "00700.HK", "00700.hk", " 700 ", "700.HK", "700"]
)
def test_secucode_normalizes_to_five_digit_hk(raw):
    """裸代码与内部 symbol 同形输入都必须得到 ``00700.HK``。"""
    assert hf.hk_fin_secucode(raw) == "00700.HK"


def test_secucode_never_double_suffixes():
    """🔴 回归: 内部 symbol 已含 ``.HK``, 再追加会得到 ``00700.HK.HK``。

    服务端对畸形 SECUCODE **静默返回空结果**而非报错, 症状是"同步成功 0 行"。
    """
    assert hf.hk_fin_secucode("00700.HK") == "00700.HK"
    assert ".HK.HK" not in hf.hk_fin_secucode("00700.HK")


@pytest.mark.parametrize("bad", ["600519", "600519.SH", "", "abc", "123456.HK", None])
def test_secucode_rejects_non_hk(bad):
    """A 股代码/空值必须抛错, 不能静默拼出一个查不到数据的 filter。"""
    with pytest.raises(ValueError):
        hf.hk_fin_secucode(bad)


# ---------------------------------------------------------------------------
# hk_fin_filter: 东财 filter 语法
# ---------------------------------------------------------------------------
def test_filter_none_when_no_condition():
    """无条件 → None (取全表), 而不是空串 —— 空串会被服务端当非法参数。"""
    assert hf.hk_fin_filter() is None


def test_filter_juxtaposition_means_and():
    """多条件**直接并列**即 AND, 不需要也不应该写 ``and`` 关键字。"""
    got = hf.hk_fin_filter(since="2024-01-01", date_type_code="001")
    assert got == "(REPORT_DATE>='2024-01-01')(DATE_TYPE_CODE=\"001\")"


def test_filter_date_window_uses_two_comparisons():
    """🔴 不能用 ``between`` —— 服务端 ANTLR 解析失败返回 code=9501。"""
    got = hf.hk_fin_filter(since="2024-01-01", until="2026-12-31")
    assert "between" not in got.lower()
    assert got == "(REPORT_DATE>='2024-01-01')(REPORT_DATE<='2026-12-31')"


def test_filter_in_clause_quotes_each_secucode():
    got = hf.hk_fin_filter(secucodes=["00700.HK", "09988.HK"])
    assert got == '(SECUCODE in ("00700.HK","09988.HK"))'


def test_filter_rejects_oversized_in_batch():
    """超过 200 只必须**本地**拒绝: 服务端会用 HTTP 414 (URI Too Long) 拒绝,
    那是网络层错误, 比本地 ValueError 难定位得多。"""
    with pytest.raises(ValueError, match="200"):
        hf.hk_fin_filter(secucodes=[f"{i:05d}.HK" for i in range(hf.HK_FIN_IN_BATCH_MAX + 1)])


def test_filter_accepts_batch_at_limit():
    codes = [f"{i:05d}.HK" for i in range(hf.HK_FIN_IN_BATCH_MAX)]
    assert hf.hk_fin_filter(secucodes=codes) is not None


# ---------------------------------------------------------------------------
# 常量: 报表名与报告期映射
# ---------------------------------------------------------------------------
def test_report_names_are_hkf10_family():
    """实测只有这三个报表名存在; 其余 ``RPT_HKF10_FN_*`` 变体全部 9501。"""
    assert hf.RPT_HK_MAIN_INDICATOR == "RPT_HKF10_FN_MAININDICATOR"
    assert hf.RPT_HK_INCOME == "RPT_HKF10_FN_INCOME"
    assert hf.RPT_HK_BALANCE == "RPT_HKF10_FN_BALANCE"


def test_date_type_codes_cover_four_period_kinds():
    """实测 00700 连续 8 期确认的映射。"""
    assert hf.HK_DATE_TYPE_NAMES == {
        "001": "年报", "002": "中报", "003": "一季报", "004": "三季报",
    }
    assert hf.HK_DATE_TYPE_ANNUAL == "001"


def test_hk_has_no_cashflow_report_but_wide_table_has_the_columns():
    """港股**没有**独立现金流量表报表, 现金流核心项作为宽表列存在。"""
    for col in ("netcash_operate", "netcash_invest", "netcash_finance", "end_cash"):
        assert col in hf.HK_CASHFLOW_COLUMNS
        assert col in hf.HK_MAIN_INDICATOR_FIELDS


def test_page_size_cap_matches_server():
    """实测传 1000/5000 服务端仍只返回 500 行, 且 pages 恒按 count/500 算。"""
    assert hf.HK_FIN_PAGE_SIZE_MAX == 500


# ---------------------------------------------------------------------------
# fetch_hk_main_indicator: 分页
# ---------------------------------------------------------------------------
def test_fetch_rejects_page_size_above_server_cap():
    """服务端会**静默截断**到 500; 若按调用方给的 page_size 判断终止条件会提前停,
    导致数据缺失。直接拒绝比静默少取数据安全。"""
    with pytest.raises(ValueError, match="500"):
        hf.fetch_hk_main_indicator(FakeClient({}), page_size=1000)


def test_fetch_stops_when_page_is_short():
    """本页行数 < page_size 即终止, 不依赖响应信封的 count/pages。"""
    c = FakeClient({1: [_mi() for _ in range(3)]})
    rows = hf.fetch_hk_main_indicator(c, page_size=5)
    assert len(rows) == 3
    assert len(c.calls) == 1  # 短页后不再翻第二页


def test_fetch_paginates_until_short_page():
    c = FakeClient({1: [_mi() for _ in range(2)], 2: [_mi(REPORT_DATE="2025-12-31")]})
    rows = hf.fetch_hk_main_indicator(c, page_size=2)
    assert len(rows) == 3
    assert [call["page_number"] for call in c.calls] == [1, 2]


def test_fetch_stops_on_empty_page():
    """``code=9201`` (合法空) 被 datacenter_report 转成 [], 翻页自然终止。"""
    c = FakeClient({1: [_mi()], 2: []})
    assert len(hf.fetch_hk_main_indicator(c, page_size=1)) == 1
    assert len(c.calls) == 2


def test_fetch_warns_and_stops_at_max_pages(caplog):
    """服务端异常返回恒定满页时不能无限翻页。"""
    c = FakeClient({i: [_mi() for _ in range(2)] for i in range(1, 10)})
    with caplog.at_level(logging.WARNING):
        rows = hf.fetch_hk_main_indicator(c, page_size=2, max_pages=3)
    assert len(rows) == 6
    assert "翻页达到上限" in caplog.text


def test_fetch_passes_filter_and_stable_sort():
    """排序必须含 SECUCODE 作次键: 只按 REPORT_DATE 排时同期内顺序不定,
    翻页会漏行或重复行。"""
    c = FakeClient({1: [_mi()]})
    hf.fetch_hk_main_indicator(c, since="2024-01-01", date_type_code="001")
    call = c.calls[0]
    assert call["flt"] == "(REPORT_DATE>='2024-01-01')(DATE_TYPE_CODE=\"001\")"
    assert "SECUCODE" in call["sort_columns"]


def test_fetch_reports_progress():
    c = FakeClient({1: [_mi() for _ in range(2)], 2: [_mi()]})
    seen = []
    hf.fetch_hk_main_indicator(
        c, page_size=2, on_progress=lambda n, p: seen.append((n, p))
    )
    assert seen == [(2, 1), (3, 2)]


# ---------------------------------------------------------------------------
# fetch_hk_statement: 长表按标的取
# ---------------------------------------------------------------------------
def test_statement_rejects_unknown_report():
    with pytest.raises(ValueError):
        hf.fetch_hk_statement(FakeClient({}), "00700.HK", report="RPT_BOGUS")


def test_statement_filters_by_single_secucode():
    c = FakeClient({1: []}, report=hf.RPT_HK_INCOME)
    hf.fetch_hk_statement(c, "00700", report=hf.RPT_HK_INCOME, since="2025-01-01")
    assert c.calls[0]["flt"] == "(REPORT_DATE>='2025-01-01')(SECUCODE in (\"00700.HK\"))"


def test_statement_accepts_internal_symbol_form():
    """传 ``00700.HK`` 与 ``00700`` 等价 (回归双后缀 bug)。"""
    c = FakeClient({1: []}, report=hf.RPT_HK_BALANCE)
    hf.fetch_hk_statement(c, "00700.HK", report=hf.RPT_HK_BALANCE)
    assert '"00700.HK"' in c.calls[0]["flt"]
    assert ".HK.HK" not in c.calls[0]["flt"]


# ---------------------------------------------------------------------------
# hk_main_indicator_rows: 宽表映射
# ---------------------------------------------------------------------------
def test_wide_mapping_symbol_is_internal_form():
    rows = hf.hk_main_indicator_rows([_mi()])
    assert rows[0]["symbol"] == "00700.HK"
    assert rows[0]["secucode"] == "00700.HK"


def test_wide_mapping_secucode_fallback_has_no_double_suffix():
    """上游缺 SECUCODE 时用 symbol 兜底, 🔴 不可再追加 ``.HK``。"""
    row = _mi()
    row["SECUCODE"] = None
    assert hf.hk_main_indicator_rows([row])[0]["secucode"] == "00700.HK"


def test_wide_mapping_period_name_from_date_type_code():
    """``period_name`` 由 ``DATE_TYPE_CODE`` 查表, **不用 ``REPORT_TYPE``** ——
    宽表 REPORT_TYPE 带年份前缀 (``2026年中报``), 长表是裸值 (``中报``), 跨表不可比。"""
    for dtc, name in hf.HK_DATE_TYPE_NAMES.items():
        assert hf.hk_main_indicator_rows([_mi(DATE_TYPE_CODE=dtc)])[0]["period_name"] == name
    row = hf.hk_main_indicator_rows([_mi(DATE_TYPE_CODE="002")])[0]
    assert row["period_name"] == "中报"
    assert row["report_type"] == "2026年中报"  # 原值保留, 但不作为展示依据


def test_wide_mapping_unknown_date_type_gives_none_period_name():
    """上游新增报告期类型时宁可 None, 不猜一个中文名。"""
    assert hf.hk_main_indicator_rows([_mi(DATE_TYPE_CODE="999")])[0]["period_name"] is None


def test_wide_mapping_lot_size_from_per_shares():
    """⭐ ``PER_SHARES`` = 每手股数。实测 8/8 匹配真值 (汇丰400/腾讯100/工行1000/
    友邦200/中移动500/阿里100/港交所100/美团100)。"""
    assert hf.hk_main_indicator_rows([_mi(PER_SHARES=400)])[0]["lot_size"] == 400


def test_wide_mapping_dates_are_iso_strings():
    """东财返回 ``"2026-06-30 00:00:00"``, 落库统一截成 ISO 日期串。"""
    row = hf.hk_main_indicator_rows([_mi()])[0]
    assert row["report_date"] == "2026-06-30"
    assert row["std_report_date"] == "2026-06-30"
    assert row["start_date"] == "2026-01-01"


def test_wide_mapping_pricing_currency_is_not_report_currency():
    """🔴 宽表 ``CURRENCY`` 是**计价币种** (港币柜台 HKD / 人民币柜台 CNY),
    不是科目金额的编报币种。字段名刻意区分以免误用。"""
    row = hf.hk_main_indicator_rows([_mi(CURRENCY="HKD")])[0]
    assert row["pricing_currency"] == "HKD"
    assert "report_currency" not in row


def test_wide_mapping_pricing_currency_tracks_cny_counter():
    """实测 3312 行 0 反例: ``pricing_currency=='CNY'`` ⟺ ``is_cny_code==1``。"""
    row = hf.hk_main_indicator_rows([_mi(SECURITY_CODE="89988", CURRENCY="CNY", IS_CNY_CODE=1)])[0]
    assert row["is_cny_code"] == 1
    assert row["pricing_currency"] == "CNY"


def test_wide_mapping_keeps_sector_specific_columns():
    """金融股专有列必须保留 (一般企业为 None): 前端按 org_type 分支渲染。"""
    general = hf.hk_main_indicator_rows([_mi()])[0]
    assert general["org_type"] == "一般企业"
    assert general["loan_assets"] is None
    bank = hf.hk_main_indicator_rows(
        [_mi(ORG_TYPE="银行", LOAN_ASSETS=61.9, NET_INTEREST_INCOME=1.2e11)]
    )[0]
    assert bank["org_type"] == "银行"
    assert bank["loan_assets"] == 61.9
    assert bank["net_interest_income"] == 1.2e11


@pytest.mark.parametrize("raw", ["-", "--", "", "None", None, "N/A"])
def test_wide_mapping_placeholder_values_become_none(raw):
    """东财用 ``"-"``/``"--"``/空串表示"不适用", 不能当成 0 或字符串落库。"""
    assert hf.hk_main_indicator_rows([_mi(PE_TTM=raw)])[0]["pe_ttm"] is None


def test_wide_mapping_numeric_strings_are_cast():
    assert hf.hk_main_indicator_rows([_mi(PE_TTM="13.171")])[0]["pe_ttm"] == 13.171
    assert hf.hk_main_indicator_rows([_mi(TOTAL_ASSETS="2,159,394,000,000")])[0]["total_assets"] == 2159394000000.0


def test_wide_mapping_skips_rows_without_symbol_or_date():
    """无法定位标的或期间的行必须丢弃, 否则会产生无法 join 的孤儿行。"""
    assert hf.hk_main_indicator_rows([_mi(SECURITY_CODE=None)]) == []
    assert hf.hk_main_indicator_rows([_mi(REPORT_DATE=None)]) == []


def test_wide_mapping_skips_non_digit_code_instead_of_writing_none():
    """脏代码 (非纯数字) 跳过, 而不是写出 ``symbol=None``。"""
    assert hf.hk_main_indicator_rows([_mi(SECURITY_CODE="ABC")]) == []


def test_wide_mapping_column_order_matches_contract():
    """列顺序即存储契约, 变动会让按位置取列的读端错位。"""
    row = hf.hk_main_indicator_rows([_mi()])[0]
    assert list(row) == list(hf.HK_MAIN_INDICATOR_COLUMNS)


def test_wide_mapping_is_cny_code_is_int_not_bool():
    """保持与东财一致的 0/1 整型, 便于与 ``is_cny_counter`` 交叉校验。"""
    assert hf.hk_main_indicator_rows([_mi(IS_CNY_CODE=1)])[0]["is_cny_code"] == 1
    assert hf.hk_main_indicator_rows([_mi(IS_CNY_CODE=0)])[0]["is_cny_code"] == 0


# ---------------------------------------------------------------------------
# hk_statement_rows: 长表映射
# ---------------------------------------------------------------------------
def _st(code="00700", **over) -> dict:
    row = {
        "SECUCODE": f"{code}.HK",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "腾讯控股",
        "ORG_CODE": "10009066",
        "ORG_TYPE": "一般企业",
        "REPORT_DATE": "2026-06-30 00:00:00",
        "STD_REPORT_DATE": "2026-06-30 00:00:00",
        "START_DATE": "2026-01-01 00:00:00",
        "FISCAL_YEAR": "12-31",
        "DATE_TYPE_CODE": "002",
        "REPORT_TYPE": "中报",
        "ACCOUNT_STANDARD": "国际会计准则",
        "CURRENCY": "人民币",
        "CURRENCY_CODE": "CNY",
        "STD_ITEM_CODE": "004001001",
        "ITEM_NAME": "营业额",
        "AMOUNT": 396431000000,
        "YOY_RATIO": 9.644899,
        "REMARK": None,
    }
    row.update(over)
    return row


def test_statement_report_currency_uses_iso_code_not_chinese_name():
    """🔴 长表 ``CURRENCY`` 是中文名 ``"人民币"``, ``CURRENCY_CODE`` 才是 ISO ``CNY``。
    取错字段会让币种过滤/分组全部失效。"""
    row = hf.hk_statement_rows([_st()])[0]
    assert row["report_currency"] == "CNY"
    assert row["report_currency"] != "人民币"


def test_statement_keeps_hk_ifrs_item_names_verbatim():
    """港股科目名是港式/IFRS 表述, 前端**不能复用 A 股标签**, 故原样保留不翻译。"""
    names = {r["item_name"] for r in hf.hk_statement_rows([
        _st(STD_ITEM_CODE="004001001", ITEM_NAME="营业额"),
        _st(STD_ITEM_CODE="004007999", ITEM_NAME="毛利"),
        _st(STD_ITEM_CODE="004010999", ITEM_NAME="经营溢利"),
        _st(STD_ITEM_CODE="004011999", ITEM_NAME="除税前溢利"),
        _st(STD_ITEM_CODE="004025002", ITEM_NAME="股东应占溢利"),
    ])}
    assert names == {"营业额", "毛利", "经营溢利", "除税前溢利", "股东应占溢利"}


def test_statement_amount_may_be_negative():
    """实测「应占联营公司溢利」为负 (-6373000000), 不能取绝对值或当缺失。"""
    row = hf.hk_statement_rows([_st(AMOUNT=-6373000000)])[0]
    assert row["amount"] == -6373000000


def test_statement_skips_rows_without_item_code():
    """长表一行一科目, 缺科目码的行无法定位, 必须丢弃。"""
    assert hf.hk_statement_rows([_st(STD_ITEM_CODE=None)]) == []
    assert hf.hk_statement_rows([_st(STD_ITEM_CODE="  ")]) == []


def test_statement_column_order_matches_contract():
    row = hf.hk_statement_rows([_st()])[0]
    assert list(row) == list(hf.HK_STATEMENT_COLUMNS)


def test_statement_and_wide_use_different_currency_field_names():
    """宽表 ``pricing_currency`` vs 长表 ``report_currency`` —— 刻意不同名,
    防止把计价币种当编报币种用 (腾讯: 计价 HKD, 编报 CNY)。"""
    wide = hf.hk_main_indicator_rows([_mi()])[0]
    stmt = hf.hk_statement_rows([_st()])[0]
    assert wide["pricing_currency"] == "HKD"
    assert stmt["report_currency"] == "CNY"
    assert "pricing_currency" not in stmt
    assert "report_currency" not in wide


# ---------------------------------------------------------------------------
# hk_lot_sizes
# ---------------------------------------------------------------------------
def test_lot_sizes_takes_latest_report_date():
    """同一标的多期取最新报告期 (港股年结日不统一, 不能按输入顺序取最后一个)。"""
    lots = hf.hk_lot_sizes([
        _mi(REPORT_DATE="2024-12-31 00:00:00", PER_SHARES=100),
        _mi(REPORT_DATE="2026-06-30 00:00:00", PER_SHARES=200),
        _mi(REPORT_DATE="2025-12-31 00:00:00", PER_SHARES=300),
    ])
    assert lots == {"00700.HK": 200}


@pytest.mark.parametrize("bad", [None, 0, -100, "--"])
def test_lot_sizes_skips_invalid(bad):
    """0/负数/占位符不是合法每手股数, 落库会污染下单取整。"""
    assert hf.hk_lot_sizes([_mi(PER_SHARES=bad)]) == {}


def test_lot_sizes_covers_multiple_symbols():
    lots = hf.hk_lot_sizes([
        _mi(SECURITY_CODE="00005", PER_SHARES=400),
        _mi(SECURITY_CODE="01398", PER_SHARES=1000),
        _mi(SECURITY_CODE="01299", PER_SHARES=200),
    ])
    assert lots == {"00005.HK": 400, "01398.HK": 1000, "01299.HK": 200}


def test_lot_sizes_skips_non_digit_code():
    assert hf.hk_lot_sizes([_mi(SECURITY_CODE="ABC", PER_SHARES=100)]) == {}


# ---------------------------------------------------------------------------
# 规范化投影表: 语义等价性与已排除的陷阱
# ---------------------------------------------------------------------------
def test_projection_targets_are_the_four_ashare_tables():
    assert set(hf.HK_CANONICAL_PROJECTION) == {
        "metrics", "income", "balance_sheet", "cash_flow",
    }


def test_projection_sources_all_exist_in_wide_schema():
    """投影引用的每个港股字段都必须真实存在于宽表 schema, 否则静默产出 null 列。"""
    for table, proj in hf.HK_CANONICAL_PROJECTION.items():
        for canonical, src in proj.items():
            assert src in hf.HK_MAIN_INDICATOR_FIELDS, f"{table}.{canonical} ← {src} 不存在"


def test_projection_excludes_fiscal_year_name_clash():
    """🔴 A 股 ``fiscal_year`` 是 Int64 年份, 港股东财 ``FISCAL_YEAR`` 是 ``"12-31"``
    字符串 (财年结日月-日)。同名不同义, 绝不能进投影表。"""
    for proj in hf.HK_CANONICAL_PROJECTION.values():
        assert "fiscal_year" not in proj
        assert "FISCAL_YEAR" not in proj.values()


def test_projection_excludes_turnover_days_vs_ratio_clash():
    """🔴 港股 ``*_TDAYS`` 是周转**天数**, A 股 ``*_turnover*`` 是周转**率/次数**,
    互为倒数关系而非同值, 映射了就是错数据。"""
    banned = {"inventory_tdays", "accounts_rece_tdays", "current_assets_tdays",
              "total_assets_tdays"}
    for proj in hf.HK_CANONICAL_PROJECTION.values():
        assert not (banned & set(proj.values()))


def test_projection_excludes_end_cash_vs_cash_and_equivalents():
    """🔴 ``END_CASH`` 是现金流量表的期末现金, ≠ 资产负债表的货币资金。"""
    for proj in hf.HK_CANONICAL_PROJECTION.values():
        assert "cash_and_equivalents" not in proj
        assert "end_cash" not in proj.values()


def test_projection_roe_maps_to_weighted_average_not_annualized():
    """A 股 ``roe`` 是加权平均, 对应 ``ROE_AVG`` 而非年化的 ``ROE_YEARLY``。"""
    assert hf.HK_CANONICAL_PROJECTION["metrics"]["roe"] == "roe_avg"


def test_hk_only_fields_do_not_collide_with_canonical_names():
    """独有字段若与规范名重名, select 会因重复列名报错。"""
    canonical = {c for proj in hf.HK_CANONICAL_PROJECTION.values() for c in proj}
    assert not (canonical & set(hf.HK_ONLY_CANONICAL_FIELDS))


def test_hk_only_fields_exist_in_wide_schema():
    for f in hf.HK_ONLY_CANONICAL_FIELDS:
        assert f in hf.HK_MAIN_INDICATOR_COLUMNS, f"{f} 不在宽表 schema 里"
