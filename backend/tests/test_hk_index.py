"""港股指数 (PR3, 方案 §11) 契约测试: 白名单 / 快照解析 / 日K列序 / 同步三态 / API 口径。

**固定样本数值断言** (CONTRIBUTING §测试矩阵): 这几处口径一旦被改错, 后果都是
静默偏移而非报错 —— 涨跌幅忘除 100 (UI 显示 -154.5%)、腾讯列序当成 OHLC
(上下影线全错)、日期分隔符写死 (27 只指数 timestamp 全 None, 新鲜度判断失效)、
volume 落成 null (``filter_halt_days`` 把整帧当停牌滤空)。只有钉死具体数值才拦得住。

全部**离线**: 网络路径由 fake client 顶替, 样本取自 2026-11-16 真机实测值。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.market import Market
from app.plugins.sina import hk_index
from app.plugins.sina.client import SinaError
from app.plugins.tencent.client import TencentError
from app.services import index_const, index_sync_hk
from app.tickflow.repository import DataStore, KlineRepository

# ---------------------------------------------------------------------------
# 真机实测样本 (2026-11-13 收盘, HSI)
# ---------------------------------------------------------------------------

#: 新浪快照原始行。字段序: 0代码 1名称 2今开 3昨收 4最高 5最低 6最新 7涨跌额
#: 8涨跌幅(**百分数**) 9/10恒零 11成交量 12成交额 13/14恒零 15/16 52周高低
#: 17日期(**斜杠**) 18时间。
SINA_HSI_LINE = (
    'var hq_str_hkHSI="HSI,恒生指数,23871.910,24008.550,23930.130,23595.360,'
    "23637.600,-370.950,-1.545,0.00000,0.00000,195857007,11525719783,"
    '0.000,0.000,27010.320,15234.670,2026/11/13,16:12";'
)

#: 2026-11-13 16:12 Asia/Hong_Kong → 毫秒。港股半日市收盘戳是 12:00 HKT,
#: 时区口径写错会让新鲜度判断偏移 (见 ``_to_timestamp_ms`` docstring)。
HSI_TS_MS = 1_794_557_520_000

#: 腾讯日K行 —— 🔴 列序 ``[date, open, CLOSE, high, low, volume]``, 收盘在 index 2。
TENCENT_HSI_ROWS = [
    ["2026-11-12", "24121.40", "24008.55", "24132.20", "23942.74", "202652361355"],
    ["2026-11-13", "23871.91", "23637.60", "23930.13", "23595.36", "195857007140"],
]


def _sina_line(code: str, name: str, last: float) -> str:
    """构造一条最小可用的新浪快照行 (19 字段, 日期用**斜杠** —— 实测真值)。"""
    f = [
        code, name, "1", "1", "1", "1", str(last), "0", "0", "0", "0",
        "1", "1", "0", "0", "1", "1", "2026/11/13", "16:12",
    ]
    assert len(f) == hk_index.HK_INDEX_FIELD_COUNT
    return f'var hq_str_hk{code}="{",".join(f)}";'


#: 核心四指数快照 (HSI 用真机实测行, 其余用最小行) —— API 缺省路径的样本。
SINA_CORE_SNAPSHOT = "\n".join(
    [
        SINA_HSI_LINE,
        _sina_line("HSTECH", "恒生科技指数", 3813.19),
        _sina_line("HSCEI", "恒生中国企业指数", 7937.53),
        _sina_line("HSCCI", "恒生香港中资企业指数", 3915.00),
    ]
)


class FakeTencent:
    """按代码返回预置日K行。

    ``empty`` 里的代码返回 ``[]`` (模拟腾讯无覆盖), ``error`` 里的抛 ``TencentError``。
    两者必须在测试里可区分 —— 这正是 ``sync_hk_index_daily`` 三态分类的依据。
    """

    def __init__(self, data=None, *, empty=(), error=()):
        self.data = dict(data or {})
        self.empty = set(empty)
        self.error = set(error)
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def kline_daily(self, code: str, count: int = 800) -> list[list]:
        self.calls.append((code, count))
        sym = code[2:] if code.startswith("hk") else code
        if sym in self.error:
            raise TencentError(f"模拟失败: {sym}")
        if sym in self.empty:
            return []
        return self.data.get(sym, [])

    def close(self) -> None:
        self.closed = True


class FakeSina:
    """顶替 ``SinaClient``: 只提供 ``fetch_hk_index_spot`` 用到的 ``_get_text``。"""

    def __init__(self, text: str = "", *, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []
        self.closed = False

    def _get_text(self, base, path, params=None, *, encoding=None, referer=None):
        self.calls.append({"base": base, "path": path, "encoding": encoding, "referer": referer})
        if self.error is not None:
            raise self.error
        return self.text

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def env(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    return TestClient(app), repo, tmp_path


# ---------------------------------------------------------------------------
# 1. 白名单契约
# ---------------------------------------------------------------------------

def test_whitelist_sizes_are_the_measured_universe():
    """27 真港股 + 12 纯A股污染 + 3 跨市场 = 42, 与 2026-11-16 单次实测一致。

    数字变了只可能是①源改了代码空间 ②有人动了清单。两者都必须显式过测试,
    不能静默生效 —— 清单是「允许写入港股指数目录」的唯一闸门。
    """
    assert len(hk_index.HK_INDEX_WHITELIST) == 27
    assert len(hk_index.HK_INDEX_A_SHARE_CONTAMINANTS) == 12
    assert len(hk_index.HK_INDEX_EXCLUDED_CROSS_MARKET) == 3


def test_whitelist_contains_no_a_share_contaminant():
    """白名单与污染集/跨市场集互斥 (模块级 assert 的测试侧镜像)。"""
    assert not (hk_index.HK_INDEX_WHITELIST & hk_index.HK_INDEX_A_SHARE_CONTAMINANTS)
    assert not (hk_index.HK_INDEX_WHITELIST & set(hk_index.HK_INDEX_EXCLUDED_CROSS_MARKET))


@pytest.mark.parametrize("code", ["SSECOMP", "CSI300", "SSE50", "SSEDIV"])
def test_pure_a_share_indices_are_rejected(code):
    assert code not in hk_index.HK_INDEX_WHITELIST
    assert code in hk_index.HK_INDEX_A_SHARE_CONTAMINANTS


@pytest.mark.parametrize(
    "code",
    ["HSCASI", "CESA80", "CES300"],
)
def test_hk_looking_prefixes_that_are_actually_a_share(code):
    """🔴 前缀会说谎: 这三个代码长得像港股指数, 实为 A 股/跨市场标的。

    它们是「不许用前缀规则或黑名单、必须显式允许清单」这条设计的**唯一证据**。
    谁把白名单换成 ``startswith(("HS","CES","CSHK"))`` 之类的规则, 这三个就会
    漏进港股指数目录 —— 此测试即护栏。
    """
    assert code not in hk_index.HK_INDEX_WHITELIST
    assert code in hk_index.HK_INDEX_EXCLUDED_CROSS_MARKET


def test_core_four_are_all_whitelisted():
    """展示层固定的核心四指数必须都在白名单内, 否则侧栏永远空白。"""
    core = set(index_const.core_index_symbols(Market.HK))
    assert core == {"HSI", "HSTECH", "HSCEI", "HSCCI"}
    assert core <= hk_index.HK_INDEX_WHITELIST


# ---------------------------------------------------------------------------
# 2. 快照解析与字段口径
# ---------------------------------------------------------------------------

def test_parse_snapshot_extracts_code_and_fields():
    got = hk_index.parse_hk_index_snapshot(SINA_HSI_LINE)
    assert len(got) == 1
    code, fields = got[0]
    assert code == "HSI"
    assert len(fields) == hk_index.HK_INDEX_FIELD_COUNT
    assert fields[1] == "恒生指数"


def test_map_quote_divides_change_pct_by_100():
    """🔴 新浪字段 8 **已是百分数** (-1.545), 本仓契约要**小数** (-0.01545)。

    忘除 100 → ``_build_index_quotes`` 再乘 100 → 侧栏显示 -154.5%。
    """
    _, fields = hk_index.parse_hk_index_snapshot(SINA_HSI_LINE)[0]
    q = hk_index.map_hk_index_quote("HSI", fields)
    assert q["change_pct"] == pytest.approx(-0.01545, abs=1e-9)
    assert q["change_amount"] == pytest.approx(-370.950)
    assert q["last_price"] == pytest.approx(23637.600)
    assert q["prev_close"] == pytest.approx(24008.550)
    assert q["open"] == pytest.approx(23871.910)
    assert q["high"] == pytest.approx(23930.130)
    assert q["low"] == pytest.approx(23595.360)
    assert q["name"] == "恒生指数"


def test_map_quote_amplitude_is_decimal():
    """振幅同为小数口径: (最高-最低)/昨收 = 334.770/24008.550。"""
    _, fields = hk_index.parse_hk_index_snapshot(SINA_HSI_LINE)[0]
    q = hk_index.map_hk_index_quote("HSI", fields)
    assert q["amplitude"] == pytest.approx((23930.130 - 23595.360) / 24008.550, rel=1e-9)


def test_map_quote_timestamp_uses_slash_date_and_hkt():
    """🔴 日期分隔符是**斜杠** ``2026/11/13``, 时区按 Asia/Hong_Kong。

    这条是回归护栏: 曾因为把分隔符当成短横线, 27 只指数 ``timestamp`` **全部**
    为 None, 前端新鲜度判断整体失效 (且失败是静默的)。
    """
    _, fields = hk_index.parse_hk_index_snapshot(SINA_HSI_LINE)[0]
    assert fields[17] == "2026/11/13"
    q = hk_index.map_hk_index_quote("HSI", fields)
    assert q["timestamp"] == HSI_TS_MS
    assert dt.datetime.fromtimestamp(q["timestamp"] / 1000, dt.UTC).hour == 8  # 16:12 HKT


@pytest.mark.parametrize("day", ["2026/11/13", "2026-11-13"])
def test_date_separator_tolerance(day):
    """斜杠是实测真值, 短横线保留是因为该端点无官方文档 —— 两种都要能解析。"""
    assert hk_index._to_timestamp_ms(day, "16:12") == HSI_TS_MS


@pytest.mark.parametrize("day,clock", [("2026年11月13日", "16:12"), ("", "16:12"), ("2026/11/13", ""), (None, None)])
def test_unparseable_date_returns_none_not_now(day, clock):
    """解析失败必须返回 None: 兜底成 ``now()`` 会把过期数据伪装成实时数据。"""
    assert hk_index._to_timestamp_ms(day, clock) is None


def test_map_quote_drops_row_without_last_price():
    """无最新价的行情行没有意义 (下游补算涨跌幅会除零), 直接丢弃。"""
    fields = ["HSI", "恒生指数"] + [""] * 17
    assert hk_index.map_hk_index_quote("HSI", fields) is None


def test_fetch_spot_drops_response_codes_outside_whitelist():
    """快照拉取: 带 Referer、按 GBK 解码, 且**响应里**的白名单外代码被丢弃。

    注意分层: ``fetch_hk_index_spot`` 对**请求**的非白名单代码是抛 ``SinaError``
    (fail closed, 调用方不该请求污染代码); 对**响应**里多出来的代码才是丢弃 +
    告警 —— 那意味着源改了代码空间, 静默写库会把 A 股指数混进港股目录。
    """
    text = SINA_HSI_LINE + '\nvar hq_str_hkSSECOMP="SSECOMP,上证综指,1,1,1,1,1,0,0,0,0,1,1,0,0,1,1,2026/11/13,15:00";\n'
    fake = FakeSina(text=text)
    rows = hk_index.fetch_hk_index_spot(fake, ["HSI"])
    assert [r["symbol"] for r in rows] == ["HSI"]  # A股污染被滤掉
    call = fake.calls[0]
    assert call["referer"] == hk_index.HK_INDEX_REFERER
    assert call["encoding"] == hk_index.HK_INDEX_ENCODING


def test_fetch_spot_rejects_requesting_contaminants():
    """请求 A 股污染代码 → 抛错而不是静默忽略 (与上一条的分层互补)。"""
    with pytest.raises(SinaError, match="白名单"):
        hk_index.fetch_hk_index_spot(FakeSina(text=SINA_HSI_LINE), ["HSI", "SSECOMP"])


def test_fetch_spot_raises_on_empty_response():
    """空响应必须抛错而非返回 ``[]``: 上游靠异常区分「失败」与「确实无数据」。"""
    with pytest.raises(SinaError):
        hk_index.fetch_hk_index_spot(FakeSina(text=""), ["HSI"])


# ---------------------------------------------------------------------------
# 3. 腾讯日K列序
# ---------------------------------------------------------------------------

def test_tencent_daily_column_order_close_at_index_2():
    """🔴 ``[date, open, CLOSE, high, low, volume]`` —— 收盘在第 3 列。

    按 OHLC 直觉读会把 close 当 high: 上下影线全错, MA/ATR/MACD 全偏。
    """
    frame = index_sync_hk._parse_tencent_daily_rows("HSI", TENCENT_HSI_ROWS)
    latest = frame.sort("date").row(-1, named=True)
    assert latest["close"] == pytest.approx(23637.60)
    assert latest["open"] == pytest.approx(23871.91)
    assert latest["high"] == pytest.approx(23930.13)
    assert latest["low"] == pytest.approx(23595.36)
    # OHLC 自洽: high 是四者最大, low 是四者最小 (列序读错时这条必炸)
    assert latest["high"] == max(latest["open"], latest["close"], latest["high"], latest["low"])
    assert latest["low"] == min(latest["open"], latest["close"], latest["high"], latest["low"])


def test_tencent_daily_string_values_become_floats():
    """腾讯所有数值以**字符串**下发, 必须落成 Float64 才能进指标计算。"""
    frame = index_sync_hk._parse_tencent_daily_rows("HSI", TENCENT_HSI_ROWS)
    for col in ("open", "high", "low", "close", "volume"):
        assert frame.schema[col] == pl.Float64, col
    assert frame.schema["date"] == pl.Date


def test_tencent_daily_has_no_amount_column():
    """🔴 刻意**不造** amount: 腾讯日K不提供成交额, 填 0 会被前端当成真实读数。"""
    frame = index_sync_hk._parse_tencent_daily_rows("HSI", TENCENT_HSI_ROWS)
    assert "amount" not in frame.columns


def test_tencent_daily_skips_bad_rows():
    """日期不可解析 / 字段数不足的行跳过, 不兜底成今天 (会污染当日分区)。"""
    rows = [
        ["2026年11月13日", "1", "2", "3", "4", "5"],  # 日期格式非法
        ["2026-11-13", "1", "2", "3"],  # 字段数不足
        TENCENT_HSI_ROWS[1],
    ]
    frame = index_sync_hk._parse_tencent_daily_rows("HSI", rows)
    assert frame.height == 1
    assert frame["date"][0] == dt.date(2026, 11, 13)


# ---------------------------------------------------------------------------
# 4. 同步: 三态分类 + 市场隔离
# ---------------------------------------------------------------------------

def test_universe_defaults_to_whitelist_and_rejects_unknown():
    assert index_sync_hk.hk_index_universe() == sorted(hk_index.HK_INDEX_WHITELIST)
    assert index_sync_hk.hk_index_universe(["hsi", "vhsi"]) == ["HSI", "VHSI"]  # 大写归一
    with pytest.raises(ValueError, match="白名单"):
        index_sync_hk.hk_index_universe(["HSI", "SSECOMP"])
    with pytest.raises(ValueError, match="空集合"):
        index_sync_hk.hk_index_universe([])


def test_sync_classifies_no_coverage_separately_from_error(env):
    """🔴 「源无覆盖」不是失败。

    实测腾讯只覆盖 27 个白名单代码中的 14 个。把无覆盖记成失败会让调度器每轮
    告警并误判为部分失败 → 重蹈 PR2 ``sync_hk_adj_factor``「部分失败但存储非空
    导致下游三态闸门误判完整」的覆辙。
    """
    _, repo, _ = env
    fake = FakeTencent(
        {"HSI": TENCENT_HSI_ROWS, "HSTECH": TENCENT_HSI_ROWS},
        empty={"HSCEI"},  # 腾讯无覆盖
        error={"HSCCI"},  # 真失败
    )
    written, failed = index_sync_hk.sync_hk_index_daily(
        repo, symbols=["HSI", "HSTECH", "HSCEI", "HSCCI"], client=fake
    )
    assert written == 4
    assert failed == ["HSCCI"]  # 只有真错误进 failed, HSCEI 不在其中
    assert fake.closed is False  # 外部传入的 client 由调用方负责关闭


def test_sync_writes_to_hk_dirs_only(env):
    """市场隔离: 港股指数只落 ``kline_hk_index_*``, A 股 ``kline_index_*`` 必须为空。"""
    _, repo, tmp_path = env
    fake = FakeTencent({"HSI": TENCENT_HSI_ROWS})
    written, failed = index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=fake)
    assert (written, failed) == (2, [])

    assert repo.execute_one("SELECT COUNT(*) FROM kline_hk_index_daily")[0] == 2
    assert repo.execute_one("SELECT COUNT(*) FROM kline_hk_index_enriched")[0] == 2
    assert list((tmp_path / "kline_index_daily").iterdir()) == []
    assert list((tmp_path / "kline_index_enriched").iterdir()) == []


def test_sync_stored_volume_is_never_null(env):
    """🔴 null volume 会让 ``filter_halt_days`` 把**整帧**当停牌滤空。

    polars 里 ``null == 0`` 求值为 null, ``~null`` 仍是 null → 行被排除。
    实测 58 行 null-volume 帧经 ``compute_enriched`` 后返回 **0 行** (静默全丢)。
    """
    _, repo, _ = env
    fake = FakeTencent({"HSI": TENCENT_HSI_ROWS})
    index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=fake)
    assert repo.execute_one("SELECT COUNT(*) FROM kline_hk_index_daily WHERE volume IS NULL")[0] == 0
    # enriched 确实算出来了 (若被滤空, 这里会是 0 行)
    assert repo.execute_one("SELECT COUNT(*) FROM kline_hk_index_enriched")[0] == 2


def test_sync_closes_self_owned_client(env, monkeypatch):
    """未传 client 时自建并必须在 finally 关闭 (令牌桶/连接池不泄漏)。"""
    _, repo, _ = env
    made: list[FakeTencent] = []

    def factory():
        f = FakeTencent({"HSI": TENCENT_HSI_ROWS})
        made.append(f)
        return f

    monkeypatch.setattr(index_sync_hk, "TencentClient", factory)
    index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"])
    assert made and made[0].closed is True


def test_sync_reports_progress_per_symbol(env):
    _, repo, _ = env
    fake = FakeTencent({"HSI": TENCENT_HSI_ROWS}, empty={"HSTECH"})
    seen: list[tuple[int, int]] = []
    index_sync_hk.sync_hk_index_daily(
        repo, symbols=["HSI", "HSTECH"], client=fake, on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen == [(1, 2), (2, 2)]  # 无覆盖的也计数, 否则进度条永远到不了 100%


def test_sync_all_failed_does_not_raise_and_api_stays_200(env, monkeypatch):
    """全失败时 sync 不抛, 且 API 仍 200 + 空 rows。

    空目录下 DuckDB ``read_parquet`` 无匹配文件会抛 → 视图**注册不上**, 这是既有
    行为 (同 ``test_hk_api.py::test_instruments_empty_before_first_sync``)。因此这里
    断言的是**用户可见契约**: 查询端点把 CatalogException 兜成空结果, 不 500。
    """
    client, repo, _ = env
    fake = FakeTencent({}, error={"HSI"})
    written, failed = index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=fake)
    assert (written, failed) == (0, ["HSI"])

    r = client.get("/api/hk/index_daily", params={"symbol": "HSI"})
    assert r.status_code == 200
    assert r.json()["rows"] == []
    # 快照降级路径同样不 500 (读不到日K → 空 rows + index_daily 来源标注)
    _patch_sina(monkeypatch, error=SinaError("模拟不可用"))
    j = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()
    assert j["rows"] == [] and j["source"] == "index_daily"


def test_sync_error_in_one_symbol_does_not_abort_batch(env):
    _, repo, _ = env
    fake = FakeTencent({"HSI": TENCENT_HSI_ROWS, "VHSI": TENCENT_HSI_ROWS}, error={"HSTECH"})
    written, failed = index_sync_hk.sync_hk_index_daily(
        repo, symbols=["HSI", "HSTECH", "VHSI"], client=fake
    )
    assert written == 4
    assert failed == ["HSTECH"]
    assert {r[0] for r in repo.execute_all("SELECT DISTINCT symbol FROM kline_hk_index_daily")} == {"HSI", "VHSI"}


# ---------------------------------------------------------------------------
# 5. API 契约
# ---------------------------------------------------------------------------

def _patch_sina(monkeypatch, text: str = SINA_CORE_SNAPSHOT, *, error: Exception | None = None):
    holder: list[FakeSina] = []

    def factory():
        f = FakeSina(text=text, error=error)
        holder.append(f)
        return f

    monkeypatch.setattr(index_sync_hk, "SinaClient", factory)
    return holder


def test_indices_defaults_to_core_four(env, monkeypatch):
    """缺省 = 核心四指数 (CONTRIBUTING §指数契约: 展示层固定, index_const 是唯一真源)。"""
    client, _, _ = env
    _patch_sina(monkeypatch)
    r = client.get("/api/hk/indices")
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 4
    assert j["source"] == "realtime"
    assert {row["symbol"] for row in j["rows"]} == set(index_const.core_index_symbols(Market.HK))


def test_indices_response_carries_hk_semantics(env, monkeypatch):
    """🔴 ``quote_delay_minutes`` 是前端「延时」标注的**唯一来源**, 不得前端硬编码。"""
    client, _, _ = env
    _patch_sina(monkeypatch)
    j = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()
    assert j["quote_delay_minutes"] == 15
    assert j["currency"] == "HKD"
    assert j["market"] == "HK"
    row = j["rows"][0]
    assert row["symbol"] == "HSI"
    assert row["last_price"] == pytest.approx(23637.600)
    assert row["change_pct"] == pytest.approx(-0.01545, abs=1e-9)
    assert row["timestamp"] == HSI_TS_MS


def test_indices_normalizes_case_and_preserves_caller_order(env, monkeypatch):
    client, _, _ = env
    _patch_sina(
        monkeypatch,
        text="\n".join([SINA_HSI_LINE, _sina_line("VHSI", "恒指波幅指数", 19.93)]),
    )
    j = client.get("/api/hk/indices", params={"symbols": "vhsi,hsi"}).json()
    assert [r["symbol"] for r in j["rows"]] == ["VHSI", "HSI"]  # 按调用方顺序, 非源顺序


def test_indices_skips_symbols_the_source_did_not_return(env, monkeypatch):
    """源漏返回的代码不伪造行, 但其余照常返回 (部分可用优于整块空白)。"""
    client, _, _ = env
    _patch_sina(monkeypatch, text=SINA_HSI_LINE)  # 只有 HSI 一行
    j = client.get("/api/hk/indices", params={"symbols": "HSI,HSTECH"}).json()
    assert [r["symbol"] for r in j["rows"]] == ["HSI"]
    assert j["count"] == 1


@pytest.mark.parametrize("bad", ["00700.HK", "SSECOMP", "HSCASI", "CES300", "NOPE"])
def test_indices_rejects_non_hk_index_codes(env, bad):
    """🔴 校验用**指数白名单**而非 ``is_hk_symbol``。

    股票契约 (``[0-9]{5}\\.HK``) 与 A 股污染代码都必须 400 —— 静默丢弃会让
    调用方以为拿到了数据。``HSCASI``/``CES300`` 是「前缀会说谎」的反例。
    """
    client, _, _ = env
    r = client.get("/api/hk/indices", params={"symbols": bad})
    assert r.status_code == 400
    assert bad in r.json()["detail"]


def test_index_daily_reads_store_and_declares_no_volume_unit(env):
    client, repo, _ = env
    index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=FakeTencent({"HSI": TENCENT_HSI_ROWS}))
    j = client.get("/api/hk/index_daily", params={"symbol": "HSI"}).json()
    assert j["symbol"] == "HSI"
    assert j["name"] == "恒生指数"
    # 🔴 两源量纲矛盾 (新浪 195857007 vs 腾讯 195857007140, ×1000) → 不断言单位,
    # 编一个会让前端把量能用错 1000 倍。
    assert j["volume_unit"] is None
    assert j["currency"] == "HKD"
    assert len(j["rows"]) == 2
    assert j["rows"][0]["date"] == "2026-11-13"  # DESC
    assert j["rows"][0]["close"] == pytest.approx(23637.60)


def test_index_daily_empty_before_first_sync(env):
    """从未同步过 → 200 + 空 rows, 不是 500 (视图在空目录下不注册)。"""
    client, _, _ = env
    r = client.get("/api/hk/index_daily", params={"symbol": "HSI"})
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_index_daily_honours_date_window(env):
    client, repo, _ = env
    index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=FakeTencent({"HSI": TENCENT_HSI_ROWS}))
    j = client.get(
        "/api/hk/index_daily", params={"symbol": "HSI", "start": "2026-11-13", "limit": 10}
    ).json()
    assert [r["date"] for r in j["rows"]] == ["2026-11-13"]


def test_sync_index_daily_route_reports_three_state(env, monkeypatch):
    """路由把 service 的 ``(written, failed)`` 翻成 ok/partial/empty 三态。"""
    client, _, _ = env
    fake = FakeTencent({"HSI": TENCENT_HSI_ROWS}, empty={"HSTECH"}, error={"HSCEI"})
    real = index_sync_hk.sync_hk_index_daily
    monkeypatch.setattr(
        index_sync_hk,
        "sync_hk_index_daily",
        lambda repo, **kw: real(repo, client=fake, **kw),
    )
    r = client.post(
        "/api/hk/sync_index_daily",
        json={"symbols": ["HSI", "HSTECH", "HSCEI"], "count": 60},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "partial"  # 有写入但有真失败
    assert j["count"] == 2
    assert j["failed"] == ["HSCEI"]


def test_sync_index_daily_route_rejects_unknown_symbol(env):
    """白名单校验失败 → 400 (ValueError 转译), 不是 500。"""
    client, _, _ = env
    r = client.post("/api/hk/sync_index_daily", json={"symbols": ["SSECOMP"]})
    assert r.status_code == 400
    assert "SSECOMP" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 降级路径 (新浪不可用 → 读日K)
# ---------------------------------------------------------------------------

def test_indices_falls_back_to_daily_when_sina_fails(env, monkeypatch):
    client, repo, _ = env
    index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=FakeTencent({"HSI": TENCENT_HSI_ROWS}))
    _patch_sina(monkeypatch, error=SinaError("模拟限流"))
    j = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()
    assert j["source"] == "index_daily"  # 如实标注, 不伪装成实时
    assert j["count"] == 1
    row = j["rows"][0]
    assert row["last_price"] == pytest.approx(23637.60)
    # 涨跌额/幅由最近两根算出: (23637.60 - 24008.55) / 24008.55
    assert row["change_amount"] == pytest.approx(-370.95)
    assert row["change_pct"] == pytest.approx(-0.0154507, abs=1e-7)
    assert row["trade_date"] == "2026-11-13"
    assert row["timestamp"] is None  # 日K只有日期没有时刻, 不伪造成 00:00 的毫秒戳
    assert "amount" not in row  # 腾讯无成交额 → 不产出该键 (填 0 会被当成真实读数)


def test_fallback_matches_realtime_change_pct(env, monkeypatch):
    """交叉验证: 降级路径与实时路径对同一交易日必须给出**同一个**涨跌幅。

    两条路径完全独立 (新浪字段 8 vs 腾讯相邻两根收盘价), 数值一致说明
    「÷100 的小数口径」与「腾讯列序」两处都没有读错。
    """
    client, repo, _ = env
    index_sync_hk.sync_hk_index_daily(repo, symbols=["HSI"], client=FakeTencent({"HSI": TENCENT_HSI_ROWS}))
    _patch_sina(monkeypatch)
    live = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()["rows"][0]
    _patch_sina(monkeypatch, error=SinaError("down"))
    fb = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()["rows"][0]
    assert fb["change_pct"] == pytest.approx(live["change_pct"], abs=1e-6)
    assert fb["last_price"] == pytest.approx(live["last_price"])


def test_fallback_without_previous_bar_leaves_change_pct_none(env, monkeypatch):
    """只有一根历史 → 算不出涨跌幅, 留 None 让前端显示占位符。

    补 0 会渲染成「今天没动」, 那是假数据。
    """
    client, repo, _ = env
    index_sync_hk.sync_hk_index_daily(
        repo, symbols=["HSI"], client=FakeTencent({"HSI": TENCENT_HSI_ROWS[-1:]})
    )
    _patch_sina(monkeypatch, error=SinaError("down"))
    row = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()["rows"][0]
    assert row["change_pct"] is None
    assert row["change_amount"] is None
    assert row["prev_close"] is None


def test_fallback_returns_empty_when_never_synced(env, monkeypatch):
    """从未同步过 + 新浪不可用 → 空 rows, 且仍带港股语义字段 (前端据此显示空态)。"""
    client, _, _ = env
    _patch_sina(monkeypatch, error=SinaError("down"))
    j = client.get("/api/hk/indices", params={"symbols": "HSI"}).json()
    assert j["rows"] == []
    assert j["count"] == 0
    assert j["source"] == "index_daily"
    assert j["quote_delay_minutes"] == 15
    assert j["currency"] == "HKD"
