"""港股标的维表同步 (instruments_hk) 的落库、防掏空与隔离行为。

网络一律用假 client / monkeypatch 挡掉: 东财 push2 对短时突发请求会触发 **IP 级
临时封禁** (实测, 见 ``app/plugins/eastmoney/client.py`` 模块注释), 测试触网会把
开发机封掉, 且结果不可复现。
"""

from __future__ import annotations

import polars as pl
import pytest

from app.plugins.eastmoney import hk, hk_profile
from app.plugins.eastmoney.client import EastmoneyError
from app.plugins.tencent.client import TencentError
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


def _profile(code: str, name: str, *, industry: str | None = "软件服务",
             trade_unit: int | None = 100, shares: int | None = 9_092_644_500,
             listed: str | None = "2004-06-16 00:00:00") -> dict:
    """最小可用的 F10 概况行 (``RPT_HKF10_INFO_ORGPROFILE``, 35 列里只填用到的)。

    PR6 换源后的**主**fixture; ``_row`` (clist) 保留是因为下面多数测试测的是
    服务层落库/防掏空行为, 用哪个映射器造行都一样 (两者输出同一套 16 列契约)。
    """
    return {
        "SECUCODE": f"{code}.HK",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name,
        "BELONG_INDUSTRY": industry,
        "INDUSTRY_TYPE": "通讯服务-媒体与娱乐-互动媒体与服务-互动媒体与服务",
        "TRADE_UNIT": trade_unit,
        "HK_SHARES": shares,
        "LISTING_DATE": listed,
        "BELONG_MARKET": "香港主板",
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

    # 🔴 签名原为 ``(*, fs=hk.FS_HK_EQUITY, client=None)``。PR6 换源后维表不再走
    # clist, ``fs`` (东财列表过滤器) 这个参数**已无语义** —— 留着一个不起作用的
    # 参数比删掉它更危险 (调用方以为能改口径)。
    def _fake(*, client=None, tencent_client=None):
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


def test_schema_inference_covers_all_rows_not_just_the_first_100(tmp_path, patched_fetch):
    """🔴 回归: polars 默认只按**前 100 行**推断 schema, 港股维表必炸。

    维表按代码升序, 前 100 行全是 ``00xxx`` 港币柜台 ⇒ ``hkd_counter`` 整段为
    ``None`` ⇒ 被推成 ``Null`` dtype; 翻到 ``8xxxx`` 人民币柜台时抛
    ``ComputeError: could not append value: "00016.HK" of type: str``。

    实测 2862 行时 100% 复现 (2026-11-21 真机), 但脱网单测行数少测不出来 ——
    所以这里刻意造 102 行, 让配对柜台落在第 100 行之后。
    """
    codes = [f"{i:05d}" for i in range(1, 101)] + ["09988", "89988"]
    # 🔴 89988 的名称必须以 R 结尾: 双柜台是三信号判定 (代码 8 开头 + 名称 R 结尾
    # + 配对港币代码在列表里), 少一个信号就判成非人民币柜台, hkd_counter 会
    # **合法地**整列为空, 这条回归也就测不到东西了。
    names = {c: f"名称{c}" for c in codes}
    names["09988"], names["89988"] = "阿里巴巴-W", "阿里巴巴-WR"
    rows = hk_profile.hk_profile_instrument_rows(
        [_profile(c, names[c]) for c in codes],
        live_prices={f"{c}.HK": 10.0 for c in codes},
    )
    assert len(rows) == 102
    assert all(r["hkd_counter"] is None for r in rows[:100]), "前 100 行必须全空才会触发"
    assert rows[-1]["hkd_counter"] == "09988.HK", "第 102 行才出现非空值"

    patched_fetch["rows"] = rows
    n = sync_hk.sync_instruments_hk(tmp_path)

    assert n == 102
    df = _read(tmp_path)
    assert df.schema["hkd_counter"] == pl.String, "不得退化成 Null dtype"
    got = df.filter(pl.col("symbol") == "89988.HK")
    assert got["hkd_counter"][0] == "09988.HK"
    # 港股无涨跌停 → 恒 None → 全行推断后仍是 Null dtype, 与 A 股
    # instruments.parquet 现状一致。钉住以免被误当成 schema 缺陷去「修」。
    assert df.schema["limit_up"] == pl.Null
    assert df.schema["limit_down"] == pl.Null


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


def test_price_limit_columns_stay_null(tmp_path, patched_fetch):
    """港股无涨跌停 → limit_up/down 恒 None。

    保留这两列只为与 A 股 schema 对齐, 让共用读代码不必分叉。**不得**填 0:
    存量守卫把「limit_up 为 0/null」当无限价处理, 但 0 会被读成「限价 0 元」。

    🔴 本测试原名 ``test_price_limit_and_lot_size_columns_stay_null``, 连
    ``lot_size`` 一起断言全 None, 理由是「东财不给每手股数」。PR6 换源后该断言
    **事实上已不成立** —— F10 概况的 ``TRADE_UNIT`` 就是每手股数。故拆出
    :func:`test_lot_size_comes_from_trade_unit` 反向钉住, 而不是把旧断言删掉了事。
    """
    patched_fetch["rows"] = hk.hk_instrument_rows(ROWS)
    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)

    for col in ("limit_up", "limit_down"):
        assert df[col].null_count() == df.height, f"{col} 应为全 None"


def test_lot_size_comes_from_trade_unit(tmp_path, patched_fetch):
    """⭐ 换源收益: 每手股数直接来自 F10 的 ``TRADE_UNIT``, 不再依赖财务宽表回填。

    取值已逐只核对港交所真实每手股数 (2026-11-21 实测):
    ``00005`` 汇丰 400 / ``00700`` 腾讯 100 / ``02318`` 平安 500 / ``03988`` 中行 1000。
    clist 那一路拿不到这个字段 (``f292`` 已实测排除), 是换源的直接动机之一。
    """
    patched_fetch["rows"] = hk_profile.hk_profile_instrument_rows(
        [_profile("00700", "腾讯控股", trade_unit=100),
         _profile("00005", "汇丰控股", trade_unit=400)],
        live_prices={"00700.HK": 344.0, "00005.HK": 129.2},
    )
    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)

    assert dict(zip(df["symbol"], df["lot_size"], strict=True)) == {
        "00700.HK": 100, "00005.HK": 400,
    }


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
    (``financial_sync_hk.backfill_hk_lot_size``); PR6 换源后改由 F10 的
    ``TRADE_UNIT`` 直接给出, 但**仍须留在保留集**: 一是实测在市 2862 行里有 1 行
    ``TRADE_UNIT`` 为空, 二是回填结果不该被一次维表重跑冲掉。

    ``limit_up`` / ``limit_down`` 仍恒为 None (港股无涨跌停, 见
    ``app.market.HK_RULES.has_price_limit=False``), 故继续排除。
    """
    assert "industry" in sync_hk._HK_PRESERVE_FIELDS
    assert "lot_size" in sync_hk._HK_PRESERVE_FIELDS
    for col in ("limit_up", "limit_down"):
        assert col not in sync_hk._HK_PRESERVE_FIELDS


def test_backfilled_lot_size_survives_instrument_resync(tmp_path, patched_fetch):
    """已有的每手股数必须扛过维表重同步 (本轮 F10 未给 ``TRADE_UNIT`` 时)。

    这是 ``_HK_PRESERVE_FIELDS`` 含 ``lot_size`` 的行为级证明: 只断言常量在名单里
    挡不住「名单对了但 coalesce 路径没走到」的情况。

    🔴 前置条件原为「clist 恒写 None」。PR6 换源后 ``lot_size`` 正常有值, 但
    实测在市 2862 行里仍有 1 行 ``TRADE_UNIT`` 为空 —— 保留集正是为这种缺值兜底,
    故改用「F10 缺 TRADE_UNIT」构造同一场景, 测试意图不变。
    """
    out = tmp_path / sync_hk.HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame({
        "symbol": ["00700.HK"],
        "name": ["腾讯控股"],
        "industry": ["互动媒体及服务"],
        "lot_size": [100],
    }), out)

    rows = hk_profile.hk_profile_instrument_rows(
        [_profile("00700", "腾讯控股", trade_unit=None)],
        live_prices={"00700.HK": 344.0},
    )
    assert rows[0]["lot_size"] is None, "前置条件: 本轮 F10 未给每手股数"
    patched_fetch["rows"] = rows

    sync_hk.sync_instruments_hk(tmp_path)
    df = _read(tmp_path)
    assert df["lot_size"].to_list() == [100], "历史值被重同步抹掉了"
    assert df["name"].to_list() == ["腾讯控股"], "非保留列应取新值"


def test_a_share_preserve_default_unchanged():
    """泛化 _preserve_instrument_fields 后, A 股默认补全字段集必须逐字不变。"""
    from app.services.instrument_sync import _SHARE_FIELDS

    assert _SHARE_FIELDS == ("listing_date", "total_shares", "float_shares", "tick_size")


# ---------------------------------------------------------------------------
# fetch 层: 口径与 client 生命周期
# ---------------------------------------------------------------------------

class _FakeEmClient:
    """假 EastmoneyClient: 换源后维表只用 ``datacenter_report_all`` 一个方法。"""

    def __init__(self, rows):
        self._rows = rows
        self.closed = False
        self.calls: list[tuple] = []

    def datacenter_report_all(self, report_name, **kwargs):
        self.calls.append((report_name, kwargs))
        return list(self._rows)

    def close(self):
        self.closed = True


class _FakeTxClient:
    """假 TencentClient: 只为生命周期断言存在, 快照本身被 ``patched_sources`` 挡掉。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _live(symbol: str, price: float) -> dict:
    """腾讯快照归一化后的最小记录 (只填维表用得到的两键)。"""
    return {"symbol": symbol, "last_price": price}


@pytest.fixture
def patched_sources(monkeypatch):
    """把两个数据源都换成桩, 返回可写的 state。

    🔴 桩打在**服务层看到的插件边界**上 (``sync_hk.hk_profile`` / ``sync_hk.hk_spot``),
    不打在 httpx 上: 本文件测的是「服务怎么把两个源拼成维表」, 腾讯字段下标的
    归一化由 ``tests/test_hk_tencent_spot.py`` 单独钉住, 两层各自可替换。
    """
    state: dict = {"profiles": [], "live": [], "gate_kwargs": None, "gate_symbols": []}

    def _fake_profiles(client):
        return list(state["profiles"])

    def _fake_gate(client, symbols, **kwargs):
        state["gate_kwargs"] = kwargs
        state["gate_symbols"] = list(symbols)
        return list(state["live"])

    monkeypatch.setattr(sync_hk.hk_profile, "fetch_hk_org_profiles", _fake_profiles)
    monkeypatch.setattr(sync_hk.hk_spot, "fetch_hk_spot", _fake_gate)
    return state


def test_fetch_universe_is_profile_intersect_tencent_live(patched_sources):
    """🔴 维表口径 = F10 概况 ∩ 腾讯在市。两个过滤各自挡掉什么, 必须分别钉住。

    F10 概况全量 6920 行**不是**在市股票清单 (报表无在市状态列): 含债务证券
    (``4xxxx`` / 字母开头)、待上市新股 (``H2xxx``)、REITs 与约 2200 个退市/长期
    停牌代码。实测 5122 个 5 位 0/8 开头代码经腾讯闸门后剩 **2862** 只, 与 clist
    ``m:128+t:3,t:4`` 记载的主板 2621 + GEM 310 = 2931 同量级。

    本测试替换掉原 ``test_fetch_defaults_to_equity_universe``: 那条断言的是
    ``fs == hk.FS_HK_EQUITY`` (clist 的 ``t:`` 子类过滤器), 换源后该参数已不存在,
    但「不含 REITs/债券/窝轮」的**意图**照旧, 改由代码前缀 + 在市闸门两道承担。
    """
    patched_sources["profiles"] = [
        _profile("00700", "腾讯控股"),
        _profile("09988", "阿里巴巴-W", industry="专业零售"),
        _profile("89988", "阿里巴巴-WR", industry="专业零售"),
        _profile("00011", "恒生银行", industry="银行"),        # 已退市 → 腾讯不返回
        _profile("00405", "越秀房产信托基金", industry=None),   # REIT → 腾讯归一化丢弃
        _profile("40001", "CCB N2611C", industry=None),        # 债务证券 → 首位非 0/8
        _profile("H2001", "Aqara", industry="软件服务"),        # 待上市新股 → 非 5 位数字
    ]
    patched_sources["live"] = [
        _live("00700.HK", 344.0), _live("09988.HK", 83.35), _live("89988.HK", 71.1),
    ]

    rows = sync_hk.fetch_hk_instrument_rows(
        client=_FakeEmClient([]), tencent_client=_FakeTxClient(),
    )

    assert sorted(r["symbol"] for r in rows) == ["00700.HK", "09988.HK", "89988.HK"]
    # 债券/待上市新股在**发给腾讯之前**就剔掉, 不白占 300 只/请求的批量配额
    assert "40001.HK" not in patched_sources["gate_symbols"]
    assert not any(not s[:5].isdigit() for s in patched_sources["gate_symbols"])
    # 双柜台三信号判定沿用 clist 那一路的口径 (换源不换语义)
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["89988.HK"]["is_cny_counter"] is True
    assert by_sym["89988.HK"]["hkd_counter"] == "09988.HK"
    assert by_sym["09988.HK"]["is_cny_counter"] is False


def test_fetch_demands_all_tencent_batches(patched_sources):
    """🔴 在市闸门必须开 ``raise_on_partial``。

    缺一批时「腾讯没返回这只」既可能是退市/停牌, 也可能是那批请求失败 ——
    二者不可区分。默认行为 (丢一批继续) 会把最多 300 只在市股票**静默**写成退市
    并落库, 搜索/标的池随即少一批票, 且日志里只有一条 warning。
    """
    patched_sources["profiles"] = [_profile("00700", "腾讯控股")]
    patched_sources["live"] = [_live("00700.HK", 344.0)]

    sync_hk.fetch_hk_instrument_rows(
        client=_FakeEmClient([]), tencent_client=_FakeTxClient(),
    )

    assert patched_sources["gate_kwargs"] == {"raise_on_partial": True}


def test_fetch_closes_only_self_created_clients(patched_sources):
    """注入的两个 client 由调用方负责关闭; 自建的必须关 (否则连接泄漏)。"""
    em, tx = _FakeEmClient([]), _FakeTxClient()
    patched_sources["profiles"] = [_profile("00700", "腾讯控股")]
    patched_sources["live"] = [_live("00700.HK", 344.0)]

    sync_hk.fetch_hk_instrument_rows(client=em, tencent_client=tx)

    assert em.closed is False, "不该关掉调用方传进来的东财 client"
    assert tx.closed is False, "不该关掉调用方传进来的腾讯 client"


def test_fetch_closes_self_created_clients_on_error(monkeypatch):
    """抛错路径也要关**两个** client (finally), 否则封禁重试期间连接堆积。

    🔴 刻意**不**用 ``patched_sources``: 那个 fixture 把 ``fetch_hk_org_profiles``
    整个换掉, 假 client 的方法根本不会被调用, 抛错路径也就测不到。
    """
    created: list = []

    class _BoomEm(_FakeEmClient):
        def __init__(self):
            super().__init__([])
            created.append(self)

        def datacenter_report_all(self, report_name, **kwargs):
            raise EastmoneyError("boom")

    class _BoomTx(_FakeTxClient):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(sync_hk, "EastmoneyClient", _BoomEm)
    monkeypatch.setattr(sync_hk, "TencentClient", _BoomTx)
    with pytest.raises(EastmoneyError):
        sync_hk.fetch_hk_instrument_rows()
    assert len(created) == 2, "两个 client 都该被自建并登记"
    assert all(c.closed for c in created), "抛错路径漏关 client"


def test_tencent_gate_error_keeps_history_untouched(tmp_path, patched_fetch):
    """在市闸门失败 → 返回 0 且**不写盘**。

    缺闸门时写盘等于把 F10 概况里约 2200 个退市/非股票代码当在市标的落库,
    比保留昨天的维表严重得多 (fail closed, CONTRIBUTING §4)。
    """
    _seed_history(tmp_path, ["00700.HK", "09988.HK"])
    before = _read(tmp_path)
    patched_fetch["exc"] = TencentError("港股快照: 全部 10 批失败")

    assert sync_hk.sync_instruments_hk(tmp_path) == 0
    assert _read(tmp_path).equals(before), "历史维表被覆盖"
