"""东方财富港股分红派息 (F10) → 复权因子 — 直连扩展模块。

**为什么需要本模块**: 项目契约是「落库必须原始价」(:func:`hk.fetch_hk_kline`
的 ``fqt=0``), 复权由 :func:`app.indicators.pipeline._apply_adj_factor` 用**乘法
因子链**重建。港股若缺因子, 每个除净日都会留下假跳空, MA/MACD 随之失真 ——
即 CONTRIBUTING §3 警告的「看起来对但错」。

🔴 **腾讯 ``hkfqkline`` 的前复权不能直接拿来当因子源** (实测 2026-11, 证据见
方案 §3.3 与 ``_scratch/probe_hk_adj3/4.py``): 它是**等差复权**
``qfq = raw − Σ未来每股港币分红``, 而本项目契约是**等比因子**。等差偏移除以随
行情波动的原始价 → ``qfq/raw`` 逐日漂移, 用比值差分反推因子在汇丰这类季度派息
股上产出 **207 个假事件** (真实约 3 个), 信号被噪声完全淹没。故本模块自取分红
事件、按交易所除净公式重建等比因子。

数据源: ``datacenter-web.eastmoney.com`` 报表 ``RPT_HKF10_MAIN_DIVBASIC``, 与
:mod:`app.plugins.eastmoney.hk_financials` 同主机 —— 该主机独立于 push2 的封禁
池, 不与行情同步争抢配额。

实测口径 (2026-11-15 活体验证; 32 只跨行业抽样, 1159 条有除净日的事件):

- ``IS_BFP="0"`` **必须带**: 不带时腾讯返回 91 行, 其中 64 行是
  ``PLAN_EXPLAIN='未派发或宣派股息'`` 的占位行 (``EX_DIVIDEND_DATE`` 为 null)。
- 过滤键 ``SECUCODE="00700.HK"`` 与 ``SECURITY_CODE="00700"`` **实测等价**
  (均 91/27 行), 统一用前者以复用 :func:`hk_fin_secucode`, 避免代码规范化分叉。
- ⚠️ **报表无数值型派息列**: 全部 12 个字段里金额只存在于 ``PLAN_EXPLAIN``
  中文文本 (``'每股派港币5.3元'``), 必须解析。抽样 1159 条事件见 51 种句式骨架,
  :func:`parse_hk_dividend_plan` 覆盖 **96.98%**, 余下 35 条源数据本身无港币
  数值 (外币未折算 / 分派股份 / 权证), 一律 warn+skip 而非编造汇率。
- ⚠️ **不支持按除净日过滤**: ``(EX_DIVIDEND_DATE>='2024-01-01')`` 返回 0 行,
  故只能每标的拉全史。单页 200 足够 (腾讯 27 条 / 汇丰 95 条季度派息)。
- ✅ **交叉验证**: 汇丰 2026-11-05 除净 ``每股派美元0.1元(相当于港币0.784812元)``,
  与独立测得的腾讯等差偏移 ``0.784`` 精确吻合, 且该偏移恰在此日归零。

**与 A 股因子的差异** (勿照搬 :mod:`app.plugins.eastmoney.provider`):

- A 股 ``_ref_price`` 把参考价四舍五入到 **0.01**, 因为要与官方涨跌停价对齐;
  港股**无涨跌停**且最小价位随价格档变化 (0.001~0.5), 故本模块**不做取整** ——
  用理论值能让复权序列更连续, 也避免引入一张易过期的价位表。
- A 股有「涨跌停自检」剔除异常因子; 港股改用 :data:`HK_ADJ_MAX_DIVIDEND_RATIO`
  等**比例合理性闸**替代 (见 :func:`build_hk_adj_factors`)。
"""

from __future__ import annotations

import logging
import math
import re
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import NamedTuple

import polars as pl

from app.plugins.eastmoney.client import EastmoneyClient
from app.plugins.eastmoney.hk_financials import hk_fin_secucode

logger = logging.getLogger(__name__)

#: 港股分红派息报表 (F10「核心必读」)。一行 = 一个分配方案。
RPT_HK_DIVIDEND = "RPT_HKF10_MAIN_DIVBASIC"

#: 单页上限。实测腾讯 27 条 / 汇丰 95 条, 200 足以单页取全史。
HK_DIV_PAGE_SIZE = 200

#: 排序键: 与 akshare 一致, 保证同一除净日的多行顺序稳定。
HK_DIV_SORT_COLUMNS = "NOTICE_DATE,EX_DIVIDEND_DATE"
HK_DIV_SORT_TYPES = "-1,-1"

#: 现金分红占前收盘的比例上限。超过即视为解析错误 (典型误判是把汇率当金额),
#: 剔除该事件而不是让一个错因子污染整条复权链。
HK_ADJ_MAX_DIVIDEND_RATIO = 0.9

#: 每股送转比例上限 (每 1 股送 N 股)。港股送转极少超过 1:1, 取 5 留足余量。
HK_ADJ_MAX_BONUS_RATIO = 5.0

# ── PLAN_EXPLAIN 解析 ─────────────────────────────────────────────
# 抽样 1159 条事件得到 51 种句式骨架, 按占比排序的主要形态:
#   586  每股派港币#元
#   212  每股派人民币#元(相当于港币#元)
#    85  每股派人民币#元(相当于港币#元(计算值))
#    82  每股派港币#元(可选择以股代息)
#    19  每股派美元#元(相当于港币#元)
#    17  每股派港币#元,每#股派送#股
#     2  每#股派送#股红利认股权证            ← 权证, 非送股, 必须排除
#     1  特殊说明:每#股分派#股…(相当于每股派#港元)
#     1  特殊说明:每21股腾讯股份分派1股京东集团A类普通股股份  ← 无等值, 跳过
_NUM = r"(\d+(?:\.\d+)?)"

#: ① 外币折算的港币等值, 金额在**币种之后**: ``相当于港币0.784812元`` /
#:    ``相当于港币#港元`` / ``相当于港币#(计算值)`` / ``相当于计算值港币#元`` /
#:    ``相等于港币#元`` / ``相当于港币股#元`` (源数据笔误)。故**不要求**结尾的「元」。
_RE_EQUIV_HKD = re.compile(
    rf"相[当等][于於](?:计算值)?港币股?\s*{_NUM}"
    #: ② 金额在**币种之前**: ``相当于每股派18.13港元`` (分派股份 spin-off 的等值) /
    #:    ``相等於每股0.3149港元`` (繁体「於」)。
    rf"|相[当等][于於]每股派?\s*{_NUM}\s*港[元币]"
)
#: ③ 直接港币派息: ``每股派港币5.3元``。
_RE_CASH_HKD = re.compile(rf"每股派港币\s*{_NUM}\s*元")
#: 送转股: ``每20股派送1股`` → 0.05, ``每10股转1股`` → 0.1。
#: 负向断言排除 ``每12股派送1股红利认股权证`` —— 权证不是送股, 且无价值数据。
_RE_BONUS = re.compile(r"每\s*(\d+)\s*股(?:派送|转)\s*(\d+)\s*股(?!红利认股权证)")


class HkDividendPlan(NamedTuple):
    """一条分红方案解析后的**数值**形态 (金额统一折算为港币/每股)。"""

    dividend_hkd: float
    """每股现金分红 (港币)。``0.0`` 表示纯送转无现金。"""

    bonus_ratio: float
    """每股送转比例: 每持有 1 股获送 ``bonus_ratio`` 股。``0.0`` 表示无送转。"""


def parse_hk_dividend_plan(text: str | None) -> HkDividendPlan | None:
    """解析 ``PLAN_EXPLAIN`` 中文文本 → :class:`HkDividendPlan`; 无法解析返回 ``None``。

    现金金额按优先级取**第一个命中**的港币值:

    1. :data:`_RE_EQUIV_HKD` —— 外币派息的港币折算等值 (占抽样约 34%), 兼容
       金额在币种前 (``相当于每股派18.13港元``) 与在后 (``相当于港币0.784812元``)
       两种语序, 以及 ``计算值`` / 繁体 ``於`` / 源数据笔误 ``港币股`` 等变体;
    2. :data:`_RE_CASH_HKD` —— 直接以港币宣派 (``每股派港币5.3元``, 占抽样约 51%)。

    ⚠️ **真正的护栏是「港」字, 不是匹配顺序**: 1 的两个分支都**强制要求**「港」,
    所以 ``每股派港币#元(相当于人民币#元)`` 里的人民币值永远不会被 1 命中, 会落到
    2 取港币值; ``汇率人民币#元:港币#元`` 这类**汇率**片段不含「相当于/相等于」
    前缀, 同样不会被误命中。

    (变异测试实测: 把 1、2 的先后顺序对调, 全部真实样本上结果**完全不变** ——
    顺序只是第二道防线; 放宽「港」字要求才是致命的, 那会让因子偏小约 8% 且数值
    看起来完全正常。)

    ``(可选择以股代息)`` (scrip dividend) **按全额现金处理** —— 现金股息是宣派
    的法定金额, 股东选择以股代息是个人的现金/股票取舍, 不改变除净日的价格调整。

    返回 ``None`` 的已知情形 (调用方须 warn+skip, **不得**当 0 处理)。抽样实测
    覆盖率 **1124/1159 = 96.98%**, 失败的 35 条全部属于「源数据未给港币数值」:

    - 外币宣派且无港币折算值 **23 条**: ``每股派美元0.1元(可选择以股代息)`` (20) /
      ``每股派人民币0.05元`` (3);
    - 分派股份 (spin-off) 无等值 **9 条**: ``每21股腾讯股份分派1股京东集团A类普通股股份``;
    - 红利认股权证 **3 条**: ``每12股派送1股红利认股权证`` (权证非送股, 已被
      :data:`_RE_BONUS` 的负向断言排除)。

    这些情形**宁缺勿造**: 编一个汇率或市价, 比留一条可见告警更危险 (CONTRIBUTING §4)。
    """
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None

    dividend = 0.0
    for pattern in (_RE_EQUIV_HKD, _RE_CASH_HKD):
        m = pattern.search(s)
        if m:
            # _RE_EQUIV_HKD 是双分支 (金额在币种前/后), 命中的分支决定哪个组非空。
            dividend = float(next(g for g in m.groups() if g is not None))
            break

    bonus = 0.0
    m = _RE_BONUS.search(s)
    if m:
        per, got = int(m.group(1)), int(m.group(2))
        if per > 0:
            bonus = got / per

    if dividend <= 0.0 and bonus <= 0.0:
        return None
    return HkDividendPlan(dividend_hkd=dividend, bonus_ratio=bonus)


def _parse_ex_date(value) -> date | None:
    """``EX_DIVIDEND_DATE`` → :class:`date`; 为空或无法解析 → ``None``。

    🔴 上游**同一份报表里两种日期格式并存**: ``NOTICE_DATE`` 是
    ``'2026-10-14 00:00:00'`` (横杠 + 时间尾巴), 而 ``EX_DIVIDEND_DATE`` /
    ``DIVIDEND_DATE`` 是 ``'2026-11-05'`` (**斜杠**, 无时间)。2026-11-21 实测汇丰
    ``00005`` 的 95 条事件, ``EX_DIVIDEND_DATE`` **全部**是斜杠格式,
    ``datetime.fromisoformat`` 一律拒绝 ⇒ 整只标的的分红史被静默判成「无除权事件」,
    复权因子库空着却把标的记进 ``coverage.json`` 的 covered (fail open), 下游
    ``close == raw_close`` 且**不报错**。故两种分隔符都接受 —— 与
    :mod:`app.plugins.tencent.hk_spot` 处理港股时间戳同一纪律 (那边也是斜杠)。
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        # 先砍时间尾巴再统一分隔符; fromisoformat 接受纯日期串
        return datetime.fromisoformat(s[:19].replace("/", "-")).date()
    except ValueError:
        logger.warning("港股分红除净日无法解析, 跳过: %r", value)
        return None


def fetch_hk_dividend_events(
    client: EastmoneyClient,
    symbol: str,
    *,
    page_size: int = HK_DIV_PAGE_SIZE,
) -> list[dict]:
    """拉单只港股的**已实际派付**分红事件 (原始 dict 行, 未解析)。

    ``IS_BFP="0"`` 过滤掉「未派发或宣派股息」占位行 (实测腾讯 91→27 行)。
    无记录时 ``datacenter_report`` 把 ``code=9201`` 转成 ``[]`` —— 这是**合法空
    结果** (该股从未派息), 与请求失败抛 :class:`EastmoneyError` 语义不同, 调用方
    须区分二者 (参见 ``kline_sync.fetch_adj_factor_single`` 的 ``strict`` 约定)。

    ⚠️ 服务端**不支持**按 ``EX_DIVIDEND_DATE`` 过滤, 故无法只拉增量窗口;
    每标的 1 次请求取全史, 由调用方落库后做本地增量。
    """
    secucode = hk_fin_secucode(symbol)
    flt = f'(SECUCODE="{secucode}")(IS_BFP="0")'
    return client.datacenter_report(
        RPT_HK_DIVIDEND,
        flt,
        page_size=page_size,
        page_number=1,
        sort_columns=HK_DIV_SORT_COLUMNS,
        sort_types=HK_DIV_SORT_TYPES,
    )


def build_hk_adj_factors(
    symbol: str,
    events: Iterable[Mapping],
    closes: Mapping[date, float],
) -> pl.DataFrame:
    """分红事件 + 原始收盘价 → 规范复权因子帧 (``symbol/trade_date/ex_factor`` + 明细列)。

    与 A 股 :meth:`provider.EastmoneyProvider.get_adj_factors` 同构, 但按港股规则:

    - **除净参考价** ``ref = (P − D) / (1 + B)`` —— 交易所除净公式在
      「无配股 (``A=0``)」下的退化形式; ``P`` 取**除净日前最后一个交易日**的原始
      收盘 (来自本地 ``kline_hk_daily``, 与 A 股取 ``closes[prev_days[-1]]`` 同源),
      ``D`` 每股现金分红 (港币), ``B`` 每股送转比例。
    - **``ex_factor = P / ref``** —— 单次事件的**前后比价, 不是累计值**;
      累计由 :func:`_apply_adj_factor` 的 ``cum_prod().over("symbol")`` 负责。
    - **不取整**: 港股无涨跌停, 无需对齐官方参考价的最小价位 (见模块 docstring)。
    - **同一除净日多条方案合并**为一条: ``D`` 求和、``B`` 求和后再算 ``ref``。
      ``_apply_adj_factor`` 会按 ``(symbol, trade_date)`` 去重, 不合并会丢事件。

    以下情形 **warn + 跳过该事件** (沿用 A 股 ``_ref_price`` 返回 None 的既有策略,
    符合 CONTRIBUTING §4「clearly indicate it」而非静默产出错值):

    - 方案文本无法解析 (:func:`parse_hk_dividend_plan` 返回 ``None``);
    - 本地缺除净日之前的收盘价 (无法定 ``P``);
    - ``D/P >`` :data:`HK_ADJ_MAX_DIVIDEND_RATIO` 或 ``B >``
      :data:`HK_ADJ_MAX_BONUS_RATIO` —— 比例不合理, 典型是把汇率误当金额;
    - ``P`` 或 ``ref`` 非正 / 非有限 (分红超过股价, 或上游收盘价是 NaN)。NaN 必须
      挡: 它穿过所有 ``>``/``<=`` 比较 (NaN 比较恒 False), 产出的 NaN 因子经
      ``cum_prod`` 会污染该除净日之后**整条**价格序列, 比漏一个事件严重得多。

    ``closes`` 为空或 ``events`` 为空 → 返回**空帧**, 语义是「无除权事件」
    (原始价即复权价), 与「取数失败」不同, 调用方不可混淆。
    """
    # 剔除无收盘价的行 (停牌 / 数据缺失): 它们定不出参考价, 且 bisect 应回退到更早
    # 的**有效**交易日 —— 那才是「除净日前最后一个有成交的交易日」。顺带挡掉
    # float(None) 的 TypeError (一只脏数据不该炸掉整批)。
    closes = {d: c for d, c in closes.items() if c is not None}
    if not closes:
        return pl.DataFrame()

    # 按除净日聚合: 同日多方案 (如末期息 + 特别息) 必须合并, 否则去重会丢事件。
    agg: dict[date, list[float]] = {}
    skipped = 0
    for ev in events:
        ex = _parse_ex_date(ev.get("EX_DIVIDEND_DATE"))
        if ex is None:
            continue  # 无除净日 = 尚未实施, 不影响历史价格
        plan = parse_hk_dividend_plan(ev.get("PLAN_EXPLAIN"))
        if plan is None:
            skipped += 1
            logger.warning(
                "港股分红方案无法解析为数值, 跳过该除权事件 (复权链在此日前后会有偏差): "
                "%s %s %r",
                symbol, ex.isoformat(), ev.get("PLAN_EXPLAIN"),
            )
            continue
        slot = agg.setdefault(ex, [0.0, 0.0])
        slot[0] += plan.dividend_hkd
        slot[1] += plan.bonus_ratio

    if skipped:
        logger.warning("港股 %s 共 %d 条分红方案无法解析 (已逐条告警)", symbol, skipped)

    trade_dates = sorted(closes)
    rows: list[dict] = []
    for ex in sorted(agg):
        dividend, bonus = agg[ex]
        # 除净日前最后一个交易日 (bisect_left 命中 ex 本身时取其前一位)
        i = bisect_left(trade_dates, ex) - 1
        if i < 0:
            logger.warning(
                "港股 %s 除净日 %s 之前无本地收盘价, 无法定参考价, 跳过", symbol, ex
            )
            continue
        prev_day = trade_dates[i]
        p = float(closes[prev_day])
        # NaN 必须挡: 它会穿过下面所有 `>`/`<=` 比较 (NaN 比较恒 False), 产出 NaN
        # ex_factor, 再经 _apply_adj_factor 的 cum_prod 污染该日之后整条价格序列。
        if not math.isfinite(p) or p <= 0.0:
            logger.warning("港股 %s %s 前收盘价非正或非有限 (%s), 跳过", symbol, prev_day, p)
            continue
        if dividend / p > HK_ADJ_MAX_DIVIDEND_RATIO:
            logger.warning(
                "港股 %s 除净日 %s 现金分红 %.6f 占前收盘 %.4f 的 %.1f%%, 超出合理上限 "
                "%.0f%% (疑似把汇率/总额误当每股金额), 跳过",
                symbol, ex, dividend, p, dividend / p * 100, HK_ADJ_MAX_DIVIDEND_RATIO * 100,
            )
            continue
        if bonus < 0.0 or bonus > HK_ADJ_MAX_BONUS_RATIO:
            logger.warning(
                "港股 %s 除净日 %s 送转比例 %.4f 超出合理上限 %.1f, 跳过",
                symbol, ex, bonus, HK_ADJ_MAX_BONUS_RATIO,
            )
            continue
        ref = (p - dividend) / (1.0 + bonus)
        if not math.isfinite(ref) or ref <= 0.0:
            logger.warning(
                "港股 %s 除净日 %s 参考价非正或非有限 (P=%.4f D=%.6f B=%.4f), 跳过",
                symbol, ex, p, dividend, bonus,
            )
            continue
        rows.append({
            "symbol": symbol,
            "trade_date": ex,
            "ex_factor": p / ref,
            "dividend": dividend,
            "bonus": bonus,
            "allot": None,        # 港股分红无配股
            "allot_price": None,
            "prev_close": p,
        })

    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("ex_factor").cast(pl.Float64),
        *[pl.col(c).cast(pl.Float64) for c in ("dividend", "bonus", "allot", "allot_price", "prev_close")],
    )
