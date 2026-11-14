"""app.market 规则表 / symbol 判据 / 半日市机制的测试。

本文件钉住三条最容易被后续改动破坏的契约:

1. **双载体平价** — 同一套 symbol 判据同时以 Python 正则和 SQL 正则两种载体
   存在 (Python 侧过滤 DataFrame, SQL 侧下推到 DuckDB)。两份正则必须同源生成,
   否则迟早漂移, 表现为「列表页有这只票、详情页 404」。
2. **未知市场不静默兜底** — ``parse_market`` 返回 None、``rules_for`` 抛错。
   兜底成 CN 会让「拼错 market」变成「悄悄按 A 股算」。
3. **A 股判据收紧后不误伤存量** — CN 正则从 ``[0-9]{6}\\.[A-Z]{2}`` 收紧为
   显式交易所白名单, 必须对全量存量 instruments 零回归。
"""

from __future__ import annotations

from datetime import date
from datetime import time as dt_time

import pytest

from app.market import (
    CN_RULES,
    HK_RULES,
    RULES_BY_MARKET,
    Market,
    MarketTradingRules,
    SymbolMarketMismatchError,
    assert_symbol_market,
    clear_half_days,
    cn_exchange_of,
    half_day_close,
    infer_market,
    is_a_share_symbol,
    is_hk_symbol,
    is_valid_symbol,
    market_symbol_pattern,
    market_symbol_sql_regex,
    parse_market,
    register_half_days,
    rules_for,
    sessions_for,
    trading_total_minutes,
)

# 覆盖: 两市合法票、大小写变体、位数错误、后缀错误、空值、注入型畸形串。
# 双载体平价测试会对这个列表逐个比对 Python 与 DuckDB 的判定结果。
SYMBOL_SAMPLES = [
    # --- A 股合法 ---
    "600519.SH", "000001.SZ", "300750.SZ", "301001.SZ", "302132.SZ",
    "688981.SH", "689009.SH", "920001.BJ", "830799.BJ", "000651.SZ",
    # --- 港股合法 ---
    "00700.HK", "09988.HK", "00005.HK", "01398.HK", "08001.HK", "09001.HK",
    # --- 大小写变体 (严格判据应拒, 宽松判据应认) ---
    "00700.hk", "600519.sh", "00700.Hk",
    # --- 位数/后缀错误 ---
    "007000.HK",      # 6 位 + .HK: 既不是合法港股(5位), 也不该被当成 A 股
    "0070.HK",        # 4 位
    "0070000.HK",     # 7 位
    "60051.SH",       # 5 位 + A 股后缀
    "6005199.SH",     # 7 位 + A 股后缀
    "600519.XX",      # 不存在的交易所
    "600519.SS",      # 常见误写 (雅虎风格), 不是本仓规范后缀
    "00700.SH",       # 港股位数 + A 股后缀
    "600519.HK",      # A 股位数 + 港股后缀
    # --- 畸形/空 ---
    "", " ", "600519", "00700", ".SH", ".HK", "SH.600519",
    "600519.SH ", " 600519.SH", "600519.SH\n",
    "600519.SH;DROP", "' OR 1=1--", "600519.S%HH",
    "６００５１９．ＳＨ",  # 全角数字
    None,
]


@pytest.fixture(autouse=True)
def _clean_half_days():
    """半日市表是模块级可变状态, 逐个测试前后清空防泄漏。"""
    clear_half_days()
    yield
    clear_half_days()


# ================================================================
# 规则表
# ================================================================

def test_rules_table_covers_every_market():
    """规则表必须覆盖枚举里的每个市场 — 新增市场忘了配规则要当场失败。"""
    assert set(RULES_BY_MARKET) == set(Market)
    for market, rules in RULES_BY_MARKET.items():
        assert rules.market is market
        assert rules.label and rules.currency and rules.iana_timezone
        assert rules.sessions, f"{market} 没有交易时段"
        assert rules.default_benchmark


def test_cn_rules_match_pre_refactor_hardcoded_values():
    """A 股规则必须与改造前散落在 market_time.py 里的硬编码值逐位一致。"""
    assert CN_RULES.market is Market.CN
    assert CN_RULES.currency == "CNY"
    assert CN_RULES.t_plus_1 is True
    assert CN_RULES.lot_size == 100
    assert CN_RULES.has_price_limit is True
    assert CN_RULES.quote_delay_minutes == 0
    assert CN_RULES.default_benchmark == "000001.SH"
    assert [(s.start, s.end) for s in CN_RULES.sessions] == [
        (dt_time(9, 30), dt_time(11, 30)),
        (dt_time(13, 0), dt_time(15, 0)),
    ]
    # 改造前 market_time._TRADING_TOTAL_MINUTES == 240
    assert CN_RULES.trading_total_minutes == 240


def test_hk_rules_differ_from_cn_where_they_must():
    """港A 差异清单里属于「规则」的部分必须真的不同, 不能沿用 A 股值。"""
    assert HK_RULES.currency == "HKD"
    assert HK_RULES.t_plus_1 is False, "港股 T+0 回转"
    assert HK_RULES.has_price_limit is False, "港股无涨跌停"
    assert HK_RULES.quote_delay_minutes == 15, "免费港股源是 15 分钟延时"
    # lot_size=None 表示「按标的元数据」, 消费方不得假设 100 或 1。
    assert HK_RULES.lot_size is None
    assert HK_RULES.default_benchmark == "HSI"
    assert [(s.start, s.end) for s in HK_RULES.sessions] == [
        (dt_time(9, 30), dt_time(12, 0)),
        (dt_time(13, 0), dt_time(16, 0)),
    ]
    assert HK_RULES.trading_total_minutes == 330


def test_rules_are_frozen():
    """规则表是全局共享的只读契约, 必须不可变 (防止被下游就地改写)。"""
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        CN_RULES.currency = "USD"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        HK_RULES.lot_size = 100  # type: ignore[misc]


def test_probe_basket_symbols_are_valid_for_their_market():
    """探针篮子写错市场会让交易日探针恒判 True (见 trading_day 文档)。"""
    for market, rules in RULES_BY_MARKET.items():
        assert rules.probe_basket, f"{market} 探针篮子为空"
        for symbol in rules.probe_basket:
            assert is_valid_symbol(symbol, market), f"{market} 探针篮子含非法 symbol: {symbol}"
            assert infer_market(symbol) is market


# ================================================================
# market 解析: 过滤场景不兜底, 取规则场景抛错, 推断场景才兜底
# ================================================================

@pytest.mark.parametrize("value,expected", [
    (Market.CN, Market.CN), (Market.HK, Market.HK),
    ("CN", Market.CN), ("cn", Market.CN), ("Cn", Market.CN),
    ("HK", Market.HK), ("hk", Market.HK),
    ("HKEX", Market.HK), ("hk_stock", Market.HK), ("港股", Market.HK),
    ("A", Market.CN), ("A_SHARE", Market.CN), ("ASHARE", Market.CN),
    ("SSE", Market.CN), ("SZSE", Market.CN), ("A股", Market.CN),
    (" hk ", Market.HK),
    (CN_RULES, Market.CN), (HK_RULES, Market.HK),
])
def test_parse_market_accepts_aliases(value, expected):
    assert parse_market(value) is expected


@pytest.mark.parametrize("value", [None, "", "  ", "xx", "US", "us", "CRYPTO", "00700.HK", 123])
def test_parse_market_returns_none_for_unknown_without_fallback(value):
    """过滤场景: 未知 market 返回 None, **绝不**兜底成 CN。

    兜底会让「不传 market / 传错 market」静默变成「只看 A 股」,
    用户以为看到的是全市场结果。
    """
    assert parse_market(value) is None


@pytest.mark.parametrize("value", [None, "xx", "US", ""])
def test_rules_for_raises_on_unknown(value):
    """取规则场景: 未知 market 抛错, 不静默按 A 股算。"""
    with pytest.raises(ValueError, match="未知市场"):
        rules_for(value)


def test_rules_for_defaults_to_cn():
    assert rules_for() is CN_RULES
    assert rules_for(Market.CN) is CN_RULES
    assert rules_for("hk") is HK_RULES


def test_infer_market_falls_back_to_cn_only_for_routing():
    """推断场景 (决定数据往哪条管道走) 才允许兜底。

    与 parse_market/rules_for 的取舍相反: 路由失败会让港股数据流进 A 股管道,
    比「把畸形 symbol 当 A 股」更糟。
    """
    assert infer_market("600519.SH") is Market.CN
    assert infer_market("00700.HK") is Market.HK
    assert infer_market("00700.hk") is Market.HK, "推断宽松: 先 upper"
    assert infer_market("garbage") is Market.CN, "未知兜底 CN (仅用于路由)"


# ================================================================
# symbol 判据
# ================================================================

@pytest.mark.parametrize("symbol", [
    "600519.SH", "000001.SZ", "300750.SZ", "301001.SZ", "302132.SZ",
    "688981.SH", "689009.SH", "920001.BJ", "830799.BJ",
])
def test_valid_cn_symbols(symbol):
    assert is_valid_symbol(symbol) is True
    assert is_valid_symbol(symbol, Market.CN) is True
    assert is_a_share_symbol(symbol) is True
    assert is_hk_symbol(symbol) is False


@pytest.mark.parametrize("symbol", ["00700.HK", "09988.HK", "00005.HK", "08001.HK", "09001.HK"])
def test_valid_hk_symbols(symbol):
    assert is_valid_symbol(symbol, Market.HK) is True
    assert is_hk_symbol(symbol) is True
    assert is_a_share_symbol(symbol) is False
    # 港股 symbol 不是合法 A 股 symbol
    assert is_valid_symbol(symbol, Market.CN) is False


def test_is_valid_symbol_is_strict_about_case():
    """严格判据**不**做大小写规范化。

    规范化是数据源插件层的职责 (CONTRIBUTING §4)。若这里也 upper, 非规范
    symbol 会进存储层, 与规范 symbol 并存成两行 (``00700.hk`` / ``00700.HK``),
    破坏 join 与去重。
    """
    assert is_valid_symbol("00700.hk", Market.HK) is False
    assert is_valid_symbol("600519.sh") is False


def test_hk_symbol_is_not_mistaken_for_a_share():
    """回归: CN 正则曾写成宽松的 ``[0-9]{6}\\.[A-Z]{2}``。

    ``HK`` 正好是两个大写字母, 于是 ``007000.HK`` (6 位数字 + .HK) 被判成
    **合法 A 股 symbol** —— 港股票混进 A 股维表, 后续按 A 股规则套 ±10% 涨跌停。
    收紧为显式交易所白名单后必须拒绝。
    """
    assert is_valid_symbol("007000.HK", Market.CN) is False
    assert is_valid_symbol("007000.HK") is False
    assert is_a_share_symbol("007000.HK") is False
    assert infer_market("007000.HK") is Market.CN  # 位数不对, 也不是港股
    # 白名单外的两字母后缀一律拒
    for suffix in ("XX", "SS", "HK", "US", "AB"):
        assert is_valid_symbol(f"600519.{suffix}", Market.CN) is False


@pytest.mark.parametrize("symbol", [
    "", " ", None, "600519", "00700", ".SH", ".HK", "SH.600519",
    "60051.SH", "6005199.SH", "0070.HK", "0070000.HK",
    "600519.SH ", " 600519.SH", "600519.SH\n", "600519.SH;DROP",
])
def test_malformed_symbols_rejected_by_both_markets(symbol):
    assert is_valid_symbol(symbol, Market.CN) is False
    assert is_valid_symbol(symbol, Market.HK) is False


def test_assert_symbol_market_raises_on_mismatch():
    """护栏: 港股 symbol 进 A 股专属层要**当场报错**, 不能静默按 A 股算。"""
    with pytest.raises(SymbolMarketMismatchError):
        assert_symbol_market("00700.HK", Market.CN)
    with pytest.raises(SymbolMarketMismatchError):
        assert_symbol_market("600519.SH", Market.HK)
    # 同市场放行
    assert_symbol_market("600519.SH", Market.CN)
    assert_symbol_market("00700.HK", Market.HK)
    assert_symbol_market("600519.SH")  # 默认 CN


def test_assert_symbol_market_is_lenient_about_case():
    """护栏针对的是**市场错**, 不是**大小写错** —— 后者由严格判据负责。

    若护栏也严格, ``00700.hk`` 会被判成「不是港股」从而**通过** A 股护栏,
    正好放过了它要拦的东西。
    """
    with pytest.raises(SymbolMarketMismatchError):
        assert_symbol_market("00700.hk", Market.CN)
    assert_symbol_market("00700.hk", Market.HK)


@pytest.mark.parametrize(
    "bare", ["000001", "300001", "688001", "689001", "600001", "830001", "", "00700"]
)
def test_assert_symbol_market_allows_codes_matching_no_market(bare):
    """⭐ 护栏只拦「确实属于别的市场」, **不拦「匹配不到任何市场」**。

    仓内内部路径大量使用裸 6 位代码 (无交易所后缀), 且与带后缀代码在**同一列**
    里混用 —— ``tests/test_st_limit_and_sharpe.py`` 构造的帧就是
    ``["300001", "688001", "689001", "600001", "000001", "830001.BJ"]``。
    ``board_limit_pct`` 靠前缀判板块, 本就支持裸码。

    曾经把这里写成「不匹配声明市场就报错」, 结果
    ``build_market_data_matrix → write_numpy_price_limit_matrix →
    numpy_limit_pct_vectors → board_limit_pct`` 整条回测链在裸码上炸掉。
    格式校验是 :func:`is_valid_symbol` 在存储 / API **边界**上的职责,
    规范化是插件层的职责 (CONTRIBUTING §4) —— 三者分工不同, 不能互相顶替。
    """
    assert_symbol_market(bare, Market.CN)  # 不得抛


def test_strict_and_lenient_judgments_stay_distinct():
    """严格判据与宽松护栏必须**同时**存在且语义不同, 不能被合并成一个。

    合并的后果二选一, 都错: 全严格 → 打断存量裸码路径; 全宽松 → 非规范 symbol
    进存储, 与规范形并存成重复行, join / 去重全乱。
    """
    assert is_valid_symbol("000001") is False, "边界判据必须要求规范形"
    assert_symbol_market("000001", Market.CN)  # 内部护栏必须放行
    assert is_valid_symbol("600519.SH") is True
    assert is_valid_symbol("00700.HK") is True


def test_bare_codes_keep_their_board_limit_rules():
    """裸码经 ``board_limit_pct`` 仍得到正确板块涨跌幅 (既有约定, 护栏不得打断)。"""
    from app.price_limits import board_limit_pct

    assert board_limit_pct("300001") == 0.20   # 创业板
    assert board_limit_pct("301001") == 0.20
    assert board_limit_pct("302001") == 0.20
    assert board_limit_pct("688001") == 0.20   # 科创板
    assert board_limit_pct("689001") == 0.20
    assert board_limit_pct("600001") == 0.10   # 沪主板
    assert board_limit_pct("000001") == 0.10   # 深主板
    assert board_limit_pct("830001.BJ") == 0.30  # 北交所


def test_bare_beijing_code_falls_back_to_main_board():
    """已知怪癖钉住: 北交所判定靠 ``.BJ`` **后缀**, 裸码 ``830001`` 回落主板 10%。

    这是改造前就有的行为, 不是港股引入的。钉住它有两个目的:
    (1) 若哪天有人「顺手修好」, 这条会失败, 逼他确认影响面 (回测矩阵里有多少
        裸北交所码); (2) 说明为什么裸码只是「不报错」而不等于「判定正确」——
        规范形仍然重要, 只是该在边界上要求, 不是在内部护栏上。
    """
    from app.price_limits import board_limit_pct

    assert board_limit_pct("830001") == 0.10
    assert board_limit_pct("830001.BJ") == 0.30


def test_symbol_market_mismatch_is_a_value_error():
    """继承 ValueError: 现有 ``except ValueError`` 的调用方不会漏接。"""
    assert issubclass(SymbolMarketMismatchError, ValueError)


def test_cn_exchange_of():
    assert cn_exchange_of("600519.SH") == "SH"
    assert cn_exchange_of("000001.SZ") == "SZ"
    assert cn_exchange_of("920001.BJ") == "BJ"
    assert cn_exchange_of("00700.HK") is None
    assert cn_exchange_of("garbage") is None


# ================================================================
# 双载体平价: Python 正则 vs DuckDB regexp_matches
# ================================================================

def test_python_and_sql_regex_are_generated_from_one_source():
    """两种载体必须同源: SQL 正则由与 Python 正则相同的 body 生成。"""
    for market in Market:
        py_body = market_symbol_pattern(market).pattern
        sql_body = market_symbol_sql_regex(market)
        # 两侧都是 ^(body)$ 形态, 去掉锚点后 body 必须逐字符相同
        assert py_body.startswith("^(") and py_body.endswith(")$")
        assert sql_body.startswith("^(") and sql_body.endswith(")$")
        assert py_body[2:-2] == sql_body[2:-2], f"{market} 双载体正则漂移"


def test_cn_exchange_regex_is_deterministic_across_processes():
    """交易所白名单必须排序固定, 否则正则字符串随 set 哈希随机化而变。

    正则串不稳定 → 同一份数据在不同进程里判定不同, 且无法写快照测试。
    """
    from app.market import _CN_EXCHANGES

    assert list(_CN_EXCHANGES) == sorted(_CN_EXCHANGES)
    assert market_symbol_sql_regex(Market.CN) == r"^([0-9]{6}\.(?:BJ|SH|SZ))$"
    assert market_symbol_sql_regex(Market.HK) == r"^([0-9]{5}\.HK)$"


@pytest.mark.parametrize("market", list(Market))
def test_dual_carrier_parity_python_vs_duckdb(market):
    """⭐ 核心平价测试: Python 与 DuckDB 对同一批 symbol 的判定必须完全一致。

    这是「列表页有这只票、详情页 404」这类 bug 的唯一自动化防线。
    注意 DuckDB 的 ``regexp_matches`` **区分大小写**, 而 Python 侧的宽松判据
    会先 upper —— 因此这里比对的是**严格**判据 (is_valid_symbol), 两侧都不
    做规范化, 才谈得上平价。
    """
    duckdb = pytest.importorskip("duckdb")
    pattern = market_symbol_sql_regex(market)
    samples = [s for s in SYMBOL_SAMPLES if isinstance(s, str)]

    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (symbol VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?)", [(s,) for s in samples])
        rows = con.execute(
            "SELECT symbol, regexp_matches(symbol, ?) FROM t", [pattern]
        ).fetchall()
    finally:
        con.close()

    sql_verdicts = dict(rows)
    assert len(sql_verdicts) == len(samples), "DuckDB 去重了重复样本, 平价比对不完整"

    mismatches = [
        (s, is_valid_symbol(s, market), sql_verdicts[s])
        for s in samples
        if is_valid_symbol(s, market) != sql_verdicts[s]
    ]
    assert not mismatches, f"{market} 双载体判定不一致 (symbol, python, sql): {mismatches}"


@pytest.mark.parametrize("market", list(Market))
def test_dual_carrier_parity_includes_null_and_empty(market):
    """NULL / 空串在两侧都必须判否 —— SQL 侧 NULL 参与正则返回 NULL 而非 False。"""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    try:
        sql = con.execute(
            "SELECT regexp_matches(NULL, ?), regexp_matches('', ?)",
            [market_symbol_sql_regex(market)] * 2,
        ).fetchone()
    finally:
        con.close()
    # SQL 侧: NULL -> NULL (falsy), '' -> False
    assert not sql[0] and sql[1] is False
    # Python 侧同判否
    assert is_valid_symbol(None, market) is False
    assert is_valid_symbol("", market) is False


@pytest.mark.parametrize("padded", ["600519.SH ", " 600519.SH", "\t600519.SH", "600519.SH\n"])
def test_is_valid_symbol_does_not_strip_whitespace(padded):
    """回归: ``is_valid_symbol`` 曾 ``.strip()`` 后再匹配, SQL 载体不会 strip。

    于是 ``"600519.SH "`` 在 Python 侧合法、在 DuckDB 侧非法 —— 正是双载体
    漂移。放行带空白的 symbol 还有第二个害处: 它以非规范形式入库, 与
    ``"600519.SH"`` 并存成两行, join 不上、去重失效。
    """
    assert is_valid_symbol(padded) is False
    assert is_valid_symbol(padded, Market.CN) is False
    # 宽松路由层仍然容忍 (它的职责是别把标的送错市场, 不是校验规范形式)
    assert infer_market(padded) is Market.CN


def test_python_dollar_anchor_does_not_leak_trailing_newline():
    """回归: Python 的 ``$`` 允许匹配「末尾换行符之前」, DuckDB 的 ``$`` 不允许。

    ``re.match(r"^(X)$", "X\\n")`` 为真 → 尾带换行的 symbol 在 Python 侧合法、
    SQL 侧非法。必须用 ``fullmatch`` (要求整串被消费) 消除这个差异。
    """
    import re

    pattern = market_symbol_pattern(Market.CN)
    assert pattern.match("600519.SH\n"), "前提: $ 确实有尾换行怪癖"
    assert not pattern.fullmatch("600519.SH\n"), "fullmatch 必须拒"
    assert is_valid_symbol("600519.SH\n") is False
    assert is_valid_symbol("00700.HK\n", Market.HK) is False
    # 正常串不受影响
    assert pattern.fullmatch("600519.SH")
    assert re.fullmatch(pattern.pattern, "600519.SH")


# ================================================================
# 存量数据零回归
# ================================================================

def test_tightened_cn_regex_keeps_all_existing_instruments_valid():
    """收紧 CN 正则后, 全量存量 instruments 必须仍然合法。

    收紧前 ``[0-9]{6}\\.[A-Z]{2}`` 会放行 ``007000.HK``; 收紧为白名单后
    若存量数据里存在白名单外的后缀, 那些票会**从维表判定里消失**。
    数据文件不在时跳过 (CI 与本地开发机都可能没有)。
    """
    from pathlib import Path

    import polars as pl

    candidates = [
        Path("data/instruments/instruments.parquet"),
        Path("../data/instruments/instruments.parquet"),
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        pytest.skip("instruments.parquet 不在, 跳过存量回归")

    df = pl.read_parquet(path, columns=["symbol"])
    symbols = df["symbol"].to_list()
    assert symbols, "instruments.parquet 为空"

    from app.market import _CN_EXCHANGE_SUFFIXES

    invalid = [s for s in symbols if not is_valid_symbol(s, Market.CN)]
    assert not invalid, f"{len(invalid)} 只存量票被收紧后的正则拒绝, 样例: {invalid[:10]}"

    # 后缀分布也钉一下: 出现白名单外后缀说明数据源变了, 需要重新评估
    suffixes = {str(s).rsplit(".", 1)[-1] for s in symbols}
    assert suffixes <= set(_CN_EXCHANGE_SUFFIXES), f"存量出现白名单外后缀: {suffixes}"


# ================================================================
# 半日市机制
# ================================================================

def test_half_day_table_is_empty_by_default():
    """默认**空表**是刻意的安全方向, 不是没写完。

    港交所半日市日期由 HKEX 逐年公布 (惯例为圣诞前夕/除夕/农历新年除夕),
    无免费公开接口, 农历日期更无法在不引入农历库的前提下推算。
    - 漏标半日市 → 退化为全日市: 下午多轮询一段无行情的时间; 量比分母偏大
      导致**低估**, 不丢数据, 补上日期后自愈。
    - 误标全日市为半日市 → **丢掉整个下午的行情**, 不可自愈。
    因此宁可漏标。新增日期前必须核对 HKEX 当年公告。
    """
    for market in Market:
        assert half_day_close(market, date(2026, 12, 24)) is None
        assert sessions_for(market, date(2026, 12, 24)) == RULES_BY_MARKET[market].sessions


def test_half_day_truncates_to_morning_session_only():
    register_half_days(Market.HK, [date(2026, 12, 24)])
    day = date(2026, 12, 24)
    assert half_day_close(Market.HK, day) == dt_time(12, 0)
    assert sessions_for(Market.HK, day) == (HK_RULES.sessions[0],)
    assert trading_total_minutes(Market.HK, day) == 150
    # 非半日市日不受影响
    assert trading_total_minutes(Market.HK, date(2026, 12, 23)) == 330


def test_half_day_registration_does_not_leak_across_markets():
    """注册港股半日市不得影响 A 股 (反之亦然)。"""
    day = date(2026, 12, 24)
    register_half_days(Market.HK, [day])
    assert trading_total_minutes(Market.HK, day) == 150
    assert trading_total_minutes(Market.CN, day) == 240
    assert half_day_close(Market.CN, day) is None

    register_half_days(Market.CN, [day])
    assert half_day_close(Market.CN, day) == dt_time(11, 30)
    assert trading_total_minutes(Market.CN, day) == 120


def test_register_half_days_is_idempotent_and_accumulates():
    d1, d2 = date(2026, 12, 24), date(2026, 12, 31)
    register_half_days(Market.HK, [d1])
    register_half_days(Market.HK, [d1, d2])
    assert half_day_close(Market.HK, d1) == dt_time(12, 0)
    assert half_day_close(Market.HK, d2) == dt_time(12, 0)


def test_clear_half_days_scoped_and_global():
    d1 = date(2026, 12, 24)
    register_half_days(Market.HK, [d1])
    register_half_days(Market.CN, [d1])
    clear_half_days(Market.HK)
    assert half_day_close(Market.HK, d1) is None
    assert half_day_close(Market.CN, d1) == dt_time(11, 30), "清港股不得影响 A 股"
    clear_half_days()
    assert half_day_close(Market.CN, d1) is None


def test_half_day_uses_first_session_end_not_a_hardcoded_time():
    """半日市收市时刻取自规则表首段 end, 不得另写一份常量。"""
    for market, rules in RULES_BY_MARKET.items():
        register_half_days(market, [date(2026, 12, 24)])
        assert half_day_close(market, date(2026, 12, 24)) == rules.sessions[0].end


def test_missing_half_day_understates_volume_ratio_but_loses_no_data():
    """量化「漏标半日市」的代价, 证明默认空表是可接受的安全方向。

    量比 = 当日累计成交量 / (过去均量 × 已交易分钟 / 全天分钟)。
    半日市实际只交易 150 分钟, 若按 330 分钟当分母, 收盘时量比被低估
    330/150 ≈ 2.2 倍 —— 数值偏差, 但**行情一条不丢**, 补上日期即自愈。
    """
    day = date(2026, 12, 24)
    full = trading_total_minutes(Market.HK, day)          # 漏标: 按全日市
    register_half_days(Market.HK, [day])
    half = trading_total_minutes(Market.HK, day)          # 正确: 半日市
    assert full == 330 and half == 150
    ratio = full / half
    assert 2.0 < ratio < 2.5, f"低估倍数应约 2.2x, 实际 {ratio:.2f}"


# ================================================================
# 类型契约
# ================================================================

def test_market_enum_is_str_enum():
    """str 枚举: 可直接进 JSON 响应 / SQL 参数 / 缓存 key, 无需 .value。"""
    assert isinstance(Market.CN, str)
    assert Market.CN == "CN"
    assert Market.HK.value == "HK"


def test_market_enum_str_and_fstring_yield_bare_value():
    """⭐ ``str()`` / f-string 必须得到 ``"CN"``, 不是 ``"Market.CN"``。

    这是选 ``StrEnum`` 而非 ``(str, Enum)`` 的唯一理由, 也是最容易被「顺手改回
    (str, Enum)」破坏的一条。破坏后的症状不是报错, 而是:
    ``f"{market}" == "CN"`` 恒假 → 分支静默走 else; ``cache[f"{market}"]`` 与
    ``cache["CN"]`` 变成两个键 → 缓存永不命中。
    QuantMind 的同类事故: ``str(OrderSide.SELL) == "OrderSide.SELL"`` 永不成立,
    导致港股印花税静默归零。
    """
    assert str(Market.CN) == "CN"
    assert str(Market.HK) == "HK"
    assert f"{Market.CN}" == "CN"
    assert f"{Market.HK}" == "HK"
    # %-格式化是**独立**的一条路径 (走 __str__ 而非 __format__), 同样必须钉住:
    # (str, Enum) 下它给出 "Market.HK"。日志与旧式模板里大量使用, 不能漏。
    assert "%s" % Market.HK == "HK"  # noqa: UP031 — 刻意测 % 格式化, 不是待迁移的旧写法
    # 因此可以安全地直接拼进 key / 路径 / SQL 参数
    assert {Market.CN: 1, Market.HK: 2}["CN"] == 1
    assert Market("CN") is Market.CN
    assert Market("HK") is Market.HK


def test_rules_expose_trading_total_minutes_as_sum_of_sessions():
    """派生属性必须由 sessions 求和, 不得另存一份常量 (会与 sessions 漂移)。"""
    for rules in RULES_BY_MARKET.values():
        assert rules.trading_total_minutes == sum(s.minutes for s in rules.sessions)
        assert isinstance(rules, MarketTradingRules)
