"""港股标的维表同步 (instruments_hk) 的落库、防掏空与隔离行为。

网络一律用假 client / monkeypatch 挡掉: 东财 push2 对短时突发请求会触发 **IP 级
临时封禁** (实测, 见 ``app/plugins/eastmoney/client.py`` 模块注释), 测试触网会把
开发机封掉, 且结果不可复现。
"""

from __future__ import annotations

import polars as pl
import pytest

from app.plugins.eastmoney import hk
from app.plugins.eastmoney.client import EastmoneyError
from app.services import instrument_sync_hk as sync_hk
from app.services.fs_utils import atomic_write_parquet


def _row(code: str, name: str, price: float, *, shares: float = 1e9,
         listed: int = 20040616, industry: str = "互动媒体及服务") -> dict:
    """最小可用的 clist 行 (字段语义见 hk.py 模块 docstring 的实测字典)。"""
    return {
        "f2": price, "f3": 1.0, "f4": 0.1, "f5": 100000, "f6": price * 100000,
        "f7": 2.0, "f8": 0.5, "f12": code, "f13": 128, "f14": name,
        "f15": price * 1.02, "f16": price * 0.98, "f17": price, "f18": price - 0.1,
        "f20": price * shares, "f21": price * shares, "f26": listed,
        "f38": shares, "f100": industry, "f124": 1794556669,
    }


ROWS = [
    _row("00700", "腾讯控股", 389.0, shares=9.19e9),
    _row("09988", "阿里巴巴-W", 120.0, shares=1.99e10, listed=20191126, industry="专业零售"),
    _row("89988", "阿里巴巴-WR", 76.8, shares=1.99e10, listed=20230619, industry="专业零售"),
]


@pytest.fixture
def patched_fetch(monkeypatch):
    """把 fetch 换成可控桩, 返回一个 setter 让各测试决定返回什么/抛什么。"""
    state: dict = {"rows": None, "exc": None}

    def _fake(*, fs=hk.FS_HK_EQUITY, client=None):
        if state["exc"] is not None:
            raise state["exc"]
        return state["rows"]

    monkeypatch.setattr(sync_hk, "fetch_hk_instrument_rows", _fake)
    return state


def _read(data_dir):
    return pl.read_parquet(data_dir / sync_hk.HK_INSTRUMENT_RELPATH)


# ---------------------------------------------------------------------------
# 正常落库
# ---------------------------------------------------------------------------

def test_sync_writes_to_instruments_hk_not_a_share_dir(tmp_path, patched_fetch):
    """闸3: 港股维表落在 instruments_hk/, 绝不碰 A 股的 instruments/。"""
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)
    n = sync_hk.sync_instruments_hk(tmp_path)

    assert n == 3
    assert (tmp_path / sync_hk.HK_INSTRUMENT_RELPATH).exists()
    # A 股目录必须完全没被创建/写入 (DataStore 才会 mkdir, 本函数不该顺手建)
    assert not (tmp_path / "instruments" / "instruments.parquet").exists()


def test_synced_schema_matches_hk_instrument_columns_plus_as_of(tmp_path, patched_fetch):
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)
    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)

    assert set(df.columns) == set(hk.HK_INSTRUMENT_COLUMNS) | {"as_of"}
    assert df["as_of"].null_count() == 0


def test_dual_counter_survives_round_trip(tmp_path, patched_fetch):
    """两个柜台都落库, 靠 is_cny_counter 区分; 统计口径只算港币柜台。"""
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)
    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)

    cny = df.filter(pl.col("is_cny_counter"))
    hkd = df.filter(~pl.col("is_cny_counter"))
    assert cny["symbol"].to_list() == ["89988.HK"]
    assert sorted(hkd["symbol"].to_list()) == ["00700.HK", "09988.HK"]
    # 人民币柜台指回其港币柜台, 供读端做去重/联动
    assert cny["hkd_counter"].to_list() == ["09988.HK"]


def test_price_limit_and_lot_size_columns_stay_null(tmp_path, patched_fetch):
    """港股无涨跌停 → limit_up/down 恒 None; 东财不给每手股数 → lot_size 恒 None。

    保留这三列只为与 A 股 schema 对齐, 让共用读代码不必分叉。**不得**填 0:
    存量守卫把「limit_up 为 0/null」当无限价处理, 但 0 会被读成「限价 0 元」。
    """
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)
    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)

    for col in ("limit_up", "limit_down", "lot_size"):
        assert df[col].null_count() == df.height, f"{col} 应为全 None"


def test_float_shares_equals_total_shares_for_hk(tmp_path, patched_fetch):
    """港股全流通 (实测 f38 == f39) → 换手率分母用总股本, 不能留 None。

    留 None 会让 enriched 的 turnover_rate 整列为空, 依赖换手率的策略筛不出任何港股。
    """
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)
    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)

    assert df["float_shares"].null_count() == 0
    assert df["total_shares"].null_count() == 0
    assert (df["float_shares"] == df["total_shares"]).all()


# ---------------------------------------------------------------------------
# 防掏空: 拉取失败绝不覆盖历史维表
# ---------------------------------------------------------------------------

def _seed_history(data_dir, symbols: list[str]) -> None:
    df = pl.DataFrame({
        "symbol": symbols,
        "name": [f"N{i}" for i in range(len(symbols))],
        "industry": ["地产建筑"] * len(symbols),
    })
    out = data_dir / sync_hk.HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)


def test_fetch_error_keeps_history_untouched(tmp_path, patched_fetch):
    """东财报错 → 返回 0 且**不写盘**。写空表会让搜索/标的池全瞎。"""
    _seed_history(tmp_path, ["00700.HK", "09988.HK"])
    before = _read(tmp_path)
    patched_fetch["exc"] = EastmoneyError("push2 被封禁")

    assert sync_hk.sync_instruments_hk(tmp_path) == 0
    assert _read(tmp_path).equals(before), "历史维表被覆盖"


def test_empty_result_keeps_history_untouched(tmp_path, patched_fetch):
    """0 行与「真的没有港股」无法区分 → 一律当异常处理, 不覆盖。"""
    _seed_history(tmp_path, ["00700.HK"])
    patched_fetch["rows"] = []

    assert sync_hk.sync_instruments_hk(tmp_path) == 0
    assert _read(tmp_path)["symbol"].to_list() == ["00700.HK"]


def test_incomplete_fetch_carries_over_missing_symbols(tmp_path, patched_fetch, caplog):
    """本次只拉到历史的 1/3 (< 50%) → 判定拉取不完整, 补回缺失 symbol。

    港股一年退市约 1%, 一夜少一半只可能是分页/封禁导致的残缺结果。
    """
    _seed_history(tmp_path, ["00700.HK", "09988.HK", "89988.HK", "00005.HK",
                             "01398.HK", "03690.HK"])
    patched_fetch["rows"] = hk.hk_instrument_rows([ROWS[0]])   # 只有腾讯

    with caplog.at_level("WARNING"):
        n = sync_hk.sync_instruments_hk(tmp_path)

    assert n == 6, "应补回 5 行历史"
    assert "拉取不完整" in caplog.text
    got = set(_read(tmp_path)["symbol"].to_list())
    assert got == {"00700.HK", "09988.HK", "89988.HK", "00005.HK", "01398.HK", "03690.HK"}


def test_complete_fetch_drops_delisted_symbols(tmp_path, patched_fetch):
    """拉取完整 (>= 50%) 时**不**补历史 —— 否则退市标的永远留在维表里。"""
    _seed_history(tmp_path, ["00700.HK", "09988.HK", "89988.HK", "99999.HK"])
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)   # 3 行 / 历史 4 行 = 75%

    n = sync_hk.sync_instruments_hk(tmp_path)
    assert n == 3
    assert "99999.HK" not in _read(tmp_path)["symbol"].to_list()


# ---------------------------------------------------------------------------
# 字段补全 (复用 A 股的逐行 coalesce)
# ---------------------------------------------------------------------------

def test_null_industry_is_filled_from_history(tmp_path, patched_fetch):
    """东财 f100 偶发返回 '-' → industry 为 None, 必须从历史维表逐行补回。

    行业是概念/行业板块功能的主键, 被一次抖动掏空会让整个板块视图空掉。
    """
    out = tmp_path / sync_hk.HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({
        "symbol": ["00700.HK"],
        "name": ["腾讯控股"],
        "industry": ["互动媒体及服务"],
        "listing_date": [None],
        "total_shares": [None],
        "float_shares": [None],
        "tick_size": [None],
    }), out)

    rows = hk.hk_instrument_rows([_row("00700", "腾讯控股", 389.0, industry="-")])
    assert rows[0]["industry"] is None, "前置条件: '-' 应被清洗成 None"
    patched_fetch["rows"] = rows

    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)
    assert df["industry"].to_list() == ["互动媒体及服务"], "未从历史补回"


def test_preserve_fields_covers_hk_specific_columns():
    """港股补全字段集必须含 industry 与 lot_size, 且不含恒 None 的涨跌停两列。

    🔴 ``lot_size`` 在 PR2a 时**不**在保留集里 —— 那时东财 clist 不提供每手股数,
    该列恒为 None, 保留它没有意义。PR2b 起它由财务宽表的 ``PER_SHARES`` 回填
    (``financial_sync_hk.backfill_hk_lot_size``), 而 clist 重同步仍写 None:
    不在保留集里的话, **每次维表重同步都会抹掉回填结果**, 下单取整随即失效。

    ``limit_up`` / ``limit_down`` 仍恒为 None (港股无涨跌停, 见
    ``app.market.HK_RULES.has_price_limit=False``), 故继续排除。
    """
    assert "industry" in sync_hk._HK_PRESERVE_FIELDS
    assert "lot_size" in sync_hk._HK_PRESERVE_FIELDS
    for col in ("limit_up", "limit_down"):
        assert col not in sync_hk._HK_PRESERVE_FIELDS


def test_backfilled_lot_size_survives_instrument_resync(tmp_path, patched_fetch):
    """回填的每手股数必须扛过维表重同步 (clist 恒写 None)。

    这是 ``_HK_PRESERVE_FIELDS`` 含 ``lot_size`` 的行为级证明: 只断言常量在名单里
    挡不住「名单对了但 coalesce 路径没走到」的情况。
    """
    out = tmp_path / sync_hk.HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({
        "symbol": ["00700.HK"],
        "name": ["腾讯控股"],
        "industry": ["互动媒体及服务"],
        "lot_size": [100],
    }), out)

    rows = hk.hk_instrument_rows([_row("00700", "腾讯控股", 389.0)])
    assert rows[0]["lot_size"] is None, "前置条件: clist 不提供每手股数"
    patched_fetch["rows"] = rows

    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)
    assert df["lot_size"].to_list() == [100], "回填值被重同步抹掉了"
    assert df["name"].to_list() == ["腾讯控股"], "非保留列应取新值"


def test_a_share_preserve_default_unchanged():
    """泛化 _preserve_instrument_fields 后, A 股默认补全字段集必须逐字不变。"""
    from app.services.instrument_sync import _SHARE_FIELDS

    assert _SHARE_FIELDS == ("listing_date", "total_shares", "float_shares", "tick_size")


# ---------------------------------------------------------------------------
# fetch 层: 口径与 client 生命周期
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.closed = False
        self.calls: list[dict] = []

    def _push2_data(self, base, path, params):
        self.calls.append(dict(params))
        pn = params["pn"]
        return self._pages[pn - 1] if pn <= len(self._pages) else None

    def close(self):
        self.closed = True


def test_fetch_defaults_to_equity_universe():
    """默认口径 = 主板 + GEM, 不含 REITs/债券/窝轮牛熊证。"""
    client = _FakeClient([{"total": 1, "diff": [ROWS[0]]}])
    rows = sync_hk.fetch_hk_instrument_rows(client=client)

    assert len(rows) == 1
    assert client.calls[0]["fs"] == hk.FS_HK_EQUITY
    assert "m:128+t:3" in hk.FS_HK_EQUITY and "m:128+t:4" in hk.FS_HK_EQUITY
    assert "t:5" not in hk.FS_HK_EQUITY, "窝轮牛熊证混入会污染全市场统计"
    assert "t:1" not in hk.FS_HK_EQUITY and "t:2" not in hk.FS_HK_EQUITY


def test_fetch_closes_only_self_created_client():
    """注入的 client 由调用方负责关闭; 自建的必须关 (否则连接泄漏)。"""
    injected = _FakeClient([{"total": 1, "diff": [ROWS[0]]}])
    sync_hk.fetch_hk_instrument_rows(client=injected)
    assert injected.closed is False, "不该关掉调用方传进来的 client"


def test_fetch_closes_self_created_client_on_error(monkeypatch):
    """抛错路径也要关 client (finally), 否则封禁重试期间连接堆积。"""
    created: list = []

    class _Boom(_FakeClient):
        def __init__(self):
            super().__init__([])
            created.append(self)

        def _push2_data(self, base, path, params):
            raise EastmoneyError("boom")

    monkeypatch.setattr(sync_hk, "EastmoneyClient", _Boom)
    with pytest.raises(EastmoneyError):
        sync_hk.fetch_hk_instrument_rows()
    assert created and created[0].closed is True
