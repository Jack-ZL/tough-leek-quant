"""P1-11 数据体检打分 — 数据先体检, 再给 AI 用。

学 astock `quality_gate.py` 的「评级注入下游 prompt」思路, 但按 TSP 哲学改造:
- **纯规则硬检查, 零 LLM 成本** (astock 用一次 LLM 复审出 A-F, 成本高且慢;
  本项目检查对象是结构化行情数据, 规则足够, 也符合 AI 成本台账的省钱原则)
- **默认只警示不拦截** (fail-open): 体检结果注入 prompt 让 AI 自行降置信度;
  仅当用户显式打开 preferences.ai_quality_block_enabled 时, D 级硬伤才拒绝分析
- **只读缓存, 不触发重扫**: 日K用分析流程已加载的 df; 分钟缺口读体检接口
  落盘的快照 (kline.py minute-gaps 端点全量扫描时写入); 全市场问题读
  integrity_check 的 latest_report 小 JSON — 体检本身必须是毫秒级, 不能拖慢分析

评分: 100 分制按严重度扣分 (high -25 / medium -10 / low -3 / info -0),
等级 A(≥90) / B(≥75) / C(≥60) / D(<60); 「硬伤」= 存在 high 级问题。

消费方 (三处 AI 入口统一注入, 见各自 P1-11 标记):
- stock_analyzer.analyze_stock_stream  个股分析 (meta 事件带 quality 供前端徽章)
- stock_debate.build_materials         多空辩论 (材料文本追加体检块, 多空同源)
- market_recap                         市场回顾 (注入全市场级别体检块)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.market_time import cn_now, cn_today

logger = logging.getLogger(__name__)

# ===== 评分常量 =====
# 每档扣分: high=硬伤级(单项即可拉到 D), medium=明显缺陷, low=轻微, info=仅告知
DEDUCTIONS = {"high": 25, "medium": 10, "low": 3, "info": 0}
# 等级下限 (score >= 阈值即该级); D 为兜底
GRADE_THRESHOLDS = (("A", 90), ("B", 75), ("C", 60))
# 结果缓存: 同一标的短时间内重复分析不重算 (体检本身毫秒级, 缓存主要为
# 避免分钟快照/体检报告 JSON 的重复磁盘读)
_CACHE_TTL_S = 600.0
# 分钟缺口快照 / 全市场体检报告的可用时效: 超龄只降级为 info 提示, 不参与扣分
_SNAPSHOT_FRESH_H = 48

_cache: dict[str, tuple[str, float, dict]] = {}  # symbol -> (fingerprint, ts, report)
_cache_lock = threading.Lock()


def _issue(code: str, severity: str, message: str) -> dict:
    """一条体检问题。code 供程序判断, message 是给 AI/用户看的中文说明。"""
    return {"code": code, "severity": severity, "message": message}


def _grade_of(score: int) -> str:
    for grade, floor in GRADE_THRESHOLDS:
        if score >= floor:
            return grade
    return "D"


# ===== 交易日历 (复用 data_integrity 的年份日历缓存, 取不到回退工作日近似) =====

def _trading_calendar() -> set[date] | None:
    """A 股交易日集合; None = 日历不可用 (回退周几近似, 节假日会误判为交易日)。"""
    from app.services import data_integrity
    return data_integrity._trading_calendar()


def _is_trading_day(d: date, calendar: set[date] | None) -> bool:
    if calendar is not None:
        return d in calendar
    return d.weekday() < 5


def _expected_last_trading_day(today: date, now_hm: tuple[int, int]) -> date:
    """当前时刻"数据应更新到的最后交易日"。

    盘后管道 15:35 才写今天 → 15:35 前期望值是上一交易日, 否则今天。
    与 data_integrity 的尾盘定版线口径一致 (见其模块 docstring)。
    """
    cutoff = (15, 35)
    d = today
    if now_hm < cutoff:
        d = today - timedelta(days=1)
    calendar = _trading_calendar()
    # 往回找最近一个交易日; 日历缺失时按工作日回退, 上限 15 天防死循环
    for _ in range(15):
        if _is_trading_day(d, calendar):
            return d
        d -= timedelta(days=1)
    return d


def _trading_days_between(start: date, end: date) -> int:
    """(start, end] 区间内的交易日数; start >= end 时为 0。"""
    if start >= end:
        return 0
    calendar = _trading_calendar()
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if _is_trading_day(d, calendar):
            n += 1
        d += timedelta(days=1)
    return n


def _to_date(v) -> date | None:
    """date/datetime/ISO 字符串归一为 date; 解析失败 None。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


# ===== 分钟缺口快照 (minute-gaps 体检接口全量扫描时落盘, 这里只读) =====

def minute_snapshot_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "minute_gaps_latest.json"


def save_minute_snapshot(data_dir: Path, scan: dict) -> None:
    """把一次**全量**分钟缺口扫描结果落盘为快照 (P1-11, kline.py 端点调用)。

    只存体检打分需要的字段; 过滤扫描 (指定 symbols/dates) 结果不完整, 不落盘。
    """
    try:
        from app.services.fs_utils import atomic_write_text
        payload = {
            "checked_at": cn_now().isoformat(),
            "status": scan.get("status"),
            "scanned_dates": scan.get("scanned_dates", 0),
            # 只留 symbol/date/missing_bars 三列, 2000 行上限时 JSON 也在百 KB 级
            "gaps": [
                {"symbol": g.get("symbol"), "date": g.get("date"),
                 "missing_bars": g.get("missing_bars")}
                for g in (scan.get("gaps") or [])
            ],
        }
        path = minute_snapshot_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)  # atomic_write_text 不建父目录
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — 快照写失败不影响体检接口本身
        logger.warning("[P1-11] 分钟缺口快照落盘失败", exc_info=True)


def _read_minute_snapshot(data_dir: Path) -> dict | None:
    """读快照; 不存在/损坏/超龄返回 None (调用方降级为 info 提示)。"""
    p = minute_snapshot_path(data_dir)
    if not p.exists():
        return None
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(snap["checked_at"])
        age_h = (cn_now() - checked).total_seconds() / 3600
        if age_h > _SNAPSHOT_FRESH_H:
            return None
        return snap
    except Exception:  # noqa: BLE001
        return None


# ===== 全市场体检报告 (integrity_check.latest_report, 小 JSON 只读) =====

def _read_integrity_report(data_dir: Path) -> dict | None:
    """最近一次全市场数据体检报告; 超龄/不存在返回 None。"""
    try:
        from app.services import integrity_check
        report = integrity_check.latest_report(Path(data_dir))
        if not report:
            return None
        checked = datetime.fromisoformat(report["checked_at"])
        if (cn_now() - checked).total_seconds() / 3600 > _SNAPSHOT_FRESH_H:
            return None
        return report
    except Exception:  # noqa: BLE001
        return None


# ===== 单标的体检打分 =====

def score_symbol(
    data_dir: Path,
    symbol: str,
    df: pl.DataFrame,
    fins: dict[str, list[dict]] | None,
    asset_type: str = "stock",
    *,
    now: datetime | None = None,
    use_cache: bool = True,
) -> dict:
    """对"即将喂给 AI 的数据"打分。df/fins 就是分析流程已加载的同一份数据 —
    体检对象与 AI 实际看到的内容严格一致, 不存在第二套口径。

    now 参数仅供测试注入; use_cache=False 强制重算 (测试用)。
    返回 {version, symbol, score, grade, hard, issues, checked_at}。
    """
    now = now or cn_now()
    today = now.date()

    # 缓存指纹: 行数 + 最后日期 + 财务最新报告期 — 数据没变则直接复用结果
    last_d = _to_date(df["date"][-1]) if len(df) and "date" in df.columns else None
    fin_period = _latest_period_end(fins) if asset_type == "stock" else None
    fingerprint = f"{len(df)}|{last_d}|{fin_period}"
    if use_cache:
        with _cache_lock:
            hit = _cache.get(symbol)
            if hit and hit[0] == fingerprint and now.timestamp() - hit[1] < _CACHE_TTL_S:
                return hit[2]

    issues: list[dict] = []
    issues.extend(_check_kline(df, last_d, today, now))
    if asset_type == "stock":
        issues.extend(_check_financials(fins, fin_period, today))
        issues.extend(_check_minute_gaps(data_dir, symbol))
    issues.extend(_check_market_report(data_dir, symbol))

    score = 100 - sum(DEDUCTIONS.get(i["severity"], 0) for i in issues)
    score = max(0, min(100, score))
    report = {
        "version": 1,
        "symbol": symbol,
        "score": score,
        "grade": _grade_of(score),
        "hard": any(i["severity"] == "high" for i in issues),
        "issues": issues,
        "checked_at": now.isoformat(),
    }
    if use_cache:
        with _cache_lock:
            _cache[symbol] = (fingerprint, now.timestamp(), report)
    return report


def _check_kline(df: pl.DataFrame, last_d: date | None, today: date, now: datetime) -> list[dict]:
    """日K检查: 行数 / 停更 / 零成交尾巴 / 指标缺失 / 日期乱序。"""
    out: list[dict] = []
    n = len(df)
    # 1) 行数: MA60 需要 60 根才有意义, 30 根以下长周期指标全不可信 → 硬伤
    if n < 30:
        out.append(_issue("kline_rows_low", "high",
                          f"日K仅 {n} 根, MA60 等长周期指标不可用, 技术面结论不可信"))
    elif n < 60:
        out.append(_issue("kline_rows_low", "medium",
                          f"日K仅 {n} 根 (不足 60), 长周期指标 (MA60) 可能缺失或失真"))
    # 2) 停更: 与"当前时刻应有的最后交易日"比, 按交易日计龄
    if last_d is not None:
        expected = _expected_last_trading_day(today, (now.hour, now.minute))
        if last_d < expected:
            stale = _trading_days_between(last_d, expected)
            if stale > 5:
                out.append(_issue("kline_stale", "high",
                                  f"日K停更 {stale} 个交易日 (最后 {last_d.isoformat()}), 价格/指标严重过期"))
            elif stale >= 2:
                out.append(_issue("kline_stale", "medium",
                                  f"日K停更 {stale} 个交易日 (最后 {last_d.isoformat()})"))
            else:
                out.append(_issue("kline_stale", "low",
                                  f"日K最后日期 {last_d.isoformat()}, 落后 1 个交易日 (盘后管道可能未跑)"))
    # 3) 零成交尾巴: 连续 3 日 volume=0 — 停牌或数据异常, 量价类指标全部失真
    if n >= 3 and "volume" in df.columns:
        try:
            tail_vol = df.tail(3)["volume"].to_list()
            if all((v or 0) == 0 for v in tail_vol):
                out.append(_issue("zero_volume_tail", "medium",
                                  "近 3 日成交量均为 0, 疑似停牌或数据异常, 量价指标失真"))
        except Exception:  # noqa: BLE001
            pass
    # 4) 指标缺失: 最后一行 ma20 为空 → enriched 指标列未算好
    if n and "ma20" in df.columns and df.tail(1)["ma20"][0] is None:
        out.append(_issue("indicator_missing", "low", "最新一日 MA20 为空, 技术指标可能未重算完成"))
    # 5) 日期乱序/重复: enriched 生成异常的信号
    if n >= 2 and "date" in df.columns:
        try:
            dates = [_to_date(v) for v in df["date"].to_list()]
            if any(a is not None and b is not None and a >= b
                   for a, b in zip(dates, dates[1:], strict=False)):
                out.append(_issue("date_disorder", "medium", "日K日期存在重复或乱序, 数据可能损坏"))
        except Exception:  # noqa: BLE001
            pass
    return out


def _latest_period_end(fins: dict[str, list[dict]] | None) -> date | None:
    """财务数据里最新报告期 (metrics/income 两表取最大 period_end)。"""
    latest: date | None = None
    for rows in (fins or {}).values():
        for rec in rows or []:
            d = _to_date(rec.get("period_end"))
            if d and (latest is None or d > latest):
                latest = d
    return latest


def _check_financials(fins: dict[str, list[dict]] | None, fin_period: date | None, today: date) -> list[dict]:
    """财务检查: 缺失 (info, 提示词已有 Free 兜底文案) / 报告期过期。"""
    if not any((fins or {}).values()):
        return [_issue("fin_missing", "info", "暂无财务数据 (Free 模式或尚未同步)")]
    if fin_period and (today - fin_period).days > 120:
        return [_issue("fin_stale", "low",
                       f"财务数据陈旧: 最新报告期 {fin_period.isoformat()}, 距今超 120 天")]
    return []


def _check_minute_gaps(data_dir: Path, symbol: str) -> list[dict]:
    """分钟缺口: 只读快照 (不重扫分区); 快照缺失/超龄仅 info 提示不扣分。"""
    snap = _read_minute_snapshot(data_dir)
    if snap is None:
        return [_issue("minute_snapshot_stale", "info", "分钟缺口体检快照不存在或已超龄, 未纳入评分")]
    hits = [g for g in snap.get("gaps", []) if g.get("symbol") == symbol]
    if hits:
        total = sum(int(g.get("missing_bars") or 0) for g in hits)
        dates = ", ".join(sorted({str(g.get("date")) for g in hits})[:3])
        return [_issue("minute_gaps", "medium",
                       f"分钟K存在 {len(hits)} 处缺口 (共缺 {total} 根, 涉及 {dates} 等)")]
    return []


def _check_market_report(data_dir: Path, symbol: str) -> list[dict]:
    """全市场体检报告: 该标的被点名 → medium; 仅有全局问题 → info 供 AI 参考。"""
    report = _read_integrity_report(data_dir)
    if not report or report.get("status") != "issues":
        return []
    for row in report.get("issues", []):
        # symbols 列表在报告里截断到前 10 个 — 命中是确定性的, 未命中不代表没有问题,
        # 所以未命中时只给全局 info, 不误报"该标的安全"
        if symbol in (row.get("symbols") or []):
            return [_issue("integrity_flagged", "medium",
                           f"该标的在全市场数据体检中被标记: {row.get('date')} {row.get('kind_label', '')}")]
    total = len(report.get("issues", []))
    return [_issue("integrity_global", "info", f"全市场数据体检存在 {total} 处待修复问题")]


# ===== prompt 注入 =====

def prompt_block(quality: dict | None) -> str:
    """单标的体检块 (个股分析 / 多空辩论共用)。

    无实质问题 → 空串不注入 (省 token, 也没必要让 AI 复述"数据正常")。
    info 级只是背景说明 (如"快照不存在"/"暂无财务数据"), 提示词已有对应兜底
    文案, 不进体检块 — 与前端徽章口径一致 (徽章同样过滤 info)。
    有问题 → 等级 + 问题清单 + 按等级分档的使用指令。
    自带 "## 数据体检" 标题与前导空行, 与 scorecard prompt_line 同风格。
    """
    if not quality:
        return ""
    real_issues = [i for i in quality.get("issues", []) if i.get("severity") != "info"]
    if not real_issues:
        return ""
    grade = quality.get("grade", "?")
    lines = [
        "",
        f"## 数据体检 (评级 {grade}, {quality.get('score', '?')}/100)",
        "本次分析所用数据存在以下已知问题:",
    ]
    for i in real_issues:
        lines.append(f"- [{i['severity']}] {i['message']}")
    if grade == "D":
        lines.append(
            "\n数据质量差 (D 级): 请在报告开头显著声明数据局限, 全文降低确定性表述, "
            "不要基于上述受损数据给出趋势或价位方面的肯定性结论。")
    elif grade == "C":
        lines.append(
            "\n数据质量一般 (C 级): 涉及上述问题的维度请明示数据局限并降低置信度。")
    else:
        lines.append("\n请在涉及上述问题的维度适当注明数据局限。")
    return "\n".join(lines)


def market_block(data_dir: Path) -> str:
    """全市场级体检块 (市场回顾用): 只报 integrity 体检状态, 无问题不打扰。"""
    report = _read_integrity_report(data_dir)
    if not report or report.get("status") != "issues":
        return ""
    total = len(report.get("issues", []))
    repairable = report.get("repairable_count", 0)
    kinds = sorted({r.get("kind_label", "") for r in report.get("issues", []) if r.get("kind_label")})
    return (
        "\n## 数据体检 (全市场)\n"
        f"最近一次数据体检发现 {total} 处问题 (可自动修复 {repairable} 处), 类型: {'、'.join(kinds) or '未知'}。"
        "\n涉及个别标的的数值请在报告中留有余地, 不要将数据缺口解读为市场现象。"
    )


# ===== 门控 (默认关闭 — 只警示不拦截) =====

def should_block(quality: dict | None) -> bool:
    """是否拒绝本次 AI 分析。默认 False (警示模式); 仅当用户在偏好中显式开启
    ai_quality_block_enabled 且体检为 D 级硬伤时拦截 (fail-closed 可选档)。"""
    if not quality or not quality.get("hard") or quality.get("grade") != "D":
        return False
    try:
        from app.services import preferences
        return bool(preferences.get_ai_quality_block_enabled())
    except Exception:  # noqa: BLE001 — 偏好读取失败按默认(不拦截)
        return False


def block_message(quality: dict) -> str:
    """拦截时的用户可见文案 (NDJSON error 事件 / HTTP 409 共用)。"""
    reasons = "; ".join(i["message"] for i in quality.get("issues", []) if i["severity"] == "high")
    return (f"数据体检 D 级硬伤, 已按设置拦截本次 AI 分析: {reasons}。"
            "请先在「数据页 → 数据体检」修复后重试, 或在偏好设置中关闭 ai_quality_block_enabled。")
