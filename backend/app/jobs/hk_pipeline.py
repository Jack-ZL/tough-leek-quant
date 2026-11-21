"""港股定时同步 job —— 与 A 股盘后管道**调度隔离 + 单向让路**。

为什么单独一个模块、而不是塞进 ``daily_pipeline.start_scheduler``
================================================================

方案 (docs/hk-market-plan.md §14「调度接线」) 的硬要求是: **港股失败不能危及
A 股关键路径**。这条要求在本仓库里有个不显然的坑 —— ``pipeline_jobs`` 的两道
并发闸都是**全局**的:

1. ``job_store.create()`` 单飞: 已有 pending/running 任务时返回 ``is_new=False``,
   调用方直接跳过本轮。
2. ``try_acquire_run_slot()`` 重任务执行槽: 全局互斥, 非阻塞抢占。

港股 job 若复用 ``_run_tracked``(即同时用上这两道闸), 后果是**双向**的: A 股在跑
时港股跳过(可接受), 但港股在跑时 **A 股盘后管道也会整轮跳过**(不可接受 —— 那就是
港股危及了 A 股关键路径)。

因此本模块刻意:

* **不进 ``job_store``** —— 港股任务对 A 股完全不可见, A 股的单飞判定永远不会
  因为港股而触发。代价是港股进度不出现在 ``/api/pipeline/jobs`` 列表里, 只进日志。
* **不占执行槽** —— 同理, A 股永远抢得到槽。
* **单向让路** —— 港股开跑前查 ``job_store.active_id()``, 只要有活跃任务(A 股盘后
  管道或任何手动同步)就本轮跳过。让路只发生在港股这一侧。
* **异常全吞** —— 任何失败只记日志, 绝不向 APScheduler 上抛。

并发写盘安全性
--------------

港股四张表 (``instruments_hk`` / ``kline_hk_daily`` / ``adj_factor_hk`` /
``kline_hk_enriched``) 与 A 股目录物理隔离, 不存在同文件竞态; 视图重建统一走
:meth:`KlineRepository.rebuild_views`, 内部由 ``_ddl_lock`` (RLock) 互斥, 与 A 股
管道并发调用是安全的。共享的 ``EastmoneyClient`` 限流令牌桶按 host 计数, 港股与
A 股请求会互相排队 —— 这是**保护**而非危害, 且默认时刻(16:40 HKT)本就与 A 股
盘后管道(默认 15:35)错开。

一轮定时同步包含**两条独立链路**
--------------------------------

:func:`hk_sync_task` 依次跑:

1. :func:`run_hk_sync` —— 港股**个股** (东财 push2): 维表 → 日K → 复权因子 →
   enriched → 重建视图, 带 fail-closed 依赖链。
2. :func:`run_hk_index_sync` —— 港股**指数** (腾讯 ``ifzq`` 日K): 与个股**源不同、
   表不同、失败语义不同**, 因此个股阶段的任何早退都不连带指数不同步。

两条链路共用同一个调度开关 (``hk_sync_schedule.enabled``) 与同一套隔离纪律
(不进 job_store / 不占执行槽 / 单向让路 / 异常全吞)。指数日K另有手动入口
``POST /api/hk/sync_index_daily``, 供指数页单独补数。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:  # pragma: no cover
    from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

HK_SYNC_JOB_ID = "hk_daily_sync"

#: 港股时段用 Asia/Hong_Kong。与 Asia/Shanghai 同为 UTC+8 且均无夏令时, 偏移一致,
#: 但语义上港股 job 该挂港股时区 —— 万一未来任一地引入夏令时也不会跟着错。
HK_SYNC_TIMEZONE = "Asia/Hong_Kong"

#: ``on_progress`` 每标的回调一次, 2900+ 标的会刷爆日志, 按固定步长节流。
_PROGRESS_LOG_EVERY = 200


def _throttled_progress(stage: str) -> Callable[[int, int], None]:
    """把 ``on_progress(done, total)`` 转成节流日志, 首尾与整步长各记一条。"""

    def emit(done: int, total: int) -> None:
        if not total:
            return
        if done == total or done % _PROGRESS_LOG_EVERY == 0:
            logger.info("港股同步 %s: %d/%d", stage, done, total)

    return emit


def _yield_to_ashare() -> str | None:
    """有活跃数据任务时返回让路原因, 否则 None。

    只读 ``job_store.active_id()``, **不创建任务也不抢执行槽** —— 见模块 docstring:
    港股一旦在那两处留下痕迹, A 股盘后管道就会被单飞判定跳过。
    """
    try:
        from app.services.pipeline_jobs import job_store

        active = job_store.active_id()
    # 让路检查本身绝不能成为失败源; 查不到状态时保守跳过(见下)。
    except Exception as e:  # noqa: BLE001
        logger.warning("港股同步: 活跃任务检查失败, 保守跳过本轮: %s", e)
        return "活跃任务状态不可知(保守让路)"
    if active:
        return f"已有数据任务在运行 (job_id={active})"
    return None


def run_hk_sync(
    repo: KlineRepository,
    *,
    include_cny_counter: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """跑一轮完整港股同步: 维表 → 日K → 复权因子 → enriched → 重建视图。

    返回各阶段计数的 dict, **单阶段失败不抛**, 记进 ``errors`` 并按依赖关系决定
    是否继续。阶段依赖:

    * 维表失败 → 软失败, 沿用上一份维表继续 (拉取失败时 ``sync_instruments_hk``
      本就返回 0 且不写盘)。
    * 维表为空 → 硬中止, 后面三步都无从谈起。
    * 日K失败 → 中止 enriched (enriched 直接读日K)。
    * 复权因子有标的失败 → **中止 enriched**, 见下方 🔴 说明。

    🔴 复权因子部分失败为什么必须挡住 enriched
    ------------------------------------------

    ``sync_hk_adj_factor`` 是**逐批 merge** 落盘的: 即使有标的拉取失败, 成功那部分
    也已经写进 ``adj_factor_hk``。``failed`` 只用于决定「要不要落一个空库当凭据」,
    并不回滚已 merge 的行。于是因子库**非空但不完整**, ``load_hk_adj_factors`` 的
    三态闸门 (None=未知→拒算 / 空=确认无事件→照算 / 非空→照算) 会判定为「非空」而
    放行; 失败标的在 ``_apply_adj_factor`` 里拿到 ``cum_factor.fill_null(1.0)``,
    被当成「该股从无分红」—— 前复权价静默偏高、指标静默失真。

    这正是 CONTRIBUTING §4「fail closed, 绝不静默切换到错误数据或错误语义」要拦的
    情形, 所以本 job 在因子阶段报失败时**宁可本轮不出 enriched**, 等下一轮补齐。
    (服务层已补第二道防线: ``sync_hk_adj_factor`` 落一份**按标的**的完整性凭据
    ``adj_factor_hk/coverage.json``, ``sync_hk_enriched`` 据此逐个拒算 uncovered 标的、
    凭据缺失则整轮中止, 手动 API 路径因此也已闭合。本函数**保留**这道更严的整体让路:
    定时全量轮次要求「要么齐要么不算」, 半轮 enriched 会让看板出现新旧指标混杂的
    截面。)
    """
    from app.plugins.eastmoney.client import EastmoneyClient
    from app.plugins.tencent.client import TencentClient
    from app.services import instrument_sync_hk, kline_sync_hk

    emit = on_progress or (lambda stage, done, total: None)
    result: dict[str, Any] = {
        "instruments": 0,
        "universe": 0,
        "daily_written": 0,
        "adj_factor_rows": 0,
        "adj_factor_failed": 0,
        "enriched_written": 0,
        "skipped": [],
        "errors": [],
    }
    data_dir = repo.store.data_dir

    # 🔴 换源后日K与复权因子分属**两个上游主机**, 故各持一个 client:
    #   日K      → 腾讯 ``ifzq.gtimg.cn``  (东财 push2his 对本机 IP 级封禁)
    #   复权因子 → 东财 ``datacenter-web`` (F10 分红, 与 push2 不同主机, 未被封)
    # 两个 client 各自持有连接池与「按 host 分桶」的令牌桶/熔断器, 互不牵连
    # (一个源被限流不该拖慢另一个)。维表阶段自建 client, 见 instrument_sync_hk。
    client = EastmoneyClient()
    tx_client = TencentClient()
    try:
        # --- 1. 维表 (软失败) -------------------------------------------------
        emit("instruments", 0, 0)
        try:
            result["instruments"] = instrument_sync_hk.sync_instruments_hk(data_dir)
            if not result["instruments"]:
                # 返回 0 有两种可能: 拉取失败(保留旧维表) 或 真的空市场。前者更常见,
                # 记 warning 后继续 —— 旧维表仍可用, 不该因此中断整轮。
                msg = "港股维表同步返回 0 行(拉取失败则已保留上一份维表), 沿用现有维表继续"
                logger.warning("港股同步: %s", msg)
                result["errors"].append(msg)
        except Exception as e:
            msg = f"港股维表同步异常: {e}"
            logger.exception("港股同步: 维表阶段失败")
            result["errors"].append(msg)

        # --- 2. 标的清单 (硬闸) ----------------------------------------------
        universe = kline_sync_hk.hk_daily_universe(
            data_dir, include_cny_counter=include_cny_counter
        )
        result["universe"] = len(universe)
        if not universe:
            msg = "港股维表为空, 中止本轮 (请先成功执行一次 POST /api/hk/sync_instruments)"
            logger.error("港股同步: %s", msg)
            result["errors"].append(msg)
            return result

        # --- 3. 日K (失败则中止后续) -----------------------------------------
        emit("daily", 0, len(universe))
        try:
            result["daily_written"] = kline_sync_hk.sync_hk_daily(
                repo,
                include_cny_counter=include_cny_counter,
                incremental=True,
                client=tx_client,
                on_progress=_throttled_progress("日K"),
            )
        except Exception as e:
            msg = f"港股日K同步失败, 中止 enriched: {e}"
            logger.exception("港股同步: 日K阶段失败")
            result["errors"].append(msg)
            result["skipped"].append("enriched(日K失败)")
            return result

        # --- 4. 复权因子 ------------------------------------------------------
        emit("adj_factor", 0, len(universe))
        failed: list[str] = []
        try:
            rows, _changed = kline_sync_hk.sync_hk_adj_factor(
                repo, client=client, failed_out=failed,
                on_progress=_throttled_progress("复权因子"),
            )
            result["adj_factor_rows"] = rows
            result["adj_factor_failed"] = len(set(failed))
        except Exception as e:
            msg = f"港股复权因子同步异常, 中止 enriched: {e}"
            logger.exception("港股同步: 复权因子阶段失败")
            result["errors"].append(msg)
            result["skipped"].append("enriched(复权因子异常)")
            return result

        if failed:
            # 见 docstring 的 🔴 段: 因子库此时非空但不完整, 放行 enriched 会让
            # 失败标的的前复权静默失真。
            msg = (
                f"港股复权因子有 {len(set(failed))} 个标的拉取失败, 因子库不完整, "
                f"本轮跳过 enriched (fail closed, 等下一轮补齐)"
            )
            logger.error("港股同步: %s", msg)
            result["errors"].append(msg)
            result["skipped"].append("enriched(复权因子不完整)")
            return result

        # --- 5. enriched ------------------------------------------------------
        emit("enriched", 0, len(universe))
        try:
            result["enriched_written"] = kline_sync_hk.sync_hk_enriched(
                repo,
                on_progress=_throttled_progress("enriched"),
                require_adj_factor=True,
            )
        except Exception as e:
            msg = f"港股 enriched 生成失败: {e}"
            logger.exception("港股同步: enriched 阶段失败")
            result["errors"].append(msg)
            return result

        # --- 6. 视图重建 ------------------------------------------------------
        # sync_hk_daily 只落 parquet 不刷视图; sync_hk_enriched 仅在 written>0 时刷。
        # 这里无条件补一次, 保证「日K写了但 enriched 没写」时 SQL 侧也能读到新数据,
        # 否则就是 CONTRIBUTING 明列的拒收情形「写入后存在已知缓存不一致」。
        # rebuild_views 内部走 _ddl_lock, 与 A 股管道并发调用安全。
        try:
            repo.rebuild_views()
        except Exception as e:
            msg = f"港股同步后重建视图失败: {e}"
            logger.exception("港股同步: 视图重建失败")
            result["errors"].append(msg)
    finally:
        client.close()
        tx_client.close()

    logger.info(
        "港股同步完成: 维表 %d / 标的 %d / 日K +%d 行 / 因子 +%d 行 / enriched +%d 行"
        " / 跳过 %s / 错误 %d",
        result["instruments"], result["universe"], result["daily_written"],
        result["adj_factor_rows"], result["enriched_written"],
        result["skipped"] or "无", len(result["errors"]),
    )
    return result


def run_hk_index_sync(repo: KlineRepository) -> dict[str, Any]:
    """跑一轮港股**指数**日K同步 (腾讯源), 与个股管道完全独立。

    为什么不并进 :func:`run_hk_sync`
    --------------------------------

    指数与个股是两条**没有共同依赖**的链路, 三处都不同:

    * **源不同** —— 个股走东财 push2, 指数走腾讯 ``ifzq``。东财被 IP 级封禁时
      (2026-11 实测) 个股整轮失败, 指数仍应照常同步。
    * **表不同** —— ``kline_hk_index_daily`` / ``kline_hk_index_enriched`` 与个股
      四张表物理隔离, 无同文件竞态。
    * **失败语义不同** —— 个股管道有「复权因子不完整就挡住 enriched」的
      fail-closed 依赖链; 指数不做复权, 不存在这条链。

    因此 ``run_hk_sync`` 的任何一个早退分支(维表空 / 日K失败 / 因子不完整 /
    enriched 异常)都**不该**连带指数不同步 —— 否则指数页会在个股源出问题时
    一起静默变陈旧。本函数由 :func:`hk_sync_task` 在个股阶段之后**无条件**调用。

    失败语义与个股阶段一致: 异常全吞、只记日志。``sync_hk_index_daily`` 返回的
    ``failed`` **只含真正的拉取错误**, 不含「腾讯无覆盖」(27 个白名单代码中仅
    14 个有日K, 无覆盖是源的客观事实, 记为失败会让每轮都告警), 详见
    :mod:`app.services.index_sync_hk` 的三态契约。
    """
    from app.services import index_sync_hk

    result: dict[str, Any] = {"index_daily_written": 0, "index_failed": []}
    try:
        written, failed = index_sync_hk.sync_hk_index_daily(
            repo, on_progress=_throttled_progress("指数日K")
        )
        result["index_daily_written"] = written
        result["index_failed"] = list(failed)
    except Exception as e:
        msg = f"港股指数日K同步失败: {e}"
        logger.exception("港股同步: 指数阶段失败")
        result.setdefault("errors", []).append(msg)
    logger.info(
        "港股指数同步完成: 日K +%d 行 / 真失败 %d 个",
        result["index_daily_written"], len(result["index_failed"]),
    )
    return result


def hk_sync_task(repo: KlineRepository) -> dict[str, Any] | None:
    """APScheduler 入口: 让路检查 → 个股同步 → 指数同步 → 吞掉一切异常。

    返回 result dict 仅供测试/日志断言; 调度路径不消费返回值。

    🔴 指数阶段**不受个股阶段成败影响**: 个股抛未预期异常时仍会跑指数
    (理由见 :func:`run_hk_index_sync`)。只有「让路」和「两段都炸」才返回 None。
    """
    reason = _yield_to_ashare()
    if reason:
        logger.info("港股定时同步跳过: %s", reason)
        return None

    stock: dict[str, Any] | None = None
    try:
        from app.services import preferences

        sched = preferences.get_hk_sync_schedule()
        stock = run_hk_sync(
            repo, include_cny_counter=bool(sched.get("include_cny_counter", False))
        )
    except Exception:
        logger.exception("港股定时同步未预期失败(已隔离, A 股调度不受影响)")

    try:
        index = run_hk_index_sync(repo)
    except Exception:
        logger.exception("港股指数同步未预期失败(已隔离, A 股调度不受影响)")
        index = None

    if stock is None and index is None:
        return None
    return {**(stock or {}), **(index or {})}


def register_hk_job(scheduler, repo: KlineRepository) -> bool:
    """按偏好注册/更新港股同步 job。返回是否已注册。

    与 ``_register_review_job`` 同套路: ``replace_existing=True``, 重复调用只更新
    trigger, 供启动时与 settings API 改时间时共用。

    ⚠️ 传给 ``add_job`` 的必须是**函数对象**而非 lambda 包裹的协程 —— 见
    ``daily_pipeline._register_review_job`` 的注释。这里 ``hk_sync_task`` 是同步
    函数, APScheduler 会放线程池执行, 不会阻塞事件循环。
    """
    from app.services import preferences

    sched = preferences.get_hk_sync_schedule()
    if not sched["enabled"]:
        return False

    scheduler.add_job(
        hk_sync_task,
        args=[repo],
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=sched["hour"],
            minute=sched["minute"],
            timezone=HK_SYNC_TIMEZONE,
        ),
        id=HK_SYNC_JOB_ID,
        # 港股同步非关键路径, 但也不宜拖太久: 错过 2 小时(比如服务重启)就放弃本轮,
        # 免得在盘中/深夜补跑一次无意义的全量拉取。
        misfire_grace_time=7200,
        replace_existing=True,
    )
    logger.info(
        "hk_daily_sync enabled @%02d:%02d mon-fri (%s, include_cny_counter=%s)",
        sched["hour"], sched["minute"], HK_SYNC_TIMEZONE, sched["include_cny_counter"],
    )
    return True


def unregister_hk_job(scheduler) -> bool:
    """移除港股同步 job。job 本就不存在时返回 False, 不抛。"""
    try:
        scheduler.remove_job(HK_SYNC_JOB_ID)
    # 从未开启过时 APScheduler 抛 JobLookupError, 属正常路径, 无需记日志。
    except Exception:  # noqa: BLE001
        return False
    logger.info("hk_daily_sync disabled (job removed)")
    return True
