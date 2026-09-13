"""板块资金流向服务 — 复盘页卡片 + AI 复盘上下文。

自建 HTTP 客户端分级降级拉取行业/概念资金流排行, 无插件依赖:
- 一级: 东财多节点 (push2delay → push2 → 82.push2 → push2his)
- 二级: 新浪 MoneyFlow.ssl_bkzj_bk (行业/概念)
- 数据契约: 行业/概念各含 inflow(净流入前100) + outflow(净流出前100), 归一化字段见 clients
- 历史日不可变 → 按日落 JSON 缓存 (data/sector_fund_flow/date=YYYY-MM-DD.json)
- 当日盘中数据会变 → 不落缓存, 每次现拉; 当日拉取全失败 → 回退上一交易日缓存并标 fallback_prev

与 dragon_tiger / auction_benchmark 同构: 交易日解析复用本地 kline_daily 分区。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import date as date_cls
from pathlib import Path

from app.market_time import cn_today

logger = logging.getLogger(__name__)

_DATE_DIR_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def _local_trading_days(data_dir: Path) -> list[date_cls]:
    """本地日K分区日期 = 已知交易日集合 (升序)。扫描失败返回空。"""
    root = data_dir / "kline_daily"
    out: list[date_cls] = []
    try:
        for d in root.iterdir():
            m = _DATE_DIR_RE.match(d.name)
            if d.is_dir() and m:
                try:
                    out.append(date_cls.fromisoformat(m.group(1)))
                except ValueError:
                    continue
    except OSError:
        return []
    return sorted(out)


def resolve_trade_date(data_dir: Path, target: date_cls | None) -> date_cls | None:
    """目标日 → ≤目标日的最近本地交易日。None 或本地无分区 → 原样返回。"""
    if target is None:
        return None
    days = _local_trading_days(data_dir)
    if not days:
        return target
    candidates = [d for d in days if d <= target]
    return max(candidates) if candidates else target


def _prev_trading_day(data_dir: Path, d: date_cls) -> date_cls | None:
    days = _local_trading_days(data_dir)
    earlier = [x for x in days if x < d]
    return max(earlier) if earlier else None


def _cache_path(data_dir: Path, d: date_cls) -> Path:
    return data_dir / "sector_fund_flow" / f"date={d.isoformat()}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _fetch() -> tuple[str, dict]:
    """分级降级拉取: 东财 → 新浪。返回 (source, boards)。"""
    from app.services.sector_fund_flow_clients import (
        SectorFundFlowError,
        fetch_sector_fund_flow,
        fetch_sina_sector_fund_flow,
    )

    try:
        return "eastmoney", fetch_sector_fund_flow()
    except SectorFundFlowError as e:
        logger.warning("东财板块资金流失败, 降级新浪: %s", e)
    try:
        return "sina", fetch_sina_sector_fund_flow()
    except SectorFundFlowError as e:
        raise SectorFundFlowError(f"板块资金流全部数据源失败: {e}") from e


def get_sector_fund_flow(data_dir: Path, target: date_cls | None = None) -> dict:
    """取行业+概念资金流排名。返回给前端的统一容器。

    state: ok (正常) | fallback_prev (当日拉取失败, 已回退上一交易日缓存)
           | no_data (拉取失败且无可用回退)
    """
    # 无目标日 → 最近本地交易日 (本地无任何分区才让客户端取默认)
    if target is None:
        days = _local_trading_days(data_dir)
        target = max(days) if days else None
    trade_date = resolve_trade_date(data_dir, target)
    today = cn_today()

    # 历史日缓存优先 (纯本地, 不触发网络)
    if trade_date is not None and trade_date < today:
        cached = _load_cache(_cache_path(data_dir, trade_date))
        if cached is not None:
            return cached

    from app.services.sector_fund_flow_clients import SectorFundFlowError

    explicit = trade_date.isoformat() if trade_date is not None else None
    try:
        source, boards = _fetch()
        payload = {
            "state": "ok",
            "source": source,
            "requested_date": explicit,
            "trade_date": explicit,
            **boards,
        }
        # 历史日不可变 → 落缓存; 当日不缓存 (盘中会变, 盘后补充都以现拉为准)
        if trade_date is not None and trade_date < today:
            with contextlib.suppress(OSError):
                _store_cache(_cache_path(data_dir, trade_date), payload)
        return payload
    except SectorFundFlowError as e:
        # 当日拉取失败 → 回退上一交易日缓存一次
        if trade_date is not None:
            prev = _prev_trading_day(data_dir, trade_date)
            if prev is not None:
                cached_prev = _load_cache(_cache_path(data_dir, prev))
                if cached_prev is not None:
                    base = dict(cached_prev)
                    base.pop("state", None)
                    return {**base, "state": "fallback_prev",
                            "requested_date": explicit}
        logger.warning("板块资金流拉取失败: %s", e)
        return {"state": "no_data", "message": str(e)}


def build_recap_context(data_dir: Path) -> str:
    """AI 复盘的板块资金流摘要段 (纯文本, 失败返回空串不影响复盘)。"""
    try:
        payload = get_sector_fund_flow(data_dir, None)
        if payload.get("state") not in ("ok", "fallback_prev"):
            return ""
        trade_date = payload.get("trade_date") or ""
        lines = [f"(数据日期: {trade_date})"]

        concept = payload.get("concept", {})
        inflow = concept.get("inflow") or []
        outflow = concept.get("outflow") or []
        top_in = [i for i in inflow if (i.get("main_net_inflow") or 0) > 0][:5]
        if top_in:
            lines.append("概念资金净流入居前: " + "; ".join(
                f"{i.get('name')} 净流入{float(i.get('main_net_inflow') or 0)/1e8:.2f}亿"
                f" 涨跌{(i.get('change_pct') or 0)*100:.1f}%"
                for i in top_in))
        top_out = [i for i in outflow if (i.get("main_net_inflow") or 0) < 0][:5]
        if top_out:
            lines.append("概念资金净流出居前: " + "; ".join(
                f"{i.get('name')} 净流出{abs(float(i.get('main_net_inflow') or 0))/1e8:.2f}亿"
                for i in top_out))

        industry = payload.get("industry", {})
        ind_in = [i for i in (industry.get("inflow") or []) if (i.get("main_net_inflow") or 0) > 0][:5]
        if ind_in:
            lines.append("行业资金净流入居前: " + "; ".join(
                f"{i.get('name')} 净流入{float(i.get('main_net_inflow') or 0)/1e8:.2f}亿"
                for i in ind_in))
        return "\n".join(lines)
    except Exception as e:  # 摘要失败不影响复盘主流程
        logger.debug("板块资金流复盘摘要构建失败: %s", e)
        return ""
