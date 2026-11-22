"""港股波动率情绪 (VHSI) —— A 股「情绪周期」在港股的**对应维度**, 只读聚合。

为什么港股要另起一个维度, 而不是复用 A 股的情绪周期
--------------------------------------------------
A 股的情绪周期 (:mod:`app.services.market_phase`) 完全建立在**连板梯队**上:
首板家数、N 板宽度、晋级率、梯队完整度。这四个驱动量在港股**根本不存在** ——
港股无涨跌停 (``rules_for(Market.HK).has_price_limit is False``, CONTRIBUTING §3.5),
因此没有「涨停」「连板」「炸板」, 也就没有晋级率。把 A 股的阶段规则套到港股上
不会报错, 只会恒等于兜底档「修复」, 是典型的「看起来对但错」。

港股市场上唯一可得的、口径清晰的**全市场情绪计**是恒生波动率指数 VHSI
(港股版 VIX; A 股的 iVIX 已停发, 所以这个维度 A 股反而没有)。本模块把它做成
一个可审计的档位判定, 而不是又画一条曲线。

🔴 单位: VHSI 点位就是**年化隐含波动率的百分点数**
--------------------------------------------------
``24.09`` 表示市场预期未来 30 天的年化波动率约 ``24.09%``。它**不是**小数、
**不需要**也**绝不能**再 ×100 (那会显示成 2409%)。这与 CONTRIBUTING §3.1 里
``change_pct`` 的「小数 vs 百分数」陷阱同类, 但方向相反: VHSI 天生就是百分数
口径的**指数点位**, 而本模块另算的 ``change_pct_1d`` (VHSI 自己今天的涨跌幅)
同样是百分数。两者都写进响应字段名带 ``_pct`` 的即为百分数, 前端不再换算。

🔴 阈值用 VHSI **自身历史分位**, 不用 VIX 的 20/30
--------------------------------------------------
市面上常见的「VIX < 20 平静 / > 30 恐慌」是**美股**口径。VHSI 的分布与之不同:
本地实测 (400 个交易日, 2025-04-09 ~ 2026-11-20) 中位数 ``21.31``、p90 ``25.60``、
p99 ``31.62``; 腾讯可回溯的 2000 根 (2018-10-08 起) 区间是 ``12.93 ~ 64.80``。
若直接套 VIX 的 20/30, 「常态」会被判成「偏高」、真正的恐慌反而判不成恐慌。
因此本模块的档位边界是**从样本自身算出来的分位值**, 并把每个分位的具体数值
一并返回 (``percentiles`` 字段), 让「为什么今天是这个档」在界面上可查证,
而不是埋在代码常量里。

代价必须如实说明: 这是一个**相对**标尺。按构造, 样本里恒有约 10% 的日子落在
「极低波」、约 2% 落在「恐慌」—— 它回答的是「现在比过去紧张还是宽松」,
不回答「绝对波动率是高是低」。故响应同时给出**绝对点位**与**样本窗口**
(``sample_days`` / ``sample_start`` / ``sample_end``), 由使用者自行判断。
本模块**不刻意加一个手挑的绝对下限**去「修正」这件事: 那等于编造阈值。

🔴 只用 ``close``, 不碰 ``volume`` / ``amount``
---------------------------------------------
指数的量额两个源互相矛盾 (新浪 HSI 2026-11-13 成交量 ``195857007``, 腾讯同日
``195857007140``, 恰好 ×1000; 而新浪自带的 额/量 = 58.84 与点位 23637.6 也不自洽),
``plugins/sina/hk_index.py`` 坑 3 已判定「无法确定真值 → 原样透传不折算」,
``GET /api/hk/index_daily`` 也因此返回 ``volume_unit=None``。波动率情绪不需要量能,
故本模块**只读 close**, 从源头上不碰这个矛盾字段。

🔴 样本不足时 fail closed, 不给档位
---------------------------------
分位数在小样本上没有意义 (30 天的 p98 就是第 30 天自己)。样本少于
:data:`HK_VOLATILITY_MIN_SAMPLE_DAYS` 个交易日时, ``regime`` / ``percentile`` /
``streak_days`` / ``corr_hsi`` 一律为 ``None``, 并给出 ``insufficient_reason``。
**绝不**退化成「按现有几根算一个档位」—— 那会产出一个看起来正常的标签。
本地默认同步深度是 :data:`app.services.index_sync_hk.HK_INDEX_DAILY_COUNT` (400 根),
远超该下限; 从未同步过港股指数时样本为 0, 同样走这条路 (API 返回 200 + 空态,
不是 500: 这是本地数据缺失, 不是上游故障)。

口径的两个边界, 刻意选择「简单且可解释」
---------------------------------------
1. **分位是「全样本分位」, 不是逐日滚动分位。** 阈值由整个样本算出后固定,
   再套用到每一天 (用于 ``streak_days``)。逐日滚动分位会让同一天的标签随窗口
   漂移, 无法复现; 全样本分位的代价是**历史某天的档位不是 point-in-time 的**
   (用了它之后的数据定阈值)。本模块只输出**当前**档位与当前连续天数,
   不输出历史档位序列, 因此这个代价不会变成界面上的错误信息。
2. **不产出任何交易信号。** 与 :mod:`app.services.market_phase` 同一边界
   (``docs/market-phase.md``「不生成任何交易信号; 阶段与主线仅供研究分析」)。
   档位名称是**描述性**的 (极低波/低波/常态/偏高/高波/恐慌), 不含「该买该卖」
   的暗示, 也不做「恐慌见底」这类未经回标的推断。
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from datetime import date
from typing import NamedTuple

import polars as pl

logger = logging.getLogger(__name__)

#: 波动率情绪的主标的: 恒生波动率指数。已在
#: :data:`app.plugins.sina.hk_index.HK_INDEX_WHITELIST` 内, 且是腾讯有日K覆盖的
#: 14 个港股指数之一 (故本地 ``kline_hk_index_daily`` 里有它的历史)。
HK_VOLATILITY_SYMBOL = "VHSI"

#: 用于算「波动率与大盘背离」的基准指数。VHSI 是 HSI 的期权隐含波动率,
#: 二者日收益正常应**显著负相关**; 转正往往意味着行情性质变了 (齐涨齐跌)。
HK_VOLATILITY_BENCHMARK_SYMBOL = "HSI"

#: 给出档位所需的最小样本交易日数。低于此数分位数无统计意义 → fail closed。
#: 取 120 (约半年): 再少则 p98 只由 2~3 根决定; 本地默认同步 400 根, 正常不会触发。
HK_VOLATILITY_MIN_SAMPLE_DAYS = 120

#: 返回给前端的分位阈值 (让「为什么是这个档」可查证)。
HK_VOLATILITY_PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90, 95, 98, 99)

#: 均线窗口。60 日约一个季度, 用于看波动率中枢的抬升/回落。
HK_VOLATILITY_MA_WINDOWS: tuple[int, ...] = (5, 20, 60)

#: VHSI 与 HSI 日收益的相关系数窗口。20 日约一个月, 再长会抹掉 regime 切换。
HK_VOLATILITY_CORR_WINDOW = 20

#: 档位边界: ``(百分位上界, 键, 中文名)``, 按上界升序, 最后一段上界为 ``None``
#: (无上界)。判定用**全样本分位算出的点位阈值**, 见模块 docstring「口径的两个边界」。
#:
#: 🔴 边界值本身 (10/35/70/90/98) 是**分位刻度**, 不是 VHSI 点位 —— 它们描述
#: 「排在历史什么位置」, 与 VHSI 的绝对水平无关, 因此不存在「抄了美股阈值」的问题。
HK_VOLATILITY_BANDS: tuple[tuple[float | None, str, str], ...] = (
    (10.0, "ultra_low", "极低波"),
    (35.0, "low", "低波"),
    (70.0, "normal", "常态"),
    (90.0, "elevated", "偏高"),
    (98.0, "high", "高波"),
    (None, "panic", "恐慌"),
)

#: 数据出处标识。写进 API 响应, 让「这个数从哪来」可见 (与 ``FX_SOURCE`` 同理)。
HK_VOLATILITY_SOURCE = "local_kline_hk_index_daily"


class HkVolatilityResult(NamedTuple):
    """:func:`hk_volatility` 的结果: 当前读数 + 档位 + **全部口径元信息**。

    样本三件套 (``sample_days``/``sample_start``/``sample_end``) 是必填而非可选:
    档位是相对标尺, 不带样本窗口的档位无法解读 (见模块 docstring)。
    """

    #: 主标的代码 (:data:`HK_VOLATILITY_SYMBOL`)。
    symbol: str
    #: 最新点位 (年化隐含波动率的**百分点数**, 不再 ×100)。无数据时 ``None``。
    level: float | None
    #: 最新点位所属交易日。无数据时 ``None``。
    as_of: date | None
    #: 较前一交易日的绝对变动 (点位差)。前收缺失时 ``None`` (**不填 0**)。
    change_1d: float | None
    #: 较前一交易日的变动, **百分数** (``3.5`` 即 +3.5%)。前收缺失/为 0 时 ``None``。
    change_pct_1d: float | None
    #: 最新点位在样本内的分位排名 (0~100)。样本不足时 ``None``。
    percentile: float | None
    #: 档位键 (:data:`HK_VOLATILITY_BANDS` 的第二元素)。样本不足时 ``None``。
    regime: str | None
    #: 档位中文名。样本不足时 ``None``。
    regime_label: str | None
    #: 截至最新交易日, 连续处于当前档位的天数 (含当日)。样本不足时 ``None``。
    streak_days: int | None
    #: 各均线值, 键为 ``ma5``/``ma20``/``ma60``。样本不足以算某条时该项为 ``None``。
    moving_averages: dict[str, float | None]
    #: 分位阈值表, 键为 ``p10``/``p25``/… 值为**VHSI 点位**。样本不足时为空 dict。
    percentiles: dict[str, float]
    #: 各档位的**上界点位**: 键为档位键, 值为该档 ``upper_percentile`` 对应的 VHSI
    #: 点位; 末档无上界故为 ``None``。样本不足时为空 dict。
    #:
    #: 🔴 刻意与 ``percentiles`` 分开而不是把 p35/p70 塞进后者: 档位边界
    #: (10/35/70/90/98) 与分布概览分位 (10/25/50/75/90/95/98/99) 是**两套刻度**,
    #: 前者是判定依据、后者是描述统计。让前端自己用 ``bands[].upper_percentile``
    #: 去 ``percentiles`` 里查, 一旦有人改了边界却忘了同步分位表, 查出来是
    #: ``undefined`` —— 图上少一条线, 而判定照旧, 没人会发现。这里由服务端
    #: 从同一个 quantile 函数算出, 结构上无法与 ``bands`` 分叉。
    band_upper_levels: dict[str, float | None]
    #: 窗口内最高/最低点位及其日期。无数据时均为 ``None``。
    period_high: float | None
    period_high_date: date | None
    period_low: float | None
    period_low_date: date | None
    #: VHSI 与 HSI **日收益**在 :data:`HK_VOLATILITY_CORR_WINDOW` 日窗口内的
    #: 相关系数 (−1~1)。正常应显著为负。样本不足、基准缺失、或任一侧方差为 0
    #: (相关系数无定义) 时为 ``None`` —— **绝不填 0**, 0 会被读成「实测不相关」。
    corr_hsi: float | None
    #: 相关系数实际使用的样本对数 (便于判断这个系数可不可信)。
    corr_samples: int
    #: 绘图序列, 按日期升序: ``{"date", "close", "ma20"}``。
    series: list[dict]
    #: 样本交易日数。
    sample_days: int
    #: 样本首/末交易日。无数据时 ``None``。
    sample_start: date | None
    sample_end: date | None
    #: 数据出处 (:data:`HK_VOLATILITY_SOURCE`)。
    source: str
    #: 样本是否不足以给出档位。
    insufficient_history: bool
    #: 样本不足的原因 (足够时为 ``None``)。前端直接展示, 故必须是用户能懂的话。
    insufficient_reason: str | None


def _band_of(percentile: float) -> tuple[str, str]:
    """分位排名 → ``(档位键, 中文名)``。

    边界是**左闭右开** (``percentile < 上界`` 落入该档), 最后一段无上界。
    ``percentile`` 恰好等于某个边界值时归入**更高**的一档 —— 保守方向:
    把「刚好到 p90」判成「高波」而不是「偏高」, 宁可偏紧张不可偏松懈。
    """
    for upper, key, label in HK_VOLATILITY_BANDS:
        if upper is None or percentile < upper:
            return key, label
    # 不可达: 最后一段 upper 为 None。留着是为了类型完整, 不静默返回兜底档。
    raise AssertionError(f"档位边界表缺少无上界的兜底段: {HK_VOLATILITY_BANDS!r}")


def _percentile_rank(sorted_values: list[float], target: float) -> float:
    """``target`` 在样本中的分位排名 (0~100)。

    定义为**严格小于 target 的样本占比 × 100**。刻意不用插值分位
    (``numpy.percentile`` 的线性插值有多种约定, 结果随约定漂移):
    这个定义一句话就能向用户解释清楚 ——「历史上有多少比例的日子比今天更平静」。

    ``sorted_values`` 必须**已升序**; 用 :func:`bisect.bisect_left` 取「严格小于」
    的个数, 故重复值不会被算进自己那一档。这是 O(log n) 而非 O(n):
    ``streak_days`` 要对每一天回判档位, 线性扫描会让 2000 根样本退化成
    400 万次比较 (秒级), 而 API 端点不该有这个开销。
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("分位排名需要非空样本")
    return bisect_left(sorted_values, target) / n * 100.0


def _rolling_mean(closes: list[float], window: int) -> list[float | None]:
    """简单移动平均, 前 ``window-1`` 项为 ``None`` (**不填 0、不用短窗口凑**)。

    用短窗口凑出来的「ma60」看起来是个正常数字, 但它是 5 日均线, 会误导。
    """
    out: list[float | None] = [None] * len(closes)
    if window <= 0:
        raise ValueError(f"均线窗口必须为正: {window}")
    running = 0.0
    for i, v in enumerate(closes):
        running += v
        if i >= window:
            running -= closes[i - window]
        if i >= window - 1:
            out[i] = running / window
    return out


def _pct_changes(closes: list[float]) -> list[float | None]:
    """日收益率序列 (小数口径, 首项 ``None``)。

    🔴 这里返回**小数**而不是百分数, 因为它只用于算相关系数 (量纲会被归一化掉)。
    对外暴露的 ``change_pct_1d`` 才是百分数。前收为 0 时记 ``None`` 而不是 inf:
    VHSI 不会为 0, 但真出现 0 说明数据坏了, 用一个坏值去算相关系数会污染整段。
    """
    out: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            out[i] = closes[i] / prev - 1.0
    return out


def _corr(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数。任一侧方差为 0 或样本 < 2 时返回 ``None`` (无定义)。"""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / (sxx**0.5 * syy**0.5)


def _fetch_closes(repo, symbols: tuple[str, ...]) -> dict[str, list[tuple[date, float]]]:
    """从 ``kline_hk_index_daily`` 视图取若干指数的 ``(日期, 收盘)`` 升序序列。

    与 ``GET /api/hk/index_daily`` 同一读取口径 (DuckDB 视图 + ``repo.execute_all``):
    repository 没有港股指数日K的专用 reader (``get_index_daily`` 读的是 A 股
    ``kline_index_enriched``), 视图查询是本仓既有的港股指数读法。

    视图未注册 (从未同步过港股指数) 时返回空 dict, 由调用方走 fail-closed 分支
    —— 这是**本地数据缺失**, 不是上游故障, 不该抛。

    ``close IS NULL`` 的行直接丢弃: 缺收盘的 K 线参与分位/均线计算会污染结果,
    而用 0 或前值填充都是编造数据 (§4)。
    """
    placeholders = ",".join("?" * len(symbols))
    sql = (
        f"SELECT symbol, date, close FROM kline_hk_index_daily "
        f"WHERE symbol IN ({placeholders}) AND close IS NOT NULL "
        f"ORDER BY symbol, date"
    )
    try:
        rows = repo.execute_all(sql, list(symbols))
    except Exception as e:  # noqa: BLE001
        logger.debug("kline_hk_index_daily 查询失败 (可能尚未同步港股指数): %s", e)
        return {}

    out: dict[str, list[tuple[date, float]]] = {s: [] for s in symbols}
    for sym, day, close in rows:
        if sym not in out or close is None:
            continue
        out[sym].append((day, float(close)))
    return out


def hk_volatility(
    repo,
    *,
    symbol: str = HK_VOLATILITY_SYMBOL,
    benchmark: str = HK_VOLATILITY_BENCHMARK_SYMBOL,
    limit: int | None = None,
) -> HkVolatilityResult:
    """港股波动率情绪快照 (VHSI 档位 + 分位 + 与恒指的背离)。

    参数
    ----
    repo:
        ``KlineRepository``。只读 ``kline_hk_index_daily`` 视图。
    symbol / benchmark:
        主标的与基准指数代码。默认 VHSI / HSI。
    limit:
        只取最近 N 个交易日 (含) 参与计算, ``None`` = 本地全量。
        🔴 这个参数会**改变分位阈值** (样本窗口变了), 因此响应里的
        ``sample_days``/``sample_start`` 必须随数据一起看, 不能脱离窗口解读档位。

    返回
    ----
    :class:`HkVolatilityResult`。样本不足时 ``insufficient_history=True`` 且
    ``regime``/``percentile``/``streak_days``/``corr_hsi`` 均为 ``None``。
    """
    series_map = _fetch_closes(repo, (symbol, benchmark))
    bars = series_map.get(symbol) or []
    if limit is not None and limit > 0:
        bars = bars[-limit:]

    sample_days = len(bars)
    sample_start = bars[0][0] if bars else None
    sample_end = bars[-1][0] if bars else None
    closes = [c for _, c in bars]

    mas = {f"ma{w}": _rolling_mean(closes, w) for w in HK_VOLATILITY_MA_WINDOWS}

    # 绘图序列: 只给点位与 ma20。🔴 刻意**不给逐日分位/档位** —— 阈值是全样本
    # 算出来的, 历史某天的档位不是 point-in-time 的, 画出来就是错误信息
    # (见模块 docstring「口径的两个边界」第 1 条)。
    series = [
        {
            "date": d.isoformat(),
            "close": c,
            "ma20": mas["ma20"][i],
        }
        for i, (d, c) in enumerate(bars)
    ]

    insufficient_reason: str | None = None
    if sample_days == 0:
        insufficient_reason = (
            f"本地还没有 {symbol} 的日K数据。请先在「数据」页同步港股指数日K "
            "(POST /api/hk/sync_index_daily)。"
        )
    elif sample_days < HK_VOLATILITY_MIN_SAMPLE_DAYS:
        insufficient_reason = (
            f"样本仅 {sample_days} 个交易日, 少于给出波动率档位所需的 "
            f"{HK_VOLATILITY_MIN_SAMPLE_DAYS} 个 —— 这么短的窗口上分位数没有统计意义, "
            "故不给档位。可加大同步深度 (count) 后重试。"
        )

    if insufficient_reason is not None:
        # 极值日期照常给出: 「样本不足」限制的是**分位/档位**这类需要统计意义的量,
        # 而最高/最低点位是样本内的客观事实, 藏起来反而让用户以为没有数据。
        hi_i0 = max(range(sample_days), key=lambda i: closes[i]) if closes else None
        lo_i0 = min(range(sample_days), key=lambda i: closes[i]) if closes else None
        return HkVolatilityResult(
            symbol=symbol,
            level=closes[-1] if closes else None,
            as_of=sample_end,
            change_1d=None,
            change_pct_1d=None,
            percentile=None,
            regime=None,
            regime_label=None,
            streak_days=None,
            moving_averages={k: (v[-1] if v else None) for k, v in mas.items()},
            percentiles={},
            band_upper_levels={},
            period_high=closes[hi_i0] if hi_i0 is not None else None,
            period_high_date=bars[hi_i0][0] if hi_i0 is not None else None,
            period_low=closes[lo_i0] if lo_i0 is not None else None,
            period_low_date=bars[lo_i0][0] if lo_i0 is not None else None,
            corr_hsi=None,
            corr_samples=0,
            series=series,
            sample_days=sample_days,
            sample_start=sample_start,
            sample_end=sample_end,
            source=HK_VOLATILITY_SOURCE,
            insufficient_history=True,
            insufficient_reason=insufficient_reason,
        )

    latest = closes[-1]
    prev = closes[-2] if sample_days >= 2 else None
    # 前收缺失或为 0 → None, 不填 0 (0 会被读成「今天没动」)
    change_1d = (latest - prev) if prev else None
    change_pct_1d = ((latest / prev - 1.0) * 100.0) if prev else None

    sorted_closes = sorted(closes)
    percentile = _percentile_rank(sorted_closes, latest)
    regime, regime_label = _band_of(percentile)

    # 只建一次 Series: 每个分位都新建一遍是 O(k·n) 的无谓拷贝。
    s = pl.Series(closes)
    pct_values = {f"p{p}": float(s.quantile(p / 100.0)) for p in HK_VOLATILITY_PERCENTILES}

    # 档位上界点位: 与 `bands` 同源同序, 末档无上界故为 None。
    # 🔴 用**同一个** `s.quantile` 算, 不复用 `pct_values`: 档位边界(10/35/70/90/98)
    # 与分布概览分位(10/25/50/75/90/95/98/99)是两套刻度, 只有 p10/p90/p98 重合。
    band_upper_levels: dict[str, float | None] = {
        key: (None if upper is None else float(s.quantile(upper / 100.0)))
        for upper, key, _label in HK_VOLATILITY_BANDS
    }

    hi_i = max(range(sample_days), key=lambda i: closes[i])
    lo_i = min(range(sample_days), key=lambda i: closes[i])

    # 连续处于当前档位的天数: 用**同一套**全样本分位阈值逐日回判。
    # 从尾部往前走, 遇到第一个不同档位就停。
    streak = 0
    for c in reversed(closes):
        if _band_of(_percentile_rank(sorted_closes, c))[0] != regime:
            break
        streak += 1

    # 与恒指的背离: 按日期内连接后取尾部窗口, 算日收益相关系数。
    # 🔴 取 window+1 根而不是 window 根: 日收益要两根才算得出一根, 只取 window
    # 会让实际参与相关系数计算的收益数变成 window-1, 与字段名宣称的窗口不符。
    bench = series_map.get(benchmark) or []
    corr_value: float | None = None
    corr_n = 0
    if bench:
        v_map = dict(bars)
        pairs = [(c, v_map[d]) for d, c in bench if d in v_map]
        need = HK_VOLATILITY_CORR_WINDOW + 1
        if len(pairs) > need:
            pairs = pairs[-need:]
        if len(pairs) >= 2:
            b_ret = _pct_changes([c for c, _ in pairs])
            v_ret = _pct_changes([v for _, v in pairs])
            xs = []
            ys = []
            for a, b in zip(b_ret[1:], v_ret[1:], strict=True):
                if a is not None and b is not None:
                    xs.append(a)
                    ys.append(b)
            corr_n = len(xs)
            corr_value = _corr(xs, ys)

    return HkVolatilityResult(
        symbol=symbol,
        level=latest,
        as_of=sample_end,
        change_1d=change_1d,
        change_pct_1d=change_pct_1d,
        percentile=percentile,
        regime=regime,
        regime_label=regime_label,
        streak_days=streak,
        moving_averages={k: (v[-1] if v else None) for k, v in mas.items()},
        percentiles=pct_values,
        band_upper_levels=band_upper_levels,
        period_high=closes[hi_i],
        period_high_date=bars[hi_i][0],
        period_low=closes[lo_i],
        period_low_date=bars[lo_i][0],
        corr_hsi=corr_value,
        corr_samples=corr_n,
        series=series,
        sample_days=sample_days,
        sample_start=sample_start,
        sample_end=sample_end,
        source=HK_VOLATILITY_SOURCE,
        insufficient_history=False,
        insufficient_reason=None,
    )
