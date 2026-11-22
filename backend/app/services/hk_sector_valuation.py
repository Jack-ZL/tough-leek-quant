"""港股**恒生行业估值温度计** —— 按行业聚合的 TTM 股息率与派息覆盖率 (PR6)。

方案 §10 第一梯队「恒生行业四件套」的第 2 件。热力图/轮动/L2 下钻都已在
``20abf184`` 落地, 本模块补的是唯一还缺的那一件。

为什么主指标是「派息者中位数」而不是「全体成分中位数」
----------------------------------------------------
方案原文写的是「PE 中位数 / 平均股息率 / 成分覆盖」, 并特别注明**按股息率降序**
(「价值洼地识别」), 因为「港股是红利市场, 这个比涨幅排名更有用」。

实测 (2026-11-22, 全市场 2838 只港币柜台 / 2694 只有行情 / 988 只近一年派息) 表明
**全体成分口径的中位数在港股会退化成 0**: 派息覆盖率普遍低于 50% (整体 36.8%),
31 个行业里有 15 个以上的中位数恰好是 ``0.0``, 排序后第一屏全是并列的零 —— 一张
分不出高低的温度计。所以:

- ``median_yield_pct`` / ``avg_yield_pct`` / ``max_yield_pct`` 一律**只在派息者上**
  统计, 回答「这个行业里愿意派息的公司有多大方」;
- ``coverage_pct`` 与之**并列展示**, 回答「这个行业有多少公司真的派息」。

两个数必须一起看: 只看中位数会把「8 家里 3 家派、派得很高」和「200 家里 180 家派」
当成同一件事。方案把「成分覆盖」列为独立一列, 正是为了消除这个歧义。

🔴 ``avg_yield_pct`` 受仙股一次性派息严重扭曲 (实测专业零售 均值 23.6% vs 中位数
8.1%: 有标的股价 0.075 而全年派息 0.266, 单只就贡献 354%)。**中位数是排序键**,
均值只作参考, 前端必须这样标注, 不得把均值当头条数字。

为什么没有 PE
-------------
方案要求「PE 中位数」, 本模块**刻意不提供**, 三条独立原因:

1. 东财 ``clist`` 的 ``f9`` (市盈率动) 虽然在 ``HK_SPOT_FIELDS`` 里, 但 push2 集群
   对本机是 **IP 级封禁** (见 :mod:`app.plugins.eastmoney.hk_profile` 模块
   docstring 的实测记录), 而维表主同步路径早已改走 datacenter + 腾讯, clist 只是
   封禁解除后的备用路 —— 依赖它等于把功能建在当前打不通的链路上。
2. 财务宽表 ``main_indicator.pe_ttm`` **不是历史时序, 而是同步时点的快照被复制到
   该标的的每一行报告期上**。实测 00700.HK 的 2024年报~2026中报 共 7 个不同
   ``report_date`` 上 ``pe_ttm`` 全等于 ``12.393295``、``total_market_cap`` 全等于
   ``3.3897e12``; 只有最新一次同步覆盖到的 2026三季报 是另一个值
   (``11.435873``)。
3. 该表**没有记录这个快照的 as-of 日期**, 因此无法判断手上的 PE 是今天的还是三周
   前的。把一个 as-of 未知的估值摆进「温度计」, 正是 CONTRIBUTING §4 禁止的
   「宁可明说没有, 不可冒充」。

缺源这件事由 API 层的 ``pe_available: false`` 如实下发, 前端明示而不是留一个空列
(与「港股无概念板块数据源」同一处理方式)。若日后拿到带 as-of 日期的全市场估值源,
在本模块加一列即可, 前端契约不必改。

口径 (CONTRIBUTING §3.1 / §3.2)
--------------------------------
- ``*_yield_pct`` 一律是**百分比数值**: ``5.0`` 表示 ``5.0%``, 不是 ``0.05``。
  换算在本模块内一次做完, 前端不得再乘 100。
- 分母是**不复权**收盘价 (``kline_hk_daily.close``)。用前复权价会把有分红史的
  标的股息率系统性算高 (§3.2), 与 :mod:`app.services.hk_dividend_calendar` 同一
  理由。
- 分子是**除净日**落在窗口内的每股现金派息之和 (港币)。用除净日而非公告日:
  除净日当天股价除权, 那才是「持有该股能拿到这笔钱」的时点。中期息 + 末期息
  自然都被 12 个月窗口收进来, 这就是标准 TTM 口径。
- 🔴 **只计港币柜台** (``is_cny_counter != True``)。同一公司的港币/人民币双柜台
  在维表里是两行, 不过滤会让该公司在行业里占两个席位、派息被重复计入 ——
  实测 2862 行里有 24 行是人民币柜台。
- 🔴 **行业为空的成分不归进「未知」组**。维表 2838 只里有 7 只 ``industry`` 为空
  (债券残留), 造一个「未知」桶会凭空生出一个成分数排名靠前的假行业 (前端
  ``industry-analysis/market.test.tsx`` 已就热力图钉过同一条规则)。它们计入
  ``unclassified_count`` 单独下发。

null 与 0 的区别 (§13 不得伪造)
------------------------------
- ``yield = 0.0``: 有行情、窗口内**确无**现金派息事件 —— 这是一个查证过的事实。
- ``yield = null``: 算不出来。两种成因: 最近若干个日K分区里没有收盘价 (未同步 /
  长期停牌), 或该标的窗口内所有派息事件的金额都未能解析 (外币未折算 / 纯送股 /
  权证)。后者**绝不能**当成 0: 那会把「派了但读不出多少」说成「没派」。
  这类标的计入 ``excluded_count``, 不进任何统计。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.plugins.eastmoney.client import EastmoneyClient
from app.services.hk_dividend_calendar import (
    _latest_hk_closes,
    hk_dividend_calendar,
)
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH

logger = logging.getLogger(__name__)

#: 缺省回看窗口 (自然日)。365 天 = 标准 TTM; 港股分红高度集中在年报季, 短于一年
#: 的窗口会让「一年一派」的公司整体落空, 覆盖率假性偏低。
HK_SECTOR_VALUATION_DEFAULT_DAYS = 365

#: 输出列顺序。API 层直接 ``to_dicts()``, 故这里是响应字段顺序的唯一事实来源。
HK_SECTOR_VALUATION_COLS = (
    "industry",
    "member_count",
    "priced_count",
    "payer_count",
    "excluded_count",
    "coverage_pct",
    "median_yield_pct",
    "avg_yield_pct",
    "max_yield_pct",
    "top_symbol",
    "top_name",
    "top_yield_pct",
)


class HkSectorValuation(NamedTuple):
    """一次温度计计算的结果: 行业表 + 全市场口径的元信息。

    ``meta`` 里的计数不是装饰: ``member_total`` / ``priced_total`` /
    ``unclassified_count`` 让「全市场口径」这件事**可被审计** —— 方案 §10 特别
    警告过 QuantMind 早期误用 141 只港股通子集而返工, 因此这里把分母如实交出,
    调用方 (与测试) 可以直接核对量级。
    """

    industries: pl.DataFrame
    meta: dict


def _read_hk_members(data_dir: Path) -> pl.DataFrame:
    """维表里的**港币柜台**成分: ``symbol/name/industry``。

    与 :func:`hk_dividend_calendar._read_hk_instrument_names` 分开写而不是复用:
    那边是「给日历行补名称」的左连接查找表, 不做柜台过滤 (日历要如实展示每一行
    事件); 这边是「行业成分全集」, 必须过滤人民币柜台, 否则双柜台公司占两个席位。
    两者语义不同, 强行合并会让其中一个用途拿到错的分母。

    维表缺失/不可读时返回空表 —— 调用方据此产出空温度计, 而不是 500。
    """
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "name": pl.String, "industry": pl.String}
    )
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return empty
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001 — 维表损坏不该让整个端点 500
        logger.warning("港股维表读取失败, 行业温度计将为空: %s", e)
        return empty
    if "symbol" not in df.columns:
        return empty
    if "is_cny_counter" in df.columns:
        # fill_null: 早期维表没有这一列, 读出来是 null, 应视为「不是人民币柜台」
        # 而不是被过滤掉 —— 否则整张维表会被判空。
        df = df.filter(pl.col("is_cny_counter").fill_null(False).cast(pl.Boolean).not_())
    cols = [c for c in ("symbol", "name", "industry") if c in df.columns]
    out = df.select(cols)
    for missing in ("name", "industry"):
        if missing not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.String).alias(missing))
    # 维表按构造 symbol 唯一; 这一行 unique 换掉「重复键让后续 join 静默翻倍」
    # 这个在页面上看不出来的失败模式 (与日历同处理)。
    return out.select("symbol", "name", "industry").unique(subset=["symbol"], keep="first")


def _per_symbol_ttm(cal: pl.DataFrame) -> pl.DataFrame:
    """把逐事件的日历压成**逐标的**的窗口内派息合计。

    🔴 ``dividend_hkd.sum()`` 在全 null 组上返回 ``0.0`` 而不是 null (polars 语义),
    所以必须同时带出 ``unparsed_events``: 只有它能区分「真没派」(事件数 0) 与
    「派了但金额读不出」(事件数 >0 且全部未解析)。少了这一列, 后者会被算成
    0% 股息率 —— 一个看不出来的伪造。
    """
    if cal.is_empty():
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ttm_dividend_hkd": pl.Float64,
                "event_count": pl.UInt32,
                "unparsed_events": pl.UInt32,
            }
        )
    return cal.group_by("symbol").agg(
        pl.col("dividend_hkd").sum().alias("ttm_dividend_hkd"),
        pl.len().cast(pl.UInt32).alias("event_count"),
        pl.col("dividend_hkd").is_null().sum().cast(pl.UInt32).alias("unparsed_events"),
    )


def hk_sector_valuation(
    repo,
    *,
    days: int = HK_SECTOR_VALUATION_DEFAULT_DAYS,
    end: date | None = None,
    client: EastmoneyClient | None = None,
) -> HkSectorValuation:
    """按恒生行业聚合 ``[end-days+1, end]`` 内除净的 TTM 股息率。

    Args:
        repo: ``Repository``, 只用它的 ``store.data_dir``。
        days: 回看自然日数, 缺省 :data:`HK_SECTOR_VALUATION_DEFAULT_DAYS`。
        end: 窗口末日 (含), 缺省今天。
        client: 复用已有东财客户端; ``None`` 时由日历服务自建并关闭。

    Returns:
        :class:`HkSectorValuation`。``industries`` 按 ``median_yield_pct`` 降序
        (nulls 最后), 一行 = 一个恒生行业。

    窗口内无人派息时返回**空统计而不是抛错** —— 那是合法答案。上游请求失败则原样
    抛 :class:`~app.plugins.eastmoney.client.EastmoneyError`, 由 API 层转 502;
    **不得**降级成空表, 那会把「拉不到」伪装成「没有分红」(§4)。
    """
    end_date = end or date.today()
    start_date = end_date - timedelta(days=days - 1)

    cal = hk_dividend_calendar(repo, start=start_date, end=end_date, client=client)
    data_dir = repo.store.data_dir
    members = _read_hk_members(data_dir)

    meta: dict = {
        "window_start": start_date,
        "window_end": end_date,
        "days": days,
        "event_count": int(cal.height),
        "member_total": int(members.height),
        "unclassified_count": 0,
        "priced_total": 0,
        "payer_total": 0,
        "excluded_total": 0,
    }
    empty = pl.DataFrame(
        schema={
            "industry": pl.String,
            "member_count": pl.UInt32,
            "priced_count": pl.UInt32,
            "payer_count": pl.UInt32,
            "excluded_count": pl.UInt32,
            "coverage_pct": pl.Float64,
            "median_yield_pct": pl.Float64,
            "avg_yield_pct": pl.Float64,
            "max_yield_pct": pl.Float64,
            "top_symbol": pl.String,
            "top_name": pl.String,
            "top_yield_pct": pl.Float64,
        }
    )
    if members.is_empty():
        return HkSectorValuation(empty, meta)

    # 收盘价要为**全体成分**取, 不只是派息者: 没派息的标的必须能被判成 0% 而不是
    # 被排除, 否则覆盖率的分母就只剩派息者, coverage 恒等于 100%。
    closes = _latest_hk_closes(data_dir, members["symbol"].to_list())
    base = (
        members.join(closes.select("symbol", "raw_close"), on="symbol", how="left")
        .join(_per_symbol_ttm(cal), on="symbol", how="left")
        .with_columns(
            pl.col("event_count").fill_null(0),
            pl.col("unparsed_events").fill_null(0),
            pl.col("ttm_dividend_hkd").fill_null(0.0),
        )
    )

    has_price = (
        pl.col("raw_close").is_not_null()
        & pl.col("raw_close").is_finite()
        & (pl.col("raw_close") > 0)
    )
    base = base.with_columns(
        pl.when(has_price.not_())
        .then(None)
        # 派了但金额全读不出 → 算不出来, 不是 0 (见模块 docstring)
        .when(pl.col("unparsed_events") > 0)
        .then(None)
        .when(pl.col("event_count") == 0)
        .then(0.0)
        .otherwise(pl.col("ttm_dividend_hkd") / pl.col("raw_close") * 100.0)
        .cast(pl.Float64)
        .alias("yield_pct")
    )

    classified = base.filter(
        pl.col("industry").is_not_null() & (pl.col("industry").str.strip_chars() != "")
    )
    meta["unclassified_count"] = int(base.height - classified.height)
    meta["priced_total"] = int(classified.filter(pl.col("yield_pct").is_not_null()).height)
    meta["payer_total"] = int(classified.filter(pl.col("yield_pct") > 0).height)
    meta["excluded_total"] = int(meta["member_total"] - meta["unclassified_count"] - meta["priced_total"])

    if classified.is_empty():
        return HkSectorValuation(empty, meta)

    # 🔴 三个统计量都只在**派息者** (yield > 0) 上算, 理由见模块 docstring:
    # 全体成分口径的中位数在港股会退化成 0。
    payers = pl.col("yield_pct") > 0
    grouped = classified.group_by("industry").agg(
        pl.len().cast(pl.UInt32).alias("member_count"),
        pl.col("yield_pct").is_not_null().sum().cast(pl.UInt32).alias("priced_count"),
        payers.sum().cast(pl.UInt32).alias("payer_count"),
        pl.col("yield_pct").is_null().sum().cast(pl.UInt32).alias("excluded_count"),
        pl.col("yield_pct").filter(payers).median().alias("median_yield_pct"),
        pl.col("yield_pct").filter(payers).mean().alias("avg_yield_pct"),
        pl.col("yield_pct").filter(payers).max().alias("max_yield_pct"),
    )

    # 龙头派息者单独算再左连接: 组内「取某列按另一列排序后的首行」用 struct 表达式
    # 也能写, 但可读性差且各 polars 版本行为有出入; 一次 sort + unique 更直白。
    top = (
        classified.filter(payers)
        .select("industry", "symbol", "name", "yield_pct")
        .sort("yield_pct", descending=True)
        .unique(subset=["industry"], keep="first")
        .rename(
            {
                "symbol": "top_symbol",
                "name": "top_name",
                "yield_pct": "top_yield_pct",
            }
        )
    )
    grouped = grouped.join(top, on="industry", how="left").with_columns(
        (
            pl.when(pl.col("priced_count") > 0)
            .then(pl.col("payer_count") / pl.col("priced_count") * 100.0)
            .otherwise(None)
        )
        .cast(pl.Float64)
        .alias("coverage_pct")
    )

    industries = grouped.select(list(HK_SECTOR_VALUATION_COLS)).sort(
        "median_yield_pct", descending=True, nulls_last=True
    )
    return HkSectorValuation(industries, meta)


__all__ = [
    "HK_SECTOR_VALUATION_COLS",
    "HK_SECTOR_VALUATION_DEFAULT_DAYS",
    "HkSectorValuation",
    "hk_sector_valuation",
]
