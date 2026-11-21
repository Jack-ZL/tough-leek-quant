"""``app.plugins.eastmoney.hsgt`` 单测 —— 港股通 (南向) 资金流。

夹具里的数值取自 **2026-11-19 / 2026-11-20 的真实探测** (单位: 上游「百万元」),
不是编的 (§13 禁止伪造测试数据)。用真实值的好处是三条恒等式
(``buy − sell == net_buy``、``buy + sell == turnover``、``沪 + 深 == 合计``) 会**同时**
被验证: 只要单位、字段映射、透视逻辑任一处错了, 恒等式就崩。

⚠️ 精确地说: 合计行 (``006``) 的全部字段、两个分通道的 ``NET_DEAL_AMT`` /
``DEAL_AMT`` / ``ACCUM_DEAL_AMT``、以及 ``002`` 的 ``BUY_AMT``/``SELL_AMT``
(``31289.71`` / ``24727.33``) 都是**直接实测值**; ``004`` 的 ``BUY_AMT``/``SELL_AMT``
探测时未逐字段记录, 是由两条已实测的恒等式反推的
(``BUY = (DEAL_AMT + NET_DEAL_AMT) / 2``)。:func:`test_fixture_rows_satisfy_upstream_identities`
把这一点钉住: 夹具若被改动到不再自洽, 测试会失败, 而不是让一条错误的「实测值」
悄悄留在仓库里充当证据。

重点回归闸 (每一条都对应一个「静默出错、页面上看不出来」的失败模式):

1. 🔴 ``TRADE_DATE`` 过滤只认**横杠 + 单引号**, 与 ``hk_dividend`` 的
   ``EX_DIVIDEND_DATE`` (斜杠 + 双引号) **恰好相反**。写错会 9501 报错 (还算好),
   但照搬另一个插件的写法是这里最可能犯的错。
2. 🔴 金额单位是**百万元**, 换算成元在插件层一次做完。
3. 🔴 ``006`` 合计行缺失时**不用 ``002+004`` 兜底**, 留 ``None`` 让缺口可见。
4. 🔴 ``HOLD_MARKET_CAP`` 被显式排除, 且排除理由记录在案 (口径无法定性)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.plugins.eastmoney import hsgt
from app.plugins.eastmoney.client import EastmoneyError

# ---------------------------------------------------------------------------
# 真实探测夹具 (上游单位: 百万元)
# ---------------------------------------------------------------------------

#: 2026-11-20 三个南向通道。实测 ``002 + 004 == 006`` 精确成立:
#: NET_DEAL_AMT 6562.38 + 5249.12 = 11811.50; DEAL_AMT 56017.04 + 41664.60 = 97681.64。
DAY_2026_11_20 = [
    {
        "TRADE_DATE": "2026-11-20 00:00:00",
        "MUTUAL_TYPE": "002",
        "BUY_AMT": 31289.71,
        "SELL_AMT": 24727.33,
        "NET_DEAL_AMT": 6562.38,
        "DEAL_AMT": 56017.04,
        "ACCUM_DEAL_AMT": 3253659.60,
        "INDEX_CLOSE_PRICE": 22410.8,
        "INDEX_CHANGE_RATE": -1.89,
    },
    {
        "TRADE_DATE": "2026-11-20 00:00:00",
        "MUTUAL_TYPE": "004",
        "BUY_AMT": 23456.86,
        "SELL_AMT": 18207.74,
        "NET_DEAL_AMT": 5249.12,
        "DEAL_AMT": 41664.60,
        "ACCUM_DEAL_AMT": 2281219.07,
        "INDEX_CLOSE_PRICE": 22410.8,
        "INDEX_CHANGE_RATE": -1.89,
    },
    {
        "TRADE_DATE": "2026-11-20 00:00:00",
        "MUTUAL_TYPE": "006",
        "BUY_AMT": 54746.57,
        "SELL_AMT": 42935.07,
        "NET_DEAL_AMT": 11811.50,
        "DEAL_AMT": 97681.64,
        # ⚠️ 上游列名是「累计成交额」, 但实测其值 == 历史累计**净流入**
        # (5534878.67 == NETINFLOW_STATISTICS 的 TIME_TYPE=4 BOTH == 002+004 之和)。
        "ACCUM_DEAL_AMT": 5534878.67,
        "INDEX_CLOSE_PRICE": 22410.8,
        "INDEX_CHANGE_RATE": -1.89,
    },
]

DAY_2026_11_19 = [
    {
        "TRADE_DATE": "2026-11-19 00:00:00",
        "MUTUAL_TYPE": "002",
        "BUY_AMT": 29328.42,
        "SELL_AMT": 27355.11,
        "NET_DEAL_AMT": 1973.31,
        "DEAL_AMT": 56683.53,
        "ACCUM_DEAL_AMT": 3247097.22,
        "INDEX_CLOSE_PRICE": 22841.62,
        "INDEX_CHANGE_RATE": -1.95,
    },
    {
        "TRADE_DATE": "2026-11-19 00:00:00",
        "MUTUAL_TYPE": "004",
        "BUY_AMT": 17773.11,
        "SELL_AMT": 15787.50,
        "NET_DEAL_AMT": 1985.61,
        "DEAL_AMT": 33560.61,
        "ACCUM_DEAL_AMT": 2275969.95,
        "INDEX_CLOSE_PRICE": 22841.62,
        "INDEX_CHANGE_RATE": -1.95,
    },
    {
        "TRADE_DATE": "2026-11-19 00:00:00",
        "MUTUAL_TYPE": "006",
        "BUY_AMT": 47101.53,
        "SELL_AMT": 43142.61,
        "NET_DEAL_AMT": 3958.92,
        "DEAL_AMT": 90244.14,
        "ACCUM_DEAL_AMT": 5523067.17,
        "INDEX_CLOSE_PRICE": 22841.62,
        "INDEX_CHANGE_RATE": -1.95,
    },
]


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
# 单位换算
# ---------------------------------------------------------------------------

def test_amt_to_yuan_converts_millions_and_kills_float_noise():
    """「百万元」→「元」, 且抹掉上游真实存在的浮点尾噪。

    🔴 ``-482.780000000001`` 是 2026 年实测到的上游返回值 (同一天
    ``RPT_MUTUAL_NETINFLOW_DETAILS`` 给的是 ``-482.78``)。上游精度是 0.01 百万元
    (= 1 万元), 换算后按 2 位小数取整既消噪又不丢有效精度。
    """
    assert hsgt._amt_to_yuan(-482.780000000001) == -482780000.0
    assert hsgt._amt_to_yuan(11811.50) == 11811500000.0
    assert hsgt._amt_to_yuan(5534878.67) == 5534878670000.0
    assert hsgt.HSGT_AMT_UNIT == 1_000_000


def test_amt_to_yuan_returns_none_not_zero_for_missing():
    """空值 → ``None``。填 0 会把「上游没给」伪装成「那天恰好是 0」。"""
    assert hsgt._amt_to_yuan(None) is None
    assert hsgt._amt_to_yuan("") is None
    assert hsgt._amt_to_yuan("-") is None


def test_to_float_rejects_bool_and_nan():
    """``True`` 不得被当成 1.0; NaN 归一成 ``None`` (NaN 会污染下游所有比较)。"""
    assert hsgt._to_float(True) is None
    assert hsgt._to_float(False) is None
    assert hsgt._to_float(float("nan")) is None
    assert hsgt._to_float("3.5") == 3.5
    assert hsgt._to_float(None) is None


# ---------------------------------------------------------------------------
# 日期解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-20 00:00:00", dt.date(2026, 11, 20)),  # 实测形态: 横杠 + 时间尾巴
        ("2026-11-20", dt.date(2026, 11, 20)),
        ("2026/11/20", dt.date(2026, 11, 20)),  # 同主机其他报表用斜杠, 一并接受
        ("2026/11/20 00:00:00", dt.date(2026, 11, 20)),
        (dt.datetime(2026, 11, 20, 0, 0), dt.date(2026, 11, 20)),
        (dt.date(2026, 11, 20), dt.date(2026, 11, 20)),
    ],
)
def test_parse_trade_date_accepts_known_shapes(raw, expected):
    assert hsgt._parse_trade_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "不是日期", "2026-13-45"])
def test_parse_trade_date_returns_none_on_garbage(raw):
    """解析失败 → ``None`` (该行被跳过), 不抛错也不猜。"""
    assert hsgt._parse_trade_date(raw) is None


# ---------------------------------------------------------------------------
# 🔴 过滤语法 (最可能照搬错的地方)
# ---------------------------------------------------------------------------

def test_date_bound_uses_dash_and_single_quotes():
    """``TRADE_DATE`` 只认**横杠 + 单引号**。

    实测矩阵: 横杠+双引号 → ``9501 filter字段中日期参数格式错误``;
    斜杠 (任意引号) → ``9501 参数预处理错误``。
    🔴 与 ``hk_dividend.hk_dividend_filter`` 的 ``EX_DIVIDEND_DATE`` (斜杠 + 双引号)
    **恰好相反** —— 这条测试就是为了挡住「照搬另一个插件」这个最自然的错误。
    """
    assert hsgt.hsgt_date_bound(dt.date(2026, 11, 1), ">=") == "(TRADE_DATE>='2026-11-01')"
    assert hsgt.hsgt_date_bound(dt.date(2026, 11, 20), "<=") == "(TRADE_DATE<='2026-11-20')"


def test_filter_uses_multi_value_in_syntax_for_three_channels():
    """一次请求取回三个通道, 避免 N+1 (CONTRIBUTING §6.3)。

    实测 ``(MUTUAL_TYPE in ("002","004","006"))`` 生效, count=7801 = 2756+2289+2756。
    """
    flt = hsgt.hsgt_filter()
    assert flt == '(MUTUAL_TYPE in ("002","004","006"))'
    assert hsgt.SOUTHBOUND_MUTUAL_TYPES == ("002", "004", "006")


def test_filter_appends_bounds_only_when_given():
    """子句直接拼接 (无 ``and``): 东财 datacenter 的 filter 语法就是紧邻并列。"""
    assert hsgt.hsgt_filter(start=dt.date(2026, 11, 1)) == (
        '(MUTUAL_TYPE in ("002","004","006"))(TRADE_DATE>=\'2026-11-01\')'
    )
    assert hsgt.hsgt_filter(start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 20)) == (
        '(MUTUAL_TYPE in ("002","004","006"))'
        "(TRADE_DATE>='2026-11-01')"
        "(TRADE_DATE<='2026-11-20')"
    )
    assert hsgt.hsgt_filter(end=dt.date(2026, 11, 20)) == (
        '(MUTUAL_TYPE in ("002","004","006"))(TRADE_DATE<=\'2026-11-20\')'
    )


def test_filter_excludes_northbound_channels():
    """北向 (001/003/005) **不得**混进南向过滤 —— 混进来会把上证当恒指。"""
    flt = hsgt.hsgt_filter()
    for northbound in ("001", "003", "005"):
        assert f'"{northbound}"' not in flt


# ---------------------------------------------------------------------------
# 透视: 长表 (一行一通道) → 宽表 (一行一交易日)
# ---------------------------------------------------------------------------

def test_parse_pivots_three_channels_into_one_row_per_day():
    got = hsgt.parse_southbound_daily(DAY_2026_11_20)
    assert len(got) == 1
    row = got[0]
    assert row.trade_date == dt.date(2026, 11, 20)
    # 合计取自 006, 单位已从百万元换成元
    assert row.net_buy == 11811500000.0
    assert row.buy == 54746570000.0
    assert row.sell == 42935070000.0
    assert row.turnover == 97681640000.0
    assert row.accum_net_inflow == 5534878670000.0
    # 分通道
    assert row.ggt_sh_net_buy == 6562380000.0
    assert row.ggt_sz_net_buy == 5249120000.0
    assert row.ggt_sh_turnover == 56017040000.0
    assert row.ggt_sz_turnover == 41664600000.0
    # 指数 (南向行携带的是恒指, 不是上证)
    assert row.index_close == pytest.approx(22410.8)
    assert row.index_change_pct == pytest.approx(-1.89)


def test_fixture_rows_satisfy_upstream_identities():
    """夹具自洽性守卫: 记录值必须满足上游的三条恒等式。

    这不是在「测试测试数据」, 而是在钉住一个**经验事实**: 本插件的单位换算
    (百万元→元)、字段映射与「006 是 002+004 的精确预聚合」这三个设计决定, 全部
    建立在这些恒等式在真实数据上成立之上 (实测连续 8 个交易日全部精确成立)。
    若有人改了夹具里的某个数字, 这条测试会立刻失败, 而不是让一条不再自洽的
    「实测值」留在仓库里继续充当设计依据 (§13)。
    """
    for day in (DAY_2026_11_20, DAY_2026_11_19):
        rows = {r["MUTUAL_TYPE"]: r for r in day}
        assert set(rows) == {"002", "004", "006"}
        for mt, row in rows.items():
            buy, sell = row["BUY_AMT"], row["SELL_AMT"]
            assert buy - sell == pytest.approx(row["NET_DEAL_AMT"], abs=0.005), mt
            assert buy + sell == pytest.approx(row["DEAL_AMT"], abs=0.005), mt
        for key in ("BUY_AMT", "SELL_AMT", "NET_DEAL_AMT", "DEAL_AMT", "ACCUM_DEAL_AMT"):
            assert rows["002"][key] + rows["004"][key] == pytest.approx(
                rows["006"][key], abs=0.005
            ), key


def test_parse_upstream_identities_hold_on_real_values():
    """三条恒等式必须成立 —— 它们同时验证单位、字段映射与透视逻辑。

    实测这三条在连续 8 个交易日上全部精确成立。
    """
    for day in (DAY_2026_11_20, DAY_2026_11_19):
        row = hsgt.parse_southbound_daily(day)[0]
        assert row.buy - row.sell == pytest.approx(row.net_buy, abs=1.0)
        assert row.buy + row.sell == pytest.approx(row.turnover, abs=1.0)
        assert row.ggt_sh_net_buy + row.ggt_sz_net_buy == pytest.approx(row.net_buy, abs=1.0)
        assert row.ggt_sh_turnover + row.ggt_sz_turnover == pytest.approx(row.turnover, abs=1.0)


def test_parse_accum_net_inflow_is_not_a_sum_of_channels():
    """``accum_net_inflow`` 取 006 自己的值, **不是** 002+004 相加。

    实测三者恰好相等 (3253659.60 + 2281219.07 = 5534878.67), 所以这条测试钉的是
    「取哪一行」而不是「值对不对」: 若实现改成相加, 值仍然对, 但语义就变成
    「服务端自己维护累计」, 一旦上游修数就会与官方口径分歧。
    """
    row = hsgt.parse_southbound_daily(DAY_2026_11_20)[0]
    assert row.accum_net_inflow == 5534878670000.0


def test_parse_sorts_ascending_by_trade_date():
    """升序输出 —— 前端图表无需再反转 (``HSGT_SORT_TYPES="1,1"`` 同理)。"""
    got = hsgt.parse_southbound_daily(DAY_2026_11_20 + DAY_2026_11_19)
    assert [r.trade_date for r in got] == [dt.date(2026, 11, 19), dt.date(2026, 11, 20)]


def test_parse_skips_rows_with_unparseable_date_or_missing_channel():
    """脏行跳过而不是抛错: 一行坏数据不该让整个资金流页面 500。"""
    rows = [
        *DAY_2026_11_20,
        {"TRADE_DATE": "不是日期", "MUTUAL_TYPE": "006", "NET_DEAL_AMT": 1.0},
        {"TRADE_DATE": "2026-11-18 00:00:00", "MUTUAL_TYPE": "", "NET_DEAL_AMT": 1.0},
        {"TRADE_DATE": "2026-11-18 00:00:00", "NET_DEAL_AMT": 1.0},
    ]
    got = hsgt.parse_southbound_daily(rows)
    assert [r.trade_date for r in got] == [dt.date(2026, 11, 20)]


# ---------------------------------------------------------------------------
# 🔴 缺通道时的 fail-closed 行为
# ---------------------------------------------------------------------------

def test_missing_total_row_leaves_totals_none_instead_of_summing_channels():
    """``006`` 缺失 → 合计字段全 ``None``, **不用 ``002+004`` 兜底**。

    🔴 这是本插件最重要的一条测试。虽然已实测 ``002+004 == 006`` 精确成立, 但兜底
    会把「上游停止发布合计行」这一变化悄悄掩盖掉 —— 图表上只是少一个点, 没人会去
    查 (§4 fail closed)。分通道字段仍照常输出。
    """
    rows = [r for r in DAY_2026_11_20 if r["MUTUAL_TYPE"] != "006"]
    row = hsgt.parse_southbound_daily(rows)[0]
    assert row.net_buy is None
    assert row.buy is None
    assert row.sell is None
    assert row.turnover is None
    assert row.accum_net_inflow is None
    # 分通道不受影响
    assert row.ggt_sh_net_buy == 6562380000.0
    assert row.ggt_sz_net_buy == 5249120000.0


def test_missing_total_row_falls_back_to_sh_channel_for_index_only():
    """指数在 ``006`` 缺失时退回 ``002`` —— 实测两者同为恒指。

    这是**唯一**允许的退回: 指数只是行情背景, 且两个通道携带的是同一个值;
    而金额合计的退回会掩盖上游变化, 故禁止。
    """
    rows = [r for r in DAY_2026_11_20 if r["MUTUAL_TYPE"] != "006"]
    row = hsgt.parse_southbound_daily(rows)[0]
    assert row.index_close == pytest.approx(22410.8)
    assert row.index_change_pct == pytest.approx(-1.89)


def test_missing_sz_channel_gives_none_not_zero():
    """深港通 2016-12-05 才开通, 之前的 ``004`` 行不存在 → ``None`` 而不是 0。

    🔴 实测 2015 年的行确实没有 ``004``。填 0 会在图表上画出「那天深股通净买入 0 元」
    这条根本不存在的线。
    """
    rows = [r for r in DAY_2026_11_20 if r["MUTUAL_TYPE"] != "004"]
    row = hsgt.parse_southbound_daily(rows)[0]
    assert row.ggt_sz_net_buy is None
    assert row.ggt_sz_turnover is None
    # 合计仍来自 006, 不受影响
    assert row.net_buy == 11811500000.0


def test_northbound_rows_are_not_mistaken_for_southbound():
    """混进北向行 (001/003/005) 时被忽略 —— 它们的 ``INDEX_CLOSE_PRICE`` 是上证。"""
    rows = [
        *DAY_2026_11_20,
        {
            "TRADE_DATE": "2026-11-20 00:00:00",
            "MUTUAL_TYPE": "001",
            "NET_DEAL_AMT": 99999.99,
            "INDEX_CLOSE_PRICE": 3533.79,
            "INDEX_CHANGE_RATE": 0.12,
        },
        {
            "TRADE_DATE": "2026-11-20 00:00:00",
            "MUTUAL_TYPE": "005",
            "NET_DEAL_AMT": 88888.88,
            "INDEX_CLOSE_PRICE": 3533.79,
        },
    ]
    row = hsgt.parse_southbound_daily(rows)[0]
    assert row.net_buy == 11811500000.0
    assert row.index_close == pytest.approx(22410.8)


# ---------------------------------------------------------------------------
# 🔴 被排除的列
# ---------------------------------------------------------------------------

def test_excluded_columns_are_documented_with_reasons():
    """四个被排除的列都必须**带理由**, 且 ``HOLD_MARKET_CAP`` 在列。

    ``HOLD_MARKET_CAP`` 的排除依据 (完整推理见模块 docstring): ``004/002`` 比值
    恒为 1.00029 且**逐年单调收敛到 1.0** (2019→1.008795, 2023→1.002444,
    2026→1.00029) —— 两个成分与权重都不同的股票池不可能这样, 说明两行携带的是
    同一个市场级总量, 故 ``006 = 002+004`` 是 2 倍错值。且无独立数据源可交叉验证
    (``RPT_MUTUAL_HOLDRANK_NEW`` 只有北向且按季)。
    """
    assert set(hsgt.HSGT_EXCLUDED_COLUMNS) == {
        "HOLD_MARKET_CAP",
        "FUND_INFLOW",
        "QUOTA_BALANCE",
        "QUOTA_BALANCE_TEXT",
    }
    for column, reason in hsgt.HSGT_EXCLUDED_COLUMNS.items():
        assert reason.strip(), f"{column} 缺排除理由"


def test_columns_whitelist_excludes_unusable_fields():
    """请求的列白名单里不得出现被排除的列 (少传 = 少一份误用的可能)。"""
    for excluded in hsgt.HSGT_EXCLUDED_COLUMNS:
        assert excluded not in hsgt.HSGT_COLUMNS
    # 但恒等式需要的列必须在
    for needed in ("TRADE_DATE", "MUTUAL_TYPE", "BUY_AMT", "SELL_AMT", "NET_DEAL_AMT",
                   "DEAL_AMT", "ACCUM_DEAL_AMT"):
        assert needed in hsgt.HSGT_COLUMNS


def test_currency_is_declared_cny():
    """币种必须显式声明并透传到 API 响应。

    ⚠️ 这是**推断**而非上游声明 (报表无币种列), 三条依据见模块 docstring。
    显式带字段是为了让口径可见、可纠正 (§13)。
    """
    assert hsgt.HSGT_CURRENCY == "CNY"


# ---------------------------------------------------------------------------
# fetch: 参数传递与错误传播
# ---------------------------------------------------------------------------

def test_fetch_passes_report_filter_columns_and_stable_sort():
    client = _FakeClient(DAY_2026_11_20)
    hsgt.fetch_southbound_flow(
        client, start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 20)
    )
    call = client.calls[0]
    assert call["report_name"] == "RPT_MUTUAL_DEAL_HISTORY"
    assert call["flt"] == (
        '(MUTUAL_TYPE in ("002","004","006"))'
        "(TRADE_DATE>='2026-11-01')"
        "(TRADE_DATE<='2026-11-20')"
    )
    assert call["columns"] == hsgt.HSGT_COLUMNS
    assert call["page_size"] == hsgt.HSGT_PAGE_SIZE
    assert call["max_pages"] == hsgt.HSGT_MAX_PAGES
    # 🔴 次键 MUTUAL_TYPE 是必需的: 翻页要求全序稳定, 只按 TRADE_DATE 排会让
    # 同一天的三行在页边界处顺序不定, 从而漏行或重复行。
    assert call["sort_columns"] == "TRADE_DATE,MUTUAL_TYPE"
    assert call["sort_types"] == "1,1"


def test_fetch_omits_date_bounds_when_not_given():
    client = _FakeClient([])
    hsgt.fetch_southbound_flow(client)
    assert client.calls[0]["flt"] == '(MUTUAL_TYPE in ("002","004","006"))'


def test_fetch_returns_empty_list_for_empty_window():
    """窗口内无交易日 (整段假期) → ``[]``, 这是合法答案而不是错误。

    client 已把 ``code=9201`` (返回数据为空) 归一成 ``[]``。
    """
    assert hsgt.fetch_southbound_flow(_FakeClient([])) == []


def test_fetch_propagates_eastmoney_error():
    """上游失败必须上抛 —— 降级成 ``[]`` 会把「拉不到」伪装成「没有资金流」(§4)。"""
    client = _FakeClient(EastmoneyError("9501 filter字段中日期参数格式错误"))
    with pytest.raises(EastmoneyError, match="9501"):
        hsgt.fetch_southbound_flow(client)


def test_southbound_daily_field_count_is_pinned():
    """字段数钉死: 新增字段必须是有意的决定, 而不是顺手加一列。"""
    assert len(hsgt.SouthboundDaily._fields) == 12
    assert hsgt.SouthboundDaily._fields[0] == "trade_date"
