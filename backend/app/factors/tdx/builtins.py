"""通达信标识符映射表与拒绝清单 (P1-17 第 1 批)。

以数据表为主, 便于逐条评审与后续批次扩充。

约定:
- 所有键统一**大写** —— 通达信标识符大小写不敏感, 查表前先 upper();
- 不在表里的标识符一律**原样透传**给 DSL, 由 DSL 的 E001/E002 兜底报错,
  这样用户自定义因子 id 天然可用, 也避免在两处重复维护注册表查询;
- 拒绝清单按「为什么不能做」分类, 错误信息要能让用户看懂下一步该怎么办。
"""
from __future__ import annotations

# ── 列名别名 → DSL 基础列 (app/factors/dsl.py BASE_COLUMNS) ──────────
# 只收录通达信标准的 OHLCV + 成交额, 不自造别名。
COLUMN_ALIASES: dict[str, str] = {
    "C": "close", "CLOSE": "close",
    "O": "open", "OPEN": "open",
    "H": "high", "HIGH": "high",
    "L": "low", "LOW": "low",
    "V": "volume", "VOL": "volume", "VOLUME": "volume",
    "AMOUNT": "amount",
}

# ── 函数映射: 通达信名 → DSL 算子名 (A 类: 参数个数与顺序完全一致) ────
# 口径已实测核对 (见 tests/test_factor_tdx.py):
#   STD    → ts_std  : Polars rolling_std 默认 ddof=1, 与通达信样本标准差一致
#   LOG    → log     : 两边都是自然对数
#   IF/IFF → if_else : 三目, 参数顺序一致
FUNCTION_MAP: dict[str, str] = {
    "MA": "ts_mean",
    "REF": "ts_delay",
    "HHV": "ts_max",
    "LLV": "ts_min",
    "SUM": "ts_sum",
    "STD": "ts_std",
    "IF": "if_else",
    "IFF": "if_else",
    "MAX": "max",
    "MIN": "min",
    "ABS": "abs",
    "LOG": "log",
    "SQRT": "sqrt",
    # ── 第 2 批新增: 递归算子 (dsl.py 已加 ts_ema/ts_sma/ts_barslast/ts_barssince) ──
    # EMA(X,N) 与 SMA(X,N,M) 都是指数平滑, 区别只在 alpha:
    #   EMA: alpha = 2/(N+1)   SMA: alpha = M/N
    # 注意通达信 SMA **不是**简单移动平均, 是递归指数平滑, 别望文生义。
    "EMA": "ts_ema",
    "SMA": "ts_sma",
    "BARSLAST": "ts_barslast",
    "BARSSINCE": "ts_barssince",
}

# ── 结构展开类 (B 类): 通达信函数 → 由**既有** DSL 算子组合而成, 不新增算子 ──
# 值为 (最小参数数, 最大参数数); 展开逻辑在 translate._MACROS。
# 这些函数必须自己查参数个数 —— 展开是结构性的, 参数不够会直接 IndexError。
MACRO_ARITY: dict[str, tuple[int, int]] = {
    "CROSS": (2, 2),        # A 上穿 B
    "LONGCROSS": (3, 3),    # A 在 B 下方保持 N 周期后上穿 B
    "COUNT": (2, 2),        # 最近 N 周期内条件成立的次数
    "EVERY": (2, 2),        # 最近 N 周期条件一直成立
    "EXIST": (2, 2),        # 最近 N 周期内条件至少成立一次
}

# 兼容表展示用的等价写法 (A/B 是参数, N 是窗口)。与 translate._MACROS 的实际
# 展开保持同一语义, 改一处必须同步另一处。
MACRO_EXPANSION_HINT: dict[str, str] = {
    "CROSS": "A > B and REF(A,1) <= REF(B,1)",
    "LONGCROSS": "CROSS(A,B) and REF(EVERY(A<B,N),1)",
    "COUNT": "SUM(IF(COND,1,0),N)",
    "EVERY": "COUNT(COND,N) == N",
    "EXIST": "COUNT(COND,N) > 0",
}

# ── 参数个数表 (A 类): 通达信侧先自查, 报错用**通达信函数名** ──
# 不查的话 DSL 的 E003 会说"函数 ts_sma 参数数量不符", 用户看不懂 ts_sma 是什么。
# 不在表里的仍走第 1 批行为 (透传给 DSL 的 E003 兜底)。
FUNCTION_ARITY: dict[str, tuple[int, int]] = {
    "EMA": (2, 2),
    "SMA": (3, 3),
    "BARSLAST": (1, 1),
    "BARSSINCE": (1, 1),
}

# ── 退化窗口归一 ────────────────────────────────────────────────────
# 通达信里 MA(C,1) 合法且等于 C, 但 DSL 窗口下限是 WINDOW_MIN=2;
# REF(X,0) 合法且等于 X 自身, 但 DSL ts_delay 下限是 1。
# 这两类必须在翻译期归一成「原表达式」, 否则合法公式会被 E004 误拒。
# EMA(X,1): alpha = 2/(1+1) = 1 → Y_t = X_t, 同样是恒等 (第 2 批)。
WINDOW_IDENTITY_AT_1: frozenset[str] = frozenset({"MA", "HHV", "LLV", "SUM", "EMA"})
DELAY_IDENTITY_AT_0: frozenset[str] = frozenset({"REF"})
# SMA(X,1,1) 同理是恒等 (alpha = M/N = 1), 但它是三参数, 上面的二参数分支覆盖
# 不到, 在 translate._emit_call 里单独判 (第 2 批)。

# ── 拒绝类别 → 错误码 ───────────────────────────────────────────────
# E304 = 未来函数(硬禁止), E305 = 其余不支持
REJECT_FUTURE = "future"
REJECT_DRAWING = "drawing"
REJECT_DATA = "data"
REJECT_PERF = "perf"
REJECT_PLANNED = "planned"

REJECT_CODE: dict[str, str] = {
    REJECT_FUTURE: "E304",
    REJECT_DRAWING: "E305",
    REJECT_DATA: "E305",
    REJECT_PERF: "E305",
    REJECT_PLANNED: "E305",
}

# ── 拒绝清单: 函数名 → (类别, 给用户看的原因) ────────────────────────
REJECTED_FUNCTIONS: dict[str, tuple[str, str]] = {
    # 未来函数: 会随后续行情**重绘历史信号**, 回测里等于作弊。
    # 与 P1-16 的 E005 负偏移门控同一口径 —— 一律硬拒, 不给开关。
    "BACKSET": (REJECT_FUTURE, "会把未来数据回填到历史 bar, 信号会重绘"),
    "ZIG": (REJECT_FUTURE, "之字转向的拐点会随新行情重绘"),
    "FLATZIG": (REJECT_FUTURE, "归平之字的拐点会随新行情重绘"),
    "PEAK": (REJECT_FUTURE, "前高点会随新行情重绘"),
    "PEAKBARS": (REJECT_FUTURE, "前高位置会随新行情重绘"),
    "TROUGH": (REJECT_FUTURE, "前低点会随新行情重绘"),
    "TROUGHBARS": (REJECT_FUTURE, "前低位置会随新行情重绘"),

    # 绘图函数: 只影响看图, 在因子/回测里没有数值语义。
    "DRAWTEXT": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWTEXT_FIX": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWICON": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWLINE": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWBAND": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWKLINE": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWGB": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "DRAWNUMBER": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "STICKLINE": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "POLYLINE": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),
    "PARTLINE": (REJECT_DRAWING, "绘图函数在因子计算中无意义"),

    # 缺数据: 依赖当前数据层没有的数据集, 不能静默降级成 0 或空值。
    "WINNER": (REJECT_DATA, "需要筹码分布数据, 当前数据层没有"),
    "COST": (REJECT_DATA, "需要筹码分布数据, 当前数据层没有"),
    "FINANCE": (REJECT_DATA, "需要财务快照数据, 当前数据层没有"),
    "DYNAINFO": (REJECT_DATA, "需要盘中实时数据, 因子只在收盘 bar 上计算"),
    "CAPITAL": (REJECT_DATA, "需要流通股本数据, 当前数据层没有"),
    "INDEXC": (REJECT_DATA, "需要大盘指数序列, 个股面板里没有该列"),
    "INDEXO": (REJECT_DATA, "需要大盘指数序列, 个股面板里没有该列"),
    "INDEXH": (REJECT_DATA, "需要大盘指数序列, 个股面板里没有该列"),
    "INDEXL": (REJECT_DATA, "需要大盘指数序列, 个股面板里没有该列"),
    "INDEXV": (REJECT_DATA, "需要大盘指数序列, 个股面板里没有该列"),
    "INDEXA": (REJECT_DATA, "需要大盘指数序列, 个股面板里没有该列"),

    # 无法向量化: 只能走 Python 逐行回调, 5000 股 × 1000 天会拖死, 不硬上。
    "DMA": (REJECT_PERF, "变动系数的指数加权无法向量化 (alpha 是序列而非常量)"),
    "AVEDEV": (REJECT_PERF, "真·逐窗平均绝对偏差无法向量化, 只能用慢速逐行回调"),
}


def compatibility_table() -> list[dict]:
    """导出函数兼容表 (前端「通达信兼容表」用, 与上面几张表同源不脱节)。"""
    supported = [
        {"tdx": name, "maps_to": dsl, "kind": "operator", "status": "supported"}
        for name, dsl in sorted(FUNCTION_MAP.items())
    ]
    # 结构展开类没有单一对应算子, maps_to 写等价 DSL 片段, 便于用户理解语义
    macros = [
        {"tdx": name, "maps_to": MACRO_EXPANSION_HINT[name], "kind": "macro", "status": "supported"}
        for name in sorted(MACRO_ARITY)
    ]
    rejected = [
        {"tdx": name, "category": category, "reason": reason, "kind": "rejected", "status": "rejected"}
        for name, (category, reason) in sorted(REJECTED_FUNCTIONS.items())
    ]
    return supported + macros + rejected
