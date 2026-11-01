"""BaostockProvider 契约与推导逻辑测试 (P1-12)。

不依赖真实网络与 baostock 库安装: mock 会话层 (_import_bs/_query_locked),
验证代码映射 (北交所剔除)、日K列序与量纲 (股→手)、除权因子链推导
(比值 = back_i/back_{i-1}, 首行锚点跳过, 乱序输入排序)、窗口过滤、
会话自愈 (失败重连重试一次)、逐标的软失败与进度回调。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.plugins.baostock import provider as bp
from app.plugins.baostock.provider import BaostockProvider

# ---- 实测录制样例 (2026-11 冒烟) ----
# 日K行列序 = _DAILY_FIELDS: [date, open, high, low, close, volume(股), amount(元)]
_DAILY_ROW = ["2026-11-03", "10.0000", "10.0500", "9.9500", "10.0200", "64886500", "647741483.0000"]

# 复权因子行列序: [code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor]
_ADJ_ROWS = [
    ["sz.000062", "1991-01-14", "1.000000", "1.000000", "1.000000"],  # 上市锚点, 应跳过
    ["sz.000062", "2026-09-30", "0.985300", "2.537500", "2.537500"],  # 2.5375/2.5 = 1.015
    ["sz.000062", "2020-06-15", "0.400000", "2.500000", "2.500000"],  # 2.5/1.0 = 2.5 (乱序输入)
]


# ---- 代码映射 ----
@pytest.mark.parametrize(
    "symbol,code",
    [("600000.SH", "sh.600000"), ("000001.SZ", "sz.000001"), ("000001.SH", "sh.000001")],
)
def test_symbol_to_bs_code(symbol, code):
    assert bp.symbol_to_bs_code(symbol) == code


@pytest.mark.parametrize("bad", ["920002.BJ", "600000", "600000.US", "abc.SH", "", None])
def test_symbol_to_bs_code_rejects(bad):
    """北交所上游不支持 (10004011) → None, 调用方跳过。"""
    assert bp.symbol_to_bs_code(bad) is None


# ---- 日K行映射 ----
def test_map_daily_row_units():
    """列序 OHLC 标准; volume 股→手 ÷100; amount 元透传。"""
    rec = bp._map_daily_row("600000.SH", _DAILY_ROW)
    assert rec == {
        "symbol": "600000.SH", "date": "2026-11-03",
        "open": 10.0, "high": 10.05, "low": 9.95, "close": 10.02,
        "volume": 648865.0,  # 64886500 股 → 648865 手
        "amount": 647741483.0,
    }


@pytest.mark.parametrize(
    "row",
    [None, [], ["", "1", "1", "1", "1", "1", "1"], ["2026-11-03", "10.0"]],
)
def test_map_daily_row_bad_rows(row):
    assert bp._map_daily_row("600000.SH", row) is None


def test_map_daily_row_empty_values_to_none():
    """停牌日字段为空串 → None (不伪造 0)。"""
    row = ["2026-11-03", "", "", "", "", "", ""]
    rec = bp._map_daily_row("600000.SH", row)
    assert rec["open"] is None and rec["volume"] is None and rec["amount"] is None


# ---- 除权因子链推导 ----
def test_derive_ex_factors_chain():
    """ex_factor = back_i/back_{i-1}; 首行锚点跳过; 乱序输入按除权日排序。"""
    out = bp.derive_ex_factors("000062.SZ", _ADJ_ROWS)
    assert [(r["trade_date"], round(r["ex_factor"], 6)) for r in out] == [
        ("2020-06-15", 2.5),  # 2.5 / 1.0 (锚点)
        ("2026-09-30", 1.015),  # 2.5375 / 2.5
    ]
    assert all(r["symbol"] == "000062.SZ" for r in out)


def test_derive_ex_factors_single_anchor_only():
    """只有上市锚点 (从未除权) → 空链, 不产出伪事件。"""
    assert bp.derive_ex_factors("600000.SH", [_ADJ_ROWS[0]]) == []


def test_derive_ex_factors_bad_rows_skipped():
    rows = [
        ["sh.600000", "1999-11-10", "1.0", "1.0", "1.0"],
        ["sh.600000", "", "1.0", "2.0", "2.0"],  # 无日期 → 跳过
        ["sh.600000", "2020-06-15", "1.0", "", "1.0"],  # back 缺失 → 跳过
        ["sh.600000", "2024-07-12", "1.0", "3.0", "3.0"],
    ]
    out = bp.derive_ex_factors("600000.SH", rows)
    # 有效链: 1999 锚点(1.0) → 2024 (3.0) → 单事件 3.0
    assert [(r["trade_date"], r["ex_factor"]) for r in out] == [("2024-07-12", 3.0)]


# ---- 会话层: 登录/自愈/重试 ----
class _RS:
    """假 ResultData: 游标式 next()/get_row_data() (baostock 官方用法同构)。"""

    def __init__(self, rows=None, code="0", msg=""):
        self.error_code = code
        self.error_msg = msg
        self._rows = list(rows or [])
        self._i = -1

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


class _FakeBs:
    """假 baostock 模块: 记录登录/登出次数, 按脚本弹出查询结果。"""

    def __init__(self, results, login_codes=("0",)):
        self.results = list(results)
        self.login_codes = list(login_codes)
        self.logins = 0
        self.logouts = 0
        self.queries: list[tuple] = []

    def login(self):
        code = self.login_codes.pop(0) if self.login_codes else "0"
        self.logins += 1
        return SimpleNamespace(error_code=code, error_msg="fake")

    def logout(self):
        self.logouts += 1
        return SimpleNamespace(error_code="0", error_msg="")

    def query(self, *args, **kwargs):
        self.queries.append((args, kwargs))
        item = self.results.pop(0)
        if isinstance(item, _RS):
            return item
        return _RS(rows=item)


@pytest.fixture
def fake_bs(monkeypatch):
    """替换 _import_bs 并复位全局会话状态; 返回工厂函数。"""
    monkeypatch.setattr(bp, "_LOGGED_IN", False)
    holder = {}

    def _install(fake: _FakeBs):
        holder["bs"] = fake
        monkeypatch.setattr(bp, "_import_bs", lambda: fake)
        return fake

    yield _install
    # 会话状态复位, 避免污染其他用例
    monkeypatch.setattr(bp, "_LOGGED_IN", False)


def test_query_locked_success(fake_bs):
    bs = fake_bs(_FakeBs([[_DAILY_ROW]]))
    rows = bp._query_locked(bs.query, "sh.600000")
    assert rows == [_DAILY_ROW]
    assert bs.logins == 1  # 懒登录一次


def test_query_locked_relogin_retry_once(fake_bs):
    """首查失败 (会话过期) → logout+login 重连重试一次成功。"""
    bs = fake_bs(_FakeBs([_RS(code="10004001", msg="网络接收错误"), [_DAILY_ROW]]))
    rows = bp._query_locked(bs.query, "sh.600000")
    assert rows == [_DAILY_ROW]
    assert (bs.logins, bs.logouts) == (2, 1)


def test_query_locked_exhausts_and_raises(fake_bs):
    bs = fake_bs(_FakeBs([_RS(code="10004011", msg="代码错误")] * 2))
    with pytest.raises(bp.BaostockError, match="10004011"):
        bp._query_locked(bs.query, "bad")


def test_query_locked_login_failure_raises(fake_bs):
    bs = fake_bs(_FakeBs([], login_codes=["10001001"]))
    with pytest.raises(bp.BaostockError, match="登录失败"):
        bp._query_locked(bs.query, "x")


# ---- availability ----
def test_availability_false_when_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", None)  # import 时抛 ImportError
    ok, msg = bp.availability()
    assert not ok and "安装" in msg


def test_availability_true_when_installed():
    try:
        import baostock  # noqa: F401
    except ImportError:
        pytest.skip("baostock 未安装")
    ok, msg = bp.availability()
    assert ok and msg


# ---- iter_daily / get_daily (mock 会话层) ----
@pytest.fixture
def mock_query(monkeypatch):
    """替换 _query_locked 与 _import_bs, 按 code 返回预置行/异常。"""
    calls: list[tuple] = []
    scripts: dict = {}

    def _fake_query(fn, *args, **kwargs):
        code = args[0] if args else kwargs.get("code")
        calls.append((code, kwargs))
        item = scripts.get(code, [])
        if isinstance(item, Exception):
            raise item
        return [list(r) for r in item]

    monkeypatch.setattr(bp, "_query_locked", _fake_query)
    monkeypatch.setattr(bp, "_import_bs", lambda: SimpleNamespace(
        query_history_k_data_plus=lambda *a, **k: None,
        query_adjust_factor=lambda *a, **k: None,
    ))
    return SimpleNamespace(calls=calls, scripts=scripts)


def test_iter_daily_frames_and_progress(mock_query):
    mock_query.scripts["sh.600000"] = [_DAILY_ROW]
    p = BaostockProvider()
    progress = []
    frames = list(p.iter_daily(
        ["600000.SH"], datetime(2026, 11, 1), datetime(2026, 11, 3),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    ))
    assert len(frames) == 1
    df = frames[0]
    assert df["symbol"][0] == "600000.SH"
    assert df["volume"][0] == pytest.approx(648865.0)  # 股→手
    assert df["amount"][0] == pytest.approx(647741483.0)
    assert progress == [(1, 1)]
    # 窗口转 YYYY-MM-DD 传给上游
    _, kwargs = mock_query.calls[0]
    assert kwargs["start_date"] == "2026-11-01" and kwargs["end_date"] == "2026-11-03"
    assert kwargs["adjustflag"] == "3"  # 不复权原始价


def test_iter_daily_per_symbol_soft_fail_and_bj_skip(mock_query):
    """单标的失败只丢该标的; 北交所汇总告警跳过 (不发查询)。"""
    mock_query.scripts["sh.600000"] = bp.BaostockError("down")
    mock_query.scripts["sz.000001"] = [_DAILY_ROW]
    p = BaostockProvider()
    frames = list(p.iter_daily(["600000.SH", "000001.SZ", "920002.BJ"]))
    assert len(frames) == 1  # 只剩 000001
    assert {c for c, _ in mock_query.calls} == {"sh.600000", "sz.000001"}  # BJ 未发查询


def test_iter_daily_etf_empty(mock_query):
    p = BaostockProvider()
    assert list(p.iter_daily(["510300.SH"], asset_type="etf")) == []
    assert mock_query.calls == []


def test_get_daily_aggregates(mock_query):
    mock_query.scripts["sh.600000"] = [_DAILY_ROW]
    mock_query.scripts["sz.000001"] = [_DAILY_ROW]
    df = BaostockProvider().get_daily(["600000.SH", "000001.SZ"])
    assert df.height == 2 and set(df["symbol"]) == {"600000.SH", "000001.SZ"}


# ---- get_adj_factors (mock 会话层) ----
def test_get_adj_factors_chain_and_window(mock_query):
    """全链推导后按窗口裁剪输出; 三列 schema 与存储对齐兼容。"""
    mock_query.scripts["sz.000062"] = _ADJ_ROWS
    p = BaostockProvider()
    # 全窗口: 两个事件
    df = p.get_adj_factors(["000062.SZ"])
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 2
    # 增量窗口只裁输出 (查询仍是全历史链)
    df2 = p.get_adj_factors(
        ["000062.SZ"], start_time=datetime(2026, 1, 1), end_time=datetime(2026, 12, 31)
    )
    assert df2.height == 1 and df2["trade_date"][0] == date(2026, 9, 30)
    assert df2["ex_factor"][0] == pytest.approx(1.015)
    # 查询起点固定 1990 (链完整性), 不随窗口收缩
    _, kwargs = mock_query.calls[0]
    assert kwargs["start_date"] == bp._ADJ_CHAIN_START


def test_get_adj_factors_progress_and_soft_fail(mock_query):
    mock_query.scripts["sh.600000"] = bp.BaostockError("down")
    mock_query.scripts["sz.000062"] = _ADJ_ROWS
    p = BaostockProvider()
    progress = []
    df = p.get_adj_factors(
        ["600000.SH", "000062.SZ", "920002.BJ"],
        on_chunk_done=lambda c, t: progress.append((c, t)),
    )
    assert df.height == 2  # 600000 失败、BJ 跳过, 只剩 000062
    assert progress == [(1, 3), (2, 3), (3, 3)]  # 失败/跳过也计进度


def test_get_adj_factors_non_stock_empty(mock_query):
    p = BaostockProvider()
    assert p.get_adj_factors(["510300.SH"], asset_type="etf").is_empty()
    assert p.get_adj_factors([]).is_empty()
    assert mock_query.calls == []


# ---- test_dataset ----
def test_test_dataset_daily_json_serializable(mock_query):
    import json

    mock_query.scripts["sh.600000"] = [_DAILY_ROW]
    out = BaostockProvider().test_dataset("daily", ["600000.SH"])
    json.dumps(out)  # date 已转 ISO
    assert out["rows"] == 1 and out["preview"][0]["date"] == "2026-11-03"


def test_test_dataset_adj_factor(mock_query):
    mock_query.scripts["sh.600000"] = _ADJ_ROWS
    out = BaostockProvider().test_dataset("adj_factor", ["600000.SH"])
    assert out["rows"] == 2


def test_test_dataset_unknown(mock_query):
    out = BaostockProvider().test_dataset("realtime")
    assert out["rows"] == 0 and "回退" in out["error"]


# ---- 插件注册 ----
def test_plugin_yaml_loadable():
    import yaml
    from pathlib import Path

    meta = yaml.safe_load(
        (Path(bp.__file__).parent / "plugin.yaml").read_text(encoding="utf-8")
    )
    assert meta["name"] == "baostock"
    assert meta["runtime"] == "python"
    assert set(k for k, v in meta["datasets"].items() if v) == set(bp._DATASETS)
    assert meta["api_key_env"] is None
    # requirements.txt 必须存在 (loader python 运行时安装依赖的入口)
    assert (Path(bp.__file__).parent / "requirements.txt").exists()
