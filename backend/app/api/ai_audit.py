"""AI 分析不可篡改账本 API (P2-23) — 倒查/校验/快照/重算比对。

账本本体见 app.services.ai_audit (append-only JSONL 哈希链 + gz 快照)。
所有端点走全局登录中间件保护, 不进开放平台白名单。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services import ai_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai_audit", tags=["ai-audit"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("/entries")
def list_entries(
    request: Request,
    symbol: str = Query(default="", description="按标的过滤 (复盘类为 as_of 日期)"),
    kind: str = Query(default="", description="run 类型: stock_analysis/debate/market_recap/financial"),
    limit: int = Query(default=50, ge=1, le=500),
):
    """倒序返回账本条目 (run/link/checkpoint 混合)。"""
    return {"entries": ai_audit.list_entries(
        _data_dir(request), symbol=symbol or None, kind=kind or None, limit=limit)}


@router.get("/entries/{audit_id}")
def entry_detail(request: Request, audit_id: str):
    """run 详情: 输入指纹+快照状态 (ok/expired) + 产出哈希 + link 绑定。"""
    detail = ai_audit.entry_detail(_data_dir(request), audit_id)
    if detail is None:
        raise HTTPException(404, f"audit_id 不存在: {audit_id}")
    return detail


@router.get("/entries/{audit_id}/snapshot/{name}")
def read_snapshot(request: Request, audit_id: str, name: str):
    """读取某输入的当时快照 (gz 解压后的原始 JSON); 过期/不存在返回 410/404。"""
    if ai_audit.get_entry(_data_dir(request), audit_id) is None:
        raise HTTPException(404, f"audit_id 不存在: {audit_id}")
    payload = ai_audit.read_snapshot(_data_dir(request), audit_id, name)
    if payload is None:
        raise HTTPException(410, f"快照已过期清理或不存在: {name} (账本指纹仍在)")
    return {"audit_id": audit_id, "name": name, "payload": payload}


@router.post("/verify")
def verify(request: Request):
    """全链校验: 行哈希/prev 链接/seq 连续 + 存量快照指纹复核。"""
    return ai_audit.verify_ledger(_data_dir(request))


class RecompareRequest(BaseModel):
    audit_id: str
    name: str = "kline"


@router.post("/recompare")
def recompare(request: Request, req: RecompareRequest):
    """某输入的当时指纹 vs 当前数据源重算指纹 — 回答「数据后来变没变」。"""
    repo = request.app.state.repo
    result = ai_audit.recompare_input(_data_dir(request), repo, req.audit_id, req.name)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "比对失败"))
    return result
