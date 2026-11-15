"""港股数据 API (方案 §4 闸3: 独立入口, 不与 A 股路由混用)。

**刻意独立成 ``/api/hk/*`` 而不是往 ``/api/kline``、``/api/index`` 上加 ``market`` 参数**:
A 股那些端点的语义里嵌了大量 A 股专属假设 (T+1、涨跌停、手为单位、统一视图
``kline_daily_all``)。加参数意味着每个端点内部都要分叉, 而分叉会漏 —— 目录级/路由级
隔离比 ``if market == HK`` 可靠, 这条原则在存储层已经用
``tests/test_market_architecture_guard.py`` 钉死, API 层同理。

长任务 (全市场日K) 复用 ``app.api.pipeline._submit_job``: 那是盘后管道与除权因子同步
共用的任务提交机制, 提供单飞去重、重任务执行槽、行情暂停、缓存失效与进度轮询。
港股日K与 A 股写的是**不同**目录, 但仍共享同一个执行槽 —— 这是有意的: 两者都打东财,
并发拉取会撞限流甚至触发 IP 级封禁。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.pipeline import _submit_job
from app.market import Market, rules_for
from app.plugins.eastmoney.hk_financials import HK_DATE_TYPE_ANNUAL
from app.plugins.sina import hk_index as sina_hk_index
from app.plugins.sina.client import SinaError
from app.services import (
    financial_sync_hk,
    index_const,
    index_sync_hk,
    instrument_sync_hk,
    kline_sync_hk,
)
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hk", tags=["hk"])


class HkDailySyncRequest(BaseModel):
    """港股日K同步请求。

    ``symbols`` 为空 = 全市场 (维表里的港币柜台)。``days`` 仅在未显式给 ``start``
    且关闭增量时生效, 用于「只回填最近 N 天」的有界同步。

    ``enriched`` 默认 **True**: 日K 落库后紧接着重算 ``kline_hk_enriched/``。放在
    同一个任务里是为了让常规路径**无从忘记** —— 只写原始日K 不生成 enriched 的话,
    换手率/连板这些下游要用的列根本不存在。两个阶段仍分开报进度 (与 A 股盘后管道
    一致), 需要单独重算 enriched 时用 ``POST /api/hk/sync_enriched``。
    """

    symbols: list[str] | None = None
    days: int | None = None
    incremental: bool = True
    include_cny_counter: bool = False
    enriched: bool = True


class HkEnrichedSyncRequest(BaseModel):
    """港股 enriched 重算请求 (**纯本地计算, 不发网络请求**)。

    ``symbols`` 为空 = ``kline_hk_daily`` 里的全部标的。``days`` 只裁剪**发布**范围,
    计算始终吃完整历史 (指标预热 + 连板递推都需要)。``batch_size`` 上限即
    ``HK_ENRICHED_BATCH_SIZE``, 传更大值会被夹住。
    """

    symbols: list[str] | None = None
    days: int | None = None
    batch_size: int | None = None


class HkFinancialSyncRequest(BaseModel):
    """港股财务同步请求。

    ``symbols`` 为空 = **全市场分页** (实测 6 年窗口约 60~70 页即覆盖全部港股,
    比逐标的 2600+ 次请求划算得多)。给了 ``symbols`` 则走 ``in`` 批量,
    服务端单批上限 200 (超过会 HTTP 414), 由服务层自动分批。

    ``years`` 是滚动回溯窗口; 窗口外的历史**不会被删除** (合并语义保留),
    只是本次不重新拉取。
    """

    symbols: list[str] | None = None
    years: int | None = None
    annual_only: bool = False
    backfill_lot_size: bool = True


def _read_hk_instruments(data_dir: Path) -> pl.DataFrame:
    """直接读维表 parquet, 不走 ``instruments_hk`` 视图。

    视图在目录为空时不注册 (``read_parquet`` 无匹配文件会抛, ``_register_views``
    降级为 debug), 首次同步前查询会得到 500 而不是空列表。直读文件可以把
    「还没同步过」如实表达成 ``[]``。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败: %s", e)
        return pl.DataFrame()


@router.get("/instruments")
def list_hk_instruments(
    request: Request,
    q: Annotated[str | None, Query(description="按代码或名称模糊过滤")] = None,
    include_cny_counter: Annotated[bool, Query(description="是否包含人民币柜台")] = False,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """港股标的维表。默认只返回港币柜台 (双柜台是同一份基本面, 列表里重复出现会误导)。"""
    df = _read_hk_instruments(request.app.state.repo.store.data_dir)
    if df.is_empty():
        return {"total": 0, "items": [], "synced": False}

    if not include_cny_counter and "is_cny_counter" in df.columns:
        df = df.filter(~pl.col("is_cny_counter").fill_null(False))
    if q:
        needle = q.strip().upper()
        if needle:
            df = df.filter(
                pl.col("symbol").str.contains(needle, literal=True)
                | pl.col("name").fill_null("").str.contains(needle, literal=True)
            )
    df = df.sort("symbol")
    total = df.height
    items = df.slice(offset, limit).to_dicts()
    for it in items:  # date 类型不能直接 JSON 序列化
        for k, v in it.items():
            if isinstance(v, date):
                it[k] = v.isoformat()
    return {"total": total, "items": items, "synced": True}


@router.post("/sync_instruments")
def sync_hk_instruments(request: Request) -> dict:
    """同步港股标的维表 → ``instruments_hk/``。

    分页拉取全市场快照 (数十个请求, 秒级), 因此同步执行即可, 不必任务化。
    拉取失败时**保留历史维表且不写盘** (见 ``instrument_sync_hk``), 返回 count=0。
    """
    repo = request.app.state.repo
    count = instrument_sync_hk.sync_instruments_hk(repo.store.data_dir)
    repo.rebuild_views()
    return {"status": "ok" if count else "empty", "count": count}


@router.post("/sync_daily")
async def sync_hk_daily(request: Request, req: HkDailySyncRequest) -> dict:
    """异步触发港股日K同步 → ``kline_hk_daily/``, 立即返回 job_id。

    轮询 ``/api/pipeline/jobs/{job_id}`` 拿进度 (与盘后管道同一套任务存储)。
    若已有数据任务在跑, 返回该任务 id 而不是另起一个 (防撞限流)。
    """
    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    symbols = req.symbols
    if symbols is not None:
        if not symbols:
            raise HTTPException(status_code=400, detail="symbols 传了空列表")
        universe = list(symbols)
    else:
        universe = kline_sync_hk.hk_daily_universe(
            data_dir, include_cny_counter=req.include_cny_counter
        )
        if not universe:
            raise HTTPException(
                status_code=409,
                detail="港股维表为空, 请先调用 POST /api/hk/sync_instruments",
            )

    start = None
    if not req.incremental and req.days:
        start = date.today() - timedelta(days=req.days)

    def make_run(progress):
        def run() -> dict:
            def on_progress(done: int, total: int) -> None:
                pct = int(done * 100 / total) if total else 100
                progress("hk_daily", pct, f"港股日K {done}/{total}", stage_pct=pct)

            written = kline_sync_hk.sync_hk_daily(
                repo,
                symbols=universe,
                start=start,
                incremental=req.incremental,
                on_progress=on_progress,
            )
            enriched_rows = 0
            enriched_uncovered: list[str] = []
            if req.enriched:
                def on_enriched(done: int, total: int) -> None:
                    pct = int(done * 100 / total) if total else 100
                    progress("hk_enriched", pct, f"港股 enriched {done}/{total}", stage_pct=pct)

                # 不传 start: enriched 按日分区 merge-upsert, 全量发布是幂等的,
                # 而裁剪发布范围会让「首次同步」漏掉预热期之前的行。
                enriched_rows = kline_sync_hk.sync_hk_enriched(
                    repo, symbols=universe, on_progress=on_enriched,
                    uncovered_out=enriched_uncovered,
                )
            return {
                "symbols": len(universe),
                "rows_written": written,
                "enriched_rows": enriched_rows,
                # 非空 = 这些标的复权因子覆盖不完整, 被 enriched **拒算** (fail closed)。
                # 必须回报: 否则调用方只看到 enriched_rows 偏少, 分不清是「本来没数据」
                # 还是「被闸门挡了」—— 那正是完整性凭据要消灭的静默。
                "enriched_uncovered": enriched_uncovered,
            }

        return run

    return await _submit_job(
        repo, getattr(request.app.state, "quote_service", None), make_run
    )


@router.post("/sync_enriched")
async def sync_hk_enriched(request: Request, req: HkEnrichedSyncRequest) -> dict:
    """异步触发港股 enriched 重算 → ``kline_hk_enriched/``, 立即返回 job_id。

    **纯本地 CPU 任务, 不发任何网络请求**: 输入只有已落盘的 ``kline_hk_daily`` 与
    ``instruments_hk``。因此可以在无外网环境里单独重跑 —— 典型用途是换手率口径
    修正后回填历史 (港股 volume 单位是「股」不是「手」, 走 A 股系数会高 100 倍)。

    维表缺失时服务层**明确拒绝**并返回 0 行 (不产出缺 ``turnover_rate`` 列的分区),
    此时应先调 ``POST /api/hk/sync_instruments``。

    🔴 复权因子同样 fail closed: 完整性凭据 ``adj_factor_hk/coverage.json`` 缺失 →
    整轮 0 行; 凭据里 ``uncovered`` 的标的 → **逐个拒算**, 并回在 job 结果的
    ``uncovered`` 字段。两者都意味着 ``sync_hk_adj_factor`` 没跑全 (它目前只由港股
    定时流水线调用, 无独立 HTTP 入口), 应等下一轮定时同步补齐后重跑, 不要绕闸门。
    """
    repo = request.app.state.repo

    if req.symbols is not None and not req.symbols:
        raise HTTPException(status_code=400, detail="symbols 传了空列表")
    start = date.today() - timedelta(days=req.days) if req.days else None

    def make_run(progress):
        def run() -> dict:
            def on_progress(done: int, total: int) -> None:
                pct = int(done * 100 / total) if total else 100
                progress("hk_enriched", pct, f"港股 enriched {done}/{total}", stage_pct=pct)

            uncovered: list[str] = []
            written = kline_sync_hk.sync_hk_enriched(
                repo,
                symbols=req.symbols,
                start=start,
                batch_size=req.batch_size,
                on_progress=on_progress,
                uncovered_out=uncovered,
            )
            # 见 /sync_daily 里 enriched_uncovered 的说明: 被完整性闸门拒算的标的
            # 必须显式回报, 否则 rows_written 偏少时无从判断原因。
            return {"rows_written": written, "uncovered": uncovered}

        return run

    return await _submit_job(
        repo, getattr(request.app.state, "quote_service", None), make_run
    )


@router.get("/daily")
def get_hk_daily(
    request: Request,
    symbol: Annotated[str, Query(description="港股代码, 如 00700.HK")],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict:
    """读港股日K (原始价, volume 单位为**股**)。

    ⚠️ 调用方不得假定 volume 是手: 港股内部单位是股, 换算系数见
    ``app.market.shares_per_volume_unit``。响应里带 ``volume_unit`` 明示, 避免
    前端按 A 股习惯乘 100。
    """
    from app.market import is_hk_symbol

    if not is_hk_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"非港股代码格式: {symbol}")

    repo = request.app.state.repo
    sql = "SELECT symbol, date, open, high, low, close, volume, amount FROM kline_hk_daily WHERE symbol = ?"
    params: list = [symbol]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    try:
        rows = repo.execute_all(sql, params)
    except Exception as e:  # noqa: BLE001
        # 视图未注册 = 还没同步过任何港股日K, 如实返回空而不是 500
        logger.debug("kline_hk_daily 查询失败 (可能尚未同步): %s", e)
        rows = []

    return {
        "symbol": symbol,
        "volume_unit": "shares",
        "currency": "HKD",
        "rows": [
            {
                "symbol": r[0], "date": r[1].isoformat() if isinstance(r[1], date) else str(r[1]),
                "open": r[2], "high": r[3], "low": r[4], "close": r[5],
                "volume": r[6], "amount": r[7],
            }
            for r in rows
        ],
    }


# ── 指数 (PR3, 方案 §11) ──────────────────────────────────────────
def _resolve_hk_index_symbols(raw: str | None) -> list[str]:
    """解析指数代码入参 → 白名单内的代码列表 (保持调用方给的顺序)。

    缺省 = **核心四指数** (``index_const.core_index_symbols(Market.HK)``), 与
    CONTRIBUTING §指数契约「展示层固定为核心指数, ``index_const.py`` 是唯一真源」
    一致; 要全 27 个 (指数页) 必须显式传。

    🔴 校验用**指数白名单**而不是 ``is_hk_symbol``: 后者是股票契约
    (``[0-9]{5}\\.HK``), ``HSI`` 这类指数代码必然不匹配 —— 用错校验器会让所有
    合法请求 400。白名单外的代码 400 而非静默丢弃 (fail closed)。
    """
    if not raw or not raw.strip():
        return list(index_const.core_index_symbols(Market.HK))
    wanted = [s.strip().upper() for s in raw.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in sina_hk_index.HK_INDEX_WHITELIST]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"非港股指数代码: {unknown[:5]} (共 {len(unknown)} 个)。"
            "允许的代码见 app/plugins/sina/hk_index.py 白名单",
        )
    return wanted


def _hk_index_quotes_from_daily(repo, wanted: list[str]) -> list[dict]:
    """新浪不可用时的降级: 从 ``kline_hk_index_daily`` 取每个代码最近两根算涨跌。

    取**两根**而不是一根: 只有一根算不出 ``change_pct``, 而前端侧栏/总览的核心
    展示就是涨跌幅 —— 缺了它会渲染成 0.00% (看起来像「今天没动」), 那是假数据。
    前收缺失 (只有一根历史) 时 ``change_pct`` 留 ``None``, 由前端显示占位符。
    """
    if not wanted:
        return []
    placeholders = ",".join("?" * len(wanted))
    sql = (
        "SELECT symbol, date, open, high, low, close, volume FROM ("
        "  SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn"
        f"  FROM kline_hk_index_daily WHERE symbol IN ({placeholders})"
        ") WHERE rn <= 2 ORDER BY symbol, date DESC"
    )
    try:
        rows = repo.execute_all(sql, wanted)
    except Exception as e:  # noqa: BLE001
        # 视图未注册 = 从未同步过港股指数, 如实返回空 (由调用方标注 source)
        logger.debug("kline_hk_index_daily 查询失败 (可能尚未同步): %s", e)
        return []

    by_symbol: dict[str, list[tuple]] = {}
    for r in rows:
        by_symbol.setdefault(r[0], []).append(r)

    names = index_const.core_index_names(Market.HK)
    out: list[dict] = []
    for symbol in wanted:  # 按调用方顺序, 而非 SQL 返回顺序
        bars = by_symbol.get(symbol)
        if not bars:
            continue
        latest = bars[0]
        prev_close = bars[1][5] if len(bars) > 1 else None
        close = latest[5]
        change_pct = None
        change_amount = None
        if prev_close:
            change_amount = close - prev_close
            change_pct = change_amount / prev_close  # 小数口径, 与实时快照一致
        high, low = latest[3], latest[4]
        out.append(
            {
                "symbol": symbol,
                "name": names.get(symbol),
                "last_price": close,
                "prev_close": prev_close,
                "open": latest[2],
                "high": high,
                "low": low,
                "volume": latest[6],
                # amount 刻意不产出: 腾讯日K不提供成交额, 填 0 会被前端当成
                # 「成交额为零」的真实读数 (见 index_sync_hk 模块 docstring)
                "change_pct": change_pct,
                "change_amount": change_amount,
                "amplitude": (high - low) / prev_close if high is not None and low is not None and prev_close else None,
                "timestamp": None,  # 日K只有日期没有时刻, 不伪造成 00:00 的毫秒时间戳
                "trade_date": latest[1].isoformat() if isinstance(latest[1], date) else str(latest[1]),
            }
        )
    return out


@router.get("/indices")
def get_hk_indices(
    request: Request,
    symbols: Annotated[
        str | None,
        Query(description="逗号分隔的港股指数代码, 如 HSI,HSTECH; 缺省=核心四指数"),
    ] = None,
) -> dict:
    """港股指数实时快照 (新浪, **15 分钟延时**)。

    响应形状与 ``GET /api/intraday/indices`` 对齐 (``rows/count/source``), 前端共享层
    可按市场切端点而不必分叉渲染逻辑。额外两个字段是港股专属语义:

    - ``quote_delay_minutes``: 取自 ``rules_for(Market.HK)``, 是前端「延时」标注的
      **唯一来源** (方案 §13 PR3 第 5 项)。不得在前端硬编码 15。
    - ``currency``: 港股指数以 HKD 计, 与 A 股 CNY 不同, 供金额类展示标注。

    新浪不可用时降级读日K最新一根 (``source="index_daily"``), 与 A 股同款语义:
    宁可给上一交易日收盘也不给空白, 但**必须如实标注来源**, 不伪装成实时。
    """
    wanted = _resolve_hk_index_symbols(symbols)
    repo = request.app.state.repo
    delay = rules_for(Market.HK).quote_delay_minutes

    try:
        rows = index_sync_hk.fetch_hk_index_quotes(symbols=wanted)
    except SinaError as e:
        logger.warning("港股指数快照拉取失败, 降级读日K: %s", e)
        rows = []
    # 兜住所有异常降级到日K: 快照是「锦上添花」的实时层, 任何未预期错误都不该
    # 让侧栏指数条整块 500。logger.exception 保留完整栈, 不静默。
    except Exception:
        logger.exception("港股指数快照出现未预期错误, 降级读日K")
        rows = []

    if rows:
        # 新浪可能返回白名单外的代码 (源变更), 也可能漏返回 → 按请求集过滤并保序
        by_symbol = {r["symbol"]: r for r in rows}
        ordered = [by_symbol[s] for s in wanted if s in by_symbol]
        missing = [s for s in wanted if s not in by_symbol]
        if missing:
            logger.warning("港股指数快照缺 %d 个请求代码: %s", len(missing), missing[:8])
        if ordered:
            return {
                "rows": ordered,
                "count": len(ordered),
                "source": "realtime",
                "market": Market.HK.value,
                "quote_delay_minutes": delay,
                "currency": "HKD",
            }

    fallback = _hk_index_quotes_from_daily(repo, wanted)
    return {
        "rows": fallback,
        "count": len(fallback),
        "source": "index_daily",
        "market": Market.HK.value,
        "quote_delay_minutes": delay,
        "currency": "HKD",
    }


@router.get("/index_daily")
def get_hk_index_daily(
    request: Request,
    symbol: Annotated[str, Query(description="港股指数代码, 如 HSI")],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict:
    """读港股指数日K → ``kline_hk_index_daily/`` (原始值, 未复权 —— 指数无复权概念)。

    🔴 ``volume_unit`` 返回 ``None`` 而**不是**某个具体单位: 两个源对同一个量的
    口径互相矛盾 (新浪 HSI 2026-11-13 成交量 195857007, 腾讯同日 195857007140,
    恰好 ×1000; 而新浪自带的成交额/成交量 = 58.84, 与指数点位 23637.6 也不自洽)。
    既然无法判定真值, 就**不断言** —— 编一个单位会让前端把量能用错 1000 倍。
    指数场景下量能只做定性参考 (MA/MACD 只用 close), 不影响指标正确性。

    与 ``GET /api/hk/daily`` 一样**不含** enriched 指标列: 磁盘只存窄表,
    MA/MACD 等由读时重算 (``ENRICHED_STORAGE_COLS`` 契约)。
    """
    wanted = _resolve_hk_index_symbols(symbol)  # 复用白名单校验 (单代码也走同一口径)
    repo = request.app.state.repo

    sql = (
        "SELECT symbol, date, open, high, low, close, volume "
        "FROM kline_hk_index_daily WHERE symbol = ?"
    )
    params: list = [wanted[0]]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    try:
        rows = repo.execute_all(sql, params)
    except Exception as e:  # noqa: BLE001
        # 视图未注册 = 还没同步过港股指数日K, 如实返回空而不是 500
        logger.debug("kline_hk_index_daily 查询失败 (可能尚未同步): %s", e)
        rows = []

    return {
        "symbol": wanted[0],
        "name": index_const.core_index_names(Market.HK).get(wanted[0]),
        "volume_unit": None,
        "currency": "HKD",
        "quote_delay_minutes": rules_for(Market.HK).quote_delay_minutes,
        "rows": [
            {
                "symbol": r[0],
                "date": r[1].isoformat() if isinstance(r[1], date) else str(r[1]),
                "open": r[2], "high": r[3], "low": r[4], "close": r[5],
                "volume": r[6],
            }
            for r in rows
        ],
    }


class HkIndexSyncRequest(BaseModel):
    """港股指数日K同步请求。

    ``symbols`` 为空 = 白名单全集 (27 个)。``count`` 为单标的回填根数,
    缺省 :data:`~app.services.index_sync_hk.HK_INDEX_DAILY_COUNT`。
    """

    symbols: list[str] | None = None
    count: int | None = None


@router.post("/sync_index_daily")
def sync_hk_index_daily_route(request: Request, req: HkIndexSyncRequest) -> dict:
    """同步港股指数日K + enriched → ``kline_hk_index_daily/``。

    27 个单标的请求 (腾讯日K无批量端点), 秒级 → 同步执行, 不必任务化
    (与 ``sync_hk_instruments`` 同款判断)。视图刷新在 service 内部完成。

    ``status`` 三态: ``ok`` 有写入且无失败 / ``partial`` 有写入但有真失败 /
    ``empty`` 零写入。**腾讯无覆盖的代码不算失败** (27 个中仅 14 个有日K,
    详见 :mod:`app.services.index_sync_hk`), 单独在 ``no_coverage`` 里如实返回。
    """
    repo = request.app.state.repo
    try:
        written, failed = index_sync_hk.sync_hk_index_daily(
            repo, symbols=req.symbols, count=req.count
        )
    except ValueError as e:
        # 白名单校验失败 (hk_index_universe 抛出) → 400 而不是 500
        raise HTTPException(status_code=400, detail=str(e)) from e

    status = "empty" if not written else ("partial" if failed else "ok")
    return {"status": status, "count": written, "failed": failed}


# ── 财务 (PR2b) ───────────────────────────────────────────────────
def _jsonable(rows: list[dict]) -> list[dict]:
    """把 polars ``to_dicts()`` 的结果转成可直接 JSON 序列化的形态。

    港股财务 schema 里日期一律是 ISO **字符串** (非 ``date`` 对象), 但投影/对齐
    后仍可能混入 ``date`` 或 ``Decimal``, 统一在这里兜住, 避免 500。
    """
    return [
        {k: v.isoformat() if isinstance(v, date) else v for k, v in r.items()}
        for r in rows
    ]


@router.get("/financials/status")
def hk_financials_status(request: Request) -> dict:
    """港股财务同步状态。未同步时如实返回 ``synced=false`` 而非 500。"""
    data_dir = request.app.state.repo.store.data_dir
    wide = financial_sync_hk.get_hk_financial_df(data_dir, "main_indicator")
    if wide.is_empty():
        return {
            "synced": False,
            "rows": 0,
            "symbols": 0,
            "latest_report_date": None,
            "lot_size_coverage": None,
            "tables": {
                t: financial_sync_hk.get_hk_financial_df(data_dir, t).height
                for t in ("income", "balance")
            },
        }
    latest = financial_sync_hk.latest_hk_report_date(data_dir)
    lots = financial_sync_hk.hk_lot_size_map(data_dir)
    return {
        "synced": True,
        "rows": wide.height,
        "symbols": wide.get_column("symbol").n_unique(),
        "latest_report_date": latest.isoformat() if latest else None,
        # 每手股数覆盖率: 该列由财务表回填进 instruments_hk, 是港股下单取整的依据
        "lot_size_coverage": len(lots),
        "period_names": sorted(
            {str(p) for p in wide.get_column("period_name").drop_nulls().to_list()}
        ),
        "tables": {
            t: financial_sync_hk.get_hk_financial_df(data_dir, t).height
            for t in ("income", "balance")
        },
    }


@router.post("/sync_financials")
async def sync_hk_financials(request: Request, req: HkFinancialSyncRequest) -> dict:
    """异步触发港股财务主指标同步 → ``financials_hk/main_indicator/``。

    与日K同步共享同一个重任务执行槽。虽然 datacenter 在 client 层有**独立**的
    令牌桶/熔断/封禁池 (实测 push2 被封时 datacenter 仍可用), 但共享槽位可以
    避免"日K + 财务 + 盘后管道"三路同时打东财导致整机出口 IP 被限。
    """
    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    symbols = req.symbols
    if symbols is not None and not symbols:
        raise HTTPException(status_code=400, detail="symbols 传了空列表")

    since = (
        financial_sync_hk.default_hk_fin_since(years=req.years) if req.years else None
    )
    date_type_code = HK_DATE_TYPE_ANNUAL if req.annual_only else None

    def make_run(progress):
        def run() -> dict:
            def on_progress(stage: str, pct: int, msg: str) -> None:
                progress(stage, pct, msg, stage_pct=pct)

            written = financial_sync_hk.sync_hk_main_indicator(
                data_dir,
                since=since,
                date_type_code=date_type_code,
                secucodes=symbols,
                backfill_lot_size=req.backfill_lot_size,
                on_progress=on_progress,
            )
            return {
                "symbols": len(symbols) if symbols else None,
                "rows_written": written,
            }

        return run

    return await _submit_job(
        repo, getattr(request.app.state, "quote_service", None), make_run
    )


@router.get("/financials")
def get_hk_financials(
    request: Request,
    symbol: Annotated[str, Query(description="港股代码, 如 00700.HK")],
    limit: Annotated[int, Query(ge=1, le=80, description="最近几期")] = 8,
    align: Annotated[
        bool, Query(description="是否按 A 股 metrics 列集补齐缺失列 (共用页面时开)")
    ] = False,
) -> dict:
    """单只港股的财务概况: 一次返回 metrics / income / balance_sheet / cash_flow 四张表。

    列名与 A 股 ``/api/financials/*`` **同名同 dtype**, 故前端可用同一套渲染逻辑;
    ``align=true`` 时再按 A 股真实列集补 null, 使两市场帧列集完全一致。

    🔴 响应里的 ``currency_warning`` 不是装饰: 港股金额列是**编报币种**
    (腾讯为 CNY), 估值列 (``pe_ttm``/``total_market_cap``) 是**港币**,
    且不同公司编报币种不同 (CNY/HKD/USD)。跨标的直接相加或比较金额是错的。

    ⚠️ ``announce_date`` 恒为 null —— 港股宽表不提供公告日, 不用报告期冒充
    (CONTRIBUTING:586 要求按公告日对齐历史股本, 伪造会污染下游)。
    """
    from app.market import is_hk_symbol

    if not is_hk_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"非港股代码格式: {symbol}")

    data_dir = request.app.state.repo.store.data_dir
    tables = financial_sync_hk.hk_latest_financials(data_dir, symbol, limit=limit)

    if align:
        for name, frame in list(tables.items()):
            if frame.is_empty():
                continue
            ref = _read_ashare_financials(data_dir, name)
            if not ref.is_empty():
                tables[name] = financial_sync_hk.align_hk_to_ashare(frame, ref)

    wide = financial_sync_hk.get_hk_financial_df(data_dir, "main_indicator")
    mine = (
        wide.filter(pl.col("symbol") == symbol).sort("report_date", descending=True)
        if not wide.is_empty() and "symbol" in wide.columns
        else pl.DataFrame()
    )
    meta: dict = {"symbol": symbol, "synced": not mine.is_empty()}
    if not mine.is_empty():
        first = mine.row(0, named=True)
        meta.update(
            {
                "name": first.get("name"),
                "org_type": first.get("org_type"),
                # 计价币种 (港币柜台 HKD / 人民币柜台 CNY)
                "pricing_currency": first.get("pricing_currency"),
                "lot_size": first.get("lot_size"),
                "latest_report_date": first.get("report_date"),
                "periods": mine.height,
            }
        )
        # 编报币种只在长表里 (CURRENCY_CODE); 宽表没有, 故从长表补
        stmt = financial_sync_hk.get_hk_financial_df(data_dir, "income")
        if not stmt.is_empty() and "symbol" in stmt.columns:
            rc = (
                stmt.filter(pl.col("symbol") == symbol)
                .get_column("report_currency")
                .drop_nulls()
            )
            if len(rc):
                meta["report_currency"] = rc[0]
    meta["currency_warning"] = (
        "金额列为编报币种(见 report_currency), 估值列为港币; 不同公司编报币种不同, "
        "跨标的比较金额前必须先按币种分组"
    )
    meta["tables"] = {k: _jsonable(v.to_dicts()) for k, v in tables.items()}
    return meta


def _read_ashare_financials(data_dir: Path, table: str) -> pl.DataFrame:
    """读 A 股财务表作为列集参照 (``align=true`` 时用)。失败返回空帧, 不阻断。"""
    path = data_dir / "financials" / table / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        # 只为拿 schema, 读少量行即可, 避免整表载入
        return pl.read_parquet(path, n_rows=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 A 股 financials/%s 参照失败: %s", table, e)
        return pl.DataFrame()


@router.get("/financials/statement")
def get_hk_financial_statement(
    request: Request,
    symbol: Annotated[str, Query(description="港股代码, 如 00700.HK")],
    table: Annotated[str, Query(description="income 或 balance")] = "income",
    report_date: Annotated[str | None, Query(description="只取某报告期, YYYY-MM-DD")] = None,
) -> dict:
    """港股利润表 / 资产负债表明细 (长表: 一行一个会计科目)。

    🔴 **科目名是港式/IFRS 表述, 与 A 股不同** (如「营业额」「营运收入」
    「销售及分销费用」「行政开支」「经营溢利」「融资成本」「除税前溢利」
    「股东应占溢利」「其他全面收益」), 前端**不能复用 A 股科目标签**。

    长表按标的单独同步 (全市场同步需 835~1276 页), 未同步时返回 ``synced=false``
    与空 ``items``, 由前端引导调用 ``POST /api/hk/financials/statement/sync``。
    """
    from app.market import is_hk_symbol

    if not is_hk_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"非港股代码格式: {symbol}")
    if table not in financial_sync_hk.HK_STATEMENT_REPORTS:
        raise HTTPException(
            status_code=400,
            detail=f"table 必须是 {list(financial_sync_hk.HK_STATEMENT_REPORTS)}",
        )

    data_dir = request.app.state.repo.store.data_dir
    df = financial_sync_hk.get_hk_financial_df(data_dir, table)
    if df.is_empty() or "symbol" not in df.columns:
        return {"symbol": symbol, "table": table, "synced": False, "items": []}

    mine = df.filter(pl.col("symbol") == symbol)
    if report_date:
        mine = mine.filter(pl.col("report_date") == report_date)
    if mine.is_empty():
        return {"symbol": symbol, "table": table, "synced": False, "items": []}

    mine = mine.sort(["report_date", "item_code"], descending=[True, False])
    return {
        "symbol": symbol,
        "table": table,
        "synced": True,
        "report_currency": mine.get_column("report_currency").drop_nulls().first(),
        "account_standard": mine.get_column("account_standard").drop_nulls().first(),
        "report_dates": sorted(
            set(mine.get_column("report_date").to_list()), reverse=True
        ),
        "items": _jsonable(mine.to_dicts()),
    }


@router.post("/financials/statement/sync")
def sync_hk_financial_statement(
    request: Request,
    symbol: Annotated[str, Query(description="港股代码, 如 00700.HK")],
    table: Annotated[str, Query(description="income 或 balance")] = "income",
) -> dict:
    """按单标的同步利润表 / 资产负债表。

    单标的历史行数有限 (约 27~55 科目 × 期间数, 实测腾讯 6 期 = 162 行),
    秒级完成, 故**同步执行**而不占用重任务槽位。
    """
    from app.market import Market, assert_symbol_market, is_hk_symbol

    if not is_hk_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"非港股代码格式: {symbol}")
    if table not in financial_sync_hk.HK_STATEMENT_REPORTS:
        raise HTTPException(
            status_code=400,
            detail=f"table 必须是 {list(financial_sync_hk.HK_STATEMENT_REPORTS)}",
        )
    data_dir = request.app.state.repo.store.data_dir
    try:
        assert_symbol_market(symbol, Market.HK)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    rows = financial_sync_hk.sync_hk_statement(data_dir, symbol, table=table)
    return {"symbol": symbol, "table": table, "status": "ok" if rows else "empty", "rows": rows}
