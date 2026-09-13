"""板块资金流向服务测试 (不依赖真实网络)。

覆盖: 交易日回退、历史日 JSON 缓存命中与落盘、当日拉取失败 fallback_prev、
彻底失败 no_data、损坏缓存重拉、AI 复盘摘要段、客户端归一化 (/100 转小数制)。
日期用 2026-08-26/27/28 (写作时为过去交易日), 与仓库既有绝对日期测试风格一致。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services import sector_fund_flow as sff
from app.services.sector_fund_flow_clients import (
    SectorFundFlowError,
    _normalize,
    _normalize_sina,
    _to_decimal,
    _to_float,
)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    for d in ("2026-08-26", "2026-08-27", "2026-08-28"):
        (tmp_path / "kline_daily" / f"date={d}").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _fake_boards() -> dict:
    return {
        "industry": {
            "inflow": [
                {"name": "半导体", "code": "BK1036", "change_pct": 0.021, "main_net_inflow": 2.0e9,
                 "main_net_ratio": 0.03, "super_net": 1.0e9, "large_net": 1.0e9,
                 "medium_net": -0.5e9, "small_net": -0.5e9},
                {"name": "证券", "code": "BK0473", "change_pct": 0.01, "main_net_inflow": 1.0e9,
                 "main_net_ratio": 0.02, "super_net": 0.5e9, "large_net": 0.5e9,
                 "medium_net": 0.0, "small_net": 0.0},
            ],
            "outflow": [
                {"name": "银行", "code": "BK0475", "change_pct": -0.01, "main_net_inflow": -3.0e9,
                 "main_net_ratio": -0.04, "super_net": -2.0e9, "large_net": -1.0e9,
                 "medium_net": 1.0e9, "small_net": 2.0e9},
            ],
        },
        "concept": {
            "inflow": [
                {"name": "国产芯片", "code": "BK0891", "change_pct": 0.03, "main_net_inflow": 1.5e9,
                 "main_net_ratio": 0.025, "super_net": 0.8e9, "large_net": 0.7e9,
                 "medium_net": -0.4e9, "small_net": -0.6e9},
            ],
            "outflow": [
                {"name": "白酒", "code": "BK0896", "change_pct": -0.02, "main_net_inflow": -2.0e9,
                 "main_net_ratio": -0.03, "super_net": -1.0e9, "large_net": -1.0e9,
                 "medium_net": 1.0e9, "small_net": 1.0e9},
            ],
        },
    }


def _use_fetch(monkeypatch, fn):
    monkeypatch.setattr(sff, "_fetch", fn)
    return fn


class _FakeFetch:
    """记录调用次数; fail=True 时抛 SectorFundFlowError; 返回 (source, boards)。"""

    def __init__(self, fail: bool = False, source: str = "eastmoney"):
        self.calls: list[None] = []
        self.fail = fail
        self.source = source

    def __call__(self):
        self.calls.append(None)
        if self.fail:
            raise SectorFundFlowError("东财板块资金流全部节点失败")
        return self.source, _fake_boards()


# ---- 交易日解析 ----

def test_resolve_rolls_back_non_trading_day(data_dir):
    assert sff.resolve_trade_date(data_dir, date(2026, 8, 30)) == date(2026, 8, 28)
    assert sff.resolve_trade_date(data_dir, date(2026, 8, 27)) == date(2026, 8, 27)


# ---- 状态与缓存 ----

def test_fetch_stores_cache_then_hits_cache(data_dir, monkeypatch):
    fetch = _use_fetch(monkeypatch, _FakeFetch())
    out = sff.get_sector_fund_flow(data_dir, None)  # 默认 → 最近分区 08-28
    assert out["state"] == "ok" and out["trade_date"] == "2026-08-28"
    assert out["source"] == "eastmoney"
    assert len(out["industry"]["inflow"]) == 2
    assert len(fetch.calls) == 1
    assert (data_dir / "sector_fund_flow" / "date=2026-08-28.json").exists()

    fetch.calls.clear()
    out2 = sff.get_sector_fund_flow(data_dir, date(2026, 8, 30))  # 周日 → 08-28 → 命中缓存
    assert out2["state"] == "ok" and out2["trade_date"] == "2026-08-28"
    assert fetch.calls == []


def test_explicit_history_date_uses_cache(data_dir, monkeypatch):
    fetch = _use_fetch(monkeypatch, _FakeFetch())
    sff.get_sector_fund_flow(data_dir, date(2026, 8, 27))
    assert len(fetch.calls) == 1
    fetch.calls.clear()
    sff.get_sector_fund_flow(data_dir, date(2026, 8, 27))
    assert fetch.calls == []


def test_failure_falls_back_to_prev(data_dir, monkeypatch):
    # 先落一份 08-27 缓存, 再让 08-28 拉取失败 → 回退 08-27
    fetch = _use_fetch(monkeypatch, _FakeFetch())
    sff.get_sector_fund_flow(data_dir, date(2026, 8, 27))
    assert (data_dir / "sector_fund_flow" / "date=2026-08-27.json").exists()

    fetch.calls.clear()
    monkeypatch.setattr(sff, "_fetch", _FakeFetch(fail=True))
    out = sff.get_sector_fund_flow(data_dir, date(2026, 8, 28))
    assert out["state"] == "fallback_prev"
    assert out["trade_date"] == "2026-08-27"
    assert out["requested_date"] == "2026-08-28"


def test_total_failure_returns_no_data(data_dir, monkeypatch):
    _use_fetch(monkeypatch, _FakeFetch(fail=True))
    out = sff.get_sector_fund_flow(data_dir, date(2026, 8, 28))
    assert out["state"] == "no_data"
    assert "2026-08-28" in out.get("message", "") or out.get("message")


def test_corrupt_cache_refetches(data_dir, monkeypatch):
    fetch = _use_fetch(monkeypatch, _FakeFetch())
    cache = data_dir / "sector_fund_flow" / "date=2026-08-28.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{broken json", encoding="utf-8")
    out = sff.get_sector_fund_flow(data_dir, date(2026, 8, 28))
    assert out["state"] == "ok"
    assert fetch.calls  # 缓存损坏 → 重新拉取


# ---- AI 复盘摘要 ----

def test_build_recap_context_contains_summary(data_dir, monkeypatch):
    _use_fetch(monkeypatch, _FakeFetch())
    ctx = sff.build_recap_context(data_dir)
    assert "国产芯片" in ctx and "白酒" in ctx
    assert "半导体" in ctx  # 行业净流入居前


def test_build_recap_context_empty_on_no_data(data_dir, monkeypatch):
    _use_fetch(monkeypatch, _FakeFetch(fail=True))
    assert sff.build_recap_context(data_dir) == ""


# ---- 客户端归一化 (关键: 东财百分比原值 → 小数制) ----

def test_to_float_and_decimal():
    assert _to_float("-") is None
    assert _to_float("") is None
    assert _to_float("12.5") == 12.5
    assert _to_decimal("0.52") == pytest.approx(0.0052)
    assert _to_decimal("-1.23") == pytest.approx(-0.0123)
    assert _to_decimal("-") is None


def test_normalize_maps_eastmoney_fields():
    item = _normalize({
        "f12": "BK1036", "f14": "半导体", "f3": 2.1, "f62": 2000000000,
        "f184": 3.0, "f66": 1000000000, "f72": 1000000000,
        "f78": -500000000, "f84": -500000000,
    })
    assert item["name"] == "半导体" and item["code"] == "BK1036"
    assert item["change_pct"] == pytest.approx(0.021)  # 2.1% → 小数制
    assert item["main_net_inflow"] == 2000000000
    assert item["main_net_ratio"] == pytest.approx(0.03)
    assert item["super_net"] == 1000000000 and item["small_net"] == -500000000


def test_normalize_sina_maps_fields():
    # 新浪字段已是目标制 (小数/元), 不应再做 /100 转换; 分档净额缺省为 None
    item = _normalize_sina({
        "name": "电子器件", "category": "new_dzqj",
        "avg_changeratio": "-0.000307056", "netamount": "3521141003.96",
        "ratioamount": "0.0221595", "ts_name": "铜峰电子",
    })
    assert item["name"] == "电子器件" and item["code"] == "new_dzqj"
    assert item["change_pct"] == pytest.approx(-0.000307056)
    assert item["main_net_inflow"] == pytest.approx(3521141003.96)
    assert item["main_net_ratio"] == pytest.approx(0.0221595)
    assert item["super_net"] is None and item["small_net"] is None
    assert item["leader_name"] == "铜峰电子"


def test_sina_source_recorded(data_dir, monkeypatch):
    _use_fetch(monkeypatch, _FakeFetch(source="sina"))
    out = sff.get_sector_fund_flow(data_dir, None)
    assert out["state"] == "ok" and out["source"] == "sina"


def test_tier_falls_back_to_sina(monkeypatch):
    import app.services.sector_fund_flow_clients as clients

    def boom():
        raise clients.SectorFundFlowError("东财板块资金流全部节点失败")

    def fake_sina():
        return _fake_boards()

    monkeypatch.setattr(clients, "fetch_sector_fund_flow", boom)
    monkeypatch.setattr(clients, "fetch_sina_sector_fund_flow", fake_sina)
    source, boards = sff._fetch()
    assert source == "sina"
    assert len(boards["industry"]["inflow"]) == 2


def test_tier_all_sources_fail_raises(monkeypatch):
    import app.services.sector_fund_flow_clients as clients

    def boom():
        raise clients.SectorFundFlowError("down")

    monkeypatch.setattr(clients, "fetch_sector_fund_flow", boom)
    monkeypatch.setattr(clients, "fetch_sina_sector_fund_flow", boom)
    with pytest.raises(SectorFundFlowError):
        sff._fetch()
