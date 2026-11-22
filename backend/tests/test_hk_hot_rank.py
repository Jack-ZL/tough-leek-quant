"""``app.plugins.eastmoney.hk_hot_rank`` + ``app.services.hk_hot_rank`` + ``/api/hk/hot-rank`` 测试。

服务层测试把 ``_FakeClient`` 当 ``client=`` 传进去, 因此**整条链**
(插件解析 → 服务聚合 → 维表连接) 都被真实执行, 只有 HTTP 那一层是假的。
夹具里的上游响应**逐字段照抄 2026-11-22 的活体探测结果**, 不是凭空构造 ——
这样上游改字段名时测试会失败, 而不是继续通过然后线上静默取空值。

重点回归闸:

1. 🔴 ``pageSize`` 必须是 100 且 ``pageNo`` 必须是 1: 上游对 >100 或第 2 页
   **静默返回空列表** (实测), 所以这两个值一旦被人"参数化"出去, 得到的会是
   「港股此刻没有人气数据」这个假结论。测试直接断言**发出去的 payload**。
2. 🔴 榜外代码上游返回 **200 + ``code=0`` + ``data`` 内字段全 null**, 不是 404。
   判空只能靠 ``rank is None``, 绝不能用 HTTP 状态码或信封 ``code``。
3. 🔴 ``market_all_count`` 拿不到时 ``rank_percentile`` 必须**全为 null**,
   不得拿榜单条数 (100) 当分母凑数 —— 那会把「前 3.6%」算成「前 100%」。
4. 🔴 上游失败必须 502, **不得**降级成空列表 (§4 fail closed)。
5. 🔴 ``history`` 的 ``calc_time`` 是日期、``intraday`` 的是时刻, 两种格式
   **刻意不统一** (强行统一会给日频补一个假的 ``00:00:00`` 并被 tooltip 显示)。
6. 非法 symbol (``700.HK``) **不发请求**就返回未收录 —— 发了也是白问, 且可能
   被上游解析到另一只标的。
"""

from __future__ import annotations

import inspect

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.plugins.eastmoney import hk_hot_rank as plugin
from app.plugins.eastmoney.client import EastmoneyError
from app.services import hk_hot_rank as svc
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import DataStore, KlineRepository

# ---------------------------------------------------------------------------
# 上游响应夹具 (逐字段照抄 2026-11-22 活体探测)
# ---------------------------------------------------------------------------

GLOBAL_ID = "786e4c21-70dc-435a-93bb-38"


def _env(data) -> dict:
    """上游信封。``code=0`` / ``status=0`` / ``message="OK"`` 是成功态。"""
    return {
        "globalId": GLOBAL_ID,
        "message": "OK",
        "status": 0,
        "code": 0,
        "data": data,
        "stack": None,
    }


#: 榜单前 5 行 (真实结构: ``sc``/``rk``/``rc``/``hisRc``)
LIST_ROWS = [
    {"sc": "HK|00700", "rk": 1, "rc": 0, "hisRc": 0},
    {"sc": "HK|09988", "rk": 2, "rc": 0, "hisRc": 0},
    {"sc": "HK|01810", "rk": 3, "rc": 0, "hisRc": 0},
    {"sc": "HK|09992", "rk": 11, "rc": 1, "hisRc": 0},
    {"sc": "HK|02525", "rk": 100, "rc": 1, "hisRc": -11},
]

#: 单标的最新排名 (真实字段全集, 含未文档化的 ``hisRankChange_rank``)
LATEST_00700 = {
    "marketType": "000003",
    "marketAllCount": 2811,
    "calcTime": "2026-11-22 12:50:00",
    "innerCode": "00700_2",
    "srcSecurityCode": "HK|00700",
    "rank": 1,
    "rankChange": 0,
    "hisRankChange": 0,
    "hisRankChange_rank": 1397,
    "flag": 0,
}

#: 🔴 榜外代码的真实响应: **200 + code=0 + 全 null**, 不是 404 也不是空 dict
LATEST_UNLISTED = {k: None for k in LATEST_00700}

HISTORY_ROWS = [
    {"calcTime": "2026-07-26", "rank": 3},
    {"calcTime": "2026-07-27", "rank": 2},
    {"calcTime": "2026-11-22", "rank": 1},
]

INTRADAY_ROWS = [
    {"calcTime": "2026-11-22 00:00:00", "rank": 2},
    {"calcTime": "2026-11-22 00:10:00", "rank": 1},
    {"calcTime": "2026-11-22 13:00:00", "rank": 1},
]

PATH_LIST = "/stockrank/getAllCurrHkUsList"
PATH_LATEST = "/stockrank/getCurrentHkUsLatest"
PATH_HIS = "/stockrank/getHisHkUsList"
PATH_CURR = "/stockrank/getCurrentHkUsList"


class _FakeClient:
    """按 path 路由的假东财客户端, 记录每次调用的 payload。

    ``responses``: ``{path: data | Exception}`` —— 值是 ``Exception`` 时抛出,
    否则包进标准信封返回。未配置的 path 抛错 (暴露测试没预料到的请求)。
    """

    def __init__(self, responses: dict | None = None):
        self._responses = responses if responses is not None else {
            PATH_LIST: LIST_ROWS,
            PATH_LATEST: LATEST_00700,
            PATH_HIS: HISTORY_ROWS,
            PATH_CURR: INTRADAY_ROWS,
        }
        self.calls: list[dict] = []
        self.closed = False

    def _post_json(self, base: str, path: str, json_body: dict) -> dict:
        self.calls.append({"base": base, "path": path, "body": json_body})
        if path not in self._responses:
            raise AssertionError(f"测试未预料到的请求: {path}")
        val = self._responses[path]
        if isinstance(val, Exception):
            raise val
        return _env(val)

    def calls_for(self, path: str) -> list[dict]:
        return [c for c in self.calls if c["path"] == path]

    def close(self):
        self.closed = True


@pytest.fixture()
def env(tmp_path):
    """带**已同步维表**的环境 (名称可解析)。"""
    _seed_dim(tmp_path, [
        {"symbol": "00700.HK", "name": "腾讯控股"},
        {"symbol": "09988.HK", "name": "阿里巴巴-W"},
        {"symbol": "01810.HK", "name": "小米集团-W"},
        # 刻意不给 09992 / 02525: 模拟「维表在但部分标的查不到名称」
    ])
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None
    return TestClient(app), repo, tmp_path


def _seed_dim(tmp_path, rows: list[dict]) -> None:
    out = tmp_path / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), out)


# ---------------------------------------------------------------------------
# 插件层: 纯函数
# ---------------------------------------------------------------------------

def test_hot_rank_code_accepts_only_five_digit_hk():
    """🔴 5 位是硬校验: 放过 ``700.HK`` 会拼出 ``HK|700``, 上游可能不报错却
    解析到**另一只**标的 —— 静默取错数据比取不到数据危险得多。"""
    assert plugin.hot_rank_code("00700.HK") == "00700"
    assert plugin.hot_rank_code(" 00700.hk ") == "00700"
    assert plugin.hot_rank_code("700.HK") is None
    assert plugin.hot_rank_code("000001.SZ") is None
    assert plugin.hot_rank_code("600000.SH") is None
    assert plugin.hot_rank_code("00700") is None
    assert plugin.hot_rank_code("") is None
    assert plugin.hot_rank_code(None) is None


def test_split_sc_parses_market_prefixed_code():
    assert plugin._split_sc("HK|00700") == "00700"
    assert plugin._split_sc("hk|00700") == "00700"
    # 非港股市场前缀 / 缺分隔符 / 空代码一律 None
    assert plugin._split_sc("US|AAPL") is None
    assert plugin._split_sc("00700") is None
    assert plugin._split_sc("HK|") is None
    assert plugin._split_sc(None) is None
    assert plugin._split_sc(123) is None


def test_to_int_rejects_bool_and_garbage_without_guessing():
    """``True`` 是 ``int`` 的子类, 不显式挡会被当成 1 (名次 1 = 最热门)。"""
    assert plugin._to_int(3) == 3
    assert plugin._to_int("7") == 7
    assert plugin._to_int(None) is None
    assert plugin._to_int(True) is None
    assert plugin._to_int(False) is None
    assert plugin._to_int("abc") is None
    assert plugin._to_int([]) is None


def test_payload_carries_fixed_upstream_fields():
    """上游要求 appId/globalId/marketType 三个固定字段, 缺失会被拒。"""
    body = plugin._payload(pageNo=1)
    assert body["appId"] == plugin.HOT_RANK_APP_ID
    assert body["globalId"] == GLOBAL_ID
    assert body["marketType"] == plugin.MARKET_TYPE_HK
    assert body["pageNo"] == 1


# ---------------------------------------------------------------------------
# 插件层: 榜单
# ---------------------------------------------------------------------------

def test_list_payload_pins_page_size_and_page_no():
    """🔴 闸 1: 发出去的 payload 必须是 ``pageSize=100`` / ``pageNo=1``。

    实测上游对 ``pageSize=200`` 或 ``pageNo=2`` **静默返回 ``data: []``** ——
    不报错、不提示。所以这两个值绝不能变成可配置参数, 否则调用方传个大值就会
    得到「港股此刻没有人气数据」的假结论。这条测试钉住实际发出的字节。
    """
    c = _FakeClient()
    plugin.fetch_hk_hot_rank_list(c)
    body = c.calls_for(PATH_LIST)[0]["body"]
    assert body["pageSize"] == 100
    assert body["pageNo"] == 1
    assert plugin.HOT_RANK_MAX_PAGE_SIZE == 100


def test_list_function_exposes_no_pagination_parameters():
    """🔴 闸 1 的第二道: 签名里**不允许**出现 page_size/page_no。

    上一条钉住"当前发的是 100", 这一条钉住"没法改成别的" —— 有人加参数时
    这条会失败, 逼他读模块 docstring 里那条静默空的实测结论。
    """
    params = inspect.signature(plugin.fetch_hk_hot_rank_list).parameters
    assert set(params) == {"client"}


def test_list_maps_fields_and_sorts_ascending():
    c = _FakeClient({PATH_LIST: list(reversed(LIST_ROWS))})
    got = plugin.fetch_hk_hot_rank_list(c)
    assert [e.rank for e in got] == [1, 2, 3, 11, 100]
    top = got[0]
    assert top.symbol == "00700.HK"  # 内部 symbol, 不是上游的 "HK|00700"
    assert top.code == "00700"
    assert top.rank_change == 0
    assert top.his_rank_change == 0


def test_list_preserves_both_rank_change_windows():
    """``rc`` (短周期) 与 ``hisRc`` (长周期) 是两个不同口径, 都必须保留。

    实测前 100 名里分别有 14 / 90 个非零值 —— 只留一个会丢掉大部分信息。
    负值也要原样透传 (名次下降)。
    """
    c = _FakeClient()
    got = {e.symbol: e for e in plugin.fetch_hk_hot_rank_list(c)}
    assert got["09992.HK"].rank_change == 1
    assert got["09992.HK"].his_rank_change == 0
    assert got["02525.HK"].rank_change == 1
    assert got["02525.HK"].his_rank_change == -11


def test_list_skips_unparsable_rows_but_keeps_the_rest():
    """坏行只丢自己 (名次不受影响), 整批失败才抛错。"""
    rows = [
        *LIST_ROWS,
        {"sc": "US|AAPL", "rk": 4, "rc": 0, "hisRc": 0},      # 非港股前缀
        {"sc": "HK|ABCDE", "rk": 5, "rc": 0, "hisRc": 0},     # 代码不是数字
        {"sc": "HK|00700", "rk": None, "rc": 0, "hisRc": 0},  # 缺名次
        {"sc": "00700", "rk": 6, "rc": 0, "hisRc": 0},        # 缺市场分隔符
        "not-a-dict",
    ]
    got = plugin.fetch_hk_hot_rank_list(_FakeClient({PATH_LIST: rows}))
    assert [e.rank for e in got] == [1, 2, 3, 11, 100]


def test_list_zero_pads_short_upstream_codes():
    """上游给 ``HK|700`` 时补零成 ``00700.HK`` (港股代码习惯上就是 5 位)。

    🔴 这与 :func:`plugin.hot_rank_code` 的**严格** 5 位校验是刻意的不对称:

    - **入向宽松** (上游 → 内部): 上游是"它指哪只股票"的权威, ``700`` 与
      ``00700`` 在港股是同一只, 补零由 :func:`app.plugins.eastmoney.hk.hk_symbol`
      统一负责。
    - **出向严格** (内部 → 上游): 我们必须发出规范的 5 位码。发 ``HK|700``
      上游可能不报错却解析到**另一只**标的 —— 静默取错数据比取不到危险得多。

    实测上游榜单恒为 5 位码, 本条钉住的是"万一不是"时的行为仍然正确。
    """
    got = plugin.fetch_hk_hot_rank_list(
        _FakeClient({PATH_LIST: [{"sc": "HK|700", "rk": 1, "rc": 0, "hisRc": 0}]})
    )
    assert got[0].symbol == "00700.HK"
    assert plugin.hot_rank_code("700.HK") is None  # 出向仍然严格


def test_list_raises_when_envelope_code_nonzero():
    class _Bad(_FakeClient):
        def _post_json(self, base, path, json_body):
            return {"code": 1, "message": "参数错误", "status": 1, "data": None}

    with pytest.raises(EastmoneyError, match="code=1"):
        plugin.fetch_hk_hot_rank_list(_Bad())


def test_list_raises_when_data_is_not_a_list():
    with pytest.raises(EastmoneyError, match="不是列表"):
        plugin.fetch_hk_hot_rank_list(_FakeClient({PATH_LIST: {"oops": 1}}))


def test_empty_list_is_a_legal_answer_not_an_error():
    """上游真返回空榜单时是 ``[]`` 而不是抛错 —— 「此刻没有榜单」是合法答案。"""
    assert plugin.fetch_hk_hot_rank_list(_FakeClient({PATH_LIST: []})) == []


# ---------------------------------------------------------------------------
# 插件层: 单标的最新排名
# ---------------------------------------------------------------------------

def test_latest_maps_every_upstream_field():
    got = plugin.fetch_hk_hot_rank_latest(_FakeClient(), "00700.HK")
    assert got == plugin.HkHotRankLatest(
        symbol="00700.HK",
        rank=1,
        rank_change=0,
        his_rank_change=0,
        his_rank_change_rank=1397,
        market_all_count=2811,
        calc_time="2026-11-22 12:50:00",
        flag=0,
    )


def test_latest_sends_market_prefixed_security_code():
    c = _FakeClient()
    plugin.fetch_hk_hot_rank_latest(c, "00700.HK")
    assert c.calls_for(PATH_LATEST)[0]["body"]["srcSecurityCode"] == "HK|00700"


def test_latest_returns_none_for_unlisted_symbol():
    """🔴 闸 2: 榜外代码上游给 **200 + code=0 + 全 null**, 必须判 ``rank is None``。

    实测 ``HK|99999`` 就是这个响应。若用信封 ``code`` 或 HTTP 状态码判空,
    会把「未收录」当成「查到了但排名是 null」继续往下走。
    """
    got = plugin.fetch_hk_hot_rank_latest(
        _FakeClient({PATH_LATEST: LATEST_UNLISTED}), "99999.HK"
    )
    assert got is None


def test_latest_returns_none_for_illegal_symbol_without_calling_upstream():
    """🔴 闸 6: 非法 symbol **不发请求**。发了也是白问, 且 ``HK|700`` 可能被
    上游解析到另一只标的。"""
    c = _FakeClient()
    assert plugin.fetch_hk_hot_rank_latest(c, "700.HK") is None
    assert c.calls == []


def test_latest_returns_none_when_data_missing():
    assert plugin.fetch_hk_hot_rank_latest(_FakeClient({PATH_LATEST: None}), "00700.HK") is None


# ---------------------------------------------------------------------------
# 插件层: 分母 (market_all_count)
# ---------------------------------------------------------------------------

def test_market_size_asks_with_top_ranked_symbol():
    """🔴 分母只在单标的端点里, 故用**榜首**代码换一次 (全市场级字段, 问谁都一样)。"""
    c = _FakeClient()
    entries = plugin.fetch_hk_hot_rank_list(c)
    size, calc = plugin.fetch_hk_hot_rank_market_size(c, entries)
    assert size == 2811
    assert calc == "2026-11-22 12:50:00"
    assert c.calls_for(PATH_LATEST)[0]["body"]["srcSecurityCode"] == "HK|00700"


def test_market_size_picks_min_rank_not_first_row():
    """榜首是 ``rank`` 最小的那只, 不是列表第一行 (上游顺序不保证)。"""
    entries = [
        plugin.HkHotRankEntry(rank=7, symbol="01810.HK", code="01810", rank_change=None, his_rank_change=None),
        plugin.HkHotRankEntry(rank=2, symbol="09988.HK", code="09988", rank_change=None, his_rank_change=None),
    ]
    c = _FakeClient()
    plugin.fetch_hk_hot_rank_market_size(c, entries)
    assert c.calls_for(PATH_LATEST)[0]["body"]["srcSecurityCode"] == "HK|09988"


def test_market_size_swallows_upstream_error():
    """分母是锦上添花的上下文, 不该因为它失败就让整个榜单 502。"""
    c = _FakeClient({PATH_LIST: LIST_ROWS, PATH_LATEST: EastmoneyError("限流")})
    entries = plugin.fetch_hk_hot_rank_list(c)
    assert plugin.fetch_hk_hot_rank_market_size(c, entries) == (None, None)


def test_market_size_on_empty_list_makes_no_request():
    c = _FakeClient({PATH_LIST: []})
    assert plugin.fetch_hk_hot_rank_market_size(c, []) == (None, None)
    assert c.calls_for(PATH_LATEST) == []


# ---------------------------------------------------------------------------
# 插件层: 时间序列
# ---------------------------------------------------------------------------

def test_history_and_intraday_keep_raw_calc_time_formats():
    """🔴 闸 5: 日频是 ``2026-07-26``, 盘中是 ``2026-11-22 00:00:00``,
    两种格式**原样透传不解析**。统一成 datetime 会给日频补一个假的 ``00:00:00``,
    而这个假时刻会被前端 tooltip 如实显示 (§13 不伪造)。"""
    c = _FakeClient()
    hist = plugin.fetch_hk_hot_rank_history(c, "00700.HK")
    intra = plugin.fetch_hk_hot_rank_intraday(c, "00700.HK")
    assert hist[0].calc_time == "2026-07-26"
    assert intra[0].calc_time == "2026-11-22 00:00:00"
    assert c.calls_for(PATH_HIS)[0]["body"]["srcSecurityCode"] == "HK|00700"
    assert c.calls_for(PATH_CURR)[0]["body"]["srcSecurityCode"] == "HK|00700"


def test_series_skip_rows_without_usable_calc_time():
    rows = [
        *HISTORY_ROWS,
        {"calcTime": None, "rank": 9},
        {"calcTime": "  ", "rank": 9},
        {"rank": 9},
    ]
    got = plugin.fetch_hk_hot_rank_history(_FakeClient({PATH_HIS: rows}), "00700.HK")
    assert len(got) == 3


def test_series_return_empty_for_illegal_symbol_without_calling():
    c = _FakeClient()
    assert plugin.fetch_hk_hot_rank_history(c, "700.HK") == []
    assert plugin.fetch_hk_hot_rank_intraday(c, "700.HK") == []
    assert c.calls == []


def test_series_return_empty_when_data_not_a_list():
    assert plugin.fetch_hk_hot_rank_history(_FakeClient({PATH_HIS: {"x": 1}}), "00700.HK") == []


# ---------------------------------------------------------------------------
# 服务层: 榜单
# ---------------------------------------------------------------------------

def test_service_items_have_pinned_column_order(tmp_path):
    """列顺序由 ``HK_HOT_RANK_COLS`` 唯一决定 (API 直接 ``to_dicts()``)。"""
    _seed_dim(tmp_path, [{"symbol": "00700.HK", "name": "腾讯控股"}])
    got = svc.hk_hot_rank(data_dir=tmp_path, client=_FakeClient())
    assert got.items.columns == list(svc.HK_HOT_RANK_COLS)
    assert got.count == 5
    assert got.source == "eastmoney_guba_hot_rank"


def test_service_joins_names_from_local_dimension_table(tmp_path):
    """名称来自**本地维表** (本应用里港股名称的唯一权威来源), 不由上游给。"""
    _seed_dim(tmp_path, [
        {"symbol": "00700.HK", "name": "腾讯控股"},
        {"symbol": "09988.HK", "name": "阿里巴巴-W"},
    ])
    got = svc.hk_hot_rank(data_dir=tmp_path, client=_FakeClient())
    names = dict(zip(got.items["symbol"].to_list(), got.items["name"].to_list(), strict=True))
    assert names["00700.HK"] == "腾讯控股"
    assert names["09988.HK"] == "阿里巴巴-W"
    assert got.names_synced is True


def test_service_tolerates_names_missing_from_dimension_table(tmp_path):
    """维表在但部分标的查不到 (新上市/维表过期): 名称为 null, 榜单照常有效。"""
    _seed_dim(tmp_path, [{"symbol": "00700.HK", "name": "腾讯控股"}])
    got = svc.hk_hot_rank(data_dir=tmp_path, client=_FakeClient())
    names = dict(zip(got.items["symbol"].to_list(), got.items["name"].to_list(), strict=True))
    assert names["00700.HK"] == "腾讯控股"
    assert names["01810.HK"] is None
    assert got.count == 5  # 名称缺失不影响名次这个核心事实
    assert got.names_synced is True


def test_service_reports_names_synced_false_when_dimension_table_absent(tmp_path):
    """🔴 维表未同步时 ``names_synced=false``, 让前端能说「名称暂缺」而不是
    渲染成一片空白让人以为没数据。"""
    got = svc.hk_hot_rank(data_dir=tmp_path, client=_FakeClient())
    assert got.names_synced is False
    assert got.count == 5
    assert got.items["name"].null_count() == 5


def test_service_rank_percentile_uses_market_all_count_as_denominator(tmp_path):
    """🔴 闸 3: 分母是 ``market_all_count`` (2811), **不是**榜单条数 (100)。

    拿 100 当分母会把第 1 名算成「前 1%」、第 100 名算成「前 100%」,
    数字看着完全正常, 没人会发现分母错了。
    """
    got = svc.hk_hot_rank(data_dir=tmp_path, client=_FakeClient())
    assert got.market_all_count == 2811
    pct = dict(zip(got.items["rank"].to_list(), got.items["rank_percentile"].to_list(), strict=True))
    assert pct[1] == pytest.approx(1 / 2811 * 100, abs=1e-4)
    assert pct[100] == pytest.approx(100 / 2811 * 100, abs=1e-4)
    # 分母是 2811 而不是 100 的直接证据: 第 100 名远小于 100%
    assert pct[100] < 4.0


def test_service_percentile_is_null_when_denominator_unavailable(tmp_path):
    """🔴 闸 3 的 fail-closed 侧: 分母拿不到 → 所有分位一律 null, 不拿 100 凑。"""
    c = _FakeClient({PATH_LIST: LIST_ROWS, PATH_LATEST: EastmoneyError("限流")})
    got = svc.hk_hot_rank(data_dir=tmp_path, client=c)
    assert got.market_all_count is None
    assert got.calc_time is None
    assert got.items["rank_percentile"].null_count() == got.count
    assert got.count == 5  # 榜单本身照常返回


def test_service_empty_list_yields_empty_frame_with_full_schema(tmp_path):
    got = svc.hk_hot_rank(data_dir=tmp_path, client=_FakeClient({PATH_LIST: []}))
    assert got.count == 0
    assert got.items.height == 0
    assert got.items.columns == list(svc.HK_HOT_RANK_COLS)


def test_service_propagates_upstream_error_instead_of_degrading(tmp_path):
    """🔴 闸 4: 上游失败必须抛, **不得**降级成空表 —— 那会把「拉不到」伪装成
    「港股此刻没有人气数据」, 而两者是完全不同的两件事 (§4)。"""
    c = _FakeClient({PATH_LIST: EastmoneyError("重试 3 次仍失败")})
    with pytest.raises(EastmoneyError):
        svc.hk_hot_rank(data_dir=tmp_path, client=c)


def test_service_closes_client_it_owns(tmp_path, monkeypatch):
    """``client=None`` 时自建并负责关闭 (不泄漏连接)。"""
    made: list = []

    def _fake_ctor(*a, **kw):
        c = _FakeClient()
        made.append(c)
        return c

    monkeypatch.setattr(svc, "EastmoneyClient", _fake_ctor)
    svc.hk_hot_rank(data_dir=tmp_path)
    assert made and made[0].closed is True


def test_service_does_not_close_borrowed_client(tmp_path):
    c = _FakeClient()
    svc.hk_hot_rank(data_dir=tmp_path, client=c)
    assert c.closed is False


# ---------------------------------------------------------------------------
# 服务层: 单标的详情
# ---------------------------------------------------------------------------

def test_detail_maps_found_symbol(tmp_path):
    _seed_dim(tmp_path, [{"symbol": "00700.HK", "name": "腾讯控股"}])
    got = svc.hk_hot_rank_detail(symbol="00700.HK", data_dir=tmp_path, client=_FakeClient())
    assert got.found is True
    assert got.symbol == "00700.HK"
    assert got.name == "腾讯控股"
    assert got.rank == 1
    assert got.market_all_count == 2811
    assert got.his_rank_change_rank == 1397
    assert got.calc_time == "2026-11-22 12:50:00"
    assert got.rank_percentile == pytest.approx(1 / 2811 * 100, abs=1e-4)


def test_detail_series_are_sorted_ascending(tmp_path):
    """升序是为了前端图表不用再反转 (与 ``hk_connect_flow`` 同一约定)。"""
    c = _FakeClient({
        PATH_LATEST: LATEST_00700,
        PATH_HIS: list(reversed(HISTORY_ROWS)),
        PATH_CURR: list(reversed(INTRADAY_ROWS)),
    })
    got = svc.hk_hot_rank_detail(symbol="00700.HK", data_dir=tmp_path, client=c)
    assert got.history["calc_time"].to_list() == ["2026-07-26", "2026-07-27", "2026-11-22"]
    assert got.intraday["calc_time"].to_list() == [
        "2026-11-22 00:00:00", "2026-11-22 00:10:00", "2026-11-22 13:00:00",
    ]
    assert list(svc.HK_HOT_RANK_SERIES_COLS) == ["calc_time", "rank"]


def test_detail_unlisted_symbol_is_found_false_not_an_error(tmp_path):
    """🔴 闸 2: 榜外标的是**合法答案**, 返回 found=False + 空序列, 不抛错。"""
    c = _FakeClient({PATH_LATEST: LATEST_UNLISTED, PATH_HIS: [], PATH_CURR: []})
    got = svc.hk_hot_rank_detail(symbol="99999.HK", data_dir=tmp_path, client=c)
    assert got.found is False
    assert got.rank is None
    assert got.rank_percentile is None
    assert got.market_all_count is None
    assert got.history.height == 0
    assert got.history.columns == list(svc.HK_HOT_RANK_SERIES_COLS)


def test_detail_illegal_symbol_makes_no_upstream_request(tmp_path):
    """🔴 闸 6: 非法 symbol 不发请求就返回未收录。"""
    c = _FakeClient()
    got = svc.hk_hot_rank_detail(symbol="700.HK", data_dir=tmp_path, client=c)
    assert got.found is False
    assert c.calls == []


def test_detail_without_intraday_skips_that_request(tmp_path):
    """``with_intraday=False`` 省一次上游请求 (日频历史仍照常拉)。"""
    c = _FakeClient()
    got = svc.hk_hot_rank_detail(
        symbol="00700.HK", data_dir=tmp_path, client=c, with_intraday=False
    )
    assert c.calls_for(PATH_CURR) == []
    assert got.intraday.height == 0
    assert got.history.height == 3


def test_detail_percentile_null_when_denominator_null(tmp_path):
    c = _FakeClient({PATH_LATEST: {**LATEST_00700, "marketAllCount": None},
                     PATH_HIS: [], PATH_CURR: []})
    got = svc.hk_hot_rank_detail(symbol="00700.HK", data_dir=tmp_path, client=c)
    assert got.found is True
    assert got.rank == 1
    assert got.rank_percentile is None  # 不拿默认值凑


def test_detail_propagates_upstream_error(tmp_path):
    c = _FakeClient({PATH_LATEST: EastmoneyError("熔断")})
    with pytest.raises(EastmoneyError):
        svc.hk_hot_rank_detail(symbol="00700.HK", data_dir=tmp_path, client=c)


def test_percentile_helper_rejects_bad_denominators():
    """分母为 0 / None / 负数时一律 None (除零与"拿 0 当分母"都是脏数据)。"""
    assert svc._percentile(1, 2811) == pytest.approx(0.0356, abs=1e-4)
    assert svc._percentile(1, 0) is None
    assert svc._percentile(1, None) is None
    assert svc._percentile(1, -5) is None
    assert svc._percentile(None, 2811) is None


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------

def test_api_hot_rank_success_shape(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(svc, "EastmoneyClient", lambda *a, **kw: _FakeClient())
    r = client.get("/api/hk/hot-rank")
    assert r.status_code == 200
    j = r.json()
    assert j["total"] == 5
    assert j["market"] == "HK"
    assert j["market_all_count"] == 2811
    assert j["calc_time"] == "2026-11-22 12:50:00"
    assert j["names_synced"] is True
    assert j["source"] == "eastmoney_guba_hot_rank"
    assert j["max_count"] == 100
    assert j["items"][0] == {
        "rank": 1, "symbol": "00700.HK", "code": "00700", "name": "腾讯控股",
        "rank_change": 0, "his_rank_change": 0,
        "rank_percentile": pytest.approx(0.0356, abs=1e-4),
    }


def test_api_hot_rank_does_not_expose_pagination():
    """🔴 闸 1 的 API 侧: 端点**不接受**分页参数 (传了也不该改变行为)。

    上游对 pageNo=2 静默返回空, 所以这个端点必须永远是"前 100 名"。
    纯签名断言, 不需要 app 夹具。
    """
    params = inspect.signature(hk_api.get_hk_hot_rank).parameters
    assert set(params) == {"request"}


def test_api_hot_rank_502_on_upstream_failure(env, monkeypatch):
    """🔴 闸 4: 上游失败 → 502, **不是** 200 + 空列表。"""
    client, _, _ = env

    def _boom(**kw):
        raise EastmoneyError("重试 3 次仍失败: 空响应体")

    monkeypatch.setattr(svc, "hk_hot_rank", _boom)
    r = client.get("/api/hk/hot-rank")
    assert r.status_code == 502
    assert "人气榜数据源不可用" in r.json()["detail"]


def test_api_hot_rank_reports_unsynced_dimension_table(env, monkeypatch, tmp_path_factory):
    """维表未同步 → 200 + ``names_synced=false`` + 名称全 null (榜单仍有效)。"""
    client, repo, _ = env
    empty_dir = tmp_path_factory.mktemp("no_dim")
    monkeypatch.setattr(svc, "EastmoneyClient", lambda *a, **kw: _FakeClient())
    monkeypatch.setattr(repo.store, "data_dir", empty_dir)
    j = client.get("/api/hk/hot-rank").json()
    assert j["names_synced"] is False
    assert j["total"] == 5
    assert all(it["name"] is None for it in j["items"])


def test_api_detail_success_shape(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(svc, "EastmoneyClient", lambda *a, **kw: _FakeClient())
    r = client.get("/api/hk/hot-rank/detail", params={"symbol": "00700.HK"})
    assert r.status_code == 200
    j = r.json()
    assert j["found"] is True
    assert j["symbol"] == "00700.HK"
    assert j["name"] == "腾讯控股"
    assert j["market"] == "HK"
    assert j["rank"] == 1
    assert j["market_all_count"] == 2811
    assert j["his_rank_change_rank"] == 1397
    assert j["history_days"] == 3
    assert j["intraday_points"] == 3
    assert j["history"][-1] == {"calc_time": "2026-11-22", "rank": 1}
    assert j["intraday"][-1] == {"calc_time": "2026-11-22 13:00:00", "rank": 1}


def test_api_detail_unlisted_returns_200_with_found_false(env, monkeypatch):
    """榜外标的是合法答案 → 200 + ``found=false``, 不是 404 也不是 502。"""
    client, _, _ = env
    monkeypatch.setattr(
        svc, "EastmoneyClient",
        lambda *a, **kw: _FakeClient({PATH_LATEST: LATEST_UNLISTED, PATH_HIS: [], PATH_CURR: []}),
    )
    r = client.get("/api/hk/hot-rank/detail", params={"symbol": "99999.HK"})
    assert r.status_code == 200
    j = r.json()
    assert j["found"] is False
    assert j["rank"] is None
    assert j["history"] == []
    assert j["intraday"] == []


def test_api_detail_400_on_illegal_symbol(env):
    client, _, _ = env
    for bad in ("700.HK", "000001.SZ", "00700", "TENCENT"):
        r = client.get("/api/hk/hot-rank/detail", params={"symbol": bad})
        assert r.status_code == 400, bad
        assert "非港股代码格式" in r.json()["detail"]


def test_api_detail_502_on_upstream_failure(env, monkeypatch):
    client, _, _ = env

    def _boom(**kw):
        raise EastmoneyError("熔断打开")

    monkeypatch.setattr(svc, "hk_hot_rank_detail", _boom)
    r = client.get("/api/hk/hot-rank/detail", params={"symbol": "00700.HK"})
    assert r.status_code == 502
    assert "人气榜数据源不可用" in r.json()["detail"]


def test_api_detail_with_intraday_flag_is_forwarded(env, monkeypatch):
    client, _, _ = env
    seen: list = []

    def _spy(**kw):
        seen.append(kw.get("with_intraday"))
        return svc.HkHotRankDetailResult(
            symbol=kw["symbol"], name=None, found=False, rank=None, rank_change=None,
            his_rank_change=None, his_rank_change_rank=None, market_all_count=None,
            calc_time=None, rank_percentile=None, history=pl.DataFrame(
                schema={"calc_time": pl.String, "rank": pl.Int64}),
            intraday=pl.DataFrame(schema={"calc_time": pl.String, "rank": pl.Int64}),
            source="eastmoney_guba_hot_rank",
        )

    monkeypatch.setattr(svc, "hk_hot_rank_detail", _spy)
    client.get("/api/hk/hot-rank/detail", params={"symbol": "00700.HK"})
    client.get("/api/hk/hot-rank/detail", params={"symbol": "00700.HK", "with_intraday": "false"})
    assert seen == [True, False]


def test_api_hot_rank_endpoints_do_not_return_price_fields(env, monkeypatch):
    """🔴 口径闸: 人气榜**不得**返回最新价/涨跌幅/``quote_delay_minutes``。

    价格属于行情维度, 已由看盘/自选/详情走 ``quote_service`` 提供; 这里再给一份
    就会让同一只股票在本应用里有两个价格口径 (§4)。``quote_delay_minutes`` 是
    行情延时标注, 挂在非行情端点上会暗示一个不存在的口径 (§13)。
    """
    client, _, _ = env
    monkeypatch.setattr(svc, "EastmoneyClient", lambda *a, **kw: _FakeClient())
    forbidden = {"last_price", "change_pct", "price", "quote_delay_minutes"}

    list_json = client.get("/api/hk/hot-rank").json()
    assert not (set(list_json) & forbidden)
    assert not (set(list_json["items"][0]) & forbidden)

    detail_json = client.get(
        "/api/hk/hot-rank/detail", params={"symbol": "00700.HK"}
    ).json()
    assert not (set(detail_json) & forbidden)
