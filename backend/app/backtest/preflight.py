"""回测入口检查员 (P0-6): 在真正跑回测之前把计划检查一遍, 不让用户白跑。

背景: 回测入口 (/run, /factor/run, /strategy/run, 各 SSE stream) 此前只有零散守卫
(标的数上限、服务端区间保护、分钟K覆盖), 参数本身没有体检 — 起始日晚于结束日、
手续费填成 200%、标的本地根本没数据这类问题, 要么跑出空结果要么跑到一半才报错,
用户不知道错在哪、该怎么改。

本模块提供统一的"计划体检":
  - error   级: 必然跑不出有效结果的问题 → 直接拒绝 (HTTP 400), 每条附"怎么改"建议;
  - warning 级: 能跑但结果可能没意义的问题 (区间太短、本地数据不覆盖起点等)
                → 不拦截, 随结果返回 (preflight_warnings), 前端展示提醒。

设计原则:
  - 失败开放 (fail-open): 数据检查依赖本地库查询, 查询本身异常时跳过该项检查
    (引擎层还有 daily_health 等兜底), 检查员自己绝不能成为新的故障点;
  - 只做"计划"层面的静态检查, 不预跑数据、不估算耗时;
  - 与现有守卫互补不替代: 标的数上限/服务端区间保护/分钟K覆盖等原有守卫保持原样。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# 支持回测的资产类型 (与 repository.daily_dirname 的映射保持一致)
_ASSET_LABELS = {"stock": "A股", "etf": "ETF", "index": "指数"}

# 区间短于该天数时给"样本太少"警告 (约一个半月, 低于此统计意义很弱)
MIN_RECOMMENDED_DAYS = 30

# 缺失标的清单在报错信息里最多列出的个数 (超出折叠为"等 N 只")
_MAX_LISTED_MISSING = 10

# 标的存在性检查的 IN 查询上限: 超过则跳过 (全市场级请求引擎自有抽样逻辑)
_MAX_SYMBOL_CHECK = 2000


@dataclass
class PreflightIssue:
    """一条检查结论。

    level: "error" (拒绝执行) 或 "warning" (放行但提醒);
    message: 问题描述 (说人话, 带具体数值);
    suggestion: 怎么改 (error 必填, warning 尽量给)。
    """

    level: str
    message: str
    suggestion: str = ""

    def render(self) -> str:
        """渲染为单行文本: "问题 (建议: xxx)"。"""
        if self.suggestion:
            return f"{self.message} (建议: {self.suggestion})"
        return self.message


# ------------------------------------------------------------------
# 分项检查
# ------------------------------------------------------------------


def _check_dates(start: date | None, end: date | None) -> list[PreflightIssue]:
    """日期检查: 起止顺序 / 区间长度 / 是否超到今天之后。"""
    issues: list[PreflightIssue] = []
    if start is None or end is None:
        return issues
    if start > end:
        issues.append(PreflightIssue(
            "error",
            f"起始日期 {start} 晚于结束日期 {end}",
            "调换起止日期, 或修正为有效的日期区间",
        ))
        return issues
    days = (end - start).days + 1
    if days < MIN_RECOMMENDED_DAYS:
        issues.append(PreflightIssue(
            "warning",
            f"回测区间仅 {days} 天, 样本太少, 统计意义不足",
            f"建议扩大到至少 {MIN_RECOMMENDED_DAYS} 天 (约 3 个月) 以上",
        ))
    today = date.today()
    if end > today:
        issues.append(PreflightIssue(
            "warning",
            f"结束日期 {end} 晚于今天 ({today}), 实际只能跑到最新交易日",
            "把结束日期调整为今天或更早",
        ))
    return issues


def _check_asset_type(asset_type: str | None) -> list[PreflightIssue]:
    """资产类型检查: 未知类型直接拒绝 (引擎会在更深处以更难懂的方式失败)。"""
    if asset_type is None or asset_type in _ASSET_LABELS:
        return []
    return [PreflightIssue(
        "error",
        f"不支持的资产类型: {asset_type!r}",
        f"可选值: {', '.join(sorted(_ASSET_LABELS))}",
    )]


def _daily_view(asset_type: str) -> str | None:
    """资产类型 → 日K视图名; 复用 repository 的映射, 未知类型返回 None。"""
    try:
        from app.tickflow.repository import daily_dirname
        return daily_dirname(asset_type)
    except Exception:
        return None


def _check_data(
    repo,
    start: date | None,
    symbols: list[str] | None,
    asset_type: str,
) -> list[PreflightIssue]:
    """数据覆盖检查: 本地有没有日K、起点是否早于本地最早数据、指定标的是否存在。

    所有查询失败都静默跳过 (fail-open): 检查员不能因为库忙/视图缺失而误杀请求,
    引擎层的 daily_health / EnrichedGenerationUnavailable 仍是最终兜底。
    """
    issues: list[PreflightIssue] = []
    if repo is None:
        return issues
    label = _ASSET_LABELS.get(asset_type, asset_type)
    view = _daily_view(asset_type)
    if view is None:
        return issues

    # 1) 本地最早日K日期 (按资产类型查对应视图)
    earliest: date | None = None
    try:
        row = repo.execute_one(f"SELECT min(date) FROM {view}")
        if row and row[0]:
            d = row[0]
            earliest = d if isinstance(d, date) else date.fromisoformat(str(d))
    except Exception:
        logger.debug("preflight: 查询 %s 最早日期失败, 跳过数据检查", view, exc_info=True)
        return issues

    if earliest is None:
        issues.append(PreflightIssue(
            "error",
            f"本地没有{label}日K数据, 回测无法进行",
            f"先到「数据」页同步{label}日K, 再回来跑回测",
        ))
        return issues

    # 2) 起点早于本地最早数据 → 提醒 (引擎会自动截断, 不算错误)
    if start is not None and start < earliest:
        issues.append(PreflightIssue(
            "warning",
            f"本地{label}日K最早只到 {earliest}, 回测实际将从该日开始",
            f"把起始日期调整为 {earliest} 及以后, 或先扩展历史数据",
        ))

    # 3) 指定标的的存在性: 本地完全没有该标的日K → 拒绝并列出缺失清单
    if symbols and len(symbols) <= _MAX_SYMBOL_CHECK:
        try:
            placeholders = ",".join("?" * len(symbols))
            rows = repo.execute_all(
                f"SELECT DISTINCT symbol FROM {view} WHERE symbol IN ({placeholders})",
                list(symbols),
            )
            found = {r[0] for r in rows}
            missing = [s for s in symbols if s not in found]
            if missing:
                shown = ", ".join(missing[:_MAX_LISTED_MISSING])
                more = f" 等 {len(missing)} 只" if len(missing) > _MAX_LISTED_MISSING else ""
                issues.append(PreflightIssue(
                    "error",
                    f"以下标的本地无{label}日K数据: {shown}{more}",
                    "检查代码是否填错 (如市场后缀), 或先到「数据」页同步这些标的",
                ))
        except Exception:
            logger.debug("preflight: 标的存在性查询失败, 跳过", exc_info=True)
    return issues


def _check_numeric_params(
    fees_pct: float | None = None,
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    slippage_bps: float | None = None,
    initial_capital: float | None = None,
    max_positions: int | None = None,
    holding_days: int | None = None,
    max_exposure_pct: float | None = None,
) -> list[PreflightIssue]:
    """参数合理性检查: 明显填错的数值直接拒绝, 每条附建议。

    区间口径 (与行业常识对齐, 宁可宽一点不误杀):
      - 费率类 (fees/commission/stamp_tax): 0 ~ 1% (0.01), 超过 1% 的单笔费率必是误填
        (常见错误: 把万2.5填成 2.5);
      - 滑点: 0 ~ 100bp (1%), 再大就失去"滑点"含义了;
      - 初始资金: >0 且 ≤ 1万亿, 负数/零/天文数字都属误填;
      - 最大持仓数: 1 ~ 500; 持仓天数: 1 ~ 250 (约一年交易日);
      - 总仓位上限: (0, 1], 0 等于不买, >1 等于加杠杆 (当前引擎不支持)。
    """
    issues: list[PreflightIssue] = []

    def _range(value, lo, hi, label, hint, *, lo_open=False):
        if value is None:
            return
        bad = value < lo if not lo_open else value <= lo
        if bad or value > hi:
            lo_txt = f"({'>' if lo_open else '≥'}{lo})"
            issues.append(PreflightIssue(
                "error",
                f"{label}={value} 超出合理范围 {lo_txt} ~ ≤{hi}",
                hint,
            ))

    _range(fees_pct, 0, 0.01, "单笔费率 fees_pct",
           "常见取值: 万2.5 填 0.00025; 若填 2.5 表示 250% 费率, 必是误填")
    _range(commission_pct, 0, 0.01, "佣金率 commission_pct",
           "常见取值: 万2.5 填 0.00025")
    _range(stamp_tax_pct, 0, 0.01, "印花税率 stamp_tax_pct",
           "A股卖出印花税常见为 0.0005 (万5) 或 0.001 (千1)")
    _range(slippage_bps, 0, 100, "滑点 slippage_bps",
           "单位为基点(bp): 5bp 填 5; 常见 0~20bp")
    _range(initial_capital, 0, 1e12, "初始资金 initial_capital",
           "填正数, 单位元; 常见 100000 ~ 10000000", lo_open=True)
    _range(max_positions, 1, 500, "最大持仓数 max_positions",
           "填 1~500 的整数; 组合回测常见 5~20")
    _range(holding_days, 1, 250, "持仓天数 holding_days",
           "填 1~250 的整数 (250 约等于一年交易日)")
    _range(max_exposure_pct, 0, 1, "总仓位上限 max_exposure_pct",
           "取值 (0,1]: 满仓填 1, 半仓填 0.5; 当前引擎不支持杠杆(>1)", lo_open=True)
    return issues


# ------------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------------


def validate_plan(
    *,
    start: date | None = None,
    end: date | None = None,
    repo=None,
    symbols: list[str] | None = None,
    asset_type: str = "stock",
    fees_pct: float | None = None,
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    slippage_bps: float | None = None,
    initial_capital: float | None = None,
    max_positions: int | None = None,
    holding_days: int | None = None,
    max_exposure_pct: float | None = None,
) -> list[PreflightIssue]:
    """对一份回测计划做全面体检, 返回全部问题 (error + warning)。

    参数全部关键字传入; 未提供的检查项自动跳过 (None)。
    repo 为 Repository 实例 (可选) — 提供时才做数据覆盖检查。
    """
    issues: list[PreflightIssue] = []
    issues.extend(_check_dates(start, end))
    issues.extend(_check_asset_type(asset_type))
    issues.extend(_check_numeric_params(
        fees_pct=fees_pct,
        commission_pct=commission_pct,
        stamp_tax_pct=stamp_tax_pct,
        slippage_bps=slippage_bps,
        initial_capital=initial_capital,
        max_positions=max_positions,
        holding_days=holding_days,
        max_exposure_pct=max_exposure_pct,
    ))
    # 资产类型非法时跳过数据检查 (视图名都解析不出来)
    if asset_type in _ASSET_LABELS:
        issues.extend(_check_data(repo, start, symbols, asset_type))
    return issues


def enforce_plan(**kwargs) -> list[str]:
    """执行体检: 有 error → 抛 HTTP 400 (detail 逐条"问题+建议"); 返回 warning 文本列表。

    用法 (API 层)::

        warnings = enforce_plan(start=start, end=end, repo=repo, ...)
        result = ...跑回测...
        if warnings:
            result["preflight_warnings"] = warnings
    """
    from fastapi import HTTPException

    issues = validate_plan(**kwargs)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i.render() for i in issues if i.level == "warning"]
    if errors:
        lines = "\n".join(f"- {e.render()}" for e in errors)
        raise HTTPException(
            status_code=400,
            detail=f"回测计划未通过检查, 已阻止执行:\n{lines}",
        )
    return warnings


def attach_warnings(result, warnings: list[str]):
    """把 warning 列表挂到回测结果上 (dict 才挂; 空列表不挂, 前端无感)。"""
    if warnings and isinstance(result, dict):
        result["preflight_warnings"] = warnings
    return result
