"""东方财富港股 F10 公司概况 — 标的维表属性源 (PR6 维表换源)。

**为什么单独一路而不复用 :mod:`app.plugins.eastmoney.hk`**: 后者走 push2 集群的
``clist`` 列表接口, 而 **push2 对本机是 IP 级封禁** (2026-11 实测: DNS/TCP/TLS 1.3
全部握手成功、证书有效、ALPN 协商完成, 随后服务端不发任何 HTTP 响应直接关闭连接;
换浏览器 UA + Referer 无效)。封禁是**按集群**的, ``datacenter-web`` 与 ``qt.gtimg.cn``
不受影响 —— 与 ``plugins/tencent/hk_spot.py`` 因同一原因改走腾讯是同一个决策。

本模块因此承担 clist 原本的两项职责, 并且**多拿到两项 clist 拿不到的字段**:

======================  ==========================================================
字段                    来源
======================  ==========================================================
恒生行业 (扁平)          ``BELONG_INDUSTRY`` (clist 是 ``f100``)
**每手股数**             ``TRADE_UNIT`` ⭐ clist **没有** (``f292`` 已实测排除),
                        原先只能靠财务宽表 ``PER_SHARES`` 回填
**行业四级层级**         ``INDUSTRY_TYPE`` ⭐ 形如
                        ``通讯服务-媒体与娱乐-互动媒体与服务-互动媒体与服务``,
                        比 clist 的扁平行业更细 (本模块暂不落库, 见下)
上市日期                ``LISTING_DATE`` (格式与 clist 的 ``f26`` **不同**)
股本                    ``HK_SHARES``
======================  ==========================================================

🔴 **该报表不是「在市股票清单」, 直接当维表会灌进约 2200 个死代码。**
2026-11-21 全量实测 6920 行, 成分是:

- 5 位数字 ``0``/``8`` 开头 5122 行 —— 含**已退市/长期停牌**公司 (F10 概况永久保留,
  报表**没有在市状态列**), 也含 REITs (``00405 越秀房产信托基金``) 与 5 位代码的
  债券 (``01485 TENNESSEE B2025``);
- ``4xxxx`` 5 位数字 619 行 —— 债务证券 (``40001 CCB N2611C``);
- 字母开头 1151 行 —— 债券 (``ABOCFB11008``, 11 位) 与**待上市新股**
  (``H2001 Aqara`` / ``H2002 XREAL``, 递表未挂牌, 却已带行业分类)。

所以 :func:`hk_profile_instrument_rows` 需要调用方传入 ``live_prices``
(腾讯批量快照的在市集合) 做闸门: 5122 → **2862**, 与 clist ``m:128+t:3,t:4``
文档记载的主板 2621 + GEM 310 = 2931 同量级 (差额是文档写成时至今的退市)。
腾讯归一化顺带挡掉了 REITs (``map_hk_spot_fields`` 丢弃 ``[63] != 'GP'`` 与
最新价为 0 的行), 这是**载荷行为**, 换闸门源时必须重新验证。

闸门后的数据质量 (2862 行实测): ``LISTING_DATE`` 空 **0** / ``TRADE_UNIT`` 空 **1** /
``HK_SHARES`` 空 **7** / ``BELONG_INDUSTRY`` 空 **7** (``04286 政府基债二七一二``
与 6 只 ``-T`` 后缀债券, 占 0.24%) / 不同行业 **31** 个 (= 恒生行业类目数)。

⚠️ ``INDUSTRY_TYPE`` 的四级层级**刻意不落库**: ``HK_INSTRUMENT_COLUMNS`` 是与
A 股 ``instruments`` 前 12 列同名同义同序的固定契约 (方案 §4 闸3), 加列会牵动所有
读维表的地方。层级留给 PR6 的「L2 下钻」单独评估 —— 它可能让
``ROTATION_SUPPORT.HK.industryLevels`` 从 ``false`` 升为 ``true``, 但那是前端契约
变更, 不该夹带在一次换源里。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date

from app.market import Market

from . import client as em_client
from .client import EastmoneyError

# 同包内共享的港股纯函数/常量。带下划线是模块私有约定, 但「东财缺值占位 '-'」
# 与「股票代码首位 0/8」这两条语义**必须**与 clist 那一路完全一致 —— 复制一份
# 会让两路在将来某次改动后静默分叉, 那比跨模块引用私有名危险得多。
from .hk import (
    _HK_EQUITY_CODE_PREFIXES,
    _clean_text,
    _to_float,
    hk_symbol,
    hk_tick_size,
    hkd_counter_of,
    is_cny_counter,
)

logger = logging.getLogger(__name__)

#: 港股 F10 公司概况报表名。实测 35 列, 全市场 6920 行 / 14 页 (pageSize 500)。
RPT_HK_ORG_PROFILE = "RPT_HKF10_INFO_ORGPROFILE"

#: 全量翻页的页大小。实测 500 可用 (6920 行 → 14 页); 再大未验证, 不冒进。
_PROFILE_PAGE_SIZE = 500


def parse_hk_profile_list_date(value) -> date | None:
    """F10 ``LISTING_DATE`` → :class:`datetime.date`。

    🔴 与 :func:`app.plugins.eastmoney.hk.parse_hk_list_date` **格式不同, 不可互换**:
    clist 的 ``f26`` 是 ``YYYYMMDD`` **整数** (``20230619``), 本报表是
    ``'2004-06-16 00:00:00'`` **带时间的字符串** (与 A 股 datacenter 同格式)。
    把字符串喂给 ``parse_hk_list_date`` 会因 ``_to_float`` 失败而**静默**返回
    ``None``, 整张维表的上市日期全空却不报错 —— 所以单独一个函数并由测试钉住。

    反方向是安全的: ``date.fromisoformat`` 自 py3.11 起也接受 ``YYYYMMDD`` 基本
    格式, 故本函数两种写法都吃。这是有意的宽容, 上游改回整数不会整列变空。

    缺值 (``None`` / ``''`` / ``'-'``) 与非法值一律返回 ``None``, 不伪造日期。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "-":
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        logger.warning("港股 F10 上市日期无法解析: %r", value)
        return None


def fetch_hk_org_profiles(
    client: em_client.EastmoneyClient | None = None,
) -> list[dict]:
    """分页拉取**全市场**港股 F10 公司概况原始行。

    返回原始 dict 列表 (35 列, 键见模块 docstring), 映射交给
    :func:`hk_profile_instrument_rows` —— 与 ``hk.fetch_hk_spot`` /
    ``hk.hk_instrument_rows`` 同样的「触网 / 纯函数」二分, 让映射可脱网单测。

    抛出: :class:`EastmoneyError` —— 传输/HTTP/重试耗尽/熔断, 或报表名不存在。
    **不返回 ``[]`` 表示失败**: 空列表与「港股真的没有标的」不可区分, 而下游
    会据此清空维表 (契约同 ``hk.fetch_hk_spot``, 见 ``instrument_sync_hk``)。

    ⚠️ 不加 ``filter`` 才是全市场口径 (方案 §6: 必须全市场, 不能用港股通子集 ——
    QuantMind 早期用 141 只子集后来返工)。
    """
    c = client or em_client.EastmoneyClient()
    rows = c.datacenter_report_all(
        RPT_HK_ORG_PROFILE,
        page_size=_PROFILE_PAGE_SIZE,
        sort_columns="SECURITY_CODE",
        sort_types=1,
    )
    if not rows:
        # datacenter_report_all 对合法空 (9201) 返回 []; 全市场概况为空只可能是
        # 上游语义变了。抛错而不是返回 [], 让同步保留历史维表 (fail closed)。
        raise EastmoneyError(f"港股 F10 概况 {RPT_HK_ORG_PROFILE} 返回 0 行, 疑似上游语义变更")
    logger.info("港股 F10 概况拉取完成: %d 行", len(rows))
    return rows


def _is_hk_equity_code(code: str) -> bool:
    """是否港股**股票**代码 (5 位数字, 首位 ``0`` 主板 / ``8`` 人民币柜台)。

    与 :func:`app.plugins.eastmoney.hk.partition_hk_derivatives` 同一判据。
    本报表里被它挡掉的是债务证券 (``4xxxx`` / 字母开头) 与待上市新股 (``H2xxx``)。
    """
    return len(code) == 5 and code.isdigit() and code.startswith(_HK_EQUITY_CODE_PREFIXES)


def hk_profile_symbols(rows: list[dict]) -> list[str]:
    """F10 概况行 → 候选内部 symbol 列表 (只含 5 位 ``0``/``8`` 开头的股票代码)。

    给在市闸门用: 全量 6920 行里有 1798 行是债券/待上市新股, 把它们也发给腾讯
    只是白占批量配额 (300 只/请求)。过滤判据与 :func:`hk_profile_instrument_rows`
    内部完全同一个 :func:`_is_hk_equity_code`, 两处不会分叉。
    """
    out: list[str] = []
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not _is_hk_equity_code(code):
            continue
        symbol = hk_symbol(code)
        if symbol is not None:
            out.append(symbol)
    return out


def hk_profile_instrument_rows(
    rows: list[dict],
    *,
    live_prices: Mapping[str, float] | None = None,
) -> list[dict]:
    """F10 概况行 → ``instruments_hk`` 维表行 (契约同 ``hk.hk_instrument_rows``)。

    输出键**逐键等于** :data:`app.plugins.eastmoney.hk.HK_INSTRUMENT_COLUMNS` 的 16 列,
    由测试钉住 —— 换源的前提是下游读维表的代码一行都不用改。

    ``live_prices``: 内部 symbol → 最新价, 来自腾讯批量快照
    (:func:`app.plugins.tencent.hk_spot.fetch_hk_spot`)。**一个参数担两件事**:

    1. **在市闸门** —— 不在其中的 symbol 整行丢弃。这是必需的, 不是优化:
       本报表含约 2200 个已退市/停牌/非股票代码 (见模块 docstring), 而它
       **没有在市状态列**, 单靠本报表无法过滤。
    2. **``tick_size`` 的依据** —— 港股价位表按价格分档
       (:func:`hk.hk_tick_size`), 而本报表**没有任何价格列**。

    传 ``None`` 表示不做在市过滤且 ``tick_size`` 留 ``None`` (仅供脱网单测映射逻辑;
    生产链路必须传, 否则维表会含死代码)。

    🔴 ``total_shares`` / ``float_shares`` 取 ``HK_SHARES``, 它是**港股 (H股) 股本,
    不是集团总股本**。实测 ``02318 中国平安`` HK_SHARES=7,447,576,912 (= H股 74.5亿),
    而其集团总股本约 182亿。报表里看似能当总股本的 ``REG_CAPITAL`` **不可用**:
    它是带币种后缀的字符串 (``"18107641995 CNY"``), 且对 ``00700 腾讯`` 失真为
    ``"1000000 HKD"`` (注册资本名义值, 与 91.9 亿股本差四个数量级)。
    ⇒ A+H 公司的集团总股本本表拿不到, 需要时另接源, **不得**用 REG_CAPITAL 顶替。

    ``lot_size`` 取 ``TRADE_UNIT``, 已逐只核对港交所真实每手股数:
    ``00005 汇丰`` 400 / ``00700 腾讯`` 100 / ``02318 平安`` 500 / ``03988 中行`` 1000。
    ⭐ 这是 clist 那一路**拿不到**的字段 (``f292`` 已实测排除), 原先只能靠
    ``financial_sync_hk.backfill_hk_lot_size`` 从财务宽表 ``PER_SHARES`` 回填。
    """
    clean: list[dict] = []
    rejected: list[tuple] = []
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if _is_hk_equity_code(code):
            clean.append(row)
        else:
            rejected.append((code, row.get("SECURITY_NAME_ABBR")))
    if rejected:
        logger.warning(
            "港股 F10 概况剔除 %d 行非股票代码 (债务证券/待上市新股等): %s",
            len(rejected),
            rejected[:5],
        )

    codes = {str(r.get("SECURITY_CODE") or "").strip() for r in clean}
    out: list[dict] = []
    dropped_dead = 0
    no_industry: list[tuple] = []
    for row in clean:
        code = str(row.get("SECURITY_CODE") or "").strip()
        # 走 hk_symbol 归一化而不是直接信 SECUCODE: 两条路径必须产出同一个 symbol,
        # 否则维表主键会与 K 线/财务表分叉 (SECUCODE 实测同为 '00700.HK', 但不赌)。
        symbol = hk_symbol(code)
        if symbol is None:
            continue

        last = None
        if live_prices is not None:
            last = live_prices.get(symbol)
            if last is None:
                dropped_dead += 1
                continue

        name = _clean_text(row.get("SECURITY_NAME_ABBR"))
        industry = _clean_text(row.get("BELONG_INDUSTRY"))
        if industry is None:
            no_industry.append((code, name))

        # 双柜台三信号判定, 与 hk.hk_instrument_rows 完全同口径 (见其 docstring):
        # 代码 8 开头 + 名称 R 结尾 + 配对港币代码确实在本次列表里。
        hkd = hkd_counter_of(code)
        by_code = is_cny_counter(code, name)
        by_pair = hkd is not None and hkd in codes
        if by_code != by_pair:
            logger.warning(
                "港股双柜台判定信号不一致 %s %r: 代码/名称=%s 配对存在=%s (港币柜台 %s)",
                code, name, by_code, by_pair, hkd,
            )

        shares = _to_float(row.get("HK_SHARES"))
        lot = _to_float(row.get("TRADE_UNIT"))
        out.append({
            "symbol": symbol,
            "name": name,
            "code": code,
            "exchange": Market.HK.value,
            "region": Market.HK.value,
            "type": "stock",
            "listing_date": parse_hk_profile_list_date(row.get("LISTING_DATE")),
            "total_shares": int(shares) if shares else None,
            # 港股全流通, 流通股本 = 港股股本 (与 clist 那一路 f38==f39 同口径)
            "float_shares": int(shares) if shares else None,
            "tick_size": hk_tick_size(last),
            # 港股无涨跌停 → None (不是 0: 0 会被读成「限价 0 元」)
            "limit_up": None,
            "limit_down": None,
            "lot_size": int(lot) if lot else None,
            "industry": industry,
            "is_cny_counter": by_code,
            "hkd_counter": (hk_symbol(hkd) if by_code and hkd else None),
        })

    if live_prices is not None:
        logger.info(
            "港股 F10 概况: %d 行股票代码 → 在市 %d 行 (闸门剔除 %d 行退市/停牌/非股票)",
            len(clean), len(out), dropped_dead,
        )
    if no_industry:
        # 不剔除, 只告警。实测在市集合里仅 7 行无行业, 全是债券
        # (04286 政府基债二七一二 + 6 只 -T 后缀)。
        # 🔴 刻意**不**按「无行业」过滤: 东财行业字段偶发返回 '-' (clist 的 f100
        # 有此毛病, industry 因此进了 _HK_PRESERVE_FIELDS), 拿它当判据会在某次
        # 上游抽风时把真实股票整行删掉 —— 丢真股票的代价远大于多 7 行债券,
        # 且无行业的行不会落进任何行业分桶, 对行业页/轮动矩阵无害。
        logger.warning(
            "港股 F10 概况: %d 只在市标的无恒生行业 (实测为债券, 保留但需复核): %s",
            len(no_industry),
            no_industry[:10],
        )
    return out
