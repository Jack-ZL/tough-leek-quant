# P2-23 AI 分析不可篡改账本 — 链完整性/篡改发现/fail-open/重算比对/保留期清理。
# 口径: 账本 = 篡改可发现 (哈希链 + 快照指纹复核), 快照过期只删文件、账本行永不改。
from __future__ import annotations

import gzip
import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services import ai_audit

# ===== 工具 =====

def _ledger(data_dir: Path) -> Path:
    return data_dir / "ai_audit" / "ledger.jsonl"


def _run_once(data_dir: Path, symbol: str = "600519.SH", kind: str = "stock_analysis",
              inputs_payload: dict | None = None) -> str:
    """完整走一遍 begin → add_input → finish → link, 返回 audit_id。"""
    audit_id = ai_audit.begin_run(data_dir, kind, symbol)
    inputs = []
    for name, payload in (inputs_payload or {"kline": [{"date": "2026-02-04", "close": 10.0}]}).items():
        inputs.append(ai_audit.add_input(data_dir, audit_id, name, payload, as_of="2026-02-04"))
    entry = ai_audit.finish_run(
        data_dir, audit_id=audit_id, kind=kind, symbol=symbol, inputs=inputs,
        prompt_text="sys\n\nuser", output_text="报告正文", model="m1", provider="p1",
        profile_id="prof1",
    )
    assert entry is not None
    ai_audit.link_report(data_dir, audit_id, "rpt_001", kind=kind)
    return audit_id


# ===== 链完整性 / 读取 =====

def test_chain_integrity_and_reads(tmp_path: Path):
    audit_id = _run_once(tmp_path)
    result = ai_audit.verify_ledger(tmp_path)
    assert result["ok"] is True, result["errors"]
    assert result["checked"] == 2  # run + link
    assert result["snapshots_checked"] == 1

    # seq 连续 1..2, prev_hash 链接
    lines = [json.loads(x) for x in _ledger(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in lines] == [1, 2]
    assert lines[0]["prev_hash"] == ""
    assert lines[1]["prev_hash"] == lines[0]["hash"]

    # run 条目字段
    entry = ai_audit.get_entry(tmp_path, audit_id)
    assert entry is not None and entry["type"] == "run"
    assert entry["model"] == "m1" and entry["profile_id"] == "prof1"
    assert entry["output_chars"] == len("报告正文")
    assert entry["prompt_sha256"] == ai_audit.sha256_text("sys\n\nuser")

    # link 绑定
    links = ai_audit.links_of(tmp_path, audit_id)
    assert len(links) == 1 and links[0]["report_id"] == "rpt_001"

    # detail: 快照状态 ok
    detail = ai_audit.entry_detail(tmp_path, audit_id)
    assert detail["inputs"][0]["snapshot_state"] == "ok"
    assert detail["links"][0]["report_id"] == "rpt_001"

    # 快照内容可回读 (与登记 payload 一致)
    snap = ai_audit.read_snapshot(tmp_path, audit_id, "kline")
    assert snap == [{"date": "2026-02-04", "close": 10.0}]

    # list_entries 过滤 (link 条目不带 symbol, 按 symbol 过滤只命中 run)
    assert len(ai_audit.list_entries(tmp_path, symbol="600519.SH")) == 1
    assert ai_audit.list_entries(tmp_path, symbol="000001") == []
    assert len(ai_audit.list_entries(tmp_path, kind="stock_analysis")) == 2
    assert ai_audit.list_entries(tmp_path)[0]["type"] == "link"  # 倒序


def test_checkpoint(tmp_path: Path):
    _run_once(tmp_path)
    cp = ai_audit.append_checkpoint(tmp_path, date(2000, 1, 1))
    assert cp is not None and cp["type"] == "checkpoint"
    # run 的 ts 是真实当前时间, 与伪造的 today 不同日 → runs_today=0
    assert cp["runs_today"] == 0
    assert ai_audit.verify_ledger(tmp_path)["ok"] is True


# ===== 篡改可发现 =====

def test_tamper_ledger_line_detected(tmp_path: Path):
    _run_once(tmp_path)
    path = _ledger(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["output_chars"] = 99999  # 改一个字段
    lines[0] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = ai_audit.verify_ledger(tmp_path)
    assert result["ok"] is False
    assert any("行哈希不符" in e for e in result["errors"])


def test_tamper_snapshot_detected(tmp_path: Path):
    audit_id = _run_once(tmp_path)
    snap = tmp_path / "ai_audit" / "snapshots" / audit_id / "kline.json.gz"
    with gzip.open(snap, "wt", encoding="utf-8") as f:
        f.write('[{"date":"2026-02-04","close":999.0}]')  # 偷换原始数据

    result = ai_audit.verify_ledger(tmp_path)
    assert result["ok"] is False
    assert any("快照指纹不符" in e for e in result["errors"])


def test_broken_line_and_seq_gap_detected(tmp_path: Path):
    _run_once(tmp_path)
    _run_once(tmp_path, symbol="000001")
    path = _ledger(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    # 删掉第 2 行 (seq 断档) + 追加半行垃圾 (解析失败)
    kept = [lines[0], *lines[2:]]
    path.write_text("\n".join(kept) + "\n{not-json\n", encoding="utf-8")

    result = ai_audit.verify_ledger(tmp_path)
    assert result["ok"] is False
    assert any("不连续" in e for e in result["errors"])
    assert any("JSON 解析失败" in e for e in result["errors"])


# ===== fail-open / 写一次 =====

def test_fail_open_on_unwritable_dir(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("占位文件", encoding="utf-8")
    data_dir = blocker / "sub"  # mkdir 必失败 (父是文件)

    audit_id = ai_audit.begin_run(data_dir, "stock_analysis", "600519.SH")
    assert audit_id  # 仍返回 id, 不抛
    item = ai_audit.add_input(data_dir, audit_id, "kline", [{"close": 1.0}])
    assert item["sha256"]  # 指纹照记
    assert ai_audit.finish_run(
        data_dir, audit_id=audit_id, kind="stock_analysis", symbol="600519.SH",
        inputs=[item], prompt_text="p", output_text="o",
    ) is None  # 写失败 → None, 不抛
    assert ai_audit.link_report(data_dir, audit_id, "rpt") is None
    assert ai_audit.append_checkpoint(data_dir) is None


def test_link_report_empty_audit_id_noop(tmp_path: Path):
    assert ai_audit.link_report(tmp_path, "", "rpt") is None
    assert not _ledger(tmp_path).exists()


def test_snapshot_write_once(tmp_path: Path):
    audit_id = ai_audit.begin_run(tmp_path, "stock_analysis", "600519.SH")
    first = ai_audit.add_input(tmp_path, audit_id, "kline", [{"close": 1.0}])
    second = ai_audit.add_input(tmp_path, audit_id, "kline", [{"close": 2.0}])
    assert first["sha256"] != second["sha256"]  # 指纹各算各的
    # 快照文件不被覆盖: 回读仍是第一次的内容
    assert ai_audit.read_snapshot(tmp_path, audit_id, "kline") == [{"close": 1.0}]


def test_concurrent_appends_keep_chain(tmp_path: Path):
    def worker(i: int):
        aid = ai_audit.begin_run(tmp_path, "stock_analysis", f"SYM{i}")
        ai_audit.link_report(tmp_path, aid, f"rpt_{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result = ai_audit.verify_ledger(tmp_path)
    assert result["ok"] is True, result["errors"]
    assert result["checked"] == 8
    seqs = [e["seq"] for e in ai_audit._read_lines(tmp_path)]
    assert seqs == list(range(1, 9))


# ===== 重算比对 =====

class _FakeRepo:
    pass


def test_recompare_financials_match_and_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    payload = {"income": [{"revenue": 100.0}]}
    audit_id = _run_once(tmp_path, inputs_payload={"financials": payload})

    from app.services import stock_analyzer
    monkeypatch.setattr(stock_analyzer, "_load_financials", lambda data_dir, symbol: payload)
    result = ai_audit.recompare_input(tmp_path, _FakeRepo(), audit_id, "financials")
    assert result["ok"] and result["recomparable"] and result["match"] is True

    # 数据源后来变了 → 指纹不一致
    monkeypatch.setattr(stock_analyzer, "_load_financials",
                        lambda data_dir, symbol: {"income": [{"revenue": 200.0}]})
    result = ai_audit.recompare_input(tmp_path, _FakeRepo(), audit_id, "financials")
    assert result["match"] is False
    assert result["current_sha256"] != result["stored_sha256"]


def test_recompare_current_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audit_id = _run_once(tmp_path, inputs_payload={"financials": {"income": []}})
    from app.services import stock_analyzer
    monkeypatch.setattr(stock_analyzer, "_load_financials", lambda data_dir, symbol: None)
    result = ai_audit.recompare_input(tmp_path, _FakeRepo(), audit_id, "financials")
    assert result["ok"] and result["current_available"] is False


def test_recompare_not_recomparable_and_missing(tmp_path: Path):
    audit_id = _run_once(tmp_path, inputs_payload={"quality": {"score": 90}})
    result = ai_audit.recompare_input(tmp_path, _FakeRepo(), audit_id, "quality")
    assert result["ok"] and result["recomparable"] is False

    result = ai_audit.recompare_input(tmp_path, _FakeRepo(), audit_id, "no_such_input")
    assert result["ok"] is False
    result = ai_audit.recompare_input(tmp_path, _FakeRepo(), "missing_id", "kline")
    assert result["ok"] is False


# ===== 保留期清理 =====

def test_cleanup_snapshots_retention_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TSP_AI_AUDIT_RETAIN_DAYS", raising=False)
    old_id = _run_once(tmp_path, symbol="OLD")
    new_id = _run_once(tmp_path, symbol="NEW")
    today = date.today()  # 以真实当天为基准, 新快照 mtime≈today 必在保留期内

    # 把 old 的快照目录 mtime 拨到 91 天前 (超 90 天保留期)
    old_dir = tmp_path / "ai_audit" / "snapshots" / old_id
    stale_ts = time.mktime((today - timedelta(days=91)).timetuple())
    os.utime(old_dir, (stale_ts, stale_ts))

    removed = ai_audit.cleanup_snapshots(tmp_path, today)
    assert removed == 1
    assert not old_dir.exists()
    assert (tmp_path / "ai_audit" / "snapshots" / new_id).exists()

    # 账本行不动: 链仍完整, 过期快照按 missing 计数、detail 标 expired
    result = ai_audit.verify_ledger(tmp_path)
    assert result["ok"] is True
    assert result["snapshots_missing"] == 1
    detail = ai_audit.entry_detail(tmp_path, old_id)
    assert detail["inputs"][0]["snapshot_state"] == "expired"
    assert ai_audit.read_snapshot(tmp_path, old_id, "kline") is None


def test_retain_days_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TSP_AI_AUDIT_RETAIN_DAYS", "7")
    assert ai_audit.snapshot_retain_days() == 7
    monkeypatch.setenv("TSP_AI_AUDIT_RETAIN_DAYS", "abc")
    assert ai_audit.snapshot_retain_days() == 90
    monkeypatch.setenv("TSP_AI_AUDIT_RETAIN_DAYS", "0")
    assert ai_audit.snapshot_retain_days() == 1  # 下限 1 天
    monkeypatch.delenv("TSP_AI_AUDIT_RETAIN_DAYS")
    assert ai_audit.snapshot_retain_days() == 90
