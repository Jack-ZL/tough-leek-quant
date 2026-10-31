"""标的维表同步: 某交易所同步失败时保留历史行, 不掏空维表。

回归背景: 某交易所 get_instruments 瞬时报错 (connection reset) 时, 旧实现把
只剩其余交易所的残缺结果全量覆盖写盘 —— 整个交易所的标的从维表消失, 个股搜索
(如搜沪市股票) / 标的池 / 名称表随之全部失灵。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.services import instrument_sync as isync


def _write_existing(data_dir: Path) -> None:
    out = data_dir / "instruments"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "603778.SH", "000001.SZ"],
        "name": ["浦发银行", "国晟科技", "平安银行"],
        "code": ["600000", "603778", "000001"],
        "exchange": ["SH", "SH", "SZ"],
        "region": ["CN", "CN", "CN"],
        "type": ["stock", "stock", "stock"],
    }).write_parquet(out / "instruments.parquet")


def _rows(*symbols: str) -> list[dict]:
    return [
        {
            "symbol": s, "name": s, "code": s.split(".")[0],
            "exchange": s.split(".")[1], "region": "CN", "type": "stock",
            "listing_date": None, "total_shares": None, "float_shares": None,
            "tick_size": None, "limit_up": None, "limit_down": None,
        }
        for s in symbols
    ]


def test_missing_exchange_rows_carried_over(tmp_path, monkeypatch) -> None:
    """SH 同步失败 (结果只有 SZ) → 旧 SH 行保留, 搜沪市股票不再落空。"""
    _write_existing(tmp_path)
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider",
                        lambda: _rows("000001.SZ", "000002.SZ"))

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    symbols = set(df["symbol"].to_list())
    assert {"600000.SH", "603778.SH"} <= symbols   # 旧 SH 行保留
    assert {"000001.SZ", "000002.SZ"} <= symbols   # 本次 SZ 照常更新
    assert df.filter(pl.col("exchange") == "SH").height == 2


def test_all_exchanges_present_no_carryover(tmp_path, monkeypatch) -> None:
    """各交易所都同步成功 → 全量覆盖, 旧行不残留 (退市/改名照常生效)。"""
    _write_existing(tmp_path)
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider",
                        lambda: _rows("600000.SH", "000001.SZ"))

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert set(df["symbol"].to_list()) == {"600000.SH", "000001.SZ"}


def test_carryover_new_row_wins_on_same_symbol(tmp_path, monkeypatch) -> None:
    """保留行与本次结果同 symbol 冲突时以本次新行为准 (兜底, 交易所互斥下不会发生)。"""
    _write_existing(tmp_path)
    rows = _rows("000001.SZ")
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: rows)
    # 手工把历史表里塞一个 BJ 的同 symbol 旧行制造冲突
    out = tmp_path / "instruments" / "instruments.parquet"
    old = pl.read_parquet(out)
    pl.concat([old, pl.DataFrame({
        "symbol": ["000001.SZ"], "name": ["旧名字"], "code": ["000001"],
        "exchange": ["BJ"], "region": ["CN"], "type": ["stock"],
    })], how="diagonal_relaxed").write_parquet(out)

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(out)
    dup = df.filter(pl.col("symbol") == "000001.SZ")
    assert dup.height == 1
    assert dup["exchange"][0] == "SZ"      # 新行胜出, BJ 旧行被去重掉
    assert dup["name"][0] == "000001.SZ"
    assert df.filter(pl.col("exchange") == "SH").height == 2  # 缺失交易所仍保留


def test_no_existing_table_no_carryover(tmp_path, monkeypatch) -> None:
    """首次同步无历史维表: 不报错, 直接写本次结果。"""
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider",
                        lambda: _rows("000001.SZ"))

    assert isync.sync_instruments(tmp_path) == 1

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df.height == 1
