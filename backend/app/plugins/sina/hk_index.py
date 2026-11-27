"""港股指数 — 新浪财经数据边界 (PR3, 方案 §11)。

**只服务港股指数**; A 股指数链路不经此模块 (方案 §4 闸1: 独立港股模块)。
限速/重试复用 :class:`app.plugins.sina.client.SinaClient` —— 新浪对行情快照端点
有明确的 IP 封禁策略 (akshare 同端点 docstring 原话「大量采集会被目标网站服务器
封禁 IP」), **严禁绕过 client 直连**。

真机实测 (2026-11-16 活体验证; 42 个代码合并成**单次**请求, 全部返回 19 字段)::

    GET https://hq.sinajs.cn/rn=mtf2t&list=hkHSI,hkHSTECH,...
    Header: Referer: https://vip.stock.finance.sina.com.cn/   ← 必需
    响应: **GBK** 且不带 charset 声明 (httpx 按 UTF-8 解 → 中文名全乱)
    每行: ``var hq_str_hk<CODE>="<19 个逗号分隔字段>";``

⚠️ URL 形态刻意保留 akshare 的 ``/rn=mtf2t&list=...`` (``&`` 在 path 里, 没有 ``?``)。
这是新浪该端点的既有约定, 已实测可用; 改成规范的 ``?list=`` 未经验证, 不要"顺手修正"。

字段序 (与 akshare ``index_hk_spot_sina`` 的列映射逐位吻合, 已实测比对):

===== ========== ==========================================================
idx   含义       备注
===== ========== ==========================================================
0     代码       与请求代码同形 (``HSI``)
1     名称       中文, GBK
2     今开
3     昨收
4     最高
5     最低
6     最新价
7     涨跌额
8     涨跌幅     🔴 **已是百分数** (``-1.545`` 即 -1.545%), 不是小数
9/10  恒 0.00000 占位
11    成交量     🔴 单位口径与腾讯矛盾, 见坑 3
12    成交额     🔴 同上
13/14 恒 0.000   占位
15    52周最高
16    52周最低
17    日期       🔴 ``YYYY/MM/DD`` **斜杠**分隔 (逐字符 ord 实测=47; 短横线格式解析失败)
18    时间       ``HH:MM``
===== ========== ==========================================================

🔴 坑 1 — **该端点混返 A 股指数**。请求 42 个代码, 其中 12 个是纯 A 股指数
(``SSECOMP`` 上证综合 3634.353 / ``CSI300`` 沪深300 4016.290 / ``SSE50`` 2775.023 …)。
不过滤就会把 A 股指数写进 ``kline_hk_index_daily``, 污染港股目录 (方案 §4 闸3:
物理隔离)。过滤靠 :data:`HK_INDEX_WHITELIST`。

🔴 坑 2 — **前缀规则不可用, 只能显式允许清单**。实测反例三个:
``HSCASI`` 以 ``HS`` 开头却是「恒生**A股**软件开发指数」(2171.37);
``CESA80`` 是「中华**A80**」(7225.07); ``CES300`` 是「中华**沪深港**300」(4586.44,
跨市场)。任何 ``startswith(("HS","CES","CSHK"))`` 或黑名单写法都会漏。
允许清单的好处是: 新浪将来新增代码时**默认被丢弃并告警**, 而不是默认被收进港股库。

🔴 坑 3 — **量额单位两源矛盾, 原样透传不折算**。HSI 2026-11-13 同一交易日:
新浪 成交量 ``195857007`` / 成交额 ``11525719783``; 腾讯日K 第 6 列 ``195857007140``
—— 恰好是新浪成交量 ×1000, 且不等于新浪成交额。而新浪自己的 额/量 = 58.84,
与指数点位 23637.6 也对不上。**两源对「这个数是什么单位」各执一词**, 任何
乘除折算都是在编造数据。指数的量额在本仓唯一功能作用是
:func:`app.indicators.pipeline.filter_halt_days` 的停牌判据 (指数无流通股本 →
无换手率; MA/MACD 只用 close), 不参与定量计算, 因此原值透传即可。

⚠️ 但**不可省略**: ``filter_halt_days`` 的判据是 ``(volume==0) & (amount==0)``,
polars 里 ``null == 0`` 求值为 ``null`` → ``~null`` 仍为 ``null`` → 整帧被滤空。
实测 58 行 null-volume 日K 经 ``compute_enriched`` 后剩 **0 行**, 静默丢数据。

🔴 坑 4 — **涨跌幅单位**。本仓指数行情契约 (``quote_service._build_index_quotes``)
的 ``change_pct`` 入参是**小数** (0.0366 = 3.66%), 由该函数统一 ×100 后给前端。
新浪字段 8 已经是百分数, 直接透传会让前端显示 -154.5%。:func:`map_hk_index_quote`
负责 ÷100, 并由测试钉住。

🔴 坑 5 — **指数代码过不了股票符号校验**。``infer_market("HSI")`` 返回
``Market.CN`` (兜底分支), ``is_valid_symbol("HSI", Market.HK)`` 为 ``False``。
``_SYMBOL_PATTERN_BODY`` 是**股票**契约 (HK = ``[0-9]{5}\\.HK``), 被 Python↔DuckDB
双载体一致性测试钉住, **不得为指数放宽**。因此港股指数链路必须**显式传
``market=Market.HK``**, 永远不要走 ``infer_market``。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.plugins.sina.client import HQ_BASE, SinaError

if TYPE_CHECKING:  # pragma: no cover
    from app.plugins.sina.client import SinaClient

logger = logging.getLogger(__name__)

#: 快照主机上的 Referer。实测必需, 与资金流端点的 Referer 不同。
HK_INDEX_REFERER = "https://vip.stock.finance.sina.com.cn/"

#: 响应编码。该端点返回 GBK 且不带 charset 声明。
HK_INDEX_ENCODING = "gbk"

#: 单行字段数。少于该数的行视为残缺, 丢弃并告警 (不猜位)。
HK_INDEX_FIELD_COUNT = 19

_HK_TZ = ZoneInfo("Asia/Hong_Kong")

#: 字段 17 的日期分隔符。真机实测 (2026-11-16) 是**斜杠** ``2026/11/13``
#: (逐字符 ord 验证: 分隔符 ord=47, ``%Y/%m/%d`` 解析成功、``%Y-%m-%d`` 失败)。
#: 同时兼容短横线 —— 该端点无文档, 分隔符随口径变过的风险不值得赌。
_DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d")

# ---------------------------------------------------------------------------
# 白名单 (2026-11-16 单次请求实测 42 个代码逐一分类, 证据见模块 docstring 坑 1/2)
# ---------------------------------------------------------------------------

#: 真港股指数 —— **唯一**允许写入港股指数目录的代码集合。
#:
#: 刻意用显式允许清单而非前缀规则/黑名单: 实测 ``HSCASI``/``CESA80``/``CES300``
#: 三个反例证明前缀会说谎 (坑 2)。名称不在此登记 —— 名称由响应字段 1 在运行时
#: 提供, 抄一份到这里只会与源分叉。
HK_INDEX_WHITELIST: frozenset[str] = frozenset(
    {
        # 恒生系列 (11)
        "HSI",  # 恒生指数
        "HSCEI",  # 恒生中国企业指数
        "HSTECH",  # 恒生科技指数
        "HSCCI",  # 恒生香港中资企业指数
        "VHSI",  # 恒生波动率指数
        "HSMBI",
        "HSMOGI",
        "HSMPI",
        "HSSCID",
        "HSSCPB",
        "HSIDI",
        # 中证香港系列 (9)
        "CSHK100",
        "CSHKDIV",
        "CSHKLC",
        "CSHKLRE",
        "CSHKMCS",
        "CSHKME",
        "CSHKPE",
        "CSHKSE",
        "CSRHK50",
        # 中华系列 (5)
        "CES100",
        "CES120",
        "CES280",
        "CESG10",  # 中华博彩业
        "CESHKM",
        # 标普香港 (2)
        "GEM",  # 标普香港创业板
        "HKL",  # 标普香港大型股
    }
)

#: 该端点混返的**纯 A 股**指数 (实测 12 个) —— 必须丢弃, 否则污染港股目录。
#: 留作常量而非注释: 测试要断言「这些代码即使被源返回也不入库」。
HK_INDEX_A_SHARE_CONTAMINANTS: frozenset[str] = frozenset(
    {
        "CSCMC",  # 中证内地消费 8721.892
        "CSI300",  # 沪深300 4016.290
        "SSE180",
        "SSE180GV",
        "SSE380",
        "SSE50",
        "SSECEQT",
        "SSECOMP",  # 上证综合 3634.353
        "SSEDIV",
        "SSEITOP",
        "SSEMCAP",
        "SSEMEGA",
    }
)

#: 前缀像港股、实为 A 股或跨市场的代码 (实测 3 个) —— 同样丢弃 (fail-closed)。
#: 单独列出是因为它们最能说明「前缀规则不可用」, 是坑 2 的原始证据。
HK_INDEX_EXCLUDED_CROSS_MARKET: dict[str, str] = {
    "HSCASI": "恒生A股软件开发指数 (2171.37) — HS 前缀但成分是 A 股",
    "CESA80": "中华A80 (7225.07) — CES 前缀但成分是 A 股",
    "CES300": "中华沪深港300 (4586.44) — 跨市场, 不专属港股",
}

assert not (HK_INDEX_WHITELIST & HK_INDEX_A_SHARE_CONTAMINANTS), "白名单与 A 股污染集重叠"
assert not (HK_INDEX_WHITELIST & set(HK_INDEX_EXCLUDED_CROSS_MARKET)), "白名单与跨市场排除集重叠"

# ---------------------------------------------------------------------------
# 边际代码实测排除依据 (2026-11-27 直连新浪/腾讯复核, 防后人重复调研)
# ---------------------------------------------------------------------------
# 源里还有白名单外的港股指数代码, 但逐一实测均有数据质量缺陷, 刻意不收:
#   HSCI  (恒生综合): 新浪快照当前有效, 但腾讯日K **陈旧停更于 2011-01-21**
#           (2026-11 区间 0 行; count=400 返回 300 行全止于 2011)。三态逻辑只判
#           空帧不判陈旧 → 收它会写入 2011 数据冒充当前图表, 比无覆盖更误导。
#   HSMI  (恒生中国内地综合): 新浪快照有效但 name 字段返回英文 "HSMI"、amount=0;
#           腾讯日K 无覆盖 (day=[])。仅快照、无图表、英文名, 边际价值低。
#   CESHKB: 新浪响应 code/name 字段**全空** (有价格但无名) → 落库即无名指数。
# HSCI/HSMI/HSCASI 走新浪另一套降级馈送 (name=英文代码、volume 带小数、amount=0、
# 无 52 周高低), 与白名单 27 只的正规馈送不同。结论: 27 只是经筛选的干净集,
# 「扩充」非真缺口 —— 多出的代码恰因质量差被排除 (fail-closed 设计正确性佐证)。

#: 请求代码的 URL 前缀 (港股指数在新浪是 ``hk`` + 大写代码)。
HK_INDEX_CODE_PREFIX = "hk"

_LINE_RE = re.compile(r'var hq_str_hk(?P<code>[A-Z0-9]+)="(?P<body>[^"]*)";')


def hk_index_list_param(codes: Iterable[str]) -> str:
    """把代码序列拼成 ``list=`` 参数值 (``hkHSI,hkHSTECH``)。

    刻意**一次请求打包全部代码**: 新浪按请求频次封 IP, 42 个代码分 42 次请求
    与合并 1 次的封禁风险不是一个量级。
    """
    return ",".join(f"{HK_INDEX_CODE_PREFIX}{c}" for c in codes)


def parse_hk_index_snapshot(text: str) -> list[tuple[str, list[str]]]:
    """解析快照响应 → ``[(代码, 19 字段列表), ...]``, 保持响应顺序。

    字段数不足 :data:`HK_INDEX_FIELD_COUNT` 的行**丢弃并告警**, 不补位不猜测
    (方案 §4: fail closed, 不静默切到错误语义)。
    """
    out: list[tuple[str, list[str]]] = []
    for m in _LINE_RE.finditer(text or ""):
        code = m.group("code")
        body = m.group("body")
        if not body:
            # 空 body = 该代码新浪无数据 (已退市/代码不存在), 不是错误
            logger.debug("港股指数快照 %s 返回空 body, 跳过", code)
            continue
        fields = body.split(",")
        if len(fields) < HK_INDEX_FIELD_COUNT:
            logger.warning(
                "港股指数快照 %s 字段数 %d < %d, 丢弃该行 (源格式可能已变)",
                code,
                len(fields),
                HK_INDEX_FIELD_COUNT,
            )
            continue
        out.append((code, fields))
    return out


def _to_float(value: str | None) -> float | None:
    """空串/``-``/非数字 → None, 不伪造 0 (与 ``eastmoney.hk._to_float`` 同义)。

    刻意不跨插件 import: 本仓插件惯例是各自持有一份 (见 ``baostock/provider.py``、
    ``tdxquant/provider.py``), 跨插件引用会让一个源的改动波及另一个源。
    """
    if value is None:
        return None
    value = value.strip()
    if not value or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_timestamp_ms(day: str | None, clock: str | None) -> int | None:
    """``("2026/11/13", "16:08")`` → 毫秒时间戳 (按 **Asia/Hong_Kong** 解释)。

    时区必须是港股而非上海: 两地虽同为 UTC+8, 但港股半日市 (圣诞/新年/农历除夕
    只交易上午) 的收盘戳是 12:00 HKT, 用错时区会让「行情新鲜度」判断偏移。
    解析失败返回 None, 不兜底成 ``now()`` —— 兜底会把过期数据伪装成实时数据。

    ⚠️ 静默返回 None 的代价实测过: 27 只港股指数**全部** ``timestamp=None``,
    前端新鲜度判断直接失效。日期分隔符因此按 :data:`_DATE_FORMATS` 逐个试,
    不写死单一格式。
    """
    if not day or not clock:
        return None
    day, clock = day.strip(), clock.strip()
    for date_fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(f"{day} {clock}", f"{date_fmt} %H:%M")
        except ValueError:
            continue
        return int(dt.replace(tzinfo=_HK_TZ).timestamp() * 1000)
    logger.warning("港股指数快照日期无法解析: day=%r clock=%r", day, clock)
    return None


def map_hk_index_quote(code: str, fields: list[str]) -> dict | None:
    """19 字段 → 本仓指数行情记录 (与 ``quote_service._build_index_quotes`` 契约对齐)。

    产出键: ``symbol/name/last_price/prev_close/open/high/low/volume/amount/``
    ``change_pct/change_amount/amplitude/timestamp``。

    🔴 ``change_pct`` **除以 100**: 新浪字段 8 已是百分数, 而本仓契约要求小数
    (见模块 docstring 坑 4)。``amplitude`` 同理产出小数。

    最新价缺失 (``None``) 时返回 ``None`` —— 没有价格的行情行没有意义,
    且下游 ``change_pct`` 补算会因除零/空值产生脏数据。
    """
    last_price = _to_float(fields[6])
    if last_price is None:
        logger.debug("港股指数 %s 最新价缺失, 丢弃该行", code)
        return None

    prev_close = _to_float(fields[3])
    high = _to_float(fields[4])
    low = _to_float(fields[5])

    # 涨跌幅: 源给百分数 → 契约要小数
    change_pct_raw = _to_float(fields[8])
    change_pct = None if change_pct_raw is None else change_pct_raw / 100.0

    # 振幅: (最高-最低)/昨收, 小数口径。昨收缺失或为 0 → None (不产出 inf)
    amplitude = None
    if high is not None and low is not None and prev_close:
        amplitude = (high - low) / prev_close

    return {
        "symbol": code,
        "name": (fields[1] or "").strip() or None,
        "last_price": last_price,
        "prev_close": prev_close,
        "open": _to_float(fields[2]),
        "high": high,
        "low": low,
        # 量额原样透传, 单位口径矛盾见模块 docstring 坑 3 —— 严禁在此乘除折算
        "volume": _to_float(fields[11]),
        "amount": _to_float(fields[12]),
        "change_pct": change_pct,
        "change_amount": _to_float(fields[7]),
        "amplitude": amplitude,
        "timestamp": _to_timestamp_ms(fields[17], fields[18]),
    }


def fetch_hk_index_spot(
    client: SinaClient,
    codes: Iterable[str] | None = None,
) -> list[dict]:
    """拉取港股指数实时快照 → 已归一化、已过滤白名单的行情记录列表。

    codes: 要请求的代码; 默认 :data:`HK_INDEX_WHITELIST` 全集 (排序后请求,
        保证 URL 稳定 → 便于测试与缓存键复现)。
    返回: ``list[dict]``, 键见 :func:`map_hk_index_quote`。**只含白名单内代码**。
    抛出: :class:`SinaError` —— 传输/HTTP/重试耗尽, 以及**响应为空**。

    ⚠️ 空响应**必须抛错**而不是返回 ``[]``: 上游靠 ``None``/异常 与
    「成功但确实没有数据」区分 (CONTRIBUTING 记录的 provider 契约)。
    返回 ``[]`` 会让上游误判为「港股指数今天没有行情」并写进空态缓存。
    与 ``eastmoney.hk.fetch_hk_spot`` 同款处理。
    """
    wanted = sorted(codes) if codes is not None else sorted(HK_INDEX_WHITELIST)
    if not wanted:
        raise SinaError("港股指数快照: 请求代码列表为空")

    unknown = [c for c in wanted if c not in HK_INDEX_WHITELIST]
    if unknown:
        # 调用方传了白名单外的代码 → 编程错误, 不是数据问题。fail closed。
        raise SinaError(f"港股指数快照: 代码不在白名单内 {unknown[:5]} (共 {len(unknown)} 个)")

    text = client._get_text(
        HQ_BASE,
        f"/rn=mtf2t&list={hk_index_list_param(wanted)}",
        None,
        encoding=HK_INDEX_ENCODING,
        referer=HK_INDEX_REFERER,
    )
    parsed = parse_hk_index_snapshot(text)
    if not parsed:
        raise SinaError(f"港股指数快照返回空 (请求 {len(wanted)} 个代码, 解析出 0 行)")

    out: list[dict] = []
    dropped: list[str] = []
    for code, fields in parsed:
        if code not in HK_INDEX_WHITELIST:
            # 坑 1: 该端点混返 A 股指数。已知污染集静默丢弃, 未知新代码告警 ——
            # 后者意味着源改了口径, 需要人工确认是港股还是 A 股再决定收不收。
            if code not in HK_INDEX_A_SHARE_CONTAMINANTS and code not in HK_INDEX_EXCLUDED_CROSS_MARKET:
                logger.warning("港股指数快照出现白名单外的新代码 %s, 已丢弃 (需人工确认归属)", code)
            dropped.append(code)
            continue
        row = map_hk_index_quote(code, fields)
        if row is not None:
            out.append(row)

    if dropped:
        logger.debug("港股指数快照丢弃 %d 个非港股代码: %s", len(dropped), ",".join(dropped[:8]))
    if not out:
        raise SinaError(f"港股指数快照过滤后为空 (原始 {len(parsed)} 行, 丢弃 {len(dropped)} 行)")
    return out
