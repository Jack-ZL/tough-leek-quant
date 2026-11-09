"""公式进化 HTTP API (P2-22a): 启动 / 列表 / 详情 / 取消 / 幸存者发布。

复用 MiningJobManager (kind="evolution") 与 MiningRunStore (namespace="evolution"),
进程模型、取消、恢复与挖掘/守护一致。幸存者发布走自定义因子存储
(uf_evo_* 草稿), 之后由既有挖掘/自动挖掘漏斗做深度回测验证。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.factors import store as factor_store
from app.services.mining_jobs import (
    RUN_STATUSES,
    SUCCESS_RUN_STATUSES,
    MiningRunStoreError,
    MiningRunValidationError,
)
from app.services.mining_preflight import enriched_partition_dates

router = APIRouter(prefix="/api/evolution", tags=["backtest"])

# 训练窗 ≥100 + 保留窗 ≥20 交易日, 留余量取 150 个 enriched 分区
MIN_WINDOW_PARTITIONS = 150
# 未显式给 start 时的默认回看跨度 (日历日, ~4 年)
DEFAULT_LOOKBACK_DAYS = 1460
_PUBLISH_GROUP = "进化"


def _manager(request: Request):
    manager = getattr(request.app.state, "evolution_manager", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="evolution manager 不可用")
    return manager


def _data_dir(request: Request) -> Path:
    store = getattr(request.app.state, "repo", None)
    root = getattr(getattr(store, "store", None), "data_dir", None)
    if root is None:
        raise HTTPException(status_code=500, detail="数据目录不可用")
    return Path(root)


class EvolutionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    symbols: list[str] | None = None
    asset_type: Literal["stock", "etf"] = "stock"
    start: date | None = None
    end: date | None = None
    population_size: int = Field(default=120, ge=20, le=400)
    generations: int = Field(default=12, ge=2, le=40)
    horizon: int = Field(default=5, ge=1, le=20)
    max_symbols: int = Field(default=600, ge=50, le=2000)
    max_depth: int = Field(default=4, ge=2, le=6)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    llm_propose: bool = False
    llm_count: int = Field(default=12, ge=5, le=30)
    force: bool = False

    @field_validator("symbols")
    @classmethod
    def _check_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("symbols 不能为空列表")
        if len(value) > 10000:
            raise ValueError("symbols 数量不能超过 10000")
        unique = list(dict.fromkeys(value))
        if len(unique) != len(value):
            raise ValueError("symbols 存在重复")
        return unique


class EvolutionPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str | None = Field(default=None, min_length=1, max_length=32)
    description: str = Field(default="", max_length=500)


def _project_run(store: Any, manifest: Mapping[str, Any], *, full: bool = False) -> dict[str, Any]:
    run_id = str(manifest["run_id"])
    status = str(manifest["status"])
    projected: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "request": manifest.get("request") or {},
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "error": manifest.get("error"),
    }
    summary = store.read_summary(run_id)
    if status in SUCCESS_RUN_STATUSES and isinstance(summary, Mapping):
        if full:
            projected["result"] = dict(summary)
        else:
            projected["trials"] = summary.get("trials")
            projected["panel"] = summary.get("panel")
            projected["survivor_count"] = len(summary.get("survivors") or [])
            projected["elapsed_ms"] = summary.get("elapsed_ms")
    elif isinstance(summary.get("progress"), Mapping):
        projected["progress"] = summary["progress"]
    return projected


@router.post("/runs")
def start_run(payload: EvolutionStartRequest, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    data_dir = _data_dir(request)
    partitions = enriched_partition_dates(data_dir, payload.asset_type)
    if not partitions:
        raise HTTPException(status_code=409, detail="无 enriched 行情数据, 请先完成盘后数据更新")
    end = payload.end or partitions[-1]
    start = payload.start or max(partitions[0], end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    if start >= end:
        raise HTTPException(status_code=400, detail="start 必须早于 end")
    in_range = [value for value in partitions if start <= value <= end]
    if len(in_range) < MIN_WINDOW_PARTITIONS:
        raise HTTPException(
            status_code=409,
            detail=f"数据不足: 区间内仅 {len(in_range)} 个交易日分区, 至少需要 {MIN_WINDOW_PARTITIONS} 个",
        )
    worker_request = payload.model_dump(mode="json", exclude={"force"})
    worker_request["start"] = start.isoformat()
    worker_request["end"] = end.isoformat()
    fingerprint = {
        "asset_type": payload.asset_type,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "partitions": len(in_range),
        "last_partition": in_range[-1].isoformat(),
    }
    try:
        manifest = manager.start(worker_request, fingerprint, force=payload.force, source="manual")
    except (MiningRunValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MiningRunStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to create evolution run") from exc
    return _project_run(manager.store, manifest)


@router.get("/runs")
def list_runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    status: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    manager = _manager(request)
    statuses = None
    if status:
        unknown = sorted(set(status) - RUN_STATUSES)
        if unknown:
            raise HTTPException(status_code=400, detail=f"unsupported evolution statuses: {unknown}")
        statuses = status
    try:
        manifests = manager.store.list_runs(limit=limit, statuses=statuses)
    except MiningRunValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MiningRunStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to read evolution runs") from exc
    return {"items": [_project_run(manager.store, item) for item in manifests]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    manifest = manager.store.get(run_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"evolution run not found: {run_id}")
    return _project_run(manager.store, manifest, full=True)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    try:
        manifest = manager.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"evolution run not found: {run_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _project_run(manager.store, manifest)


def _survivor_factor_id(key: str) -> str:
    digest = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:12]
    return f"uf_evo_{digest}"


@router.post("/runs/{run_id}/survivors/{index}/publish")
def publish_survivor(
    run_id: str,
    index: int,
    payload: EvolutionPublishRequest,
    request: Request,
) -> dict[str, Any]:
    """把幸存者发布为 uf_evo_* 自定义因子草稿 (幂等: 同公式只发布一次)。"""
    manager = _manager(request)
    manifest = manager.store.get(run_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"evolution run not found: {run_id}")
    if manifest["status"] not in SUCCESS_RUN_STATUSES:
        raise HTTPException(status_code=409, detail=f"运行未成功 (status={manifest['status']}), 无可发布幸存者")
    summary = manager.store.read_summary(run_id)
    survivors = summary.get("survivors") or []
    if not 0 <= index < len(survivors):
        raise HTTPException(status_code=404, detail=f"幸存者序号越界: {index} (共 {len(survivors)} 个)")
    survivor = survivors[index]
    formula = str(survivor.get("formula") or "")
    key = str(survivor.get("key") or formula)
    if not formula:
        raise HTTPException(status_code=422, detail="幸存者缺少公式文本")

    data_dir = _data_dir(request)
    factor_id = _survivor_factor_id(key)
    for definition in factor_store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            raise HTTPException(
                status_code=409,
                detail=f"该公式已发布过: {factor_id} (version {definition.get('version')})",
            )

    holdout = survivor.get("holdout") or {}
    trials = summary.get("trials") or {}
    label = (payload.label or "").strip() or f"进化因子-{factor_id[-6:]}"
    description = (payload.description or "").strip() or (
        f"P2-22a 公式进化产物 | run {run_id[:8]} | 方向 {survivor.get('direction')} | "
        f"holdout IC {holdout.get('ic_mean')} IR {holdout.get('ir')} "
        f"t {holdout.get('t_nw')} q {holdout.get('q_value')} DSR {holdout.get('dsr')} | "
        f"试验次数 {trials.get('evaluated')}"
    )[:500]
    definition = {
        "id": factor_id,
        "kind": "custom",
        "version": 1,
        "label": label[:32],
        "group": _PUBLISH_GROUP,
        "formula": formula,
        "description": description,
        "direction": survivor.get("direction") or "none",
        "lang": "dsl",
        "status": "draft",
        "created_at": factor_store._now(),
        "updated_at": factor_store._now(),
    }
    try:
        factor_store.to_spec(definition)  # schema/id/编译校验 (fail-closed)
        factor_store.register_definition(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    factor_store.save_one(data_dir, definition)
    return {"ok": True, "id": factor_id, "version": 1, "label": definition["label"]}
