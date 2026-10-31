"""AI 研判记分卡 (P0-7): 让 AI 记住自己以前准不准。

对标 StrongRookie 的"AI 复盘前翻旧账": 它把历史预测命中率写进提示词, 让 AI
带着自己的历史战绩做新判断; 我们把命中率同时展示给用户, 判断该信多少。

## 数据从哪来 (零新增采集)

P0-2 答题卡已让每次个股研判输出机读方向 (看多/偏多/中性/偏空/看空), 且随
报告落库 (stock_reports, verdict 字段)。本模块只做两件事:
  1. 结算: 研判日 (T0) 起第 5 个交易日 (T+5) 收盘后, 用本地日 K 对照方向判对错;
  2. 记账: 结果写入独立台账 data/user_data/ai_scorecard.json。

台账独立于报告存储 —— 报告只保留最近 50 份会被裁剪, 战绩必须长期累积, 所以
每条结算记录自带完整快照 (方向/日期/价格/对错), 不依赖报告是否还在。

## 结算规则 (方向事后统计, 不是回测)

  T0  = 研判日当天或之后第一个交易日的收盘价 (get_daily_asset 前复权口径)
  T5  = T0 起第 5 个交易日收盘价 (共取 6 行)
  看多/偏多 → T5 > T0 记对; 看空/偏空 → T5 < T0 记对;
  中性 → |涨跌幅| ≤ 2% 记对 (中性判断"不会大动", 动得小就算它说对了)
  数据不足 6 行 (停牌/数据滞后) → 保持 pending, 下次触发重试;
  超过 90 天仍凑不齐 → 标记 expired, 不再重试也不计入统计。

## 触发方式 (惰性结算, 不新增调度器)

  - GET /api/stock-analysis/scorecard (前端展示前顺手结算)
  - save_report 落库新研判后 (老 pending 条目顺带结算)
  - 提示词注入前不结算 (读缓存即可, 避免拖慢 AI 请求)

## 注入与展示

  prompt_line() 生成"你的历史战绩"块, 由调用方拼进 **用户提示词** (不进系统
  提示词 —— 系统提示词支持用户自定义快照, 注入会被覆盖/冲突; 数字忠实度闸门
  的白名单从 prompt 成品文本采集, 注入数字天然放行)。命中率低于阈值时提示 AI
  措辞更谨慎; 样本不足 (<10 次) 时只说样本不足, 不给命中率 —— 3 次里对 2 次
  显示 67% 会误导所有人。

## P0-10 边界 (刻意不做)

  多周期 (1/3/5/10 日) 命中率、最小样本提到 20、完整战绩面板属于 P0-10
  "AI 命中率多周期统计"。本模块条目已预留 horizon 字段, 届时扩展不破坏台账。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- 可调参数 (P0-10 时再考虑做成偏好项) ----
HORIZON_DAYS = 5          # 结算周期: T+5 个交易日
NEUTRAL_BAND_PCT = 2.0    # 中性判断的"没大动"容差 (±2%)
MIN_SAMPLE = 10           # 低于该结算样本数不展示命中率 (避免小样本误导)
EXPIRE_DAYS = 90          # 研判日超过该自然天数仍无法结算 → expired (长期停牌等)

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


def _load_ledger() -> dict:
    """读台账: {report_id: entry}。文件损坏 → 空台账 (fail-open, 战绩可重建)。"""
    p = _ledger_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
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


def _settle_one(repo, report: dict, direction: str, analysis_date: date) -> dict | None:
    """结算单条研判: 返回台账条目; 数据不足返回 None (保持 pending 等下次)。

    K 线走 repo.get_daily_asset (与个股分析同源, 前复权口径, 内存缓存), 取
    研判日起 6 行: 第 1 行 = T0 收盘, 第 6 行 = T+5 收盘。
    """
    start = analysis_date
    end = analysis_date + timedelta(days=EXPIRE_DAYS)
    try:
        asset_type = repo.resolve_asset_type(report.get("symbol") or "")
        df = repo.get_daily_asset(asset_type, report["symbol"], start, end)
    except Exception as exc:  # noqa: BLE001 — 单条结算失败不影响其他条目
        logger.warning("[scorecard] %s K线查询失败: %s", report.get("symbol"), exc)
        return None
    if df is None or df.is_empty() or len(df) < HORIZON_DAYS + 1:
        # 数据还不够 T+5 (新研判/停牌/同步滞后): 太老的就判过期, 否则等下次
        if (date.today() - analysis_date).days > EXPIRE_DAYS:
            return {
                "report_id": report.get("id"), "symbol": report.get("symbol"),
                "name": report.get("name") or "", "direction": direction,
                "analysis_date": analysis_date.isoformat(), "horizon": HORIZON_DAYS,
                "hit": None, "expired": True,
                "settled_at": datetime.now().isoformat(timespec="seconds"),
            }
        return None

    # 前复权 close 优先 (除权除息不扭曲 5 日涨跌); 个别存储无 close 列时退 raw_close
    col = "close" if "close" in df.columns else "raw_close"
    t0_close = float(df[col][0])
    t5_close = float(df[col][HORIZON_DAYS])
    if t0_close <= 0:
        return None
    pct = round((t5_close / t0_close - 1.0) * 100.0, 2)

    if direction in _BULLISH:
        hit = t5_close > t0_close
    elif direction in _BEARISH:
        hit = t5_close < t0_close
    else:  # 中性: 涨跌幅落在容差带内算对
        hit = abs(pct) <= NEUTRAL_BAND_PCT

    return {
        "report_id": report.get("id"),
        "symbol": report.get("symbol"),
        "name": report.get("name") or "",
        "direction": direction,
        "analysis_date": analysis_date.isoformat(),
        "t0_close": t0_close,
        "t5_close": t5_close,
        "pct": pct,
        "hit": bool(hit),
        "horizon": HORIZON_DAYS,  # P0-10 多周期扩展预留
        "settled_at": datetime.now().isoformat(timespec="seconds"),
    }


def settle_due(repo) -> int:
    """惰性结算所有到期研判, 返回新增结算条数。整体 fail-open。

    遍历个股报告存储 (≤50 条): 有有效方向、未入台账、研判日已过的逐条结算;
    数据不足的保持 pending 下次再试。台账只增不改 (expired 条目也是终态)。
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
            if not rid or rid in ledger:
                continue
            direction = _report_direction(report)
            analysis_date = _report_analysis_date(report)
            if direction is None or analysis_date is None or analysis_date > today:
                continue
            entry = _settle_one(repo, report, direction, analysis_date)
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


def stats() -> dict:
    """汇总战绩 (纯读台账 + 报告存储, 不触发结算)。

    返回:
      settled        已结算条数 (hit 为 True/False; expired 不计)
      hits           判对条数
      hit_rate_pct   命中率 (settled < MIN_SAMPLE 时为 None —— 样本不足不展示)
      insufficient   是否样本不足
      min_sample     最小样本阈值 (前端文案用)
      horizon_days   结算周期 (前端文案 "T+5")
      neutral_band   中性容差 (前端角标解释用)
      by_direction   分方向战绩 {方向: {settled, hits}}
      pending        有方向但未结算的报告数 (含数据不足等待中的)
      recent         最近 10 条结算记录 (倒序, 前端悬浮明细可用)
    """
    ledger = _load_ledger()
    entries = [e for e in ledger.values() if isinstance(e, dict) and e.get("hit") is not None]
    settled = len(entries)
    hits = sum(1 for e in entries if e.get("hit") is True)

    by_direction: dict[str, dict] = {}
    for e in entries:
        d = by_direction.setdefault(str(e.get("direction")), {"settled": 0, "hits": 0})
        d["settled"] += 1
        if e.get("hit") is True:
            d["hits"] += 1

    # pending: 报告里有有效方向但台账还没结算掉的 (报告被裁剪后无法统计, 可接受)
    pending = 0
    try:
        from app.services import stock_reports
        for report in stock_reports.list_reports():
            rid = report.get("id")
            if rid and rid not in ledger and _report_direction(report) is not None:
                pending += 1
    except Exception:  # noqa: BLE001
        pass

    recent = sorted(
        (e for e in ledger.values() if isinstance(e, dict)),
        key=lambda e: str(e.get("settled_at") or ""), reverse=True,
    )[:10]

    insufficient = settled < MIN_SAMPLE
    return {
        "settled": settled,
        "hits": hits,
        "hit_rate_pct": None if insufficient or settled == 0 else round(hits / settled * 100.0, 1),
        "insufficient": insufficient,
        "min_sample": MIN_SAMPLE,
        "horizon_days": HORIZON_DAYS,
        "neutral_band_pct": NEUTRAL_BAND_PCT,
        "by_direction": by_direction,
        "pending": pending,
        "recent": recent,
    }


def prompt_line() -> str:
    """生成注入用户提示词的"历史战绩"块; 无已结算样本 → 空串 (不注入噪音)。

    措辞刻意不含具体价格/日期等易与正文混淆的数字, 只给统计量; 数字忠实度
    闸门的白名单从 prompt 成品采集, 这些统计数字天然放行。
    """
    try:
        s = stats()
    except Exception:  # noqa: BLE001 — 战绩注入失败不能拖垮 AI 请求
        return ""
    if s["settled"] <= 0:
        return ""
    horizon = s["horizon_days"]
    if s["insufficient"]:
        body = (
            f"此前你给出的个股研判已有 {s['settled']} 次到达 T+{horizon} 结算, "
            f"样本不足 (少于 {s['min_sample']} 次), 暂不统计命中率。"
            "请保持谨慎措辞, 明确标注不确定性, 不要给出过度自信的预测。"
        )
    else:
        body = (
            f"此前你给出的个股研判已有 {s['settled']} 次到达 T+{horizon} 结算: "
            f"判对 {s['hits']} 次, 命中率 {s['hit_rate_pct']}%。"
            "请把这个历史战绩作为本次分析的置信度校准依据: 命中率不高时措辞应更谨慎、"
            "更明确地提示不确定性和风险, 不要夸大预测的确定性; 命中率较高时也应说明"
            "历史战绩不代表未来表现。"
        )
    return f"\n\n## 你的历史战绩 (系统自动统计)\n{body}"
