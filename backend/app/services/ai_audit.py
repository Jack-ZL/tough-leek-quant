"""P2-23 AI 分析不可篡改账本 (append-only audit ledger)。

每次 LLM 分析 (个股分析/多空辩论/AI 复盘/AI 财务分析) 记一条 run 条目:
当时喂给模型的每份原始数据 (快照 + sha256 指纹)、prompt 哈希、模型归属、
产出正文哈希; 报告落库时再追加一条 link 条目绑定 report_id。

不可篡改的实现口径 = **篡改可发现** (文件系统层做不到真不可变):
- ledger.jsonl 只追加 (O_APPEND + 单行写 + fsync), 无改写/删除 API;
- 每行带 prev_hash + 自身 hash (sha256 规范化 JSON), 改任何一行即断链;
- verify_ledger() 全链重算 + 快照指纹复核, 一键发现篡改;
- 快照保留期 (默认 90 天, env TSP_AI_AUDIT_RETAIN_DAYS) 过期只删快照文件,
  账本行永不改动, 读取方按文件存在性显示「快照已过期」。

fail-open 总原则: 审计链路任何异常只记日志, 绝不影响分析/保存主流程
(与 P0-7 记分卡、P1-11 数据体检同口径)。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LEDGER_NAME = "ledger.jsonl"
_DEFAULT_RETAIN_DAYS = 90
_APPEND_LOCK = threading.Lock()

RUN_KINDS = ("stock_analysis", "debate", "market_recap", "financial")


def snapshot_retain_days() -> int:
    raw = os.environ.get("TSP_AI_AUDIT_RETAIN_DAYS", "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_RETAIN_DAYS
    except ValueError:
        return _DEFAULT_RETAIN_DAYS


def _root(data_dir: Path) -> Path:
    return Path(data_dir) / "ai_audit"


def _ledger_path(data_dir: Path) -> Path:
    return _root(data_dir) / _LEDGER_NAME


def _snap_path(data_dir: Path, audit_id: str, name: str) -> Path:
    return _root(data_dir) / "snapshots" / audit_id / f"{name}.json.gz"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_lines(data_dir: Path) -> list[dict]:
    path = _ledger_path(data_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:  # 半行/手工破坏 → 原样保留供 verify 报错
                out.append({"__broken__": line})
    return out


def _entry_hash(entry: dict) -> str:
    return sha256_text(_canonical({k: v for k, v in entry.items() if k != "hash"}))


def _append(data_dir: Path, body: dict) -> dict:
    """追加一行账本 (调用方持锁): 填 seq/prev_hash/hash 后单行写入 + fsync。"""
    with _APPEND_LOCK:
        lines = _read_lines(data_dir)
        last = lines[-1] if lines and "__broken__" not in lines[-1] else None
        seq = int(last.get("seq", 0)) + 1 if last else 1
        entry = {**body, "seq": seq, "prev_hash": str(last.get("hash", "")) if last else ""}
        entry["hash"] = _entry_hash(entry)
        path = _ledger_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry


def begin_run(data_dir: Path, kind: str, symbol: str) -> str:
    """开一次分析run: 建快照目录并返回 audit_id (meta 事件下发给前端)。"""
    audit_id = uuid.uuid4().hex
    try:
        (_root(data_dir) / "snapshots" / audit_id).mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — fail-open (含 data_dir=None/不可写)
        logger.warning("[ai_audit] 快照目录创建失败: %s", audit_id, exc_info=True)
    return audit_id


def add_input(data_dir: Path, audit_id: str, name: str, payload: Any,
              as_of: str = "", rows: int | None = None) -> dict:
    """登记一份输入: 算 sha256 指纹 + 写一次 gzip 快照, 返回账本 inputs 元素。"""
    canon = _canonical(payload)
    digest = sha256_text(canon)
    ref = f"snapshots/{audit_id}/{name}.json.gz"
    try:
        path = _snap_path(data_dir, audit_id, name)
        if not path.exists():  # 写一次不覆盖
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wt", encoding="utf-8") as f:
                f.write(canon)
    except Exception:  # noqa: BLE001 — fail-open: 指纹照记, 快照缺失由读取方标注
        logger.warning("[ai_audit] 快照写入失败: %s/%s", audit_id, name, exc_info=True)
    if rows is None:
        rows = len(payload) if isinstance(payload, (list, dict)) else None
    return {"name": name, "as_of": as_of, "rows": rows, "sha256": digest, "snapshot_ref": ref}


def finish_run(data_dir: Path, *, audit_id: str, kind: str, symbol: str,
               inputs: list[dict], prompt_text: str, output_text: str,
               model: str = "", provider: str = "", profile_id: str = "") -> dict | None:
    """流结束写 run 条目 (产出哈希由后端算, 不依赖前端)。fail-open。"""
    try:
        return _append(data_dir, {
            "type": "run", "ts": _now_iso(), "audit_id": audit_id, "kind": kind,
            "symbol": symbol, "model": model, "provider": provider, "profile_id": profile_id,
            "inputs": inputs,
            "prompt_sha256": sha256_text(prompt_text),
            "output_sha256": sha256_text(output_text),
            "output_chars": len(output_text),
        })
    except Exception:  # noqa: BLE001
        logger.warning("[ai_audit] run 条目写入失败: %s", audit_id, exc_info=True)
        return None


def link_report(data_dir: Path, audit_id: str, report_id: str, kind: str = "") -> dict | None:
    """报告落库时追加 link 条目 (追加而非改写 run 条目)。fail-open。"""
    if not audit_id:
        return None
    try:
        return _append(data_dir, {
            "type": "link", "ts": _now_iso(), "audit_id": audit_id,
            "report_id": report_id, "kind": kind,
        })
    except Exception:  # noqa: BLE001
        logger.warning("[ai_audit] link 条目写入失败: %s→%s", audit_id, report_id, exc_info=True)
        return None


def append_checkpoint(data_dir: Path, today: date | None = None) -> dict | None:
    """每日 checkpoint 条目: 当日 run 条数 + 链尾哈希, 便于快速验尾。fail-open。"""
    try:
        day = today or date.today()
        prefix = day.isoformat()
        runs = [e for e in _read_lines(data_dir)
                if e.get("type") == "run" and str(e.get("ts", "")).startswith(prefix)]
        return _append(data_dir, {
            "type": "checkpoint", "ts": _now_iso(), "day": prefix,
            "runs_today": len(runs),
        })
    except Exception:  # noqa: BLE001
        logger.warning("[ai_audit] checkpoint 写入失败", exc_info=True)
        return None


def list_entries(data_dir: Path, symbol: str | None = None, kind: str | None = None,
                 limit: int = 50) -> list[dict]:
    """倒序返回账本条目 (run/link/checkpoint 混合, 可按 symbol/kind 过滤)。"""
    out = [e for e in _read_lines(data_dir) if "__broken__" not in e]
    if symbol:
        out = [e for e in out if e.get("symbol") == symbol]
    if kind:
        out = [e for e in out if e.get("kind") == kind or e.get("type") == kind]
    out.reverse()
    return out[:max(1, limit)]


def get_entry(data_dir: Path, audit_id: str) -> dict | None:
    """取该 audit_id 最后一条 run 条目。"""
    for e in reversed(_read_lines(data_dir)):
        if e.get("audit_id") == audit_id and e.get("type") == "run":
            return e
    return None


def links_of(data_dir: Path, audit_id: str) -> list[dict]:
    return [e for e in _read_lines(data_dir)
            if e.get("audit_id") == audit_id and e.get("type") == "link"]


def entry_detail(data_dir: Path, audit_id: str) -> dict | None:
    """run 条目 + 每份输入的快照状态 + link 绑定。"""
    entry = get_entry(data_dir, audit_id)
    if entry is None:
        return None
    inputs = []
    for item in entry.get("inputs", []):
        path = _snap_path(data_dir, audit_id, item.get("name", ""))
        state = "ok" if path.exists() else "expired"
        inputs.append({**item, "snapshot_state": state})
    return {**entry, "inputs": inputs, "links": links_of(data_dir, audit_id)}


def read_snapshot(data_dir: Path, audit_id: str, name: str) -> Any | None:
    path = _snap_path(data_dir, audit_id, name)
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def verify_ledger(data_dir: Path, snapshot_limit: int = 200) -> dict:
    """全链校验: 行哈希/prev 链接/seq 连续 + 存量快照指纹复核。"""
    errors: list[str] = []
    lines = _read_lines(data_dir)
    prev_hash = ""
    expected_seq = 1
    checked = 0
    for i, e in enumerate(lines):
        if "__broken__" in e:
            errors.append(f"line {i + 1}: JSON 解析失败 (疑似截断/篡改)")
            continue
        checked += 1
        if int(e.get("seq", -1)) != expected_seq:
            errors.append(f"seq {e.get('seq')}: 不连续 (期望 {expected_seq})")
        if str(e.get("prev_hash", "")) != prev_hash:
            errors.append(f"seq {e.get('seq')}: prev_hash 断链")
        if _entry_hash(e) != e.get("hash"):
            errors.append(f"seq {e.get('seq')}: 行哈希不符 (内容被改)")
        prev_hash = str(e.get("hash", ""))
        expected_seq += 1

    snap_checked = 0
    missing = 0
    for e in reversed(lines):
        if e.get("type") != "run" or snap_checked >= snapshot_limit:
            continue
        for item in e.get("inputs", []):
            path = _snap_path(data_dir, e.get("audit_id", ""), item.get("name", ""))
            if not path.exists():
                missing += 1
                continue
            snap_checked += 1
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    digest = sha256_text(f.read())
            except OSError:
                errors.append(f"{e.get('audit_id')}/{item.get('name')}: 快照不可读")
                continue
            if digest != item.get("sha256"):
                errors.append(f"{e.get('audit_id')}/{item.get('name')}: 快照指纹不符 (文件被改)")
    return {
        "ok": not errors, "checked": checked,
        "snapshots_checked": snap_checked, "snapshots_missing": missing,
        "retain_days": snapshot_retain_days(),
        "errors": errors[:50],
    }


def _current_payload(data_dir: Path, repo: Any, entry: dict, name: str) -> tuple[bool, Any]:
    """按输入名重取当前数据 (仅登记了重取器的名字可比对)。"""
    symbol = entry.get("symbol", "")
    if name == "kline":
        from app.services.stock_analyzer import _KLINE_KEEP_COLS, _clean_rows, _load_kline
        df = _load_kline(repo, symbol)
        if df.is_empty():
            return True, None
        return True, _clean_rows(df, _KLINE_KEEP_COLS)
    if name == "financials":
        from app.services.stock_analyzer import _load_financials
        return True, _load_financials(data_dir, symbol)
    return False, None


def recompare_input(data_dir: Path, repo: Any, audit_id: str, name: str) -> dict:
    """某输入的当时指纹 vs 当前数据源重算指纹 — 回答「数据后来变没变」。"""
    entry = get_entry(data_dir, audit_id)
    if entry is None:
        return {"ok": False, "error": "audit_id 不存在"}
    stored = next((i for i in entry.get("inputs", []) if i.get("name") == name), None)
    if stored is None:
        return {"ok": False, "error": f"该 run 无输入 {name}"}
    recomparable, current = _current_payload(data_dir, repo, entry, name)
    if not recomparable:
        return {"ok": True, "recomparable": False,
                "note": "该输入为时点上下文 (如体检/战绩), 不做当前重算比对"}
    if current is None:
        return {"ok": True, "recomparable": True, "current_available": False,
                "stored_sha256": stored.get("sha256")}
    cur_sha = sha256_text(_canonical(current))
    return {"ok": True, "recomparable": True, "current_available": True,
            "stored_sha256": stored.get("sha256"), "current_sha256": cur_sha,
            "match": cur_sha == stored.get("sha256"), "stored_as_of": stored.get("as_of")}


def cleanup_snapshots(data_dir: Path, today: date | None = None,
                      retain_days: int | None = None) -> int:
    """删除过期快照目录 (只删文件, 账本行不动)。返回删除目录数。"""
    day = today or date.today()
    retain = retain_days if retain_days is not None else snapshot_retain_days()
    cutoff = day - timedelta(days=retain)
    root = _root(data_dir) / "snapshots"
    if not root.exists():
        return 0
    removed = 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        mtime = datetime.fromtimestamp(d.stat().st_mtime).date()
        if mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("[ai_audit] 清理过期快照目录 %d 个 (cutoff %s)", removed, cutoff)
    return removed
