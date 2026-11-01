"""P1-12 layer3: 批量任务失败清单 (重跑manifest) + 备源重跑执行。

大白话: 日K/除权因子这类**落库**任务不做运行时自动切源 (中途换源会造成
数据混杂, 且落库任务失败往往意味着主源整体不可用, 自动重试只是浪费配额)。
取而代之:
  1. 任务里部分标的硬失败 (超时/异常) → 记进本清单文件 (任务状态标记),
     并汇入 stage_errors 让任务终态如实标 failed;
  2. 用户在数据页看到「用备源重跑」按钮 → 手动触发, 只重拉清单里的
     失败标的, 且**整个重跑任务只用备源** (原子性: 不中途混源);
  3. 重跑仍失败的标的留在清单里, 可再试或忽略 (忽略 = 清掉标记)。

清单文件: {data_dir}/rerun_manifest.json
  {"daily":      {"symbols": [...], "source": "主源名", "recorded_at": "..."},
   "adj_factor": {...}}
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# 支持备源重跑的数据集 (分钟/财务失败模式不同, 暂不纳入 — 见 CONTRIBUTING 分层保险)
RERUN_DATASETS = ("daily", "adj_factor")
# 单数据集清单上限: 超过说明主源大面积挂, 该走整管道重跑而非逐标的补
MAX_SYMBOLS = 3000
# 重跑取数窗口: 日K 补最近 30 天 (缺口通常就在近几天, 幂等 merge 多拉无害);
# 除权因子事件稀疏, 窗口放宽到 120 天
RERUN_WINDOW_DAYS = {"daily": 30, "adj_factor": 120}

_LOCK = threading.Lock()


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "rerun_manifest.json"


def load(data_dir: Path) -> dict:
    """读清单; 文件不存在/损坏 → 空清单 (损坏不该阻塞任何任务)。"""
    p = _path(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("重跑清单读取失败 (按空处理): %s", e)
        return {}


def record_failures(data_dir: Path, dataset: str, symbols: list[str], source: str) -> None:
    """把硬失败标的记进清单 (同数据集并集合并, 去重保序)。"""
    if dataset not in RERUN_DATASETS or not symbols:
        return
    with _LOCK:
        manifest = load(data_dir)
        entry = manifest.get(dataset) or {}
        existing = list(entry.get("symbols") or [])
        seen = set(existing)
        for s in symbols:
            if s not in seen:
                seen.add(s)
                existing.append(s)
        truncated = existing[:MAX_SYMBOLS]
        if len(existing) > MAX_SYMBOLS:
            logger.warning(
                "重跑清单 %s 超过 %d 只, 截断 (主源可能大面积故障, 建议整管道重跑)",
                dataset, MAX_SYMBOLS,
            )
        manifest[dataset] = {
            "symbols": truncated,
            "source": source,  # 失败时用的主源 (展示用)
            "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write(data_dir, manifest)
    logger.info("重跑清单: %s 记录 %d 只失败标的 (累计 %d, 主源 %s)",
                dataset, len(symbols), len(truncated), source)


def clear(data_dir: Path, dataset: str, symbols: list[str] | None = None) -> None:
    """清清单: symbols=None 整个数据集清掉 (用户忽略/重跑前重置), 否则只减掉指定标的。"""
    with _LOCK:
        manifest = load(data_dir)
        entry = manifest.get(dataset)
        if entry is None:
            return
        if symbols is None:
            manifest.pop(dataset, None)
        else:
            drop = set(symbols)
            remain = [s for s in (entry.get("symbols") or []) if s not in drop]
            if remain:
                entry["symbols"] = remain
            else:
                manifest.pop(dataset, None)
        _write(data_dir, manifest)


def _write(data_dir: Path, manifest: dict) -> None:
    from app.services.fs_utils import atomic_write_text

    atomic_write_text(
        _path(data_dir), json.dumps(manifest, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# 备源解析 + 重跑执行
# ---------------------------------------------------------------------------

def resolve_backup(dataset: str) -> str | None:
    """给数据集挑重跑备源: 免费内置源, baostock 优先。

    baostock 优先的理由: 官方免费接口稳定、无 IP 封禁前科 (东财 push2 高频
    易封禁, 且封禁池与实时/日K主链路共享, 重跑再打它可能雪上加霜);
    exclude 当前主源 — 主源刚失败, 备源不能再选它自己。
    """
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    if dataset == "daily":
        primary = preferences.get_daily_data_provider()
    elif dataset == "adj_factor":
        primary = preferences.get_adj_factor_provider()
    else:
        return None
    return custom_sources.find_complement_provider(
        dataset, exclude=primary, prefer=("baostock",)
    )


def run_backup_rerun(
    repo,
    capset,
    dataset: str,
    on_progress=None,
) -> dict:
    """用备源重跑清单里的失败标的 (同步执行, 调用方负责放后台 job)。

    原子性: 整个任务只用 provider_override 指定的备源, 内部不做任何回退/
    混源; 仍失败的标的重新记回清单。返回结果 dict 供 job_store.succeed。
    """
    from app.services import kline_sync

    if dataset not in RERUN_DATASETS:
        return {"error": f"不支持的数据集: {dataset}"}
    data_dir = repo.store.data_dir
    entry = load(data_dir).get(dataset) or {}
    symbols = list(entry.get("symbols") or [])
    if not symbols:
        return {"error": "清单为空, 无需重跑"}
    backup = resolve_backup(dataset)
    if not backup:
        return {"error": "无可用备源 (需要免费内置源声明该数据集且依赖就绪)"}

    # 先清旧标记: 本轮仍失败的会由 failed_out 重新记回, 成功的自然消失
    clear(data_dir, dataset)

    emit = on_progress or (lambda *a, **k: None)
    end = datetime.now()
    start = end - timedelta(days=RERUN_WINDOW_DAYS[dataset])
    remaining: list[str] = []

    def _chunk(cur: int, tot: int) -> None:
        emit("backup_rerun", 10 + int(80 * cur / max(tot, 1)),
             f"备源重跑批次 {cur}/{tot}", stage_pct=int(100 * cur / max(tot, 1)),
             skip_log=True)

    emit("backup_rerun", 5, f"用备源 {backup} 重跑 {len(symbols)} 只 [{dataset}]…")
    logger.info("备源重跑: dataset=%s backup=%s symbols=%d window=[%s ~ %s]",
                dataset, backup, len(symbols), start.date(), end.date())

    if dataset == "daily":
        written = kline_sync.sync_and_persist_daily_batch(
            symbols, repo, capset,
            start_date=start, end_date=end,
            on_chunk_done=_chunk,
            failed_out=remaining,
            provider_override=backup,
        )
        result = {"dataset": dataset, "backup": backup, "requested": len(symbols),
                  "written_rows": written, "still_failed": len(remaining)}
    else:
        written, affected = kline_sync.sync_adj_factor(
            symbols, repo, capset,
            start_time=start, end_time=end,
            on_chunk_done=_chunk,
            failed_out=remaining,
            provider_override=backup,
        )
        if affected:
            # 因子有变化 → 刷 adj_factor 视图 (enriched 局部重算交给下次管道/手动重建,
            # 与管道 Step 1.5 后的联动一致口径, 重跑任务保持轻量)
            from app.jobs.daily_pipeline import _refresh_single_view
            _refresh_single_view(repo, "adj_factor")
        result = {"dataset": dataset, "backup": backup, "requested": len(symbols),
                  "written_rows": written, "affected": len(affected),
                  "still_failed": len(remaining)}

    if remaining:
        # 备源也救不回来的 → 重新记回清单 (source 记备源名, 前端如实展示)
        record_failures(data_dir, dataset, remaining, backup)
    emit("backup_rerun", 100, f"备源重跑完成: {result.get('written_rows', 0)} 行")
    logger.info("备源重跑完成: %s", result)
    return result
