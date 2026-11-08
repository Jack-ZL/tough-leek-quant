"""多空辩论 API (P1-9) — 让几个 AI 开会辩论一只股票。

路由前缀: /api/stock-debate

端点:
  POST /analyze                发起辩论 (NDJSON 流式, 事件协议见 stock_debate 模块)
  GET  /reports                历史辩论记录列表
  POST /reports                保存一条辩论记录 (前端在流结束后调用)
  DELETE /reports/{report_id}  删除一条记录
  GET  /role-profiles          角色模型分配 + 可选档案列表 (设置页数据源)
  PUT  /role-profiles          保存角色模型分配

轻量按需 (QuantMind 重型服务教训): 无常驻进程/调度器, 辩论全程在一次
SSE 请求生命周期内完成; 硬上限 5 次 LLM 调用 (多/空/裁决/重写×1/终审)。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import debate_reports, stock_debate
from app.services.ndjson_heartbeat import with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-debate", tags=["stock-debate"])


class DebateRequest(BaseModel):
    """发起一场多空辩论。"""
    symbol: str
    name: str = ""
    focus: str = ""  # 可选: 用户追加的关注点 (注入双方辩手提示词)


@router.post("/analyze")
async def debate_stock(request: Request, req: DebateRequest):
    """多空辩论 — NDJSON 流式返回 (事件带 role 字段, 前端路由到双栏)。"""
    if not req.symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    async def stream_gen():
        async for chunk in with_heartbeat(
            stock_debate.debate_stream(repo, data_dir, req.symbol, req.name, req.focus)
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# 辩论记录 CRUD
# ================================================================

class SaveDebateReportRequest(BaseModel):
    """保存一条辩论记录 (字段含义见 debate_reports 模块 docstring)。"""
    symbol: str
    name: str = ""
    focus: str = ""
    bull_text: str = ""
    bear_text: str = ""
    judge_text: str = ""
    # 终审裁决 {"direction": "偏多", "reason": "…"}; 解析失败为 null (fail-honest)
    verdict: dict | None = None
    rewritten: bool = False
    roles: dict | None = None
    summary: str = ""
    close: float | None = None
    levels: dict | None = None
    # P1-15 模型归属: 生成时的 model/provider/profile_id (meta 事件下发,
    # 前端原样带回); 各角色实际用的模型已在 roles 字段 (角色档案显示名)
    model: str = ""
    provider: str = ""
    profile_id: str = ""
    # P2-23 账本 run id (meta 事件下发, 前端原样带回; 空串=旧前端/未记账)
    audit_id: str = ""


@router.get("/reports")
def list_reports(request: Request):
    """历史辩论记录 (按时间降序, 后端已裁剪到上限)。"""
    return {"reports": debate_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveDebateReportRequest):
    """保存一条辩论记录。"""
    report = debate_reports.save_report({
        "symbol": req.symbol,
        "name": req.name,
        "focus": req.focus,
        "bull_text": req.bull_text,
        "bear_text": req.bear_text,
        "judge_text": req.judge_text,
        "verdict": req.verdict,
        "rewritten": req.rewritten,
        "roles": req.roles,
        "summary": req.summary,
        "close": req.close,
        "levels": req.levels,
        # P1-15 模型归属快照 (空串=旧前端未带, 兼容不报错)
        "model": req.model,
        "provider": req.provider,
        "profile_id": req.profile_id,
    })
    # P2-23: 账本追加 link 条目绑定 report_id (fail-open, 不影响保存)
    if req.audit_id:
        from app.services import ai_audit
        ai_audit.link_report(request.app.state.repo.store.data_dir, req.audit_id,
                             report.get("id", ""), kind="debate")
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条辩论记录。"""
    return {"ok": debate_reports.delete_report(report_id)}


# ================================================================
# 角色模型分配 (设置页)
# ================================================================

class RoleProfilesRequest(BaseModel):
    """角色 → 档案 id。空串 = 用当前激活档案。"""
    bull: str = ""
    bear: str = ""
    judge: str = ""


@router.get("/role-profiles")
def get_role_profiles(request: Request):
    """当前分配 + 全部可选档案 (id/name/model, 不含 key 等敏感字段)。"""
    from app import secrets_store
    from app.services import preferences

    return {
        "ok": True,
        "assignments": preferences.get_debate_role_profiles(),
        "profiles": [
            {"id": str(p.get("id")), "name": str(p.get("name") or ""),
             "model": str(p.get("model") or p.get("codex_model") or "")}
            for p in secrets_store.list_ai_profiles()
        ],
        "active_profile_id": secrets_store.get_active_ai_profile_id(),
    }


@router.put("/role-profiles")
def set_role_profiles(request: Request, req: RoleProfilesRequest):
    """保存分配。留空的角色自动轮转分配档案; 失效 id 也回退轮转位 (辩论不因设置过期跑不了)。"""
    from app.services import preferences

    cleaned = preferences.set_debate_role_profiles(
        {"bull": req.bull, "bear": req.bear, "judge": req.judge})
    return {"ok": True, "assignments": cleaned}
