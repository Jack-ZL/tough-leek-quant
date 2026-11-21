"""港股标的维表同步服务。

写入 ``data/instruments_hk/instruments_hk.parquet`` —— 与 A 股
``instruments/instruments.parquet`` **物理分离** (方案 §4 闸3)。分离的收益是:
A 股那 11 处 ``kline_daily/**/*.parquet`` 式的递归 glob、以及 ``instruments_all``
统一视图, 都不会因为「多了一个 market 分支没改到」而静默混入港股行。

与 :mod:`app.services.instrument_sync` 的关系: 复用其 ``_preserve_instrument_fields``
的逐行 coalesce 逻辑 (港股要补的字段集不同, 通过参数覆写), 其余独立实现。
刻意**不**把港股塞进 ``sync_instruments``: 那个函数是 A 股盘前 9:10 的关键路径,
港股拉取失败绝不能连累它。

数据源 (PR6 换源): 东财 **datacenter-web** 的 F10 概况报表
``RPT_HKF10_INFO_ORGPROFILE`` 出属性, 腾讯 ``qt.gtimg.cn`` 批量快照出「在市」闸门
与最新价。免 Key。

🔴 **不再走 push2 的 clist 列表接口** —— push2 集群对本机 IP 级封禁 (2026-11 实测,
详见 :mod:`app.plugins.eastmoney.hk_profile` 模块 docstring)。换源顺带拿到 clist
没有的**每手股数** (``TRADE_UNIT``), 并让 ``tick_size`` 第一次有真实价格可依据。
``hk.fetch_hk_spot`` / ``hk.hk_instrument_rows`` 那一路**保留不删**: 它是封禁解除后
的等价替代, 且由 ``tests/test_hk_instrument_sync.py`` 继续钉住契约。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.plugins.eastmoney import hk_profile
from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError
from app.plugins.tencent import hk_spot
from app.plugins.tencent.client import TencentClient, TencentError
from app.services.fs_utils import atomic_write_parquet
from app.services.instrument_sync import _preserve_instrument_fields

logger = logging.getLogger(__name__)

#: 维表落盘相对路径 (相对 data_dir)。
HK_INSTRUMENT_RELPATH = "instruments_hk/instruments_hk.parquet"

#: 需要从历史维表逐行补全的字段。
#:
#: 比 A 股的 ``_SHARE_FIELDS`` 多一个 ``industry``: 东财行业字段偶发返回 ``"-"``,
#: 而行业是概念/行业板块功能的主键, 不能被一次抖动掏空 (换源后实测在市 2862 行里
#: 有 7 行无行业, 全是债券)。
#: **不含** ``limit_up``/``limit_down`` —— 港股无涨跌停, 两列恒 None (保留列只为
#: 与 A 股 schema 对齐), 没有「历史值」可言。
#: **含** ``lot_size`` —— 换源后该列由 F10 的 ``TRADE_UNIT`` 直接给出 (实测在市
#: 2862 行仅 1 行为空), 不再依赖财务宽表回填; 但仍列入保留集, 一是兜住那 1 行空值,
#: 二是 :func:`app.services.financial_sync_hk.backfill_hk_lot_size` 的回填结果
#: 不该被一次维表重跑冲掉。
#: **含** ``tick_size`` —— 它依赖腾讯快照的最新价, 闸门那一路若某只标的当轮无价
#: (盘中临时停牌), 保留历史价位比写成 None 更接近事实。
_HK_PRESERVE_FIELDS = (
    "listing_date", "total_shares", "float_shares", "tick_size", "industry", "lot_size",
)

#: 本次拉取行数 / 历史维表行数 低于此比例 → 判定「拉取不完整」而非「大批退市」,
#: 保留历史行。
#:
#: 回归背景 (A 股同款): 某交易所 get_instruments 瞬时 connection reset 时, 旧实现把
#: 残缺结果全量覆盖写盘, 整个交易所的标的从维表消失, 搜索/标的池/名称表随之全瞎 ——
#: 比用旧一天的维表严重得多。港股只有一个交易所, 没有「按交易所分区保留」可用,
#: 因此改用行数比例判据: 港股一年退市数十家 (约 1%), 一夜之间少一半只可能是拉取失败。
_MIN_FRESH_RATIO = 0.5


def fetch_hk_instrument_rows(
    *,
    client: EastmoneyClient | None = None,
    tencent_client: TencentClient | None = None,
) -> list[dict]:
    """拉港股 F10 概况 + 腾讯在市闸门 → ``instruments_hk`` 维表行 (未落库)。

    两个源各司一职, **缺一不可** (实测数据见
    :mod:`app.plugins.eastmoney.hk_profile` 模块 docstring):

    - 东财 datacenter-web ``RPT_HKF10_INFO_ORGPROFILE`` 出属性: 名称 / 上市日 /
      股本 / **每手股数** / **恒生行业**。但它**没有在市状态列**, 全量 6920 行里
      混着债券、待上市新股与约 2200 个退市/长期停牌代码, 单用会把死代码灌进维表。
    - 腾讯 ``qt.gtimg.cn`` 批量快照出「在市」闸门 + 最新价。最新价不是顺带的:
      港股价位表按价格分档 (:func:`app.plugins.eastmoney.hk.hk_tick_size`),
      而 F10 概况**没有任何价格列**, 没有闸门就同时没有 ``tick_size``。
      实测 5122 个候选代码 → 2862 只在市。

    拉取失败抛 :class:`EastmoneyError` / :class:`TencentError`, **不返回空列表** ——
    空列表和「真的没有标的」在调用方无法区分, 而后者会导致覆盖写盘掏空维表。
    🔴 腾讯那一路开 ``raise_on_partial=True``: 缺一批时「没返回」既可能是退市
    也可能是那批请求失败, 二者不可区分, 默认行为会把最多 300 只在市股票静默
    写成退市。宁可整轮失败保留历史维表。
    """
    own_em = client is None
    own_tx = tencent_client is None
    if own_em:
        client = EastmoneyClient()
    if own_tx:
        tencent_client = TencentClient()
    try:
        profiles = hk_profile.fetch_hk_org_profiles(client)
        # 只把股票代码发给腾讯: 6920 行里 1798 行是债券/待上市新股, 白发配额。
        candidates = hk_profile.hk_profile_symbols(profiles)
        live = hk_spot.fetch_hk_spot(
            tencent_client, candidates, raise_on_partial=True,
        )
    finally:
        if own_em:
            client.close()
        if own_tx:
            tencent_client.close()

    live_prices = {r["symbol"]: r["last_price"] for r in live if r.get("last_price")}
    return hk_profile.hk_profile_instrument_rows(profiles, live_prices=live_prices)


def _carry_over_when_incomplete(
    df: pl.DataFrame, data_dir: Path
) -> tuple[pl.DataFrame, int]:
    """本次拉取疑似不完整时, 用历史维表补齐缺失的 symbol。

    返回 (合并后的 df, 补回的行数)。补回 0 行表示本次拉取完整 (或无历史可用)。
    """
    path = data_dir / HK_INSTRUMENT_RELPATH
    if not path.exists():
        return df, 0
    try:
        existing = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments_hk 历史维表读取失败, 无法防掏空: %s", e)
        return df, 0
    if existing.is_empty() or "symbol" not in existing.columns:
        return df, 0

    ratio = df.height / existing.height if existing.height else 1.0
    if ratio >= _MIN_FRESH_RATIO:
        return df, 0

    missing = existing.filter(~pl.col("symbol").is_in(df["symbol"].to_list()))
    if missing.is_empty():
        return df, 0
    merged = (
        pl.concat([df, missing], how="diagonal_relaxed")
        .unique(subset=["symbol"], keep="first")
    )
    carried = merged.height - df.height
    logger.warning(
        "instruments_hk 本次仅拉到 %d 行 (历史 %d 行, 占比 %.0f%% < %.0f%%), "
        "判定为拉取不完整而非大批退市, 从历史维表补回 %d 行 (总 %d 行)",
        df.height, existing.height, ratio * 100, _MIN_FRESH_RATIO * 100,
        carried, merged.height,
    )
    return merged, carried


def sync_instruments_hk(data_dir: Path) -> int:
    """全量同步港股标的维表 → ``data/instruments_hk/instruments_hk.parquet``。

    返回写入的行数; 拉取失败返回 **0 且不写盘** (保留上一次的维表)。
    两个源任一失败都算失败 —— 缺在市闸门时写盘等于把死代码当在市标的落库。
    """
    try:
        rows = fetch_hk_instrument_rows()
    except EastmoneyError as e:
        logger.warning("港股标的维表拉取失败 (东财 F10 概况), 保留历史维表不覆盖: %s", e)
        return 0
    except TencentError as e:
        logger.warning("港股标的维表拉取失败 (腾讯在市闸门), 保留历史维表不覆盖: %s", e)
        return 0

    if not rows:
        logger.warning("港股标的维表返回 0 行, 保留历史维表不覆盖")
        return 0

    # 🔴 必须 infer_schema_length=None (全行推断), 不能用 polars 默认的前 100 行。
    # 维表按代码升序, 前 100 行全是 00xxx 港币柜台 ⇒ hkd_counter 整段为 None ⇒
    # 被推成 Null dtype, 翻到 8xxxx 人民币柜台时抛
    # "could not append value: '00016.HK' of type: str"。
    # 实测 2862 行时必现 (脱网单测行数少, 测不出来)。同 ext_data.py:1070 的惯例。
    # limit_up/limit_down 恒为 None → 全行推断后仍是 Null dtype, 与 A 股
    # instruments.parquet 现状一致 (港股无涨跌停), 不是回归。
    df = pl.DataFrame(rows, infer_schema_length=None)
    df = _preserve_instrument_fields(
        df,
        data_dir,
        relpath=HK_INSTRUMENT_RELPATH,
        preserve_fields=_HK_PRESERVE_FIELDS,
        label="instruments_hk",
    )
    # as_of 用**中国大陆日期**: 港股与 A 股同为 UTC+8, 且本仓所有 as_of 语义都是
    # 「同步动作发生日」而非「标的所属市场的交易日」, 保持一致以免读端分叉。
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    fresh_height = df.height
    df, carried = _carry_over_when_incomplete(df, data_dir)
    if carried:
        logger.info(
            "instruments_hk 本次新鲜行 %d, 补回历史行 %d, 落库 %d",
            fresh_height, carried, df.height,
        )

    out = data_dir / HK_INSTRUMENT_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    cny = int(df.get_column("is_cny_counter").sum()) if "is_cny_counter" in df.columns else 0
    logger.info(
        "instruments_hk synced: %d rows (含人民币柜台 %d) → %s",
        df.height, cny, out,
    )
    return df.height
