"""AH 溢价 —— A 股相对其 H 股的价差, **只读**聚合。

为什么这个功能不需要新同步、不需要新存储
--------------------------------------
四个输入里三个已经在本地: A 股收盘价 (``kline_daily``)、H 股收盘价
(``kline_hk_daily``)、H 股上市日 (``instruments_hk`` 维表)。只有两个是实时取的:
AH 配对关系 (东财 F10, 实测 2 个请求 ≈1.3s) 和港币汇率 (新浪, 1 个请求)。

配对关系**极稳定** —— 只在新 AH 股上市时才变, 一年数次。为它落一张表, 换来的
只是「同步没跑 → 溢价表是旧的」这一类额外失败模式 (§4), 而实时读既准又便宜。
故本模块**不写任何存储**, 与 :mod:`app.services.hk_dividend_calendar` 同一取舍:
上游限速/熔断由 client 负责, 这里不另加缓存, 前端 TanStack Query 的 ``staleTime``
挡住重复打开页面造成的重复请求。

单位与口径 (CONTRIBUTING §3.1 / §3.2)
-------------------------------------
``premium_pct`` 是**百分比数值**: ``78.0`` 表示 A 股比 H 股贵 ``78.0%``, 不是
``0.78``。公式::

    premium_pct = (a_close / (h_close × hkd_cny) − 1) × 100

- **正值 = A 股贵** (溢价), **负值 = H 股贵** (折价)。实测 2026-11-20 全市场
  211 对: 中位数 +78.0%, 最低是宁德时代 −20.2% (H 股贵), 与公开认知一致。
- 🔴 **两侧都必须用不复权原始价**。读的是 ``kline_daily.close`` /
  ``kline_hk_daily.close`` 这两张**基础分区表**, 而不是 enriched: 项目契约是
  「落库必须原始价」, enriched 流水线正是用 ``pl.col("close").alias("raw_close")``
  把基础价原样搬过去、再对 ``close`` 施加复权因子链 (``app/indicators/pipeline.py``
  三处)。拿前复权价算溢价会把有分红史的标的系统性算错 —— A 股分红远多于 H 股,
  误差还会**偏向一个方向**, 属于 §3 警告的「看起来对但错」。
- 🔴 **汇率换算在本项目里默认是禁止的** (``tencent/hk_kline.py`` /
  ``tencent/hk_spot.py`` / ``financial_analyzer.py`` 共四处写着「换算即伪造」/
  「禁止自行做汇率换算」)。AH 溢价是**唯一**正当例外, 因为它本质就是跨市场比价,
  不换算就没有这个指标。正当性靠四个条件换来, 缺一不可:
  (1) 汇率**有明确来源** (:mod:`app.plugins.sina.fx`, 新浪 ``fx_shkdcny``);
  (2) 汇率**带日期** (``fx_as_of``), 不用「今天的」冒充「行情那天的」;
  (3) 汇率**在响应与 UI 里显式可见**, 不埋在代码里;
  (4) **取不到就 fail closed** —— 抛 :class:`~app.plugins.sina.client.SinaError`
  由 API 层转 502, **绝不**静默用 1.0 兜底。1% 的汇率误差会线性平移**每一只**
  标的的溢价 1%, 而在页面上完全看不出来。

🔴 A/H 交易日历不同 (港股复活节/圣诞休市, A 股国庆休市), 两侧「最新收盘价」可能
不是同一天。本模块的处理是**分级**的, 不是一刀切:

- 相差 ≤ :data:`AH_PREMIUM_MAX_PRICE_LAG_DAYS` 天 → 照常计算, 但把两个日期都
  放进响应, 并计入 ``price_lag_count``。理由: 港股单独休市那天把整页清空, 恰好
  是用户最想看的时刻; 而 1~2 天的价格漂移 (通常 ±1~2%) 相对 78% 的中位溢价是
  可接受的近似, **前提是它被如实标注**。
- 相差 > 该阈值 → ``premium_pct`` 置 null 并计入 ``stale_price_count``。一周前的
  价格不是可用的比价基准, 宁可不出 (§4)。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.market import Market
from app.plugins.eastmoney.ah_pair import AhPairCandidate, fetch_ah_pair_candidates
from app.plugins.eastmoney.client import EastmoneyClient
from app.plugins.sina.client import SinaClient
from app.plugins.sina.fx import FX_SOURCE, fetch_hkd_cny
from app.services.instrument_sync_hk import HK_INSTRUMENT_RELPATH
from app.tickflow.repository import daily_dirname

logger = logging.getLogger(__name__)

#: 输出列顺序。API 层直接 ``to_dicts()``, 故这里就是响应字段顺序的唯一事实来源。
AH_PREMIUM_COLS = (
    "a_symbol",
    "a_name",
    "h_symbol",
    "h_name",
    "a_close",
    "a_price_date",
    "h_close",
    "h_price_date",
    "premium_pct",
)

#: 取最新收盘价时往回看几个**日K分区**。与 ``hk_dividend_calendar`` 同值同理:
#: 10 个分区 ≈ 两周, 足以覆盖常规停牌, 而成本只是 10 个小文件 (``kline_hk_daily``
#: 实测 9102 个分区, 全扫不可接受)。超出窗口仍无价的标的留 null, 不用更旧的价格
#: 冒充当前价。
AH_PREMIUM_CLOSE_LOOKBACK_PARTITIONS = 10

#: A/H 收盘价允许的最大日历日差。超过则 ``premium_pct`` 置 null (fail closed)。
AH_PREMIUM_MAX_PRICE_LAG_DAYS = 5

#: 溢价结果的计价币种。A 股收盘价是人民币, H 股收盘价经 ``fx_rate`` 折成人民币,
#: 故溢价本身是无量纲比值; 此字段标注的是**两个价格列**的币种。
AH_PREMIUM_CURRENCY = "CNY"


class AhPremiumResult(NamedTuple):
    """:func:`ah_premium` 的结果: 数据 + **全部口径元信息**。

    汇率三件套 (``fx_rate``/``fx_as_of``/``fx_source``) 是必填而非可选: 本模块是
    项目里唯一被允许做汇率换算的地方, 换算依据必须随数据一起交付, 前端才能把它
    显示出来 (见模块 docstring 的四个正当性条件)。
    """

    #: 列为 :data:`AH_PREMIUM_COLS` 的表, 按 ``premium_pct`` 降序 (null 在最后)。
    items: pl.DataFrame
    #: 港币→人民币汇率 (1 港币 = ``fx_rate`` 人民币)。实测 0.852 量级。
    fx_rate: float
    #: 汇率的**上游日期**, 不是「今天」。
    fx_as_of: date
    #: 汇率来源标识 (:data:`app.plugins.sina.fx.FX_SOURCE`)。
    fx_source: str
    #: 价格列币种 (:data:`AH_PREMIUM_CURRENCY`)。
    currency: str
    #: 上游给出的 AH 配对候选总数 (消歧/去重/缺价**之前**)。
    candidate_count: int
    #: 无法消歧而被丢弃的 A 股代码。如实暴露而不是悄悄吞掉 (§4)。
    ambiguous_a_symbols: tuple[str, ...]
    #: 因 A 或 H 缺本地收盘价而未产出溢价的对数。
    missing_price_count: int
    #: 因 A/H 价格日期差超过 :data:`AH_PREMIUM_MAX_PRICE_LAG_DAYS` 而置空的对数。
    stale_price_count: int
    #: A/H 价格日期不同 (但在阈值内, 仍计算了溢价) 的对数。
    price_lag_count: int


def _empty_items() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "a_symbol": pl.String,
            "a_name": pl.String,
            "h_symbol": pl.String,
            "h_name": pl.String,
            "a_close": pl.Float64,
            "a_price_date": pl.Date,
            "h_close": pl.Float64,
            "h_price_date": pl.Date,
            "premium_pct": pl.Float64,
        }
    )


def _latest_closes(
    data_dir: Path,
    rel_dirname: str,
    symbols: list[str],
    *,
    lookback: int = AH_PREMIUM_CLOSE_LOOKBACK_PARTITIONS,
) -> pl.DataFrame:
    """每只标的**最近一个有行情的分区**里的收盘价, 列为 ``symbol/close/price_date``。

    与 :func:`app.services.hk_dividend_calendar._latest_hk_closes` 是同一套逻辑,
    只是把目录参数化了 (本模块要同时读 A 股与港股两侧)。**刻意不跨服务复用那个
    私有函数**: 它是 HK 专用的 (内部硬编码 ``_hk_daily_dir``), 把它改成通用会动到
    已提交且有 29 个测试覆盖的热路径 (§5.2 热点小改)。两处各 ~35 行的重复, 换来
    两个服务互不耦合、各自可独立测试。

    🔴 读基础分区表而不是 enriched —— 见模块 docstring 的复权口径说明。

    从最新分区往回扫, 每只标的取**第一次出现**的收盘价即为其最近价; 扫满
    ``lookback`` 个分区或集齐全部标的即停。目录不存在/不可读时返回空表 —— 调用方
    据此把溢价留 null, 而不是抛错让整个页面 500。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64, "price_date": pl.Date})
    if not symbols:
        return empty

    daily_dir = data_dir / rel_dirname
    try:
        # 分区目录名是 ``date=YYYY-MM-DD``, 字典序即时间序
        partitions = sorted(
            (p for p in daily_dir.iterdir() if p.is_dir() and p.name.startswith("date=")),
            key=lambda p: p.name,
            reverse=True,
        )[:lookback]
    except OSError as e:
        logger.debug("日K目录不可读 (%s): %s", daily_dir, e)
        return empty
    if not partitions:
        return empty

    wanted = set(symbols)
    frames: list[pl.DataFrame] = []
    for part in partitions:
        path = part / "part.parquet"
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path, columns=["symbol", "date", "close"])
        except Exception as e:  # noqa: BLE001 — 单个分区损坏不该让整个页面失败
            logger.warning("日K分区读取失败, 跳过 %s: %s", path, e)
            continue
        df = df.filter(pl.col("symbol").is_in(list(wanted)))
        if df.is_empty():
            continue
        frames.append(df.rename({"date": "price_date"}).select("symbol", "close", "price_date"))
        # 越新的分区越先扫, 所以已收进来的标的可以从候选里剔除
        wanted -= set(df["symbol"].to_list())
        if not wanted:
            break

    if not frames:
        return empty
    # 新分区在前, ``keep="first"`` 即每只标的的最近价
    return pl.concat(frames, how="vertical").unique(subset=["symbol"], keep="first")


def _read_hk_instruments(data_dir: Path) -> pl.DataFrame:
    """港股维表的 ``symbol/name/listing_date``; 缺失或不可读时返回空表。

    ``listing_date`` 是**多柜台消歧的唯一依据** (见 :func:`_resolve_pairs`), 维表
    读不到时消歧就只能失败 —— 但失败方式是把该对计入 ``ambiguous`` 而不是抛错,
    因为其余 200 多对单候选的配对仍然完全可用 (优雅降级)。

    直读 parquet 而不走 ``instruments_hk`` 视图, 与 ``hk_dividend_calendar`` 同一
    理由: 视图在目录为空时不注册, 查询会 500 而不是如实表达「还没同步维表」。
    """
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "name": pl.String, "listing_date": pl.Date}
    )
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return empty
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股维表读取失败, AH 溢价将只用上游名称且无法消歧多柜台: %s", e)
        return empty
    if "symbol" not in df.columns:
        return empty
    cols = [c for c in ("symbol", "name", "listing_date") if c in df.columns]
    out = df.select(cols)
    if "name" not in out.columns:
        out = out.with_columns(pl.lit(None, dtype=pl.String).alias("name"))
    if "listing_date" not in out.columns:
        out = out.with_columns(pl.lit(None, dtype=pl.Date).alias("listing_date"))
    else:
        # 维表里已是 date, 但换源/历史分区可能是字符串; strict=False 让脏值变 null
        # 而不是抛错 —— 上市日只用于多柜台消歧, 缺了顶多计入 ambiguous。
        out = out.with_columns(pl.col("listing_date").cast(pl.Date, strict=False))
    # 维表按构造应当 symbol 唯一, 但左连接遇到重复键会**静默把行翻倍**, 这种错误
    # 在页面上看不出来。一行 unique 换掉这个失败模式 (同 ``hk_dividend_calendar``)。
    return out.select("symbol", "name", "listing_date").unique(subset=["symbol"], keep="first")


def _resolve_pairs(
    candidates: list[AhPairCandidate],
    hk_listing_dates: dict[str, date | None],
    hk_names: dict[str, str | None],
) -> tuple[list[tuple[str, str | None, str, str | None]], list[str]]:
    """候选 → 一对一配对, 返回 ``(pairs, ambiguous_a_symbols)``。

    两条消歧规则, **都只在有数据支撑时才生效, 否则一律丢弃并计入 ambiguous**
    (fail closed, §4)。绝不靠「名字看起来像」或「代码数字小」这类猜测取舍 (§13)。

    **规则 1 — 一只 A 股对应多个 H 柜台**: 取 H 股**上市日最早**的那个。
    依据是实测: ``601899.SH 紫金矿业 → ('02958.HK','02899.HK')``, 其中
    ``02899.HK`` 每手 500 股 / 2003-12-23 上市, ``02958.HK`` 每手 2000 股 /
    **2026-11-13** 上市 —— 上游名称里的 ``(五百)/(二千)`` 就是每手股数, 后者是刚开
    的第二柜台。原始柜台才是有连续历史、可与之比价的那个。``603993.SH 洛阳钼业``
    同型 (``03993.HK`` 2007 年 vs ``02957.HK`` 2026 年)。
    上市日缺失 (任一柜台) 或并列 → 丢弃 (不猜)。

    **规则 2 — 一个 H 股被多只 A 股认领**: 先比 A 股上市日取最早; **并列时**再要求
    A 股简称与 H 股简称**完全相等**, 唯一命中者胜。
    实测唯一一组: ``02607.HK`` 同时被 ``600849.SH 上药转换`` 和
    ``601607.SH 上海医药`` 认领, 两者 A 股上市日**都是 1994-03-24** (规则 2 的第一
    层失效), 而 H 股简称是 ``上海医药`` —— 与 ``601607.SH`` 精确相等, 与
    ``600849.SH`` 不等, 故取后者。
    🔴 名称相等**只用作并列时的 tiebreak, 不用作通用规则**: A/H 简称普遍不同
    (``000002.SZ 万科A`` vs ``02202.HK 万科企业``、``600775.SH 南京熊猫`` vs
    ``00553.HK 南京熊猫电子股份``), 拿它做主规则会误杀一大片。要求「精确相等且
    唯一命中」是保守的: 宁可留 ambiguous 也不误配。
    """
    ambiguous: list[str] = []
    resolved: list[tuple[str, str | None, str, str | None]] = []

    # --- 规则 1: 多柜台择一 ---
    for cand in candidates:
        if len(cand.h_candidates) == 1:
            h = cand.h_candidates[0]
            resolved.append((cand.a_symbol, cand.a_name, h, cand.h_names[0]))
            continue
        dated = [
            (hk_listing_dates.get(h), h, n)
            for h, n in zip(cand.h_candidates, cand.h_names, strict=True)
        ]
        # 🔴 只要有**任一**柜台查不到上市日就整组丢弃: 「另一个缺失」不等于「另一个
        # 更晚」。宽松处理会在"原始柜台恰好不在维表里"时静默选中新的第二柜台, 算出
        # 一个与全市场口径不一致的溢价 —— 而结果看起来完全正常。213 个候选里只有
        # 2 个是多柜台, 丢弃的代价极小。
        undated = [h for d, h, _ in dated if d is None]
        if undated:
            logger.warning(
                "AH 配对: %s 有 %d 个 H 柜台, 其中 %s 在维表里查不到上市日, 无法消歧, 已丢弃",
                cand.a_symbol,
                len(cand.h_candidates),
                undated,
            )
            ambiguous.append(cand.a_symbol)
            continue
        earliest = min(d for d, _, _ in dated)
        tied = [(h, n) for d, h, n in dated if d == earliest]
        if len(tied) != 1:
            logger.warning(
                "AH 配对: %s 的 %d 个 H 柜台上市日并列于 %s, 无法消歧, 已丢弃",
                cand.a_symbol,
                len(tied),
                earliest,
            )
            ambiguous.append(cand.a_symbol)
            continue
        resolved.append((cand.a_symbol, cand.a_name, tied[0][0], tied[0][1]))

    # --- 规则 2: 同一 H 被多只 A 认领时去重 ---
    by_h: dict[str, list[tuple[str, str | None, str, str | None]]] = defaultdict(list)
    for item in resolved:
        by_h[item[2]].append(item)

    a_listing = {c.a_symbol: c.a_listing_date for c in candidates}
    pairs: list[tuple[str, str | None, str, str | None]] = []
    for h_symbol, group in by_h.items():
        if len(group) == 1:
            pairs.append(group[0])
            continue
        dated = [(a_listing.get(a), a, an, hn) for a, an, _, hn in group]
        with_date = [x for x in dated if x[0] is not None]
        if not with_date:
            logger.warning("AH 配对: %s 被 %d 只 A 股认领且都无上市日, 已丢弃", h_symbol, len(group))
            ambiguous.extend(a for _, a, _, _ in dated)
            continue
        earliest = min(d for d, _, _, _ in with_date)
        tied = [x for x in with_date if x[0] == earliest]
        if len(tied) > 1:
            # 上市日并列 → 要求 A 简称与 H 简称精确相等且唯一命中
            h_name = hk_names.get(h_symbol)
            matched = [x for x in tied if h_name is not None and x[2] == h_name]
            if len(matched) == 1:
                tied = matched
        if len(tied) != 1:
            logger.warning(
                "AH 配对: %s 被 %s 同时认领且无法消歧, 已全部丢弃",
                h_symbol,
                [x[1] for x in tied],
            )
            ambiguous.extend(x[1] for x in dated)
            continue
        _, a_symbol, a_name, h_name = tied[0]
        pairs.append((a_symbol, a_name, h_symbol, h_name))

    pairs.sort(key=lambda item: item[0])
    return pairs, sorted(set(ambiguous))


def ah_premium(
    repo,
    *,
    client: EastmoneyClient | None = None,
    sina_client: SinaClient | None = None,
) -> AhPremiumResult:
    """全市场 AH 溢价快照 (每对一行, 用两侧各自的最新收盘价)。

    Args:
        repo: ``Repository``, 只用它的 ``store.data_dir`` (读维表与日K分区)。
        client: 复用已有的东财客户端; ``None`` 时自建并在结束时关闭。
        sina_client: 复用已有的新浪客户端 (取汇率); ``None`` 时自建并关闭。

    Returns:
        :class:`AhPremiumResult`。``items`` 按 ``premium_pct`` 降序, null 在最后。

    Raises:
        EastmoneyError: AH 配对拉取失败。**不得**降级成空表 —— 那会把「拉不到」
            伪装成「全市场没有 AH 股」(§4)。
        SinaError: 汇率拉取失败或自检不过 (三角校验偏差 > 1% / 汇率超出
            [0.6, 1.2] 合理区间)。**同样不得降级**, 尤其不得用 1.0 兜底: 那会让
            每一只标的的溢价都错 ~17%, 而页面上看不出来。

    ⚠️ ``premium_pct`` 为 null 有两种原因, 分别计入 ``missing_price_count``
    (A 或 H 无本地收盘价) 与 ``stale_price_count`` (两侧价格日期差超过
    :data:`AH_PREMIUM_MAX_PRICE_LAG_DAYS` 天)。前端必须显示为「—」, 不可显示 0。
    """
    own_em = client is None
    own_sina = sina_client is None
    if own_em:
        client = EastmoneyClient()
    if own_sina:
        sina_client = SinaClient()
    try:
        candidates = fetch_ah_pair_candidates(client)
        fx = fetch_hkd_cny(sina_client)
    finally:
        if own_em:
            client.close()
        if own_sina:
            sina_client.close()

    data_dir = repo.store.data_dir
    hk_inst = _read_hk_instruments(data_dir)
    hk_listing_dates: dict[str, date | None] = {}
    hk_names: dict[str, str | None] = {}
    for row in hk_inst.iter_rows(named=True):
        hk_listing_dates[row["symbol"]] = row["listing_date"]
        hk_names[row["symbol"]] = row["name"]

    pairs, ambiguous = _resolve_pairs(candidates, hk_listing_dates, hk_names)
    if not pairs:
        return AhPremiumResult(
            items=_empty_items(),
            fx_rate=fx.rate,
            fx_as_of=fx.as_of,
            fx_source=fx.source or FX_SOURCE,
            currency=AH_PREMIUM_CURRENCY,
            candidate_count=len(candidates),
            ambiguous_a_symbols=tuple(ambiguous),
            missing_price_count=0,
            stale_price_count=0,
            price_lag_count=0,
        )

    a_symbols = [p[0] for p in pairs]
    h_symbols = [p[2] for p in pairs]
    a_closes = _latest_closes(
        data_dir, daily_dirname("stock", market=Market.CN), a_symbols
    ).rename({"close": "a_close", "price_date": "a_price_date"})
    h_closes = _latest_closes(
        data_dir, daily_dirname("stock", market=Market.HK), h_symbols
    ).rename({"close": "h_close", "price_date": "h_price_date", "symbol": "h_symbol"})

    df = pl.DataFrame(
        {
            "a_symbol": [p[0] for p in pairs],
            "a_name": [p[1] for p in pairs],
            "h_symbol": [p[2] for p in pairs],
            # H 名称优先用本地维表 (与港股面板其余页面显示一致), 缺失时退回上游
            # STR_NAMEH; A 名称直接用上游 SECURITY_NAME_ABBR (就是交易所简称,
            # 含 ST/退 标记, 与本地维表同源同值, 不值得为它多读一张 5893 行的表)。
            "h_name": [hk_names.get(p[2]) or p[3] for p in pairs],
        },
        schema={
            "a_symbol": pl.String,
            "a_name": pl.String,
            "h_symbol": pl.String,
            "h_name": pl.String,
        },
    )
    df = df.join(a_closes.rename({"symbol": "a_symbol"}), on="a_symbol", how="left")
    df = df.join(h_closes, on="h_symbol", how="left")

    # 两侧价格日期差 (日历日)。任一侧缺价时为 null, 后续按缺价处理。
    df = df.with_columns(
        (pl.col("a_price_date") - pl.col("h_price_date"))
        .dt.total_days()
        .abs()
        .alias("_lag_days")
    )
    has_both_prices = (
        pl.col("a_close").is_not_null()
        & pl.col("a_close").is_finite()
        & (pl.col("a_close") > 0)
        & pl.col("h_close").is_not_null()
        & pl.col("h_close").is_finite()
        & (pl.col("h_close") > 0)
    )
    within_lag = pl.col("_lag_days").is_not_null() & (
        pl.col("_lag_days") <= AH_PREMIUM_MAX_PRICE_LAG_DAYS
    )

    # 🔴 溢价 = (A收盘 ÷ (H收盘 × 港币汇率) − 1) × 100, 百分比数值。
    # 分母非正/缺价/日期差过大时留 null: 0 会被读成「A 与 H 等价」, 而真相是
    # 「算不出来」。汇率 fx.rate 已由 fetch_hkd_cny 自检过区间, 不再重复判 0。
    df = df.with_columns(
        pl.when(has_both_prices & within_lag)
        .then((pl.col("a_close") / (pl.col("h_close") * fx.rate) - 1.0) * 100.0)
        .otherwise(None)
        .cast(pl.Float64)
        .alias("premium_pct")
    )

    missing_mask = ~has_both_prices
    stale_mask = has_both_prices & ~within_lag
    missing_price_count = int(df.select(missing_mask.sum()).item() or 0)
    stale_price_count = int(df.select(stale_mask.sum()).item() or 0)
    price_lag_count = int(
        df.select((has_both_prices & within_lag & (pl.col("_lag_days") > 0)).sum()).item() or 0
    )
    if stale_price_count:
        logger.warning(
            "AH 溢价: %d 对的 A/H 收盘价日期差超过 %d 天, 已置空 (fail closed)",
            stale_price_count,
            AH_PREMIUM_MAX_PRICE_LAG_DAYS,
        )

    items = (
        df.select(list(AH_PREMIUM_COLS))
        # null 排最后: 降序时 polars 默认把 null 当最小, 但显式声明更稳
        .sort("premium_pct", descending=True, nulls_last=True)
    )
    return AhPremiumResult(
        items=items,
        fx_rate=fx.rate,
        fx_as_of=fx.as_of,
        fx_source=fx.source or FX_SOURCE,
        currency=AH_PREMIUM_CURRENCY,
        candidate_count=len(candidates),
        ambiguous_a_symbols=tuple(ambiguous),
        missing_price_count=missing_price_count,
        stale_price_count=stale_price_count,
        price_lag_count=price_lag_count,
    )


__all__ = [
    "AH_PREMIUM_CLOSE_LOOKBACK_PARTITIONS",
    "AH_PREMIUM_COLS",
    "AH_PREMIUM_CURRENCY",
    "AH_PREMIUM_MAX_PRICE_LAG_DAYS",
    "AhPremiumResult",
    "ah_premium",
]
