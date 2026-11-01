"""个股资料 API (P1-13 第一批) — 解禁/质押/回购/股东户数。

非路由数据集(照龙虎榜模式): 不进能力集, 免费源直连, 失败降级 state=source_unavailable。
GET /api/stock-profile/{dataset}?symbol=600519.SH[&force=1]
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import stock_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-profile", tags=["stock-profile"])


@router.get("/{dataset}")
def get_stock_profile(
    request: Request,
    dataset: str,
    symbol: str = Query(description="标的代码, 如 600519.SH / 000001(可省后缀)"),
    force: bool = Query(default=False, description="跳过缓存强制重拉"),
):
    """一只标的的一类资料(缓存 12h 优先; 拉失败有旧缓存标 stale, 无缓存降级)。"""
    if dataset not in stock_profile.DATASETS:
        raise HTTPException(400, f"未知资料类型: {dataset}, 可选: {'/'.join(stock_profile.DATASETS)}")
    try:
        return stock_profile.get_profile(
            request.app.state.repo.store.data_dir, dataset, symbol, force=force
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
