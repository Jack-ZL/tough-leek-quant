"""AI 研判记分卡 (P0-7 → P1-10 完整版): 让 AI 记住自己以前准不准。

对标 DSA 的多 horizon 前瞻评估与 astock 的问责纪律: 每次 AI 研判在
T+1/T+3/T+5/T+10 四个周期分别回头核对方向对错, 长期累积胜率档案, 并注入
复盘与个股分析的用户提示词 —— AI 被迫面对自己的历史战绩。

## ⚠️ 这不是回测 (astock 同款自律)

只是"方向事后统计": 不含费率/滑点/仓位管理, 不算夏普不做年化, 命中率高低
都不构成收益承诺。前端面板与注入提示词都必须带这句免责声明。

## 数据从哪来 (零新增采集)

P0-2 答题卡已让每次个股研判输出机读方向 (看多/偏多/中性/偏空/看空), 且随
报告落库 (stock_reports, verdict 字段)。本模块只做两件事:
  1. 结算: 研判日 (T0) 起第 1/3/5/10 个交易日收盘后, 用本地日 K 对照方向判对错;
  2. 记账: 结果写入独立台账 data/user_data/ai_scorecard.json。

台账独立于报告存储 —— 报告只保留最近 50 份会被裁剪, 战绩必须长期累积, 所以
每条结算记录自带完整快照 (方向/日期/价格/对错), 不依赖报告是否还在。

## 台账格式 (P1-10 多周期, 兼容迁移 P0-7 旧格式)

  {report_id: {
    report_id, symbol, name, direction, analysis_date, t0_close,
    horizons: {"1": {t_close, pct, hit, bench_pct, alpha_pct, settled_at}, "3": …},
    expired?: true,        # 超期终态 (已结算的周期保留, 未结算的不再等)
    settled_at,            # 最近一次结算时间
  }}

旧格式 (单周期 horizon:5 + 顶层 hit/pct/t5_close) 在 _load_ledger 时自动迁入
horizons["5"] 槽位, 老战绩不作废; 下次写盘即完成持久化迁移。

## 结算规则 (每周期独立, 数据够一个结一个)

  T0  = 研判日当天或之后第一个交易日的收盘价 (get_daily_asset 前复权口径)
  T+h = T0 起第 h 个交易日收盘价; 日 K 行数 > h 即可结算该周期
  看多/偏多 → T+h > T0 记对; 看空/偏空 → T+h < T0 记对;
  中性 → |涨跌幅| ≤ 该周期容差带记对 (T+1 ±1% / T+3 ±1.5% / T+5 ±2% / T+10 ±3%,
         周期越长容差越宽 —— T+1 也用 ±2% 会把"没动"全判对)
  alpha = 个股涨跌幅 − 同窗口沪深300 (000300.SH) 涨跌幅, 按交易日对齐;
          指数数据缺失 → alpha 记 None, 不阻塞结算 (吸取 astock 北交所票
          永久 pending 的教训: 基准缺失只降级指标, 不卡死生命周期)
  数据不足 2 行 (连 T+1 都结不了) → 保持 pending, 下次触发重试;
  研判日超过 90 天仍有周期凑不齐 → 标记 expired 终态, 已结算周期保留计入统计。

## 触发方式 (惰性结算, 不新增调度器)

  - GET /api/stock-analysis/scorecard (前端展示前顺手结算)
  - save_report 落库新研判后 (老 pending 条目顺带结算)
  - 提示词注入前不结算 (读缓存即可, 避免拖慢 AI 请求)

## 两道硬护栏 (报告 P1-10 明确要求)

  1. MIN_SAMPLE=20: 每个周期独立判断, 结算样本 <20 不给命中率 ——
     1 次全对 ≠ 100% 胜率, 小样本百分比会误导所有人;
  2. "这不是回测": stats 返回 disclaimer 字段, 前端固定展示, prompt_line
     标题自带, 本 docstring 开头也写了一遍。

## 注入与展示

  prompt_line() 生成"你的历史战绩"块, 由调用方拼进 **用户提示词** (不进系统
  提示词 —— 系统提示词支持用户自定义快照, 注入会被覆盖/冲突; 数字忠实度闸门
  的白名单从 prompt 成品文本采集, 注入数字天然放行)。分周期列命中率, 样本
  不足的周期明说不足; 命中率低时提示 AI 措辞更谨慎。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- 可调参数 ----
HORIZONS: tuple[int, ...] = (1, 3, 5, 10)   # 结算周期 (交易日)
PRIMARY_HORIZON = 5                          # 顶层兼容字段取该周期 (P0-7 口径)
# 中性判断的"没大动"容差, 按周期缩放 (见模块 docstring)
NEUTRAL_BANDS_PCT: dict[int, float] = {1: 1.0, 3: 1.5, 5: 2.0, 10: 3.0}
MIN_SAMPLE = 20           # 低于该结算样本数不展示命中率 (astock MIN_MEANINGFUL_SAMPLE 同款)
EXPIRE_DAYS = 90          # 研判日超过该自然天数仍凑不齐 → expired (长期停牌等)
BENCHMARK_SYMBOL = "000300.SH"   # alpha 基准: 沪深300
BENCHMARK_NAME = "沪深300"

DISCLAIMER = "方向事后统计, 不是回测 — 不含费率/滑点/仓位, 命中率不构成收益承诺"

# 方向 → 涨跌期待 (与 ai_verdict.VERDICT_DIRECTIONS 枚举一致)
_BULLISH = ("看多", "偏多")
_BEARISH = ("看空", "偏空")

_LEDGER_FILENAME = "ai_scorecard.json"

# 结算写台账可能来自请求线程 (save_report / scorecard API), 加锁串行化读-改-写
_lock = threading.Lock()


def _ledger_path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / _LEDGER_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _migrate_entry(raw: dict) -> dict:
    """旧格式 (P0-7 单周期) → 新格式 (horizons 字典); 新格式原样规整返回。"""
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("horizons"), dict):
        return raw
    entry: dict = {k: raw[k] for k in
                   ("report_id", "symbol", "name", "direction", "analysis_date", "t0_close")
                   if k in raw}
    entry["settled_at"] = raw.get("settled_at")
    if raw.get("hit") is None:
        # 旧 expired / 未结算终态
        entry["horizons"] = {}
        entry["expired"] = True
        return entry
    h = str(raw.get("horizon") or PRIMARY_HORIZON)
    entry["horizons"] = {h: {
        "t_close": raw.get("t5_close"), "pct": raw.get("pct"), "hit": bool(raw["hit"]),
        "bench_pct": None, "alpha_pct": None, "settled_at": raw.get("settled_at"),
    }}
    return entry


def _load_ledger() -> dict:
    """读台账: {report_id: entry} (旧格式自动迁移)。损坏 → 空台账 (fail-open)。"""
    p = _ledger_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {rid: _migrate_entry(e) for rid, e in data.items() if isinstance(e, dict)}
    except Exception as exc:  # noqa: BLE001 — 台账坏了不能拖垮主流程
        logger.warning("[scorecard] 台账读取失败, 按空处理: %s", exc)
        return {}


def _save_ledger(ledger: dict) -> None:
    """原子写台账 (临时文件 + os.replace, 与 JsonReportStore 同款防半截 JSON)。"""
    p = _ledger_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _report_direction(report: dict) -> str | None:
    """从报告提取有效研判方向; 无答题卡/解析失败 (ok=false) → None (不入账)。"""
    verdict = report.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("ok") is False:
        return None
    direction = verdict.get("direction")
    if direction in (*_BULLISH, "中性", *_BEARISH):
        return direction
    return None


def _report_analysis_date(report: dict) -> date | None:
    """研判日 = 报告 created_at 的日期部分 (本地时区, 与落库口径一致)。"""
    created = str(report.get("created_at") or "")[:10]
    try:
        return date.fromisoformat(created)
    except ValueError:
        return None


def _is_terminal(entry: dict) -> bool:
    """终态 = expired 或四个周期全部结算完成 (不再复查 K 线)。"""
    if entry.get("expired") is True:
        return True
    horizons = entry.get("horizons") or {}
    return all(str(h) in horizons for h in HORIZONS)


def _as_date(v):
    """polars 取出的 date/datetime 统一成 date (datetime 是 date 子类, 先判 datetime)。"""
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


def _close_col(df) -> str | None:
    """前复权 close 优先 (除权除息不扭曲涨跌); 个别存储无 close 列时退 raw_close。"""
    for c in ("close", "raw_close"):
        if c in df.columns:
            return c
    return None


def _benchmark_closes(repo, start: date, end: date) -> dict:
    """沪深300 同窗口 {date: close}; 指数数据缺失/异常 → 空字典 (alpha 降级为 None)。"""
    try:
        bdf = repo.get_daily_asset("index", BENCHMARK_SYMBOL, start, end)
    except Exception as exc:  # noqa: BLE001 — 基准缺失不阻塞个股结算
        logger.warning("[scorecard] 基准指数查询失败 (alpha 降级): %s", exc)
        return {}
    if bdf is None or bdf.is_empty() or "date" not in bdf.columns:
        return {}
    col = _close_col(bdf)
    if col is None:
        return {}
    out: dict = {}
    for d, c in zip(bdf["date"].to_list(), bdf[col].to_list()):
        d = _as_date(d)
        if d is not None and c is not None:
            out[d] = float(c)
    return out


def _judge_hit(direction: str, pct: float, horizon: int) -> bool:
    if direction in _BULLISH:
        return pct > 0
    if direction in _BEARISH:
        return pct < 0
    return abs(pct) <= NEUTRAL_BANDS_PCT[horizon]


def _settle_entry(repo, report: dict, direction: str, analysis_date: date,
                  existing: dict | None) -> dict | None:
    """结算单条研判的所有未结周期。

    返回更新后的台账条目; 无进展 (数据不够/查询失败且未超期) 返回 None。
    K 线走 repo.get_daily_asset (与个股分析同源, 前复权口径, 内存缓存), 取
    研判日起最多 11 行: 第 1 行 = T0 收盘, 第 h+1 行 = T+h 收盘。
    """
    symbol = report.get("symbol") or ""
    start = analysis_date
    end = analysis_date + timedelta(days=EXPIRE_DAYS)
    try:
        asset_type = repo.resolve_asset_type(symbol)
        df = repo.get_daily_asset(asset_type, symbol, start, end)
    except Exception as exc:  # noqa: BLE001 — 单条结算失败不影响其他条目
        logger.warning("[scorecard] %s K线查询失败: %s", symbol, exc)
        return None

    aged_out = (date.today() - analysis_date).days > EXPIRE_DAYS
    base = dict(existing) if existing else {
        "report_id": report.get("id"), "symbol": symbol,
        "name": report.get("name") or "", "direction": direction,
        "analysis_date": analysis_date.isoformat(),
        "horizons": {},
    }
    horizons: dict = dict(base.get("horizons") or {})

    col = _close_col(df) if df is not None and not df.is_empty() else None
    if col is None or len(df) < 2:
        # 连 T+1 都结不了 (新研判/停牌/数据滞后): 太老判过期终态, 否则等下次
        if aged_out:
            base["horizons"] = horizons
            base["expired"] = True
            base["settled_at"] = datetime.now().isoformat(timespec="seconds")
            return base
        return None

    t0_close = float(df[col][0])
    if t0_close <= 0:
        if aged_out:
            base["horizons"] = horizons
            base["expired"] = True
            base["settled_at"] = datetime.now().isoformat(timespec="seconds")
            return base
        return None
    base["t0_close"] = t0_close

    dates = df["date"].to_list() if "date" in df.columns else None
    bench: dict | None = None  # 惰性取基准 (只在真有新周期可结时查一次)
    changed = False
    now_iso = datetime.now().isoformat(timespec="seconds")
    for h in HORIZONS:
        key = str(h)
        if key in horizons or len(df) <= h:
            continue
        t_close = float(df[col][h])
        if t_close <= 0:
            continue
        pct = round((t_close / t0_close - 1.0) * 100.0, 2)
        bench_pct = alpha_pct = None
        if dates is not None:
            if bench is None:
                bench = _benchmark_closes(repo, start, end)
            d0 = _as_date(dates[0])
            dh = _as_date(dates[h])
            b0, bh = bench.get(d0), bench.get(dh)
            if b0 and bh and b0 > 0:
                bench_pct = round((bh / b0 - 1.0) * 100.0, 2)
                alpha_pct = round(pct - bench_pct, 2)
        horizons[key] = {
            "t_close": t_close, "pct": pct,
            "hit": bool(_judge_hit(direction, pct, h)),
            "bench_pct": bench_pct, "alpha_pct": alpha_pct,
            "settled_at": now_iso,
        }
        changed = True

    if aged_out and not all(str(h) in horizons for h in HORIZONS):
        # 超期仍有周期凑不齐 (长期停牌/退市): 终态, 已结算周期保留计入统计
        base["expired"] = True
        changed = True

    if not changed:
        return None
    base["horizons"] = horizons
    base["settled_at"] = now_iso
    return base


def settle_due(repo) -> int:
    """惰性结算所有到期研判, 返回有进展 (新增/更新) 的条目数。整体 fail-open。

    遍历个股报告存储 (≤50 条): 有有效方向、未达终态、研判日已过的逐条结算;
    每周期独立, 数据够一个结一个, 不够的保持 pending 下次再试。
    已结算周期只增不改; expired 与四周期齐备均为终态, 不再复查。
    """
    try:
        from app.services import stock_reports
        reports = stock_reports.list_reports()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scorecard] 报告读取失败, 跳过结算: %s", exc)
        return 0

    today = date.today()
    with _lock:
        ledger = _load_ledger()
        added = 0
        for report in reports:
            rid = report.get("id")
            if not rid:
                continue
            existing = ledger.get(rid)
            if existing is not None and _is_terminal(existing):
                continue
            direction = _report_direction(report)
            analysis_date = _report_analysis_date(report)
            if direction is None or analysis_date is None or analysis_date > today:
                continue
            entry = _settle_entry(repo, report, direction, analysis_date, existing)
            if entry is not None:
                ledger[rid] = entry
                added += 1
        if added:
            try:
                _save_ledger(ledger)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[scorecard] 台账写入失败: %s", exc)
                return 0
        return added


def _horizon_stats(ledger: dict, h: int) -> dict:
    """单周期聚合: 命中率 (带 MIN_SAMPLE 护栏) / 平均涨跌 / 平均 alpha / 分方向。"""
    key = str(h)
    settled = hits = 0
    pcts: list[float] = []
    alphas: list[float] = []
    by_direction: dict[str, dict] = {}
    for entry in ledger.values():
        if not isinstance(entry, dict):
            continue
        hz = (entry.get("horizons") or {}).get(key)
        if not isinstance(hz, dict) or hz.get("hit") is None:
            continue
        settled += 1
        if hz.get("hit") is True:
            hits += 1
        if isinstance(hz.get("pct"), (int, float)):
            pcts.append(float(hz["pct"]))
        if isinstance(hz.get("alpha_pct"), (int, float)):
            alphas.append(float(hz["alpha_pct"]))
        d = by_direction.setdefault(str(entry.get("direction")), {"settled": 0, "hits": 0})
        d["settled"] += 1
        if hz.get("hit") is True:
            d["hits"] += 1
    insufficient = settled < MIN_SAMPLE
    return {
        "horizon_days": h,
        "settled": settled,
        "hits": hits,
        "hit_rate_pct": None if insufficient or settled == 0 else round(hits / settled * 100.0, 1),
        "insufficient": insufficient,
        "avg_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
        "avg_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else None,
        "alpha_samples": len(alphas),
        "neutral_band_pct": NEUTRAL_BANDS_PCT[h],
        "by_direction": by_direction,
    }


def stats() -> dict:
    """汇总战绩 (纯读台账 + 报告存储, 不触发结算)。

    返回:
      horizons         {周期字符串: 单周期聚合} — P1-10 主体
      settled/hits/hit_rate_pct/insufficient/by_direction
                       顶层兼容字段 = T+5 主周期口径 (P0-7 前端 chip 用)
      min_sample       最小样本阈值 (20, 前端文案用)
      horizon_days     主周期 (5)
      neutral_band_pct 主周期中性容差 (前端角标解释用)
      benchmark        alpha 基准说明 (symbol + name)
      disclaimer       "不是回测"免责声明 (前端固定展示)
      pending          有方向但未达终态的报告数 (含部分周期已结、等更长周期的)
      recent           最近 10 条台账记录 (倒序, 含分周期明细)
    """
    ledger = _load_ledger()
    hz_stats = {str(h): _horizon_stats(ledger, h) for h in HORIZONS}
    primary = hz_stats[str(PRIMARY_HORIZON)]

    # pending: 报告里有有效方向但台账未达终态 (报告被裁剪后无法统计, 可接受)
    pending = 0
    try:
        from app.services import stock_reports
        for report in stock_reports.list_reports():
            rid = report.get("id")
            if not rid or _report_direction(report) is None:
                continue
            entry = ledger.get(rid)
            if entry is None or not _is_terminal(entry):
                pending += 1
    except Exception:  # noqa: BLE001
        pass

    recent = sorted(
        (e for e in ledger.values() if isinstance(e, dict)),
        key=lambda e: str(e.get("settled_at") or ""), reverse=True,
    )[:10]

    return {
        "horizons": hz_stats,
        "settled": primary["settled"],
        "hits": primary["hits"],
        "hit_rate_pct": primary["hit_rate_pct"],
        "insufficient": primary["insufficient"],
        "min_sample": MIN_SAMPLE,
        "horizon_days": PRIMARY_HORIZON,
        "neutral_band_pct": primary["neutral_band_pct"],
        "by_direction": primary["by_direction"],
        "benchmark": {"symbol": BENCHMARK_SYMBOL, "name": BENCHMARK_NAME},
        "disclaimer": DISCLAIMER,
        "pending": pending,
        "recent": recent,
    }


def prompt_line() -> str:
    """生成注入用户提示词的"历史战绩"块; 无任何已结算周期 → 空串 (不注入噪音)。

    分周期一行: 样本充足给命中率+平均涨跌+平均超额, 不足明说不足 (MIN_SAMPLE
    护栏)。措辞刻意不含具体价格/日期等易与正文混淆的数字, 只给统计量; 数字
    忠实度闸门的白名单从 prompt 成品采集, 这些统计数字天然放行。
    """
    try:
        s = stats()
    except Exception:  # noqa: BLE001 — 战绩注入失败不能拖垮 AI 请求
        return ""
    hz = s["horizons"]
    if all(v["settled"] <= 0 for v in hz.values()):
        return ""
    lines: list[str] = []
    for h in HORIZONS:
        v = hz[str(h)]
        if v["settled"] <= 0:
            continue
        if v["insufficient"]:
            lines.append(
                f"- T+{h}: 样本不足 ({v['settled']}/{s['min_sample']}), 暂不统计命中率")
            continue
        line = (f"- T+{h}: 命中率 {v['hit_rate_pct']}% ({v['settled']} 次结算判对 {v['hits']} 次)"
                f", 平均涨跌 {v['avg_pct']}%")
        if v["avg_alpha_pct"] is not None:
            line += f", 对{s['benchmark']['name']}平均超额 {v['avg_alpha_pct']}%"
        lines.append(line)
    body = "\n".join(lines)
    return (
        "\n\n## 你的历史战绩 (系统自动统计, 方向事后统计而非回测)\n"
        f"{body}\n"
        "请把历史战绩作为本次分析的置信度校准依据: 命中率不高时措辞应更谨慎、"
        "更明确地提示不确定性和风险, 不要夸大预测的确定性; 命中率较高时也应说明"
        "历史战绩不代表未来表现。样本不足的周期请保持谨慎措辞, 明确标注不确定性。"
    )
