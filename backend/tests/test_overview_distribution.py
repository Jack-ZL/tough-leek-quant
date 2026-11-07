"""市场总览 涨跌分布 15 档 + 涨停行业集中度 (P1-19 情绪细粒度统计)。"""
from __future__ import annotations

import json
import types

import polars as pl

from app.services.market_overview_builder import (
    _limit_up_industry,
    _pct_band_rows,
)


def _fake_repo(tmp_path):
    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _write_industry_ext(tmp_path, mapping: dict[str, str]) -> None:
    """写一张 snapshot 模式的行业扩展表 (symbol → 所属行业, 支持 "一级-二级-三级")。"""
    cfg_dir = tmp_path / "ext_data" / "industry_tbl"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath("config.json").write_text(
        json.dumps({
            "id": "industry_tbl",
            "label": "行业表",
            "mode": "snapshot",
            "fields": [{"name": "所属行业", "dtype": "string", "label": "所属行业"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    pl.DataFrame({
        "symbol": list(mapping.keys()),
        "所属行业": list(mapping.values()),
    }).write_parquet(cfg_dir / "part.parquet")


def _counts(rows: list[dict]) -> dict[str, int]:
    return {b["label"]: b["count"] for b in _pct_band_rows(rows)}


# ───────────────────────── 涨跌分布 15 档 ─────────────────────────

def test_band_structure_15_with_side():
    bands = _pct_band_rows([])
    assert len(bands) == 15
    assert bands[0]["label"] == "跌停" and bands[0]["side"] == "limit_down"
    assert bands[-1]["label"] == "涨停" and bands[-1]["side"] == "limit_up"
    assert bands[7]["label"] == "平盘" and bands[7]["side"] == "flat"
    assert all(b["count"] == 0 for b in bands)


def test_limit_up_signal_takes_priority_over_pct_band():
    """涨停按信号判定优先入桶: 20cm 板 +9.5% 不进 ">7%" 档; consec>0 同样视为涨停。"""
    rows = [
        {"symbol": "A", "change_pct": 0.095, "signal_limit_up": True},
        {"symbol": "B", "change_pct": 0.19, "consecutive_limit_ups": 2},
        {"symbol": "C", "change_pct": 0.08},
    ]
    c = _counts(rows)
    assert c["涨停"] == 2
    assert c["> 7%"] == 1


def test_limit_down_signal_priority():
    rows = [{"symbol": "A", "change_pct": -0.05, "signal_limit_down": True}]
    assert _counts(rows)["跌停"] == 1


def test_band_boundaries_half_open():
    """[low, high) 半开区间: -0.07 入 -7~-5, 0.07 入 >7%, 0 入平盘。"""
    rows = [
        {"symbol": "A", "change_pct": -0.07},
        {"symbol": "B", "change_pct": -0.0701},
        {"symbol": "C", "change_pct": 0.07},
        {"symbol": "D", "change_pct": 0.0},
        {"symbol": "E", "change_pct": -0.01},
        {"symbol": "F", "change_pct": 0.01},
    ]
    c = _counts(rows)
    assert c["-7~-5%"] == 1 and c["< -7%"] == 1
    assert c["> 7%"] == 1 and c["平盘"] == 1
    assert c["-1~0%"] == 1 and c["1~2%"] == 1  # 0.01 属 [0.01, 0.02)


def test_none_pct_without_signal_skipped_and_pct_sums_100():
    rows = [
        {"symbol": "A", "change_pct": 0.02},
        {"symbol": "B", "change_pct": None},
        {"symbol": "C", "change_pct": -0.02},
    ]
    bands = _pct_band_rows(rows)
    total = sum(b["count"] for b in bands)
    assert total == 2  # None 且无信号不入桶
    assert abs(sum(b["pct"] for b in bands) - 100.0) < 1e-6


# ───────────────────────── 涨停行业集中度 ─────────────────────────

def test_concentration_top5_share_and_label(tmp_path):
    """6 家涨停: 4 家电子 + 2 家医药 → top5 占 100% → 极集中。"""
    _write_industry_ext(tmp_path, {
        "000001.SZ": "电子-半导体-芯片设计",
        "000002.SZ": "电子-半导体-芯片制造",
        "000003.SZ": "电子-消费电子-面板",
        "000004.SZ": "电子-元件-PCB",
        "000005.SZ": "医药生物-化学制药-原料药",
        "000006.SZ": "医药生物-中药-中药Ⅱ",
    })
    rows = [
        {"symbol": f"00000{i}.SZ", "change_pct": 0.1, "signal_limit_up": True}
        for i in range(1, 7)
    ]
    ind = _limit_up_industry(rows, _fake_repo(tmp_path))
    assert ind["total"] == 6 and ind["mapped_total"] == 6
    assert ind["top5_share"] == 1.0 and ind["label"] == "极集中"
    # 行业按二级拆分: level=2 → "半导体"×2, 其余各 1
    names = {t["name"]: t["count"] for t in ind["top5"]}
    assert names.get("半导体") == 2
    assert sum(names.values()) == 6


def test_concentration_four_labels(tmp_path):
    """四档标签边界: 每家涨停独占一个行业时 top5 占比 = 5/N。"""
    cases = [
        (10, "极集中"),    # 5/10 = 0.50 ≥ 0.50
        (14, "集中"),      # 5/14 ≈ 0.357 ∈ [0.35, 0.50)
        (25, "较分散"),    # 5/25 = 0.20 ∈ [0.20, 0.35)
        (40, "极分散"),    # 5/40 = 0.125 < 0.20
    ]
    for n, expected in cases:
        sub = tmp_path / f"case_{n}"
        sub.mkdir()
        _write_industry_ext(sub, {f"{i:06d}.SZ": f"行业{i}-子{i}-细{i}" for i in range(1, n + 1)})
        rows = [
            {"symbol": f"{i:06d}.SZ", "change_pct": 0.1, "signal_limit_up": True}
            for i in range(1, n + 1)
        ]
        ind = _limit_up_industry(rows, _fake_repo(sub))
        assert ind["label"] == expected, f"N={n}: {ind['label']} != {expected}"
        assert len(ind["top5"]) == 5


def test_concentration_unmapped_excluded_from_denominator(tmp_path):
    """无行业映射的涨停股不入分母: mapped_total < total。"""
    _write_industry_ext(tmp_path, {"000001.SZ": "电子-半导体-芯片设计"})
    rows = [
        {"symbol": "000001.SZ", "change_pct": 0.1, "signal_limit_up": True},
        {"symbol": "000002.SZ", "change_pct": 0.1, "signal_limit_up": True},
    ]
    ind = _limit_up_industry(rows, _fake_repo(tmp_path))
    assert ind["total"] == 2 and ind["mapped_total"] == 1
    assert ind["top5_share"] == 1.0


def test_concentration_no_limit_up_or_no_mapping(tmp_path):
    rows = [{"symbol": "000001.SZ", "change_pct": 0.02}]
    ind = _limit_up_industry(rows, _fake_repo(tmp_path))
    assert ind == {"total": 0, "mapped_total": 0, "top5": [], "top5_share": None, "label": None}

    _write_industry_ext(tmp_path, {})
    rows2 = [{"symbol": "000001.SZ", "change_pct": 0.1, "signal_limit_up": True}]
    ind2 = _limit_up_industry(rows2, _fake_repo(tmp_path))
    assert ind2["total"] == 1 and ind2["mapped_total"] == 0
    assert ind2["top5_share"] is None and ind2["label"] is None
