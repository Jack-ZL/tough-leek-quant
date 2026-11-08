"""P1-14 模拟盘运维三件套: 全局急停 / 已了结订单压缩归档 / 除权后对账。

急停 = 数据目录放 paper_kill_switch 文件 (fail-safe: 检查出错也视为急停);
归档 = retain_days 之前的已了结订单移入 archive/orders_YYYY-MM.jsonl.gz,
load_orders 合并读回; 对账 = 台账重放 vs 物化快照 (不一致自愈) +
corp_action 链核对 (只记录不修, append-only 台账不改写历史)。
"""
from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"


def _write_daily(tmp_path, rows: list[tuple[date, float, float]]) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    df = pl.DataFrame(
        {
            "symbol": [SYM] * len(rows),
            "date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [max(r[1], r[2]) for r in rows],
            "low": [min(r[1], r[2]) for r in rows],
            "close": [r[2] for r in rows],
            "volume": [10000.0] * len(rows),
            "amount": [r[2] * 10000.0 for r in rows],
        }
    )
    repo.append_daily(df)


def _write_factor(tmp_path, day: date, factor: float) -> None:
    out = tmp_path / "adj_factor" / "all.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"symbol": [SYM], "trade_date": [day], "ex_factor": [factor]},
        schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64},
    ).write_parquet(out)


def _backdate_order(tmp_path, order: dict, day: str) -> None:
    """把订单文件的 created_at/filled_at 改到指定日期 (模拟历史订单)。"""
    p = paper._orders_dir(tmp_path) / f"{order['id']}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["created_at"] = f"{day}T10:00:00+08:00"
    if data.get("filled_at"):
        data["filled_at"] = f"{day}T15:00:00+08:00"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 全局急停 ────────────────────────────────────────────
def test_kill_switch_blocks_create_order(tmp_path):
    paper.create_account(tmp_path, 1_000_000.0)
    assert paper.kill_switch_state(tmp_path) == {"active": False, "reason": ""}

    (tmp_path / paper.KILL_SWITCH_FILENAME).write_text("维护中", encoding="utf-8")
    state = paper.kill_switch_state(tmp_path)
    assert state["active"] is True and paper.KILL_SWITCH_FILENAME in state["reason"]

    order, err = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="market", ref_price=10.0)
    assert order is None and "全局急停" in err

    # 删除文件即恢复
    (tmp_path / paper.KILL_SWITCH_FILENAME).unlink()
    order, err = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="market", ref_price=10.0)
    assert order is not None and err is None


def test_kill_switch_failsafe_on_oserror(tmp_path, monkeypatch):
    real_exists = Path.exists

    def flaky(self):
        if self.name == paper.KILL_SWITCH_FILENAME:
            raise OSError("disk gone")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", flaky)
    state = paper.kill_switch_state(tmp_path)
    assert state["active"] is True and "fail-safe" in state["reason"]


def test_kill_switch_settle_skips_matching_but_keeps_bookkeeping(tmp_path):
    day = date(2026, 9, 24)
    paper.create_account(tmp_path, 1_000_000.0)
    _write_daily(tmp_path, [
        (day - timedelta(days=1), 10.0, 10.0),
        (day, 10.2, 10.8),
    ])
    order, _ = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="next_open", ref_price=10.0)
    (tmp_path / paper.KILL_SWITCH_FILENAME).touch()

    summary = paper.settle_day(tmp_path, day.isoformat())
    assert summary["kill_switch"] is True and summary["filled"] == 0
    got = paper.get_order(tmp_path, order["id"])
    assert got["status"] == "pending" and int(got.get("postponed", 0)) == 0  # 不计顺延

    # 解除后正常撮合
    (tmp_path / paper.KILL_SWITCH_FILENAME).unlink()
    summary = paper.settle_day(tmp_path, day.isoformat())
    assert summary["kill_switch"] is False and summary["filled"] == 1
    assert summary["nav"] is not None  # 记账照常


# ── 已了结订单压缩归档 ──────────────────────────────────
def test_archive_closed_orders_roundtrip(tmp_path):
    day = date(2026, 9, 24)
    paper.create_account(tmp_path, 1_000_000.0)
    _write_daily(tmp_path, [(day, 10.0, 10.0)])
    filled, _ = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="close", ref_price=10.0)
    paper.settle_day(tmp_path, day.isoformat())
    assert paper.get_order(tmp_path, filled["id"])["status"] == "filled"

    old_day = (paper.cn_today() - timedelta(days=paper.ARCHIVE_RETAIN_DAYS + 10)).isoformat()
    _backdate_order(tmp_path, filled, old_day)

    n = paper.archive_closed_orders(tmp_path)
    assert n == 1
    assert not (paper._orders_dir(tmp_path) / f"{filled['id']}.json").exists()
    gz = list(paper._archive_dir(tmp_path).glob("orders_*.jsonl.gz"))
    assert len(gz) == 1 and gz[0].name == f"orders_{old_day[:7]}.jsonl.gz"

    # load_orders 合并读回, 历史不丢
    orders = paper.load_orders(tmp_path)
    assert any(o["id"] == filled["id"] for o in orders)
    with gzip.open(gz[0], "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["id"] == filled["id"]


def test_archive_retention_boundary_and_pending_kept(tmp_path):
    paper.create_account(tmp_path, 1_000_000.0)
    today = paper.cn_today()
    cutoff_day = (today - timedelta(days=paper.ARCHIVE_RETAIN_DAYS)).isoformat()
    recent_day = (today - timedelta(days=paper.ARCHIVE_RETAIN_DAYS - 1)).isoformat()

    o_old, _ = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="close", ref_price=10.0)
    o_new, _ = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="close", ref_price=10.0)
    o_pending, _ = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="next_open", ref_price=10.0)
    for o, d in ((o_old, cutoff_day), (o_new, recent_day), (o_pending, cutoff_day)):
        _backdate_order(tmp_path, o, d)
    # o_old/o_new 手动置为 filled (不依赖行情), pending 单保持 pending
    for o in (o_old, o_new):
        p = paper._orders_dir(tmp_path) / f"{o['id']}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["status"] = "filled"
        data["filled_at"] = f"{data['created_at'][:10]}T15:00:00+08:00"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert paper.archive_closed_orders(tmp_path, today=today) == 1
    ids = {o["id"]: o for o in paper.load_orders(tmp_path)}
    assert o_old["id"] in ids           # 归档后仍可读回
    assert o_new["id"] in ids           # 未过保留期, 活跃区
    assert o_pending["id"] in ids       # pending 永不归档
    assert (paper._orders_dir(tmp_path) / f"{o_new['id']}.json").exists()
    assert not (paper._orders_dir(tmp_path) / f"{o_old['id']}.json").exists()


def test_archive_appends_to_existing_month_pack(tmp_path):
    paper.create_account(tmp_path, 1_000_000.0)
    old_day = (paper.cn_today() - timedelta(days=paper.ARCHIVE_RETAIN_DAYS + 40)).isoformat()
    ids = []
    for i in range(2):
        o, _ = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="close", ref_price=10.0)
        _backdate_order(tmp_path, o, old_day)
        p = paper._orders_dir(tmp_path) / f"{o['id']}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["status"] = "filled"
        data["filled_at"] = f"{old_day}T15:00:0{i}+08:00"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ids.append(o["id"])
        assert paper.archive_closed_orders(tmp_path) == 1  # 分两次归档, 同月追加

    gz = list(paper._archive_dir(tmp_path).glob("orders_*.jsonl.gz"))
    assert len(gz) == 1
    with gzip.open(gz[0], "rt", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert sorted(o["id"] for o in lines) == sorted(ids)


# ── 除权后对账 ──────────────────────────────────────────
def test_reconcile_ok_after_corp_action_with_after_fields(tmp_path):
    day = date(2026, 9, 24)
    paper.create_account(tmp_path, 1_000_000.0)
    _write_daily(tmp_path, [
        (day - timedelta(days=1), 10.0, 10.0),
        (day, 8.0, 8.0),
    ])
    paper.create_order(tmp_path, SYM, "buy", qty=1000, order_type="close", ref_price=10.0)
    paper.settle_day(tmp_path, (day - timedelta(days=1)).isoformat())
    _write_factor(tmp_path, day, 1.25)
    summary = paper.settle_day(tmp_path, day.isoformat())
    assert summary["corp_actions"] == 1
    assert summary["reconcile"]["ok"] is True and summary["reconcile"]["fixed"] is False

    corp = next(f for f in paper.load_fills(tmp_path) if f.get("kind") == "corp_action")
    assert corp["qty_after"] == pytest.approx(corp["qty_before"] * 1.25)
    assert corp["cost_after"] == pytest.approx(round(corp["cost_before"] / 1.25, 6))


def test_reconcile_self_heals_corrupted_snapshot(tmp_path):
    day = date(2026, 9, 24)
    paper.create_account(tmp_path, 1_000_000.0)
    _write_daily(tmp_path, [(day, 10.0, 10.0)])
    paper.create_order(tmp_path, SYM, "buy", qty=1000, order_type="close", ref_price=10.0)
    paper.settle_day(tmp_path, day.isoformat())

    # 篡改物化快照: 持仓数量 + 账户现金
    pos_file = paper._root(tmp_path) / "positions.json"
    data = json.loads(pos_file.read_text(encoding="utf-8"))
    data["positions"][SYM]["qty"] = 999999
    pos_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    acc = paper.get_account(tmp_path)
    acc["cash"] = 1.0
    paper.save_account(tmp_path, acc)

    rec = paper.reconcile_account(tmp_path)
    assert rec["ok"] is False and rec["fixed"] is True
    assert any("持仓快照" in i for i in rec["issues"])
    assert any("现金快照" in i for i in rec["issues"])

    # 自愈后与重放一致
    assert paper.load_positions(tmp_path)[SYM]["qty"] == 1000
    _positions, cash = paper.replay_positions(tmp_path)
    assert paper.get_account(tmp_path)["cash"] == pytest.approx(cash)
    assert paper.reconcile_account(tmp_path)["ok"] is True


def test_reconcile_detects_corp_chain_tamper_without_repair(tmp_path):
    day = date(2026, 9, 24)
    paper.create_account(tmp_path, 1_000_000.0)
    _write_daily(tmp_path, [
        (day - timedelta(days=1), 10.0, 10.0),
        (day, 8.0, 8.0),
    ])
    paper.create_order(tmp_path, SYM, "buy", qty=1000, order_type="close", ref_price=10.0)
    paper.settle_day(tmp_path, (day - timedelta(days=1)).isoformat())
    _write_factor(tmp_path, day, 1.25)
    paper.settle_day(tmp_path, day.isoformat())

    # 篡改台账 corp_action 行的 qty_before (append-only: 只应检出, 不应改写)
    fills_file = paper._root(tmp_path) / "fills.jsonl"
    lines = [json.loads(line) for line in fills_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    before = fills_file.read_text(encoding="utf-8")
    for row in lines:
        if row.get("kind") == "corp_action":
            row["qty_before"] = 123.0
    fills_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n", encoding="utf-8")

    rec = paper.reconcile_account(tmp_path)
    assert rec["ok"] is False and rec["fixed"] is False
    assert any("corp_action 链" in i and "qty_before" in i for i in rec["issues"])
    # 快照本身一致 → 不触发重建; 台账保持篡改后原样 (对账不改写历史)
    assert fills_file.read_text(encoding="utf-8") != before
    tampered = fills_file.read_text(encoding="utf-8")
    assert paper.reconcile_account(tmp_path)["fixed"] is False
    assert fills_file.read_text(encoding="utf-8") == tampered


def test_settle_day_summary_has_p1_14_keys(tmp_path):
    day = date(2026, 9, 24)
    paper.create_account(tmp_path, 1_000_000.0)
    _write_daily(tmp_path, [(day, 10.0, 10.0)])
    summary = paper.settle_day(tmp_path, day.isoformat())
    assert summary["kill_switch"] is False
    assert summary["archived"] == 0
    assert summary["reconcile"] == {"ok": True, "fixed": False, "issues": []}
