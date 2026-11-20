"""涨幅轮动矩阵 API。

供「概念分析 → 涨幅RPS轮动」对话框调用。返回最近 N 个交易日的概念涨幅
排名矩阵:每列(日期)各自把所有概念按当天涨幅从高到低排序。

市场支持 (CONTRIBUTING §4 fail closed):
  - ``GET /rotation``: A 股概念/行业, 港股**仅恒生行业** (港股无概念数据源,
    服务层返回空矩阵; ``level`` 对港股无意义, 被忽略)。
  - ``POST /rotation-analyze``: **仅 A 股**。AI 分析要拼大盘背景, 而大盘背景只有
    A 股源, 港股走这条链会变成串市场拼接 —— 见
    ``services/concept_rotation_analyzer.py`` 模块注释。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.market import Market, parse_market
from app.services import rps_rotation
from app.services.concept_rotation_analyzer import analyze_rotation_stream
from app.services.ndjson_heartbeat import with_heartbeat

router = APIRouter(prefix="/api/rps", tags=["rps"])


def _resolve_market(raw: str | None) -> Market:
    """解析 ``market`` 参数: 缺省 CN, 非法值 **400**。

    🔴 不静默回退 CN —— 拼错的市场参数若悄悄变成「按 A 股算」, 用户会以为看到的
    是港股轮动。与 ``api/screener.py:market_snapshot`` 同一套判据。
    """
    if raw is None:
        return Market.CN
    resolved = parse_market(raw)
    if resolved is None:
        raise HTTPException(
            status_code=400,
            detail=f"未知 market {raw!r} (支持: {', '.join(m.value for m in Market)})",
        )
    return resolved


@router.get("/rotation")
def get_rotation(
    request: Request,
    days: int = Query(12, ge=7, le=30, description="最近 N 个交易日(7-30)"),
    kind: str = Query("concept", pattern="concept|industry", description="维度: concept 概念 / industry 行业"),
    level: int | None = Query(None, ge=1, le=3, description="行业层级(仅 kind=industry): 1/2/3 级; 港股忽略"),
    # 🔴 用 Annotated 而不是 `market: str | None = Query(None)`: 后者的**默认值是
    # Query 对象本身**, 直接调用端点函数(本仓库测试的既有做法)时 `market is None`
    # 恒假 → parse_market 认不出 → 一律 400 (同 api/screener.py:market_snapshot)。
    market: Annotated[str | None, Query(description="市场 (CN/HK); 缺省 CN")] = None,
) -> dict:
    """维度涨幅轮动矩阵(概念或行业)。

    Returns:
        dates: 日期字符串列表(最新在最前)
        columns: {日期: [[成员名, 涨幅小数], ...]} 每列各自降序
        concept_count: 去重维度成员总数

    🔴 港股 + ``kind=concept`` 返回**空矩阵**而不是 400: 前端板块页对两个市场渲染
    同一个对话框, 空 columns 会落到既有的「暂无数据」空态; 400 反而要前端为港股
    单独加一条错误分支 (CONTRIBUTING §3.5 市场差异是数据不是控制流)。
    """
    resolved = _resolve_market(market)
    return rps_rotation.build_rps_rotation(
        request.app.state.repo, days, kind, level, market=resolved,
    )


class AnalyzeRequest(BaseModel):
    """AI 维度轮动分析请求(概念或行业)。"""
    days: int = 12   # 分析最近 N 个交易日
    focus: str = ""  # 用户追加的关注点
    kind: str = "concept"  # "concept" 概念 / "industry" 行业
    level: int | None = None  # 行业层级(1/2/3), 仅 kind=industry 有效
    market: str | None = None  # 市场 (CN/HK); 缺省 CN。🔴 仅 CN 可分析, 见模块注释


@router.post("/rotation-analyze")
async def analyze_rotation(request: Request, req: AnalyzeRequest):
    """AI 维度轮动分析 — NDJSON 流式返回。

    装配轮动矩阵信号 + 大盘背景 → 分析提示词 → 流式调用 LLM →
    逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。

    协议:
      {"type":"meta","days","summary"}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done"}

    🔴 无大盘背景源的市场 (``rules.has_market_overview=False``, 目前是港股)
    **不抛 4xx**: 流式端点已经返回 200 并开始推流, 此时抛异常前端只会看到一条
    断掉的空流。改为推协议自带的 ``error`` 事件 (文案见
    ``concept_rotation_analyzer.ANALYZE_UNSUPPORTED_NO_OVERVIEW``), 前端能如实渲染。
    """
    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)
    days = max(7, min(30, req.days))
    kind = "industry" if req.kind == "industry" else "concept"
    level = req.level if (kind == "industry" and req.level in (1, 2, 3)) else None
    market = _resolve_market(req.market)

    async def stream_gen():
        async for chunk in with_heartbeat(
            analyze_rotation_stream(
                repo, days, req.focus, quote_service, depth_service, kind, level,
                market=market,
            ),
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
