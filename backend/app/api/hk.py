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
from app.plugins.eastmoney.client import EastmoneyError
from app.plugins.eastmoney.hk_financials import HK_DATE_TYPE_ANNUAL
from app.plugins.sina import hk_index as sina_hk_index
from app.plugins.sina.client import SinaError
from app.services import (
    ah_premium,
    financial_sync_hk,
    hk_connect_flow,
    hk_dividend_calendar,
    hk_volatility,
    index_const,
    index_sync_hk,
    instrument_sync_hk,
    kline_sync_hk,
)
from app.services import hk_data_status as hk_status
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


@router.get("/status")
def get_hk_status(request: Request) -> dict:
    """港股数据画像 —— 数据页「港股数据」卡的唯一状态来源。

    与 A 股 ``GET /api/data/status`` 同构 (每个数据集一块, 没数据是 ``null``),
    但**只读港股目录**, 且刻意不复用 A 股那个端点: 那边挂着 ``DataStatusCache``
    与盘后管道的失效钩子, 而港股写入链路没接这套钩子, 混在一起会读到过期快照
    (CONTRIBUTING §12 的拒收情形「写入后存在已知缓存不一致」)。

    聚合口径、为什么不走 DuckDB 视图、为什么不做服务端缓存, 全部写在
    :mod:`app.services.hk_data_status` 的模块 docstring 里。
    """
    return hk_status.hk_data_status(request.app.state.repo.store.data_dir)


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

    ``enriched=true`` (默认) 时链条是 **日K → 复权因子 → enriched**, 与调度管道
    :func:`app.jobs.hk_pipeline.run_hk_sync` 同序同闸门; 复权因子部分失败则跳过
    enriched 并在响应里回报原因 (fail closed)。

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
            enriched_drop = kline_sync_hk.HkEnrichedDropReport()
            adj_rows = 0
            adj_failed: list[str] = []
            enriched_skipped: str | None = None
            if req.enriched:
                # 🔴 复权因子**必须**在 enriched 之前跑, 且此前 HTTP 端点漏了这一步:
                # ``sync_hk_enriched`` 默认 ``require_adj_factor=True``, 缺
                # ``adj_factor_hk/coverage.json`` 就 fail-closed 返回 **0 行**。
                # 该文件只由 ``sync_hk_adj_factor`` 产出, 而换源前它仅被调度管道
                # (``hk_pipeline.run_hk_sync``) 调用 ⇒ 手工点「同步日K」永远拿不到
                # enriched, 且看不出原因。这里补上与调度管道**同序同闸门**的一环。
                def on_adj(done: int, total: int) -> None:
                    pct = int(done * 100 / total) if total else 100
                    progress("hk_adj", pct, f"港股复权因子 {done}/{total}", stage_pct=pct)

                adj_rows, _changed = kline_sync_hk.sync_hk_adj_factor(
                    repo, symbols=universe, failed_out=adj_failed, on_progress=on_adj,
                )
                if adj_failed:
                    # 与 hk_pipeline 同一 fail-closed 判定: 因子库此时**非空但不完整**
                    # (逐批 merge 且部分失败不回滚), 放行 enriched 会让失败标的的
                    # 前复权静默失真 —— 宁可这一轮不出 enriched, 等下一轮补齐。
                    enriched_skipped = (
                        f"复权因子有 {len(set(adj_failed))} 个标的拉取失败, 因子库不完整, "
                        "本轮跳过 enriched (fail closed, 重跑本接口即可补齐)"
                    )
                else:
                    def on_enriched(done: int, total: int) -> None:
                        pct = int(done * 100 / total) if total else 100
                        progress("hk_enriched", pct, f"港股 enriched {done}/{total}", stage_pct=pct)

                    # 不传 start: enriched 按日分区 merge-upsert, 全量发布是幂等的,
                    # 而裁剪发布范围会让「首次同步」漏掉预热期之前的行。
                    enriched_rows = kline_sync_hk.sync_hk_enriched(
                        repo, symbols=universe, on_progress=on_enriched,
                        uncovered_out=enriched_uncovered, drop_report=enriched_drop,
                    )
            return {
                "symbols": len(universe),
                "rows_written": written,
                "adj_factor_rows": adj_rows,
                "adj_factor_failed": sorted(set(adj_failed)),
                "enriched_rows": enriched_rows,
                # 非空 = enriched 被复权因子完整性闸门挡下, 附原因。不回报的话
                # 调用方只看到 enriched_rows=0, 分不清是「没数据」还是「被闸门挡了」。
                "enriched_skipped": enriched_skipped,
                # 非空 = 这些标的复权因子覆盖不完整, 被 enriched **拒算** (fail closed)。
                # 必须回报: 否则调用方只看到 enriched_rows 偏少, 分不清是「本来没数据」
                # 还是「被闸门挡了」—— 那正是完整性凭据要消灭的静默。
                "enriched_uncovered": enriched_uncovered,
                # 被 filter_halt_days 剔除的零成交日行数。剔除是对的 (无成交即无价格
                # 发现, 留着会把 MA/ATR 压平), 但**必须回报**: 港股实测 233,704 行日K
                # 里有 36,445 行 (15.6%) 是零成交日, 不回报的话这 15.6% 的 symbol-day
                # 就凭空消失, 且 enriched_rows 偏少无从归因。
                "enriched_halt_filtered_rows": enriched_drop.halt_filtered_rows,
                # 整段历史都零成交 ⇒ enriched **0 行**的标的。它们**在** coverage.json
                # 的 covered 里 (有日K、有因子), 所以不会出现在 enriched_uncovered ——
                # 实测 17 只, 不回报就是第二类静默。
                "enriched_empty_symbols": enriched_drop.empty_symbols,
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
    """读港股日K (**前复权** OHLC + 全套技术指标), 按日期**升序**。

    🔴 数据源是 ``kline_hk_enriched`` 而**不是** ``kline_hk_daily``, 且**不回退**:
    原始价跨除净日算出的 MA/MACD 会失真, 而图表上看不出任何异常 —— 正是
    CONTRIBUTING §3 的「看起来对但错」。enriched 缺失时返回空 ``rows`` 加
    ``empty_reason`` 归因, 由前端出空态文案与对应动作, 绝不拿原始价充数。
    原始价并没有丢: 每行都带 ``raw_close`` / ``raw_high`` / ``raw_low``。

    ⚠️ 升序与 A 股 ``/api/kline/daily`` 对齐, 但**与本端点旧行为 (倒序) 相反**, 且
    ``/api/hk/index_daily`` 仍是倒序 —— 调用方不得假定港股两个端点同序 (指数页
    适配器已显式排序, 见 ``frontend/src/lib/index-market-adapter.ts``)。

    ⚠️ ``volume`` 单位是**股**不是手 (A 股是手), 换算系数见
    ``app.market.shares_per_volume_unit``; 响应带 ``volume_unit`` 明示, 避免前端按
    A 股习惯乘 100。港股 ``has_price_limit=False`` → ``limit_*`` / ``signal_limit_*``
    列恒 null, 图表不画涨跌停标记是**正确**行为而不是缺数据。

    ``limit`` 截取区间内**最近** N 根 (升序后的尾部)。``end`` 缺省 = 港股时区今天,
    ``start`` 缺省 = ``end`` 往前 365 天 (≈250 交易日)。指标预热由
    :meth:`KlineRepository.get_hk_daily` 内部外扩 150 天处理, 调用方无需多要数据。
    """
    from app.market import is_hk_symbol
    from app.market_time import market_today

    if not is_hk_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"非港股代码格式: {symbol}")

    repo = request.app.state.repo
    rules = rules_for(Market.HK)
    end = end or market_today(Market.HK)
    start = start or (end - timedelta(days=365))

    df = repo.get_hk_daily(symbol, start, end)
    if df.height > limit:
        df = df.tail(limit)
    rows = df.to_dicts()

    empty_reason = None
    if not rows:
        # 空态归因: 「日K都没有」与「日K有但 enriched 没算出来」要给用户完全不同的
        # 动作 —— 前者去同步日K, 后者去重算 enriched (而重算又可能被复权因子完整性
        # 凭据判成 uncovered 而拒算, 见 services/kline_sync_hk 的 coverage.json)。
        # 合成一句「暂无数据」会把用户指向错误的按钮。
        empty_reason = "no_enriched" if repo.hk_daily_row_count(symbol) else "no_daily"

    instruments = repo.get_hk_instruments()
    name = None
    if not instruments.is_empty() and "name" in instruments.columns:
        hit = instruments.filter(pl.col("symbol") == symbol)["name"]
        name = hit[0] if len(hit) else None

    return {
        "symbol": symbol,
        "name": name,
        "market": Market.HK.value,
        "volume_unit": "shares",
        "currency": rules.currency,
        "quote_delay_minutes": rules.quote_delay_minutes,
        "empty_reason": empty_reason,
        "rows": [
            {**r, "date": r["date"].isoformat() if isinstance(r["date"], date) else str(r["date"])}
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
    """港股财务同步状态。未同步时如实返回 ``synced=false`` 而非 500。

    实现搬到了 :func:`app.services.hk_data_status.hk_financials_status`, 与
    ``GET /api/hk/status`` 的 ``financials`` 块**共用同一份代码** —— 两处各算一遍
    的话, 「画像卡说已同步、财务页说没同步」这种自相矛盾迟早会出现。响应形状逐字
    未变 (``test_hk_api.py`` 的既有断言原样通过)。
    """
    return hk_status.hk_financials_status(request.app.state.repo.store.data_dir)


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


# ── 分红日历 ──────────────────────────────────────────────────────
# 与上面的同步端点不同, 这一块**不写任何存储**: 上游报表支持按除净日窗口取全市场
# (实测 13 个月 1592 行 / 8 页), 落库只会多出「同步没跑 → 日历过期」这一类失败
# 模式, 而分红方案本身会被公司修订, 实时读反而更准。理由与口径详见
# :mod:`app.services.hk_dividend_calendar` 的模块 docstring。

#: 缺省向前看的自然日数。港股分红高度集中在年报季 (5~9 月), 60 天足以覆盖
#: 「接下来两个月谁除净」这个主要用途; 要看更远的由调用方显式给 ``end``。
HK_DIVIDEND_DEFAULT_FORWARD_DAYS = 60

#: 窗口跨度上限 (自然日)。硬闸: 上游按 200 行/页翻页, 一年约 1500 行 = 8 页,
#: 400 天约 27 页 —— 再宽就会让一次 HTTP 请求拖到几十秒并占满令牌桶。
HK_DIVIDEND_MAX_WINDOW_DAYS = 400


@router.get("/dividends")
def get_hk_dividends(
    request: Request,
    start: Annotated[date | None, Query(description="除净日起 (含); 缺省=今天")] = None,
    end: Annotated[
        date | None,
        Query(description=f"除净日止 (含); 缺省=今天+{HK_DIVIDEND_DEFAULT_FORWARD_DAYS}天"),
    ] = None,
) -> dict:
    """港股**全市场**分红日历: 除净日落在 ``[start, end]`` 的派息/送转事件。

    一行 = 一个分配方案 (同一标的同一除净日可能有两条, 例如现金 + 送股分开宣派)。
    金额一律折算为**港币/每股**; ``dividend_yield_pct`` 是**百分比数值**
    (``5.0`` 表示 ``5.0%``), 分母为最新**不复权**收盘价, 且是单次派息的收益率
    而非 TTM —— 三个口径都由 :mod:`app.services.hk_dividend_calendar` 负责,
    前端不得再乘 100 (CONTRIBUTING §3.1)。

    三个如实暴露的数据质量字段, 前端**必须**展示而不是藏起来 (§4):

    - ``unparsed_count``: 源数据未给港币金额的方案数 (外币未折算 / 分派股份 /
      权证)。这些行**仍在 ``items`` 里**, 只是 ``dividend_hkd`` 为 null 且带
      ``plan`` 原文 —— 除净日是事实, 不能因为金额缺失就把整条公司行动藏掉。
    - ``missing_close_count``: 最近若干个日K分区里取不到收盘价的标的数, 其
      ``dividend_yield_pct`` 为 null (未同步 / 长期停牌), **不是** 0。
    - ``price_date`` (逐行): 该行收盘价对应的交易日, 让「按哪天的价算的」可见。

    上游失败抛 502 而**不**降级成空列表: 「拉不到」和「这段时间没人除净」是两种
    状态, 合并成一种就是把故障伪装成正常 (§4 fail closed)。
    """
    today = date.today()
    start_date = start or today
    end_date = end or (today + timedelta(days=HK_DIVIDEND_DEFAULT_FORWARD_DAYS))
    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail=f"start ({start_date}) 不能晚于 end ({end_date})"
        )
    span = (end_date - start_date).days
    if span > HK_DIVIDEND_MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"窗口跨度 {span} 天超过上限 {HK_DIVIDEND_MAX_WINDOW_DAYS} 天",
        )

    repo = request.app.state.repo
    try:
        df = hk_dividend_calendar.hk_dividend_calendar(
            repo, start=start_date, end=end_date
        )
    except EastmoneyError as e:
        # 上游报表失败 (报表名变更 / 列变更 / 封禁)。不降级成空表。
        logger.warning("港股分红日历拉取失败: %s", e)
        raise HTTPException(status_code=502, detail=f"港股分红数据源不可用: {e}") from e

    items = df.to_dicts()
    for it in items:  # date 类型不能直接 JSON 序列化
        for k, v in it.items():
            if isinstance(v, date):
                it[k] = v.isoformat()

    return {
        "items": items,
        "total": len(items),
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "market": Market.HK.value,
        "currency": "HKD",
        "source": "eastmoney_datacenter",
        "unparsed_count": int(sum(1 for it in items if not it["parsed"])),
        "missing_close_count": int(sum(1 for it in items if it["raw_close"] is None)),
    }


#: 南向资金流默认窗口 (自然日)。90 天 ≈ 60 个交易日, 够画一张趋势图, 且上游只需
#: 1 页 (60×3=180 行 < 500)。
HK_CONNECT_FLOW_DEFAULT_DAYS = 90

#: 窗口跨度上限 (自然日)。硬闸: 上游 3 行/交易日, 5 年 ≈ 3650 行 = 8 页 ≈ 2s。
#: 再宽就会让一次 HTTP 请求拖到数秒并占满令牌桶 —— 而全史累计已由
#: ``accum_net_inflow`` 免费给出 (上游逐日维护), 无需前端自己拉长窗口去加总。
HK_CONNECT_FLOW_MAX_WINDOW_DAYS = 1825


@router.get("/connect-flow")
def get_hk_connect_flow(
    start: Annotated[date | None, Query(description="交易日起 (含); 缺省=今天-90天")] = None,
    end: Annotated[date | None, Query(description="交易日止 (含); 缺省=今天")] = None,
) -> dict:
    """港股通 (**南向**) 每日资金流: 净买入 / 买入 / 卖出 / 成交额 / 沪深分通道。

    一行 = 一个交易日。金额一律为**元** (人民币), 由
    :mod:`app.plugins.eastmoney.hsgt` 在边界把上游「百万元」换算完成, 前端**不得**
    再乘除任何倍数 (CONTRIBUTING §3.1)。``net_buy`` 是净买入: 买入为正、卖出为负,
    上游满足 ``buy − sell == net_buy`` 与 ``buy + sell == turnover``。

    口径要点:

    - ``accum_net_inflow`` 是**历史累计净流入** (自 2014-11-17 开通起), 由上游逐日
      维护、取最新一行即得。🔴 上游列名叫 ``ACCUM_DEAL_AMT`` (累计成交额) 是**错的**,
      证据见插件 docstring; 本字段已按真实语义改名。
    - ``net_buy_5d`` / ``net_buy_20d`` 是**窗口内**最近 5 / 20 个交易日的合计。
      🔴 窗口内不足该天数、或其中任一日缺数时为 **null 而不是部分和** —— 3 天的和
      冒充「近 5 日」在卡片上看不出来 (§4)。
    - ``currency`` 恒为 ``CNY``, 但这是**推断**而非上游声明 (报表无币种列), 三条
      依据记录在插件 docstring 里。如实透传, 便于日后纠正 (§13)。
    - 🔴 **不提供持股市值**: 上游 ``HOLD_MARKET_CAP`` 口径无法定性 (两种解释相差
      整整 2 倍) 且无独立数据源可验证, 见 ``HSGT_EXCLUDED_COLUMNS``。宁缺勿错。
    - 2015-11-13 之前 ``ggt_sz_*`` 为 **null** (深港通 2016-12-05 才开通), 不是 0。

    上游失败抛 502 而**不**降级成空列表: 「拉不到」和「这段时间没有资金流」是两种
    状态, 合并成一种就是把故障伪装成正常 (§4 fail closed)。窗口内确实没有交易日
    (整段假期) 时返回 ``total=0`` + 全 null 汇总, 这是合法答案。
    """
    today = date.today()
    start_date = start or (today - timedelta(days=HK_CONNECT_FLOW_DEFAULT_DAYS))
    end_date = end or today
    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail=f"start ({start_date}) 不能晚于 end ({end_date})"
        )
    span = (end_date - start_date).days
    if span > HK_CONNECT_FLOW_MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"窗口跨度 {span} 天超过上限 {HK_CONNECT_FLOW_MAX_WINDOW_DAYS} 天",
        )

    try:
        result = hk_connect_flow.hk_connect_flow(start=start_date, end=end_date)
    except EastmoneyError as e:
        # 上游报表失败 (报表名/列名/过滤格式变更、限流、熔断)。不降级成空表。
        logger.warning("南向资金流拉取失败: %s", e)
        raise HTTPException(status_code=502, detail=f"港股通资金流数据源不可用: {e}") from e

    items = result.items.to_dicts()
    for it in items:  # date 类型不能直接 JSON 序列化
        for k, v in it.items():
            if isinstance(v, date):
                it[k] = v.isoformat()

    return {
        "items": items,
        "total": len(items),
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "market": Market.HK.value,
        "currency": result.currency,
        "source": result.source,
        "trading_days": result.trading_days,
        "latest_trade_date": (
            result.latest_trade_date.isoformat() if result.latest_trade_date else None
        ),
        "latest_net_buy": result.latest_net_buy,
        "accum_net_inflow": result.accum_net_inflow,
        "net_buy_5d": result.net_buy_5d,
        "net_buy_20d": result.net_buy_20d,
        "index_close": result.index_close,
        "index_change_pct": result.index_change_pct,
    }


@router.get("/ah-premium")
def get_hk_ah_premium(request: Request) -> dict:
    """全市场 **AH 溢价**: 每只 A+H 两地上市公司一行, A 股相对 H 股贵多少。

    ``premium_pct`` 是**百分比数值** (``78.0`` 表示 A 股比 H 股贵 ``78.0%``),
    **正值 = A 股贵 (溢价), 负值 = H 股贵 (折价)**。前端不得再乘 100
    (CONTRIBUTING §3.1)。按 ``premium_pct`` 降序, null 在最后。

    🔴 **汇率换算是本端点唯一被允许的例外**, 正当性靠四个条件换来 (详见
    :mod:`app.services.ah_premium` 的 docstring): 汇率有明确来源、带日期、在响应里
    显式可见、取不到就 502。响应因此**必须**带 ``fx_rate`` / ``fx_as_of`` /
    ``fx_source`` 三个字段, 前端**必须**把它们显示出来 —— 1% 的汇率误差会线性平移
    **每一只**标的的溢价 1%, 而在页面上完全看不出来。项目其余各处一律禁止换算
    (「换算即伪造」), 不要把这个端点当成先例。

    🔴 两侧价格都取**不复权原始收盘价** (基础分区表, 非 enriched)。A 股分红远多于
    H 股, 用前复权价会把有分红史的标的系统性算错, 且误差偏向一个方向。

    四个如实暴露的数据质量字段, 前端**必须**展示而不是藏起来 (§4):

    - ``missing_price_count``: A 或 H 无本地收盘价的对数 (未同步 / 已退市 / 长期
      停牌)。这些行**仍在 ``items`` 里**, 只是 ``premium_pct`` 为 null。
    - ``stale_price_count``: A/H 收盘价日期差超过 5 天而置空的对数。
    - ``price_lag_count``: 两侧日期不同但在阈值内、**仍计算了溢价**的对数。A/H
      交易日历不同 (港股复活节/圣诞休市), 这是常态而非异常; 逐行的
      ``a_price_date`` / ``h_price_date`` 让「按哪天的价算的」可见。
    - ``ambiguous_a_symbols``: 无法消歧而丢弃的 A 股代码 (多柜台上市日并列等)。
      宁可少一对也不误配 —— 误配的溢价数字看着完全正常, 没人会发现 (§13)。

    ⚠️ ``premium_pct`` 为 null 时前端必须显示「—」, **不可显示 0** —— 0 会被读成
    「A 与 H 等价」, 而真相是「算不出来」。

    上游失败抛 502 而**不**降级: 配对拉不到 (``EastmoneyError``) 与汇率拉不到
    (``SinaError``) 都不得兜底, 尤其**绝不用 1.0 当汇率** —— 那会让每一只标的的
    溢价都错约 17%, 而页面上看不出来 (§4 fail closed)。
    """
    repo = request.app.state.repo
    try:
        result = ah_premium.ah_premium(repo)
    except EastmoneyError as e:
        logger.warning("AH 配对拉取失败: %s", e)
        raise HTTPException(status_code=502, detail=f"AH 配对数据源不可用: {e}") from e
    except SinaError as e:
        # 🔴 汇率失败必须 502, 不得用 1.0 兜底 (见 docstring)。
        logger.warning("港币汇率拉取失败, AH 溢价拒绝降级: %s", e)
        raise HTTPException(status_code=502, detail=f"港币汇率数据源不可用: {e}") from e

    items = result.items.to_dicts()
    for it in items:  # date 类型不能直接 JSON 序列化
        for k, v in it.items():
            if isinstance(v, date):
                it[k] = v.isoformat()

    return {
        "items": items,
        "total": len(items),
        "currency": result.currency,
        "source": "eastmoney_datacenter_f10_orginfo+local_kline",
        "fx_rate": result.fx_rate,
        "fx_as_of": result.fx_as_of.isoformat(),
        "fx_source": result.fx_source,
        "candidate_count": result.candidate_count,
        "missing_price_count": result.missing_price_count,
        "stale_price_count": result.stale_price_count,
        "price_lag_count": result.price_lag_count,
        "ambiguous_a_symbols": list(result.ambiguous_a_symbols),
    }


#: 波动率序列默认返回的交易日数。250 ≈ 一年, 够看清一轮波动率中枢的抬升与回落;
#: 全量 (本地默认 400, 手动加深后可达 2000) 会让一次响应带上数千个点,
#: 而图表在 250 点以上已看不出差别。🔴 这个参数**只裁剪 ``series``**,
#: 不裁剪分位样本 —— 档位必须用全样本标定, 否则「近一年」的 p90 与「八年」的 p90
#: 是两个不同的尺子, 界面上却长得一样。
HK_VOLATILITY_DEFAULT_SERIES_DAYS = 250

#: ``series_days`` 上限。硬闸: 腾讯日K 实测 2000 根封顶 (回溯到 2018-10-08),
#: 故本地不可能超过这个量级; 再大的值只会白白放大响应体。
HK_VOLATILITY_MAX_SERIES_DAYS = 2000


@router.get("/volatility")
def get_hk_volatility(
    request: Request,
    series_days: Annotated[
        int | None,
        Query(
            ge=1,
            le=HK_VOLATILITY_MAX_SERIES_DAYS,
            description=f"绘图序列返回的交易日数; 缺省={HK_VOLATILITY_DEFAULT_SERIES_DAYS}。"
            "只裁剪 series, 不影响档位标定样本",
        ),
    ] = None,
) -> dict:
    """港股**波动率情绪** (VHSI 恒生波动率指数): 当前档位 + 自身历史分位 + 与恒指背离。

    这是 A 股「情绪周期」(``GET /api/regime/phases``) 在港股的**对应维度**, 不是同一个
    东西的港股版: A 股那套完全建立在连板梯队上 (首板家数 / N 板宽度 / 晋级率),
    而港股无涨跌停 (``rules_for(Market.HK).has_price_limit is False``), 连板这个量
    **结构上不存在**。把 A 股规则套过来不会报错, 只会恒等于兜底档, 属「看起来对但错」。
    港股市场上口径清晰的全市场情绪计只有 VHSI (A 股的 iVIX 已停发, 故这个维度
    A 股反而没有)。详见 :mod:`app.services.hk_volatility`。

    🔴 **单位**: ``level`` 是年化隐含波动率的**百分点数** —— ``24.09`` 即 ``24.09%``,
    **不是**小数, 前端**不得**再 ×100 (会显示成 2409%)。``change_pct_1d`` 是 VHSI
    自己今天的涨跌幅, 同为百分数 (``13.63`` 即 ``+13.63%``)。

    🔴 **档位是相对标尺**: ``regime`` 由 ``percentile`` (最新点位在**样本内**的分位
    排名) 落档, 边界见 ``HK_VOLATILITY_BANDS``。按构造样本里恒有约 10% 的日子是
    「极低波」、约 2% 是「恐慌」—— 它回答「现在比过去紧张还是宽松」, 不回答
    「绝对波动率高不高」。故 ``percentiles`` (各分位对应的**实际点位**) 与样本三件套
    (``sample_days`` / ``sample_start`` / ``sample_end``) 一并返回, 让判定可查证。
    **刻意不用 VIX 的 20/30**: VHSI 分布与 VIX 不同 (八年样本 p50=22.51 / p90=29.54),
    套美股阈值会把常态日判成偏高。

    🔴 **样本不足时 fail closed**: ``insufficient_history=true`` 且 ``regime`` /
    ``percentile`` / ``streak_days`` / ``corr_hsi`` 全为 ``null``, 原因在
    ``insufficient_reason`` 里 (可直接展示)。**绝不**用几十根算一个看起来正常的档位。
    本地默认同步深度 400 根 (``HK_INDEX_DAILY_COUNT``), 高于 120 根下限;
    想要更稳的标定可加深: ``POST /api/hk/sync_index_daily {"symbols":["VHSI"],"count":2000}``
    (实测腾讯 2000 根封顶, 回溯到 2018-10-08)。

    ``corr_hsi`` 是 VHSI 与 HSI **日收益**在近 20 个交易日的相关系数, 正常应显著为负;
    转正意味着两者齐涨齐跌, 是行情性质变化的信号。样本不足 / 基准缺失 / 任一侧
    方差为 0 时为 ``null`` —— **不填 0**, 0 会被读成「实测不相关」。

    ``name`` 对 VHSI 恒为 ``null``: 它不在四个核心指数里 (``index_const.HK_CORE_INDEX_NAMES``),
    而 Sina 插件刻意不登记指数名称 (「名称由响应字段在运行时提供, 抄一份只会与源分叉」)。
    展示名由前端适配器提供, 不在后端编一个。

    数据全部来自本地 ``kline_hk_index_daily`` (无上游请求), 故**没有 502 分支**:
    从未同步过时返回 200 + ``insufficient_history=true``, 这是合法的「还没有数据」,
    不是故障。
    """
    repo = request.app.state.repo
    days = series_days or HK_VOLATILITY_DEFAULT_SERIES_DAYS

    result = hk_volatility.hk_volatility(repo)

    # 🔴 只裁剪绘图序列, 不动档位/分位/均线 —— 那些必须基于全样本 (见常量注释)。
    series = result.series[-days:] if days < len(result.series) else result.series

    rules = rules_for(Market.HK)
    return {
        "symbol": result.symbol,
        "name": index_const.core_index_names(Market.HK).get(result.symbol),
        "market": Market.HK.value,
        # VHSI 是无量纲的波动率点位, 不是价格; 但沿用 HK 指数端点的既有字段形状,
        # 让前端不必为这一个页面分叉渲染逻辑 (CONTRIBUTING §4)。
        "currency": "HKD",
        "quote_delay_minutes": rules.quote_delay_minutes,
        "source": result.source,
        "level": result.level,
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "change_1d": result.change_1d,
        "change_pct_1d": result.change_pct_1d,
        "percentile": result.percentile,
        "regime": result.regime,
        "regime_label": result.regime_label,
        "streak_days": result.streak_days,
        "moving_averages": result.moving_averages,
        "percentiles": result.percentiles,
        # 档位上界点位: 让前端能在图上画出**可查证**的档位分界线, 而不必自己拿
        # `bands[].upper_percentile` 去 `percentiles` 里查 —— 那是两套刻度, 只有
        # p10/p90/p98 重合, 查不到时前端只会静默少画一条线。
        "band_upper_levels": result.band_upper_levels,
        "period_high": result.period_high,
        "period_high_date": (
            result.period_high_date.isoformat() if result.period_high_date else None
        ),
        "period_low": result.period_low,
        "period_low_date": result.period_low_date.isoformat() if result.period_low_date else None,
        "corr_hsi": result.corr_hsi,
        "corr_samples": result.corr_samples,
        "corr_window": hk_volatility.HK_VOLATILITY_CORR_WINDOW,
        "bands": [
            {"upper_percentile": upper, "key": key, "label": label}
            for upper, key, label in hk_volatility.HK_VOLATILITY_BANDS
        ],
        "min_sample_days": hk_volatility.HK_VOLATILITY_MIN_SAMPLE_DAYS,
        "series": series,
        "series_days": len(series),
        "sample_days": result.sample_days,
        "sample_start": result.sample_start.isoformat() if result.sample_start else None,
        "sample_end": result.sample_end.isoformat() if result.sample_end else None,
        "insufficient_history": result.insufficient_history,
        "insufficient_reason": result.insufficient_reason,
    }
