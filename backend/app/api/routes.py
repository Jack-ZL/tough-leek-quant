"""API 路由 — Phase 0 仅 /health 与 /api/capabilities。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.market_time import cn_today
from app.services import trading_day
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities, tier_label

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        # 三态: none(无key/无效) / free(免费key) / api_key(付费档)
        "mode": tf_client.current_mode(),
    }


@router.get("/api/capabilities")
def capabilities() -> dict:
    """前端用来决定哪些功能可用、哪些灰显。"""
    capset = detect_capabilities()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }


@router.get("/api/market/trading-status")
def market_trading_status() -> dict:
    """侧栏「日期/时间 + 是否交易日」提示。

    is_trading_day: True=交易日, False=确定休市, None=未知 (未配 fuyao 且
    tickflow 不可用时, 由交易日探针返回; 前端据此显示「状态未知」)。
    next_trading_day 仅在确定休市时返回, 供「下一交易日」提示。
    """
    verdict = trading_day.is_trading_day()
    return {
        "today": cn_today().isoformat(),
        "is_trading_day": verdict,
        "next_trading_day": (
            trading_day.next_trading_day().isoformat() if verdict is False else None
        ),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """用户在设置页"重新检测"按钮。"""
    capset = detect_capabilities(force=True)
    # 同步刷新 app.state 快照 (minute_refresh 等服务的门控读这里) 与财务调度器,
    # 与 settings.py 各探测路径一致 — 否则重检测后服务侧仍读旧 capset 被错误门控
    request.app.state.capabilities = capset
    from app.api.settings import _sync_financial_scheduler_caps
    _sync_financial_scheduler_caps(request.app.state, capset)
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }
