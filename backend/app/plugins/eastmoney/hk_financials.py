"""东方财富港股财务接口 — 报表名、filter 构造、分页取数与行映射。

数据源: ``datacenter-web.eastmoney.com/api/data/v1/get``。该 host 在
``client.py`` 中拥有**独立的令牌桶 / 熔断器 / 封禁池**, 与 push2 行情互不影响
(实测 push2 被封时 datacenter 仍可用), 因此财务同步不会与行情同步争抢配额。

**不引入 akshare**: 直连即可拿到全部所需字段, 少一个重依赖 (akshare 会拉入
lxml/beautifulsoup4/py-mini-racer 等), 也少一层版本漂移风险。

═══════════════════════════════════════════════════════════════════
实测口径 (2026-08, 探测脚本见 docs/hk-market-plan.md)
═══════════════════════════════════════════════════════════════════

**报表名** — 港股 F10 财务系列, 前缀 ``RPT_HKF10_FN_``:

============================  ==========  ================================
报表名                        形态        规模 (REPORT_DATE>='2024-01-01')
============================  ==========  ================================
``RPT_HKF10_FN_MAININDICATOR`` 宽表       15,774 行 / **32 页**
``RPT_HKF10_FN_INCOME``        长表(科目) 417,063 行 / **835 页**
``RPT_HKF10_FN_BALANCE``       长表(科目) 637,825 行 / **1276 页**
============================  ==========  ================================

长表是宽表的 26~40 倍 (约 55 个科目 × 标的 × 期间), **全市场同步不现实**,
因此: 宽表走全市场分页, 长表按单标的取 (详情页下钻)。

**不存在的报表名** (均返回 ``code=9501 报表配置不存在``, 已逐个试过):
``RPT_HKF10_FN_MAINFINADATA`` / ``_CASHFLOW`` / ``_CASHFLOWS`` / ``_CASH`` /
``_CF`` / ``_CASHFLW`` / ``_CASHFLOWSTATE`` / ``_GOLD`` / ``_MAININDEX`` /
``_INDICATOR`` / ``_EQUITY`` / ``_RATIOS``, 以及 ``RPT_HKFINANCE_MAINFINADATA``、
``RPT_DMSK_HKFIN_MAIN``。

**港股没有独立的现金流量表报表** — 但现金流核心项已作为宽表列存在:
``NETCASH_OPERATE`` / ``NETCASH_INVEST`` / ``NETCASH_FINANCE`` / ``END_CASH`` /
``OCF_SALES`` / ``PER_NETCASH_OPERATE``。

**filter 语法**:

- 单条件 ``(SECUCODE="00700.HK")``; 也可用 ``(SECURITY_CODE="00700")``。
- **多条件并列即 AND**: ``(REPORT_DATE>='2024-01-01')(DATE_TYPE_CODE="001")``。
- 支持 ``in``: ``(SECUCODE in ("00700.HK","09988.HK"))``。
- 🔴 **不支持 ``between``** — 会返回 ``code=9501 参数预处理错误:
  org.antlr.v4.runtime.NoViableAltException``。必须拆成 ``>=`` 与 ``<=``。

**分页与批量上限 (实测)**:

- ``pageSize`` **硬上限 500** — 传 1000 / 5000 服务端仍只返回 500 行,
  且 ``result.pages`` 恒按 count/500 计算。
- ``in`` 批量: n=200 (filter 长 2215 字符) 正常; **n=500 触发 HTTP 414
  (URI Too Long)** — filter 走 query string。故 ``_IN_BATCH_MAX = 200``。
- 响应信封 ``result`` 含 ``count`` / ``pages`` / ``data``, 但
  ``client.datacenter_report`` 只返回 ``data``; 本模块以
  "本页行数 < page_size" 作为翻页终止条件, 不依赖信封。

**``DATE_TYPE_CODE`` 报告期类型** (实测 00700 连续 8 期确认):

=====  ========  ============
代码   含义      REPORT_DATE
=====  ========  ============
001    年报      各股自己的财年结日 (**不统一**, 见下)
002    中报      06-30
003    一季报    03-31
004    三季报    09-30
=====  ========  ============

🔴 **港股年结日不统一** — 与 A 股 (全部 12-31) 的根本差异。实测
``(DATE_TYPE_CODE="001")`` 的 REPORT_DATE 覆盖 03-31 / 04-30 / 05-31 /
06-30 / 12-31 等。因此**不能假设年报=12月**, 排序与"最新一期"判断必须以
``REPORT_DATE`` 实际值为准, 且同比只能与 ``_SQ`` 字段 (去年同期) 或
同 ``DATE_TYPE_CODE`` 的上一期比, 不能与"上一个自然年 12-31"比。

**``REPORT_TYPE`` 表示法按表不同**: 宽表是 ``"2026年中报"`` (带年份前缀),
长表是 ``"中报"`` (裸值)。不要跨表比较该字段。

🔴 **币种是双轨的, 且两张表表示法不同** (实测 00700 腾讯):

===================  ==============  =============================
表                   字段            值
===================  ==============  =============================
MAININDICATOR        ``CURRENCY``    ``HKD``   (计价/上市币种)
BALANCE              ``CURRENCY``    ``人民币`` (编报币种, 中文名!)
BALANCE              ``CURRENCY_CODE`` ``CNY``  (编报币种, ISO)
===================  ==============  =============================

腾讯实际以**人民币**编报, 但作为港股以**港币**计价交易。含义:

- 报表科目金额 (INCOME / BALANCE 的 ``AMOUNT``) 用**编报币种**;
- 估值类 (``PE_TTM`` / ``PB_TTM`` / ``TOTAL_MARKET_CAP`` / ``HKSK_MARKET_CAP``)
  与 ``DPS_HKD`` 用**港币**;
- 宽表的 ``CURRENCY`` 是计价币种, **不是**科目金额的币种。

因此本模块把宽表 ``CURRENCY`` 映射为 ``pricing_currency``, 把长表
``CURRENCY_CODE`` 映射为 ``report_currency``, **两个字段名不同以免误用**。
任何跨标的比较金额前必须先按币种分组, 否则会把人民币与港币直接相加。

**``ORG_TYPE`` 决定字段族是否有值**: 实测取值 ``一般企业`` / ``银行`` /
``证券`` / ``保险``。金融股专有列 (``LOAN_ASSETS`` / ``DEPOSIT_ASSETS`` /
``NET_INTEREST_INCOME`` / ``PREMIUM_INCOME`` / ``FEE_COMMISSION_INCOME``)
对一般企业全为 ``None``; 反之 ``INVENTORY_TDAYS`` 等对银行无意义。
前端必须按 ``org_type`` 分支或隐藏空区块, 不能一律渲染。

**``IS_CNY_CODE``** — 东财自带的人民币柜台标记 (0/1), 是
``hk.is_cny_counter`` (按代码前缀 + 名称 ``-R`` 后缀推导) 的**独立交叉验证源**。
实测 ``IS_CNY_CODE=1`` 的样本为 ``80941.HK 中国移动-R`` / ``80883.HK
中国海洋石油-R`` / ``80175.HK 吉利汽车-R`` 等, 与推导规则一致。

⭐ **``PER_SHARES`` = 每手股数 (board lot)** — 实测 8/8 精确匹配已知真值:

===============  ==========  ======
代码             名称        PER_SHARES
===============  ==========  ======
00005            汇丰控股    400
00700            腾讯控股    100
01398            工商银行    1000
09988            阿里巴巴-W  100
00388            香港交易所  100
03690            美团-W      100
01299            友邦保险    200
00941            中国移动    500
===============  ==========  ======

港股每手股数**因股而异** (非市场级常量), 故 ``market.HK_RULES.lot_size``
保持 ``None``, 由本表按标的落到 ``instruments_hk.lot_size``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from app.plugins.eastmoney.client import EastmoneyClient

logger = logging.getLogger(__name__)

# ── 报表名 ────────────────────────────────────────────────────────
#: 财务主指标 (宽表): 一行 = 一个标的 × 一个报告期。财务概况页的主数据源。
RPT_HK_MAIN_INDICATOR = "RPT_HKF10_FN_MAININDICATOR"
#: 利润表 (长表): 一行 = 一个科目。``STD_ITEM_CODE`` / ``ITEM_NAME`` / ``AMOUNT``。
RPT_HK_INCOME = "RPT_HKF10_FN_INCOME"
#: 资产负债表 (长表): 结构同利润表。
RPT_HK_BALANCE = "RPT_HKF10_FN_BALANCE"

#: 港股**没有**独立现金流量表报表; 现金流核心项在宽表列里。
HK_CASHFLOW_COLUMNS = (
    "netcash_operate",
    "netcash_invest",
    "netcash_finance",
    "end_cash",
    "ocf_sales",
    "per_netcash_operate",
)

#: ``DATE_TYPE_CODE`` → 中文报告期名。实测确认, 用于展示与去重。
HK_DATE_TYPE_NAMES: dict[str, str] = {
    "001": "年报",
    "002": "中报",
    "003": "一季报",
    "004": "三季报",
}

#: 年报的代码。**注意港股年报的 REPORT_DATE 是各股自己的财年结日, 不统一为 12-31。**
HK_DATE_TYPE_ANNUAL = "001"

#: ``pageSize`` 服务端硬上限 (实测传更大值仍只返回 500 行)。
HK_FIN_PAGE_SIZE_MAX = 500

#: ``SECUCODE in (...)`` 单批标的数上限。实测 n=200 正常, n=500 触发 HTTP 414。
#: 取 200 而非更接近 414 阈值的值, 给 URL 其余部分留余量。
HK_FIN_IN_BATCH_MAX = 200

#: 全市场分页的安全页数上限。按实测规模 (2 年窗口 32 页) 留 8 倍余量,
#: 防止服务端异常返回恒定行数时无限翻页。
HK_FIN_MAX_PAGES = 256


# ── filter 构造 ───────────────────────────────────────────────────
#: 内部 symbol 的后缀, 与 ``Market.HK.value`` 一致。
_HK_SUFFIX = ".HK"


def hk_fin_secucode(symbol: str) -> str:
    """内部港股 symbol → datacenter 的 ``SECUCODE`` 值。

    接受 ``00700.HK`` (内部 symbol 规范形) 与裸 ``00700`` 两种输入, 均返回
    ``00700.HK`` — 内部 symbol 与东财 ``SECUCODE`` **恰好同形**, 故本函数只做
    规范化与校验。

    🔴 **绝不可对已带后缀的输入再追加 ``.HK``**: 会得到 ``00700.HK.HK``, 而
    服务端对畸形 ``SECUCODE`` **静默返回空结果 (``code=9201``) 而非报错**,
    症状是"同步成功但 0 行", 极难定位。
    """
    from app.plugins.eastmoney.hk import hk_symbol

    s = str(symbol or "").strip()
    code = s[: -len(_HK_SUFFIX)] if s.upper().endswith(_HK_SUFFIX) else s
    norm = hk_symbol(code)
    if norm is None:
        raise ValueError(f"非法港股代码: {symbol!r}")
    return norm


def hk_fin_filter(
    *,
    since: str | None = None,
    until: str | None = None,
    date_type_code: str | None = None,
    secucodes: Sequence[str] | None = None,
) -> str | None:
    """构造 datacenter ``filter`` 串; 无条件时返回 ``None`` (取全表)。

    **多个条件直接并列即 AND** (东财语法, 不需要 ``and`` 关键字)。
    🔴 **不能用 ``between``** — 服务端 ANTLR 解析失败返回 ``code=9501``,
    故日期区间拆成 ``>=`` 与 ``<=`` 两个条件。

    Args:
        since: ``REPORT_DATE>=`` 下界, ``YYYY-MM-DD``。
        until: ``REPORT_DATE<=`` 上界, ``YYYY-MM-DD``。
        date_type_code: 报告期类型, 见 ``HK_DATE_TYPE_NAMES``。
        secucodes: 标的白名单 (``00700.HK`` 形式); 超过
            ``HK_FIN_IN_BATCH_MAX`` 抛 ``ValueError`` — 调用方应自行分批,
            而不是让服务端用 HTTP 414 拒绝。
    """
    parts: list[str] = []
    if since:
        parts.append(f"(REPORT_DATE>='{since}')")
    if until:
        parts.append(f"(REPORT_DATE<='{until}')")
    if date_type_code:
        parts.append(f'(DATE_TYPE_CODE="{date_type_code}")')
    if secucodes:
        codes = list(secucodes)
        if len(codes) > HK_FIN_IN_BATCH_MAX:
            raise ValueError(
                f"secucodes 单批上限 {HK_FIN_IN_BATCH_MAX}, 收到 {len(codes)}; "
                "请分批调用 (超限会被服务端以 HTTP 414 拒绝)"
            )
        joined = ",".join(f'"{c}"' for c in codes)
        parts.append(f"(SECUCODE in ({joined}))")
    return "".join(parts) or None


# ── 分页取数 ──────────────────────────────────────────────────────
def fetch_hk_main_indicator(
    client: EastmoneyClient,
    *,
    since: str | None = None,
    until: str | None = None,
    date_type_code: str | None = None,
    secucodes: Sequence[str] | None = None,
    page_size: int = HK_FIN_PAGE_SIZE_MAX,
    max_pages: int = HK_FIN_MAX_PAGES,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """分页拉取港股财务主指标 (宽表) 原始 payload。

    以"本页行数 < page_size"作为终止条件, 不依赖响应信封的 ``count``/``pages``
    (``client.datacenter_report`` 只返回 ``data``, 不透出信封)。

    ``code=9201`` (合法空结果) 由 ``datacenter_report`` 转成 ``[]``, 自然终止翻页。

    Args:
        on_progress: ``(已取行数, 已翻页数)`` 回调, 供长任务进度上报。
    """
    if page_size > HK_FIN_PAGE_SIZE_MAX:
        # 服务端会静默截断到 500, 若按调用方给的 page_size 判断终止条件会提前停,
        # 导致数据缺失。直接拒绝比静默少取数据安全。
        raise ValueError(f"page_size 上限 {HK_FIN_PAGE_SIZE_MAX} (服务端硬截断), 收到 {page_size}")

    flt = hk_fin_filter(
        since=since, until=until, date_type_code=date_type_code, secucodes=secucodes
    )
    out: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        rows = client.datacenter_report(
            RPT_HK_MAIN_INDICATOR,
            flt,
            page_size=page_size,
            page_number=page,
            sort_columns="REPORT_DATE,SECUCODE",
            sort_types="-1,-1",
        )
        if not rows:
            break
        out.extend(rows)
        if on_progress is not None:
            on_progress(len(out), page)
        if len(rows) < page_size:
            break
    else:
        logger.warning(
            "港股财务主指标翻页达到上限 %d 页 (%d 行), 结果可能被截断", max_pages, len(out)
        )
    return out


def fetch_hk_statement(
    client: EastmoneyClient,
    symbol: str,
    *,
    report: str = RPT_HK_INCOME,
    since: str | None = None,
    max_pages: int = 8,
) -> list[dict[str, Any]]:
    """按单标的拉取港股利润表 / 资产负债表 (长表) 原始 payload。

    长表**不做全市场同步** (835~1276 页), 只在详情页下钻时按标的取。
    单标的历史行数有限 (约 55 科目 × 期间数), ``max_pages`` 默认 8 页足够。

    Args:
        report: ``RPT_HK_INCOME`` 或 ``RPT_HK_BALANCE``。
    """
    if report not in (RPT_HK_INCOME, RPT_HK_BALANCE):
        raise ValueError(f"report 必须是 {RPT_HK_INCOME} 或 {RPT_HK_BALANCE}, 收到 {report!r}")
    flt = hk_fin_filter(since=since, secucodes=[hk_fin_secucode(symbol)])
    out: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        rows = client.datacenter_report(
            report,
            flt,
            page_size=HK_FIN_PAGE_SIZE_MAX,
            page_number=page,
            sort_columns="REPORT_DATE,STD_ITEM_CODE",
            sort_types="-1,1",
        )
        if not rows:
            break
        out.extend(rows)
        if len(rows) < HK_FIN_PAGE_SIZE_MAX:
            break
    return out


# ── 行映射: 主指标宽表 ────────────────────────────────────────────
def _to_float(value: Any) -> float | None:
    """宽松 float 转换: ``None``/``""``/``"-"``/``"--"`` → ``None``。

    东财对缺失值会返回 ``None``、空串或占位横线, 且财务字段常见
    ``"--"`` 表示"不适用"(如一般企业的 ``LOAN_ASSETS``)。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s in {"-", "--", "None", "null", "NaN"}:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def _to_date(value: Any) -> str | None:
    """东财日期 (``"2026-06-30 00:00:00"`` 或 ``"2026-06-30"``) → ISO 日期串。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:10]


#: 宽表 → 内部 schema 的数值字段映射 (内部名 → 东财字段名)。
#: 全部小写 snake_case; 分组顺序即列顺序, 便于人工核对。
HK_MAIN_INDICATOR_FIELDS: dict[str, str] = {
    # ── 估值 (港币计价) ──
    "pe_ttm": "PE_TTM",
    "pb_ttm": "PB_TTM",
    "total_market_cap": "TOTAL_MARKET_CAP",
    "hksk_market_cap": "HKSK_MARKET_CAP",
    # ── 每股 ──
    "basic_eps": "BASIC_EPS",
    "diluted_eps": "DILUTED_EPS",
    "eps_ttm": "EPS_TTM",
    "bps": "BPS",
    "dps_hkd": "DPS_HKD",
    "dps_hkd_ly": "DPS_HKD_LY",
    "per_oi": "PER_OI",
    "per_netcash_operate": "PER_NETCASH_OPERATE",
    # ── 股本 ──
    "issued_common_shares": "ISSUED_COMMON_SHARES",
    "hk_common_shares": "HK_COMMON_SHARES",
    "common_acs": "COMMON_ACS",
    # ── 盈利能力 (%) ──
    "roa": "ROA",
    "roe_avg": "ROE_AVG",
    "roe_yearly": "ROE_YEARLY",
    "roic_yearly": "ROIC_YEARLY",
    "net_profit_ratio": "NET_PROFIT_RATIO",
    "gross_profit_ratio": "GROSS_PROFIT_RATIO",
    "tax_ebt": "TAX_EBT",
    # ── 偿债 / 结构 ──
    "debt_asset_ratio": "DEBT_ASSET_RATIO",
    "equity_ratio": "EQUITY_RATIO",
    "current_ratio": "CURRENT_RATIO",
    "equity_multiplier": "EQUITY_MULTIPLIER",
    # ── 营收与利润 (编报币种金额) ──
    "operate_income": "OPERATE_INCOME",
    "operate_income_yoy": "OPERATE_INCOME_YOY",
    "operate_income_qoq": "OPERATE_INCOME_QOQ",
    "operate_profit": "OPERATE_PROFIT",
    "gross_profit": "GROSS_PROFIT",
    "gross_profit_yoy": "GROSS_PROFIT_YOY",
    "pretax_profit": "PRETAX_PROFIT",
    "holder_profit": "HOLDER_PROFIT",
    "holder_profit_yoy": "HOLDER_PROFIT_YOY",
    "holder_profit_qoq": "HOLDER_PROFIT_QOQ",
    # ── 金融股专有 (一般企业为 None) ──
    "net_interest_income": "NET_INTEREST_INCOME",
    "net_interest_income_yoy": "NET_INTEREST_INCOME_YOY",
    "premium_income": "PREMIUM_INCOME",
    "premium_income_yoy": "PREMIUM_INCOME_YOY",
    "premium_expense": "PREMIUM_EXPENSE",
    "fee_commission_income": "FEE_COMMISSION_INCOME",
    "fee_commission_income_yoy": "FEE_COMMISSION_INCOME_YOY",
    "loan_assets": "LOAN_ASSETS",
    "loan_deposit": "LOAN_DEPOSIT",
    "loan_equity": "LOAN_EQUITY",
    "deposit_assets": "DEPOSIT_ASSETS",
    "deposit_equity": "DEPOSIT_EQUITY",
    # ── 资产负债 ──
    "total_assets": "TOTAL_ASSETS",
    "total_liabilities": "TOTAL_LIABILITIES",
    "total_parent_equity": "TOTAL_PARENT_EQUITY",
    # ── 现金流 (港股无独立现金流量表报表, 核心项在此) ──
    "netcash_operate": "NETCASH_OPERATE",
    "netcash_invest": "NETCASH_INVEST",
    "netcash_finance": "NETCASH_FINANCE",
    "end_cash": "END_CASH",
    "ocf_sales": "OCF_SALES",
    # ── 周转天数 ──
    "inventory_tdays": "INVENTORY_TDAYS",
    "accounts_rece_tdays": "ACCOUNTS_RECE_TDAYS",
    "current_assets_tdays": "CURRENT_ASSETS_TDAYS",
    "total_assets_tdays": "TOTAL_ASSETS_TDAYS",
}

#: 主指标表内部列顺序 (存储契约)。身份列在前, 便于 ``SELECT`` 与人工核对。
HK_MAIN_INDICATOR_COLUMNS: tuple[str, ...] = (
    "symbol",
    "secucode",
    "name",
    "org_code",
    "org_type",
    "report_date",
    "std_report_date",
    "start_date",
    "fiscal_year",
    "date_type_code",
    "period_name",
    "report_type",
    "account_standard",
    # 🔴 计价币种 (宽表 CURRENCY, 实测腾讯=HKD) — **不是**科目金额的币种。
    "pricing_currency",
    "is_cny_code",
    # ⭐ 每手股数 (东财 PER_SHARES, 实测 8/8 匹配已知 board lot)
    "lot_size",
    *HK_MAIN_INDICATOR_FIELDS,
)


def hk_main_indicator_rows(payloads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """主指标宽表 payload → 内部 schema 行列表。

    - ``symbol`` 取 ``SECURITY_CODE`` 并 zfill(5), 与 ``instruments_hk`` 对齐;
    - ``period_name`` 由 ``DATE_TYPE_CODE`` 查 ``HK_DATE_TYPE_NAMES`` 得到,
      **不直接用 ``REPORT_TYPE``** — 宽表的 ``REPORT_TYPE`` 带年份前缀
      (``"2026年中报"``), 长表是裸值 (``"中报"``), 跨表不可比;
    - ``lot_size`` 取 ``PER_SHARES``;
    - 缺 ``SECURITY_CODE`` 或 ``REPORT_DATE`` 的行跳过 (无法定位标的或期间)。
    """
    from app.plugins.eastmoney.hk import hk_symbol

    out: list[dict[str, Any]] = []
    for p in payloads:
        code = p.get("SECURITY_CODE")
        report_date = _to_date(p.get("REPORT_DATE"))
        if not code or not report_date:
            continue
        sym = hk_symbol(str(code))
        if sym is None:
            # 非纯数字代码 (脏数据), 无法与 instruments_hk 对齐, 跳过而非写出 symbol=None
            logger.debug("港股财务主指标跳过非法代码: %r", code)
            continue
        dtc = str(p.get("DATE_TYPE_CODE") or "").strip()
        row: dict[str, Any] = {
            "symbol": sym,
            # 🔴 回退值不可再追加 ".HK" — hk_symbol 已含后缀 (见 hk_fin_secucode)
            "secucode": str(p.get("SECUCODE") or sym),
            "name": str(p.get("SECURITY_NAME_ABBR") or "").strip() or None,
            "org_code": str(p.get("ORG_CODE") or "").strip() or None,
            "org_type": str(p.get("ORG_TYPE") or "").strip() or None,
            "report_date": report_date,
            "std_report_date": _to_date(p.get("STD_REPORT_DATE")),
            "start_date": _to_date(p.get("START_DATE")),
            "fiscal_year": str(p.get("FISCAL_YEAR") or "").strip() or None,
            "date_type_code": dtc or None,
            "period_name": HK_DATE_TYPE_NAMES.get(dtc),
            "report_type": str(p.get("REPORT_TYPE") or "").strip() or None,
            "account_standard": str(p.get("ACCOUNT_STANDARD") or "").strip() or None,
            "pricing_currency": str(p.get("CURRENCY") or "").strip() or None,
            "is_cny_code": _to_int(p.get("IS_CNY_CODE")),
            "lot_size": _to_int(p.get("PER_SHARES")),
        }
        for internal, src in HK_MAIN_INDICATOR_FIELDS.items():
            row[internal] = _to_float(p.get(src))
        out.append(row)
    return out


#: 长表 (利润表 / 资产负债表) 内部列顺序。
HK_STATEMENT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "secucode",
    "name",
    "org_code",
    "org_type",
    "report_date",
    "std_report_date",
    "start_date",
    "fiscal_year",
    "date_type_code",
    "period_name",
    "report_type",
    "account_standard",
    # 🔴 编报币种 (长表 CURRENCY_CODE, 实测腾讯=CNY; 长表 CURRENCY 是中文名"人民币")
    "report_currency",
    "item_code",
    "item_name",
    "amount",
    "yoy_ratio",
    "remark",
)


def hk_statement_rows(payloads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """利润表 / 资产负债表 (长表) payload → 内部 schema 行列表。

    长表一行 = 一个会计科目。🔴 **港股科目名是 IFRS/港式表述, 与 A 股不同**,
    前端不能复用 A 股标签。实测 00700 的科目包括:
    ``营业额`` / ``营运收入`` / ``营运支出`` / ``毛利`` / ``销售及分销费用`` /
    ``行政开支`` / ``经营溢利`` / ``融资成本`` / ``应占联营公司溢利`` /
    ``除税前溢利`` / ``税项`` / ``除税后溢利`` / ``少数股东损益`` /
    ``股东应占溢利`` / ``每股基本盈利`` / ``其他全面收益`` / ``全面收益总额``。

    ``report_currency`` 取 ``CURRENCY_CODE`` (ISO), **不取 ``CURRENCY``**
    (后者在长表里是中文名 ``"人民币"``, 与宽表的 ISO 值 ``"HKD"`` 语义都不同)。
    """
    from app.plugins.eastmoney.hk import hk_symbol

    out: list[dict[str, Any]] = []
    for p in payloads:
        code = p.get("SECURITY_CODE")
        report_date = _to_date(p.get("REPORT_DATE"))
        item_code = str(p.get("STD_ITEM_CODE") or "").strip()
        if not code or not report_date or not item_code:
            continue
        sym = hk_symbol(str(code))
        if sym is None:
            logger.debug("港股财务长表跳过非法代码: %r", code)
            continue
        dtc = str(p.get("DATE_TYPE_CODE") or "").strip()
        out.append(
            {
                "symbol": sym,
                # 🔴 回退值不可再追加 ".HK" — hk_symbol 已含后缀 (见 hk_fin_secucode)
                "secucode": str(p.get("SECUCODE") or sym),
                "name": str(p.get("SECURITY_NAME_ABBR") or "").strip() or None,
                "org_code": str(p.get("ORG_CODE") or "").strip() or None,
                "org_type": str(p.get("ORG_TYPE") or "").strip() or None,
                "report_date": report_date,
                "std_report_date": _to_date(p.get("STD_REPORT_DATE")),
                "start_date": _to_date(p.get("START_DATE")),
                "fiscal_year": str(p.get("FISCAL_YEAR") or "").strip() or None,
                "date_type_code": dtc or None,
                "period_name": HK_DATE_TYPE_NAMES.get(dtc),
                "report_type": str(p.get("REPORT_TYPE") or "").strip() or None,
                "account_standard": str(p.get("ACCOUNT_STANDARD") or "").strip() or None,
                "report_currency": str(p.get("CURRENCY_CODE") or "").strip() or None,
                "item_code": item_code,
                "item_name": str(p.get("ITEM_NAME") or "").strip() or None,
                "amount": _to_float(p.get("AMOUNT")),
                "yoy_ratio": _to_float(p.get("YOY_RATIO")),
                "remark": str(p.get("REMARK") or "").strip() or None,
            }
        )
    return out


# ── 与 A 股财务表的规范化投影 ────────────────────────────────────
# 需求: 港股与 A 股**共用一个财务概况页**, 故 API 需返回同形结构。
# A 股财务表 (data/financials/<table>/part.parquet) 的实测契约:
#   共享键  symbol(str) / period_end(str ISO) / announce_date(str ISO)
#   派生键  fiscal_year(**Int64 年份**) / report_date_ms(公告日 epoch ms)
#           / period_end_ms(报告期末 epoch ms)
# 下面只映射**语义确实等价**的字段; 港股宽表没有的一律留 null, 不猜测不伪造。
#
# 🔴 已排除的同名/近名陷阱 (映射了就是错数据):
#   - ``FISCAL_YEAR``: A 股是 Int64 年份 (2026), 港股宽表是**字符串 "12-31"**
#     (财年结日的月-日)。同名不同义, 故港股的 ``fiscal_year`` 由
#     ``period_end`` 的年份重新推导, 不取东财字段。
#   - ``INVENTORY_TDAYS`` / ``ACCOUNTS_RECE_TDAYS`` / ``TOTAL_ASSETS_TDAYS``
#     是周转**天数**, A 股 ``inventory_turnover`` / ``receive_account_turnover_ratio``
#     / ``total_assets_turnover_ratio`` 是周转**率/次数**, 互为倒数关系而非同值。
#   - ``END_CASH`` (期末现金及等价物, 现金流量表) ≠ A 股 ``cash_and_equivalents``
#     (货币资金, 资产负债表)。
#   - ``EQUITY_MULTIPLIER`` (权益乘数) ≠ ``long_term_debt_equity_ratio``。
#   - ``ROE_YEARLY`` (年化) ≠ ``ROE_AVG`` (加权平均); A 股 ``roe`` 对应后者。
#: A 股规范名 → 港股宽表内部名。按目标表分组。
HK_CANONICAL_PROJECTION: dict[str, dict[str, str]] = {
    "metrics": {
        "eps_basic": "basic_eps",
        "bps": "bps",
        "revenue_yoy": "operate_income_yoy",
        "net_income_yoy": "holder_profit_yoy",
        "roa": "roa",
        "roe": "roe_avg",
        "gross_margin": "gross_profit_ratio",
        "net_margin": "net_profit_ratio",
        "current_ratio": "current_ratio",
        "debt_to_asset_ratio": "debt_asset_ratio",
        # A 股"经营现金/营业收入" ≡ 东财 OCF_SALES
        "operating_cash_to_revenue": "ocf_sales",
    },
    "income": {
        "revenue": "operate_income",
        "operating_profit": "operate_profit",
        "total_profit": "pretax_profit",
        "net_income_attributable": "holder_profit",
        "basic_eps": "basic_eps",
    },
    "balance_sheet": {
        "total_assets": "total_assets",
        "total_liabilities": "total_liabilities",
        "total_equity": "total_parent_equity",
    },
    "cash_flow": {
        "net_operating_cash_flow": "netcash_operate",
        "net_investing_cash_flow": "netcash_invest",
        "net_financing_cash_flow": "netcash_finance",
    },
}

#: 港股独有、A 股财务表没有但对港股页面必需的字段 (随投影一并带出)。
HK_ONLY_CANONICAL_FIELDS: tuple[str, ...] = (
    "name",
    "org_type",  # 决定金融股专有字段族是否有值, 前端须据此分支
    "period_name",  # 年报/中报/一季报/三季报
    "date_type_code",
    "pricing_currency",  # 🔴 计价币种; 金额跨标的比较前必须先按币种分组
    "lot_size",  # ⭐ 每手股数
    "pe_ttm",
    "pb_ttm",
    "total_market_cap",
)


def hk_lot_sizes(payloads: Iterable[dict[str, Any]]) -> dict[str, int]:
    """从主指标 payload 提取 ``{symbol: lot_size}`` (每手股数)。

    同一标的多期取**最新报告期**的值 (payload 若已按 ``REPORT_DATE`` 降序,
    则首次出现即最新; 为稳妥起见显式比较日期)。
    """
    from app.plugins.eastmoney.hk import hk_symbol

    best: dict[str, tuple[str, int]] = {}
    for p in payloads:
        code = p.get("SECURITY_CODE")
        lot = _to_int(p.get("PER_SHARES"))
        report_date = _to_date(p.get("REPORT_DATE"))
        if not code or lot is None or lot <= 0 or not report_date:
            continue
        sym = hk_symbol(str(code))
        if sym is None:
            continue
        prev = best.get(sym)
        if prev is None or report_date > prev[0]:
            best[sym] = (report_date, lot)
    return {sym: lot for sym, (_d, lot) in best.items()}
