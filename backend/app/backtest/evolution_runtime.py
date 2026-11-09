"""公式进化运行时 (P2-22a): GP 种群进化 + holdout 统计裁决。

进程模型与挖掘/守护一致: MiningJobManager 线程经 worker 子进程调用
run_evolution_runtime, 共用 "mining" heavy 槽 (串行, 避免多份全市场面板并发)。

纪律 (CONTRIBUTING §数据契约 / 防未来函数):
- 适应度只看训练窗; holdout 窗只在最终裁决时对幸存者各碰一次,
  进化循环本身永不接触 holdout (防止反复进化吃掉 OOS)。
- _fwd 前向收益是**标签**, 故意使用 shift(-horizon) 未来信息; 它只进入
  IC 统计, 永不作为因子表达式的输入列 (compile 的 dependencies 白名单
  也不含它), 与挖掘 attach_single_forward_return 同一口径。
- trials_evaluated 记录所有真正跑过数据的去重公式数, DSR 的期望最大
  Sharpe 按它折减 (多重检验校正, 防"试一万次蒙中一个")。
- 所有公式经 compile_formula 安全闸; 退化 (常量/全 NaN) fitness=0 自然淘汰。

统计口径说明: DSR 输入的是"日度 rank IC 序列"的 Sharpe (IC-IR), 不是
策略收益 Sharpe — 这是 v1 的近似 (进化只做初筛); 幸存者发布为 uf_evo_*
自定义因子后, 走既有挖掘/自动挖掘的嵌套 OOS 真实回测漏斗做深度验证。
"""
from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.backtest.mining import compute_rank_correlation
from app.backtest.stats_v2 import (
    bh_fdr_qvalues,
    deflated_sharpe_psr,
    expected_max_sharpe,
    newey_west_t,
    normal_two_sided_p,
)
from app.factors.dsl import FACTOR_COLUMN, compile_formula_cached
from app.factors.evolution import (
    DEFAULT_MAX_DEPTH,
    canonical,
    crossover,
    initial_population,
    mutate,
    random_tree,
    unparse,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]
CancelCheck = Any  # threading.Event / mp.Event 或返回 bool 的可调用

EVOLUTION_ALGORITHM_VERSION = "evolution-v1"

# 预热日历天数: 生成树最深 DEFAULT_MAX_DEPTH 层、每层窗口 ≤60 交易日,
# 最坏回看 ~240 bar; 500 日历日 (~340 交易日) 覆盖有余。
EVOLUTION_WARMUP_DAYS = 500
MIN_TRAIN_DAYS = 100
MIN_HOLDOUT_DAYS = 20
DEFAULT_HOLDOUT_RATIO = 0.25
SURVIVOR_CORR_THRESHOLD = 0.8
MAX_SURVIVORS = 8
# 训练窗内有效 IC 天数低于该值视为退化 (fitness=0)
MIN_IC_DAYS = 20

_PANEL_COLUMNS = (
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "turnover_rate",
)


class EvolutionRuntimeCancelledError(RuntimeError):
    pass


def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is None:
        return
    triggered = cancel_check.is_set() if hasattr(cancel_check, "is_set") else bool(cancel_check())
    if triggered:
        raise EvolutionRuntimeCancelledError("evolution run cancelled")


def _finite(value: Any, ndigits: int = 8) -> float | None:
    """JSON 安全化: 非有限浮点 → None (worker 结果要过 json.dumps)。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, ndigits)


def _parse_date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 日期") from exc


def _int_param(payload: Mapping[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    raw = payload.get(name, default)
    if raw is None:
        raw = default
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"{name} 必须在 [{lo},{hi}] 内")
    return value


def run_evolution_runtime(
    payload: Mapping[str, Any],
    *,
    data_dir: Path,
    service: Any,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """执行一轮公式进化; 返回紧凑 JSON 结果 (worker 会 json.dumps)。"""
    del data_dir  # 面板经 service.engine 读取, 运行时本身不碰存储
    t0 = time.perf_counter()
    request = dict(payload)

    asset_type = str(request.get("asset_type") or "stock")
    if asset_type not in ("stock", "etf"):
        raise ValueError(f"asset_type 必须是 stock/etf: {asset_type}")
    start = _parse_date(request.get("start"), "start")
    end = _parse_date(request.get("end"), "end")
    if start >= end:
        raise ValueError("start 必须早于 end")
    raw_symbols = request.get("symbols")
    symbols = tuple(dict.fromkeys(str(s) for s in raw_symbols)) if raw_symbols else None

    population_size = _int_param(request, "population_size", 120, 20, 400)
    generations = _int_param(request, "generations", 12, 2, 40)
    horizon = _int_param(request, "horizon", 5, 1, 20)
    max_symbols = _int_param(request, "max_symbols", 600, 50, 2000)
    max_depth = _int_param(request, "max_depth", DEFAULT_MAX_DEPTH, 2, 6)
    seed = _int_param(request, "seed", 20261108, 0, 2**31 - 1)
    llm_propose = bool(request.get("llm_propose"))
    llm_count = _int_param(request, "llm_count", 12, 5, 30)

    rng = random.Random(seed)

    def _emit(message: dict) -> None:
        if progress_cb is not None:
            progress_cb(message)

    _raise_if_cancelled(cancel_check)
    _emit({"phase": "panel", "label": "加载行情面板", "done": 0, "total": generations})

    engine = service.engine
    panel = engine.load_panel(
        list(symbols) if symbols else None,
        start - timedelta(days=EVOLUTION_WARMUP_DAYS),
        end,
        columns=list(_PANEL_COLUMNS),
        asset_type=asset_type,
    )
    if panel is None or panel.is_empty():
        raise ValueError("区间内无行情数据, 无法进化")
    panel = panel.sort(["symbol", "date"])
    universe_total = panel["symbol"].n_unique()

    # 确定性符号子采样 (控制单轮算力; rng 由 seed 决定, 可复现)
    if universe_total > max_symbols:
        all_symbols = panel["symbol"].unique().sort().to_list()
        keep = set(rng.sample(all_symbols, max_symbols))
        panel = panel.filter(pl.col("symbol").is_in(list(keep))).sort(["symbol", "date"])
    n_symbols = panel["symbol"].n_unique()

    # 前向收益标签 (仅用于 IC 统计, 永不进入因子表达式输入)
    panel = panel.with_columns(
        (pl.col("close").shift(-horizon) / pl.col("close") - 1.0)
        .over("symbol")
        .alias("_fwd")
    )

    window_dates = (
        panel.filter(pl.col("date").is_between(start, end))["date"]
        .unique()
        .sort()
        .to_list()
    )
    n_days = len(window_dates)
    if n_days < MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS:
        raise ValueError(
            f"数据不足: 区间内仅 {n_days} 个交易日, 至少需要 {MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS} 个"
        )
    n_holdout = max(MIN_HOLDOUT_DAYS, round(n_days * DEFAULT_HOLDOUT_RATIO))
    n_train = n_days - n_holdout
    train_dates = window_dates[:n_train]
    holdout_dates = window_dates[n_train:]
    base = panel.with_columns(
        pl.col("date").is_in(train_dates).alias("_in_train"),
        pl.col("date").is_in(holdout_dates).alias("_in_holdout"),
    )
    del panel

    _emit({
        "phase": "panel",
        "label": f"面板就绪: {n_symbols} 只 × {n_days} 交易日 (训练 {n_train} / 保留 {n_holdout})",
        "done": 0,
        "total": generations,
    })

    # ---------------------------------------------------------- LLM 提议 (P2-22b, fail-open)
    # 提案经编译闸后注入初始种群; 评估计入 trials 账本, DSR 校正自动覆盖。
    # 任何失败 (未配 Key/超时/全废) 都退化为纯 GP 继续, 不废任务。
    llm_info: dict | None = None
    extra_formulas: list[str] = []
    if llm_propose:
        _raise_if_cancelled(cancel_check)
        _emit({"phase": "llm", "label": f"AI 提议公式 (请求 {llm_count} 条)", "done": 0, "total": generations})
        from app.services.evolution_llm import propose_formulas  # 延迟导入: 纯 GP 运行不加载 AI 栈

        llm_info = propose_formulas(count=llm_count, asset_type=asset_type, horizon=horizon)
        extra_formulas = [str(item["formula"]) for item in llm_info.get("accepted") or []]
        label = f"AI 提案: 接受 {len(extra_formulas)} / 提议 {llm_info.get('proposed', 0)}"
        if llm_info.get("error"):
            label += f" · {llm_info['error']}"
        _emit({"phase": "llm", "label": label, "done": 0, "total": generations})
        if not extra_formulas:
            logger.warning(
                "evolution: LLM 提议通道无可用公式, 退化为纯 GP: %s",
                llm_info.get("error") or "提案全部被编译闸拒绝",
            )
        _raise_if_cancelled(cancel_check)

    # ---------------------------------------------------------- 评估
    trials = 0
    ir_sum = 0.0
    ir_sumsq = 0.0
    invalid_count = 0
    duplicate_count = 0
    seen: dict[str, dict] = {}

    def _ic_stats(compiled: Any, flag: str) -> dict | None:
        """在 flag 标记的日期子集上算日度 rank IC 统计; 无法评估返回 None。"""
        required = ["symbol", "date", flag, "_fwd", *sorted(compiled.dependencies)]
        frame = base.select(required)
        out = compiled.frame_transform(frame)
        if out is None:
            return None
        scored = out.select(
            "date",
            pl.col(FACTOR_COLUMN).cast(pl.Float64, strict=False).alias("_f"),
            "_fwd",
            flag,
        ).filter(pl.col(flag))
        total = scored.height
        if total == 0:
            return None
        finite_f = pl.col("_f").is_not_null() & pl.col("_f").is_finite()
        coverage = float(scored.select(finite_f.mean()).item() or 0.0)
        eligible = scored.filter(
            finite_f & pl.col("_fwd").is_not_null() & pl.col("_fwd").is_finite()
        )
        daily = (
            eligible.group_by("date")
            .agg(
                pl.corr(
                    pl.col("_f").rank(method="average"),
                    pl.col("_fwd").rank(method="average"),
                ).alias("ic")
            )
            .filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
            .sort("date")
        )
        ics = [float(v) for v in daily["ic"].to_list()]
        if len(ics) < MIN_IC_DAYS:
            return {
                "ic_mean": 0.0, "ic_std": 0.0, "ir": 0.0,
                "coverage": coverage, "n_days": len(ics), "ics": ics,
                "degenerate": True,
            }
        arr = np.asarray(ics)
        ic_mean = float(arr.mean())
        ic_std = float(arr.std())
        ir = ic_mean / ic_std if ic_std > 1e-12 else 0.0
        return {
            "ic_mean": ic_mean, "ic_std": ic_std, "ir": ir,
            "coverage": coverage, "n_days": len(ics), "ics": ics,
            "degenerate": False,
        }

    def _evaluate(tree: dict) -> dict | None:
        """评估一棵树; 返回记录 (缓存于 seen) 或 None (编译失败/无法物化)。"""
        nonlocal trials, ir_sum, ir_sumsq, invalid_count
        key = canonical(tree)
        cached = seen.get(key)
        if cached is not None:
            return cached
        text = unparse(tree)
        compiled = compile_formula_cached(text)
        if not compiled.ok:
            invalid_count += 1
            return None
        record: dict[str, Any] = {"key": key, "formula": text, "tree": tree}
        try:
            stats = _ic_stats(compiled, "_in_train")
        except Exception:  # noqa: BLE001 — 单公式失败隔离, 不中断整轮进化
            logger.warning("evolution: 公式评估异常, 记为无效: %s", text, exc_info=True)
            stats = None
        if stats is None:
            invalid_count += 1
            return None
        stats.pop("ics", None)
        degenerate = stats.pop("degenerate")
        fitness = 0.0 if degenerate else abs(stats["ir"]) * stats["coverage"]
        record["train"] = {**stats, "fitness": round(fitness, 8)}
        record["fitness"] = fitness
        record["_compiled"] = compiled
        seen[key] = record
        # 试验账本: 凡是真正跑过数据的去重公式都计数 (DSR 多重检验校正用)
        trials += 1
        ir_sum += stats["ir"]
        ir_sumsq += stats["ir"] * stats["ir"]
        return record

    # ---------------------------------------------------------- 进化主循环
    def _breed(records: list[dict]) -> list[dict]:
        nonlocal duplicate_count
        elite_n = max(1, int(len(records) * 0.1))
        pool = records[: max(elite_n * 2, len(records) // 2, 2)]
        children: list[dict] = [record["tree"] for record in records[:elite_n]]
        while len(children) < population_size:
            child = None
            for _ in range(3):
                roll = rng.random()
                if roll < 0.5 and len(pool) >= 2:
                    a, b = rng.choice(pool), rng.choice(pool)
                    child = crossover(a["tree"], b["tree"], rng)
                elif roll < 0.9:
                    child = mutate(rng.choice(pool)["tree"], rng, max_depth)
                else:
                    child = random_tree(rng, max_depth)
                if child is None:
                    continue
                if canonical(child) in seen:
                    duplicate_count += 1
                    child = None
                    continue
                break
            if child is None:
                child = random_tree(rng, max_depth)
            children.append(child)
        return children

    population_trees = initial_population(rng, population_size, max_depth, extra_formulas=extra_formulas)
    generation_stats: list[dict] = []
    for gen in range(generations):
        _raise_if_cancelled(cancel_check)
        records: list[dict] = []
        for index, tree in enumerate(population_trees):
            if index % 25 == 0:
                _raise_if_cancelled(cancel_check)
            record = _evaluate(tree)
            if record is not None:
                records.append(record)
        if not records:
            raise ValueError("整代公式均无法评估, 请检查数据列完整性")
        records.sort(key=lambda item: item["fitness"], reverse=True)
        fitness_values = [item["fitness"] for item in records]
        best_raw = max(fitness_values)
        mean_raw = sum(fitness_values) / len(fitness_values)
        generation_stats.append({
            "gen": gen + 1,
            "evaluated": len(records),
            "best": _finite(best_raw),
            "mean": _finite(mean_raw),
            "best_formula": records[0]["formula"],
            "trials_total": trials,
        })
        _emit({
            "phase": "generation",
            "label": f"第 {gen + 1}/{generations} 代: best={best_raw:.4f} mean={mean_raw:.4f} 累计试验={trials}",
            "done": gen + 1,
            "total": generations,
            "best": _finite(best_raw),
            "mean": _finite(mean_raw),
            "trials_total": trials,
        })
        if gen == generations - 1:
            break
        population_trees = _breed(records)

    # ---------------------------------------------------------- 幸存者裁决
    _raise_if_cancelled(cancel_check)
    _emit({"phase": "survivors", "label": "holdout 统计裁决", "done": generations, "total": generations})
    all_records = sorted(seen.values(), key=lambda item: item["fitness"], reverse=True)
    candidates = all_records[: MAX_SURVIVORS * 3]

    # 幸存者间相关性剪枝 (holdout 窗, 复用挖掘的秩相关口径)。
    # 每个候选的因子列都在完整 base 上物化 (窗口需要预热行), 再按
    # _in_holdout 过滤 — base 已按 symbol/date 排序, filter 保序,
    # 各候选列与 base_holdout 行对齐。
    pruned: list[dict] = []
    kept: list[dict] = []
    if candidates:
        base_holdout = base.filter(pl.col("_in_holdout")).select("symbol", "date")
        names: list[str] = []
        for index, record in enumerate(candidates):
            compiled = record["_compiled"]
            out = compiled.frame_transform(
                base.select(["symbol", "date", "_in_holdout", *sorted(compiled.dependencies)])
            )
            if out is None:
                pruned.append({"key": record["key"], "formula": record["formula"], "reason": "holdout 物化失败"})
                continue
            name = f"_evo_{index}"
            series = out.filter(pl.col("_in_holdout"))[FACTOR_COLUMN]
            base_holdout = base_holdout.with_columns(series.alias(name))
            record["_column"] = name
            names.append(name)
        if names:
            corr = compute_rank_correlation(base_holdout, names)
            kept_names: list[str] = []
            for record in candidates:
                name = record.get("_column")
                if name is None:
                    continue
                if len(kept) >= MAX_SURVIVORS:
                    pruned.append({"key": record["key"], "formula": record["formula"], "reason": "超幸存者名额"})
                    continue
                conflict = None
                row = names.index(name)
                for kept_name in kept_names:
                    value = corr.matrix[row][names.index(kept_name)]
                    if value is not None and math.isfinite(value) and abs(value) > SURVIVOR_CORR_THRESHOLD:
                        conflict = (kept_name, float(value))
                        break
                if conflict is not None:
                    pruned.append({
                        "key": record["key"],
                        "formula": record["formula"],
                        "reason": f"与更优幸存者相关性 {abs(conflict[1]):.2f} > {SURVIVOR_CORR_THRESHOLD}",
                    })
                    continue
                kept.append(record)
                kept_names.append(name)

    # holdout 统计 + FDR + DSR
    survivors: list[dict] = []
    holdout_stats: list[dict] = []
    for record in kept:
        stats = _ic_stats(record["_compiled"], "_in_holdout")
        if stats is None:
            pruned.append({"key": record["key"], "formula": record["formula"], "reason": "holdout 无有效数据"})
            continue
        ics = stats.pop("ics")
        stats.pop("degenerate", None)
        train_ic_mean = record["train"]["ic_mean"] or 0.0
        direction_sign = 1.0 if train_ic_mean >= 0 else -1.0
        directed = [value * direction_sign for value in ics]
        nw = newey_west_t(directed, lag=horizon) if len(directed) > horizon + 2 else None
        t_stat = nw[0] if nw else None
        p_value = normal_two_sided_p(t_stat) if t_stat is not None and math.isfinite(t_stat) else None
        arr = np.asarray(directed)
        skew = kurt = None
        if arr.size >= 5 and float(arr.std()) > 1e-12:
            standardized = (arr - arr.mean()) / arr.std()
            skew = float((standardized**3).mean())
            kurt = float((standardized**4).mean())
        holdout_stats.append({
            "record": record,
            "stats": stats,
            "direction": "high" if direction_sign > 0 else "low",
            "ir_directed": abs(stats["ir"]),
            "t_nw": t_stat,
            "p_value": p_value,
            "skew": skew,
            "kurt": kurt,
            "n_days": stats["n_days"],
        })
    q_values = bh_fdr_qvalues([item["p_value"] for item in holdout_stats])
    # DSR: 期望最大 Sharpe 按全部试验次数折减; 方差用训练窗 IR 的全体试验方差
    var_sharpes = 0.0
    if trials > 1:
        var_sharpes = max(0.0, ir_sumsq / trials - (ir_sum / trials) ** 2)
    em_sharpe = expected_max_sharpe(trials, var_sharpes)
    for item, q_value in zip(holdout_stats, q_values, strict=True):
        record = item["record"]
        dsr = deflated_sharpe_psr(
            item["ir_directed"],
            item["n_days"],
            skewness=item["skew"],
            kurtosis=item["kurt"],
            expected_max_sharpe=em_sharpe,
        )
        train_stats = dict(record["train"])
        survivors.append({
            "key": record["key"],
            "formula": record["formula"],
            "direction": item["direction"],
            "train": {
                "ic_mean": _finite(train_stats["ic_mean"]),
                "ir": _finite(train_stats["ir"]),
                "coverage": _finite(train_stats["coverage"]),
                "fitness": _finite(train_stats["fitness"]),
                "n_days": train_stats["n_days"],
            },
            "holdout": {
                "ic_mean": _finite(item["stats"]["ic_mean"]),
                "ir": _finite(item["stats"]["ir"]),
                "t_nw": _finite(item["t_nw"], 4),
                "p_value": _finite(item["p_value"], 6),
                "q_value": _finite(q_value, 6),
                "dsr": _finite(dsr, 6),
                "coverage": _finite(item["stats"]["coverage"]),
                "n_days": item["stats"]["n_days"],
            },
        })

    return {
        "status": "succeeded",
        "algorithm_version": EVOLUTION_ALGORITHM_VERSION,
        "run_id": str(request.get("run_id") or ""),
        "request": {
            "asset_type": asset_type,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": list(symbols) if symbols else None,
            "population_size": population_size,
            "generations": generations,
            "horizon": horizon,
            "max_symbols": max_symbols,
            "max_depth": max_depth,
            "seed": seed,
            "llm_propose": llm_propose,
            "llm_count": llm_count,
        },
        "llm": llm_info,
        "panel": {
            "universe_total": universe_total,
            "n_symbols": n_symbols,
            "n_rows": base.height,
            "n_days": n_days,
            "train_days": n_train,
            "holdout_days": n_holdout,
            "train_start": train_dates[0].isoformat(),
            "train_end": train_dates[-1].isoformat(),
            "holdout_start": holdout_dates[0].isoformat(),
            "holdout_end": holdout_dates[-1].isoformat(),
        },
        "generations": generation_stats,
        "trials": {
            "evaluated": trials,
            "duplicates": duplicate_count,
            "invalid": invalid_count,
        },
        "dsr": {
            "expected_max_sharpe": _finite(em_sharpe, 6),
            "variance_sharpes": _finite(var_sharpes, 8),
        },
        "survivors": survivors,
        "pruned": pruned,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
