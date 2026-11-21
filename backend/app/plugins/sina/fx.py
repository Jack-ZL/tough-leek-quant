"""港元↔人民币汇率 — 新浪财经数据边界 (PR7 AH 溢价用)。

**为什么本仓需要一个汇率模块**: 全仓纪律是「换算即伪造」
(:mod:`app.plugins.tencent.hk_kline`、:mod:`app.plugins.tencent.hk_spot`、
:func:`app.services.financial_analyzer` 三处明文**禁止**自行做汇率折算)。AH 溢价
是这条纪律的**唯一合法例外** —— 它本质上就是「同一家公司的 A 价与 H 价之比」,
不折算就无法比较。合法的前提是: 汇率必须**有明确出处、有明确日期、在 UI 上可见**,
且取不到时**失败关闭**而不是悄悄代入 ``1.0``。本模块只负责把这三件事做实。

数据源 (2026-11-22 真机实测)::

    GET https://hq.sinajs.cn/list=fx_shkdcny,fx_susdcny,fx_susdhkd
    Header: Referer: https://finance.sina.com.cn/     ← client 默认值即可, 无需覆盖
    响应: GBK, 每行 ``var hq_str_fx_<pair>="<18 个逗号分隔字段>";``

与 :mod:`app.plugins.sina.hk_index` **同主机同客户端**, 故不新增主机白名单、
不新增限速桶 —— 复用 :class:`~app.plugins.sina.client.SinaClient` 的令牌桶与
熔断 (新浪对 ``hq.sinajs.cn`` 有明确的 IP 封禁策略, **严禁绕过 client 直连**)。

字段序 (18 字段; 三个货币对逐位比对得出, 已用三角关系交叉验证)::

    ===== ========== ====================================================
    idx   含义       备注
    ===== ========== ====================================================
    0     时间       ``HH:MM:SS``
    1     **汇率**   ← 取这一位, 判据见下
    2     买价       与 [1] 差 ~0.01% (``fx_susdcny``: 6.6714 vs 6.6723)
    3     卖价       同上量级
    4     (量/占位)  ``fx_shkdcny`` 恒 0
    5-8   最高/最低等 三个货币对实测与 [1] 同值或极接近
    9     中文名     ``港元兑人民币`` / ``在岸人民币`` / ``美元兑港元即期汇率``
    10    涨跌幅
    11    涨跌额
    12    (占位)
    13    归属声明   ``此行情由新浪财经计算得出``
    14/15 52周高低   ``fx_susdhkd`` 给出 7.8491/7.7815
    16    空
    17    **日期**   🔴 ``YYYY-MM-DD`` **横杠**分隔 (与港股指数字段 17 的斜杠不同!)
    ===== ========== ====================================================

🔴 坑 1 — **字段 1 的语义无文档, 只能交叉验证**。三个货币对里 ``[1]``/``[2]``/``[3]``
彼此只差万分之一, 单看一个响应无法判断哪位是「汇率」。判据是**美元三角关系**:
``USD→CNY ÷ USD→HKD`` 必须约等于 ``HKD→CNY``。2026-11-22 实测
``6.6714 / 7.8389 = 0.851066`` vs ``[1]=0.8519701810``, 相对偏差 **0.11%**
(两者快照时刻不同: 03:00:00 与 06:59:30, 残差属正常)。若误取 ``[10]=-0.0060``
这类字段, 偏差会是几个数量级 —— :func:`fetch_hkd_cny` 因此**每次请求都带上两个
美元对做自检**, 偏差超 :data:`FX_TRIANGLE_MAX_DEVIATION` 即抛错。

⚠️ 这也意味着「只请求 ``fx_shkdcny`` 一个代码」是**退化**的: 省一次带宽, 丢掉
唯一的正确性证据。三个代码合并成**单次**请求 (逗号分隔), 成本与单代码相同。

🔴 坑 2 — **日期分隔符与港股指数模块不同**。``hk_index`` 的字段 17 是斜杠
``2026/11/13``, 本端点是横杠 ``2026-11-21``。同一主机、同一响应格式、不同分隔符。
故 :data:`_DATE_FORMATS` 两种都试, 但**不共用** ``hk_index`` 的常量 —— 跨模块
共享一个「实测如此」的格式假设, 会在其中一个源改口径时同时静默出错。

🔴 坑 3 — **``as_of`` 是源给的日期, 不是 ``today()``**。汇率有中间价/收盘价/
即期价之分, 且周末与节假日不更新。把 ``as_of`` 如实透传, 前端才能显示
「按 2026-11-21 的汇率折算」; 用 ``today()`` 冒充会让周一早上看到的溢价率
挂着一个从未存在过的日期。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import NamedTuple

from app.plugins.sina.client import HQ_BASE, SinaError

logger = logging.getLogger(__name__)

#: 响应编码。``hq.sinajs.cn`` 返回 GBK 且不带 charset 声明 (同港股指数端点)。
FX_ENCODING = "gbk"

#: 单行字段数。少于该数视为残缺, 丢弃并告警 (不猜位)。
FX_FIELD_COUNT = 18

#: 汇率所在字段下标。判据见模块 docstring 坑 1。
FX_IDX_RATE = 1

#: 中文名所在字段下标 (用于日志与自检)。
FX_IDX_NAME = 9

#: 日期所在字段下标。
FX_IDX_DATE = 17

#: 港元→人民币。**1 HKD = rate CNY**, 即港币价乘该值得人民币价。
FX_HKD_CNY = "fx_shkdcny"

#: 美元→人民币 (在岸)。仅用于三角自检, 不对外产出。
FX_USD_CNY = "fx_susdcny"

#: 美元→港元 (即期)。仅用于三角自检, 不对外产出。
FX_USD_HKD = "fx_susdhkd"

#: 三角自检的相对偏差上限。实测正常值 ~0.11% (三个报价快照时刻不同导致的残差);
#: 取 1% 既容得下日内波动, 又能在「取错字段」或「源改格式」时立刻失败。
FX_TRIANGLE_MAX_DEVIATION = 0.01

#: 汇率合理区间。HKD 与 USD 联系汇率制锚定在 ~7.8, CNY/USD 近二十年 6.1~7.4,
#: 故 HKD/CNY 的理论区间约 0.78~0.95。取 [0.6, 1.2] 留足余量 —— 这个闸的作用是
#: 挡住「小数点错位」和「取到涨跌幅字段」这类量级错误, 不是做精确预测。
FX_HKD_CNY_MIN = 0.6
FX_HKD_CNY_MAX = 1.2

#: 字段 17 的日期格式。实测横杠; 同时兼容斜杠 (该端点无文档, 不赌单一格式)。
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")

_LINE_RE = re.compile(r'var hq_str_(?P<code>fx_[a-z]+)="(?P<body>[^"]*)";')


class FxQuote(NamedTuple):
    """一个货币对的解析结果 (原始形态, 未做合理性判断)。"""

    code: str
    """请求代码 (``fx_shkdcny``)。"""

    rate: float
    """字段 1 的数值。"""

    name: str | None
    """字段 9 的中文名; 空串归一为 ``None``。"""

    as_of: date | None
    """字段 17 的日期; 解析失败为 ``None`` (**不**兜底成 ``today()``)。"""


class HkdCnyRate(NamedTuple):
    """经自检与合理性闸后的港元→人民币汇率 (AH 溢价的唯一折算依据)。"""

    rate: float
    """**1 HKD = rate CNY**。港币价 × rate = 人民币价。"""

    as_of: date | None
    """源给出的汇率日期。可能为 ``None`` (源没给), 此时前端必须显示「日期未知」。"""

    source: str
    """出处标识, 恒为 :data:`FX_SOURCE`。写进 API 响应, 让「这个数从哪来」可见。"""

    triangle_deviation: float | None
    """三角自检的相对偏差 (``0.0011`` = 0.11%)。缺任一美元对时为 ``None``。
    暴露出来而不是只当断言用: 偏差持续走高意味着源在漂移, 应当能被发现。"""


#: :attr:`HkdCnyRate.source` 的取值。
FX_SOURCE = "sina_hq_fx"


def fx_list_param(codes: Iterable[str]) -> str:
    """把货币对代码拼成 ``list=`` 参数值 (``fx_shkdcny,fx_susdcny``)。

    刻意**一次请求打包全部代码**: 新浪按请求频次封 IP, 3 个代码分 3 次请求与
    合并 1 次的封禁风险不是一个量级 (与 ``hk_index.hk_index_list_param`` 同理)。
    """
    return ",".join(codes)


def _to_float(value: str | None) -> float | None:
    """空串/``-``/非数字 → ``None``, 不伪造 0。

    刻意不跨插件/跨模块 import: 本仓插件惯例是各自持有一份 (见
    ``baostock/provider.py``、``tdxquant/provider.py``、``sina/hk_index.py``)。
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


def _to_date(value: str | None) -> date | None:
    """字段 17 → :class:`datetime.date`; 解析失败返回 ``None`` 并告警。

    ⚠️ 不兜底成 ``date.today()``: 兜底会把过期汇率伪装成当日汇率, 而 AH 溢价
    对汇率误差是**线性敏感**的 (汇率错 1%, 全部溢价率错 1%)。
    """
    if not value:
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    logger.warning("新浪汇率日期无法解析: %r (源格式可能已变)", value)
    return None


def parse_fx_snapshot(text: str) -> dict[str, FxQuote]:
    """解析快照响应 → ``{代码: FxQuote}``。

    字段数不足 :data:`FX_FIELD_COUNT` 的行**丢弃并告警**, 不补位不猜测 (§4
    fail closed); 汇率字段非数值的行同样丢弃 —— 留着只会让下游拿到 ``None``
    却以为「源有这个货币对」。
    """
    out: dict[str, FxQuote] = {}
    for m in _LINE_RE.finditer(text or ""):
        code = m.group("code")
        body = m.group("body")
        if not body:
            # 空 body = 该货币对新浪无数据, 不是传输错误
            logger.debug("新浪汇率 %s 返回空 body, 跳过", code)
            continue
        fields = body.split(",")
        if len(fields) < FX_FIELD_COUNT:
            logger.warning(
                "新浪汇率 %s 字段数 %d < %d, 丢弃该行 (源格式可能已变)",
                code,
                len(fields),
                FX_FIELD_COUNT,
            )
            continue
        rate = _to_float(fields[FX_IDX_RATE])
        if rate is None:
            logger.warning("新浪汇率 %s 字段 %d 非数值 (%r), 丢弃该行",
                           code, FX_IDX_RATE, fields[FX_IDX_RATE])
            continue
        name = (fields[FX_IDX_NAME] or "").strip() or None
        out[code] = FxQuote(
            code=code,
            rate=rate,
            name=name,
            as_of=_to_date(fields[FX_IDX_DATE]),
        )
    return out


def triangle_deviation(hkd_cny: float, usd_cny: float, usd_hkd: float) -> float | None:
    """美元三角自检: ``|hkd_cny − usd_cny/usd_hkd| / hkd_cny``。

    返回相对偏差 (``0.0011`` = 0.11%); 任一输入非正时返回 ``None`` (算不出,
    不伪造 0)。这是**唯一**能证明「字段 1 真的是汇率」的证据, 见模块 docstring 坑 1。
    """
    if hkd_cny <= 0 or usd_cny <= 0 or usd_hkd <= 0:
        return None
    implied = usd_cny / usd_hkd
    return abs(hkd_cny - implied) / hkd_cny


def fetch_hkd_cny(client, *, strict: bool = True) -> HkdCnyRate:
    """拉取港元→人民币汇率, 带三角自检与合理区间闸。

    Args:
        client: :class:`~app.plugins.sina.client.SinaClient` (复用其限速与重试)。
        strict: ``True`` (默认) 时三角偏差超
            :data:`FX_TRIANGLE_MAX_DEVIATION` 或汇率落在
            [:data:`FX_HKD_CNY_MIN`, :data:`FX_HKD_CNY_MAX`] 之外**一律抛错**。
            ``False`` 只告警不抛 —— 仅供测试构造退化场景, 生产路径不得关闭。

    Returns:
        :class:`HkdCnyRate`。``rate`` 恒为正有限数。

    Raises:
        SinaError: 传输失败 / 响应为空 / ``fx_shkdcny`` 缺失 / 自检不通过。
            **刻意不返回 ``1.0`` 之类的兜底值**: 汇率错 1% 会让全部 AH 溢价率
            错 1%, 而这种错误在页面上完全看不出来 (§4 fail closed)。
    """
    codes = (FX_HKD_CNY, FX_USD_CNY, FX_USD_HKD)
    text = client._get_text(
        HQ_BASE,
        f"/list={fx_list_param(codes)}",
        None,
        encoding=FX_ENCODING,
    )
    quotes = parse_fx_snapshot(text)
    if not quotes:
        raise SinaError("新浪汇率快照返回空 (请求 3 个货币对, 解析出 0 行)")

    target = quotes.get(FX_HKD_CNY)
    if target is None:
        raise SinaError(
            f"新浪汇率快照缺少 {FX_HKD_CNY} (实到 {sorted(quotes)}); 不做兜底折算"
        )

    usd_cny = quotes.get(FX_USD_CNY)
    usd_hkd = quotes.get(FX_USD_HKD)
    deviation = (
        triangle_deviation(target.rate, usd_cny.rate, usd_hkd.rate)
        if usd_cny is not None and usd_hkd is not None
        else None
    )

    problems: list[str] = []
    if deviation is None:
        # 缺美元对 ⇒ 失去唯一的正确性证据。仍然放行 (主汇率本身在区间内),
        # 但必须告警: 这意味着自检退化成了纯区间检查。
        logger.warning(
            "新浪汇率三角自检不可用 (缺 %s), 仅按区间 [%s, %s] 校验 %s=%s",
            ",".join(c for c, q in ((FX_USD_CNY, usd_cny), (FX_USD_HKD, usd_hkd)) if q is None),
            FX_HKD_CNY_MIN, FX_HKD_CNY_MAX, FX_HKD_CNY, target.rate,
        )
    elif deviation > FX_TRIANGLE_MAX_DEVIATION:
        problems.append(
            f"三角自检偏差 {deviation:.4%} > 上限 {FX_TRIANGLE_MAX_DEVIATION:.2%} "
            f"(hkd_cny={target.rate}, usd_cny={usd_cny.rate if usd_cny else None}, "
            f"usd_hkd={usd_hkd.rate if usd_hkd else None}) — 字段序可能已变"
        )
    if not (FX_HKD_CNY_MIN <= target.rate <= FX_HKD_CNY_MAX):
        problems.append(
            f"{FX_HKD_CNY}={target.rate} 落在合理区间 "
            f"[{FX_HKD_CNY_MIN}, {FX_HKD_CNY_MAX}] 之外 — 疑似取错字段或量级错误"
        )

    if problems:
        detail = "; ".join(problems)
        if strict:
            raise SinaError(f"港元汇率自检未通过, 拒绝折算: {detail}")
        logger.warning("港元汇率自检未通过 (strict=False, 仍然返回): %s", detail)

    return HkdCnyRate(
        rate=target.rate,
        as_of=target.as_of,
        source=FX_SOURCE,
        triangle_deviation=deviation,
    )


__all__ = [
    "FX_ENCODING",
    "FX_FIELD_COUNT",
    "FX_HKD_CNY",
    "FX_HKD_CNY_MAX",
    "FX_HKD_CNY_MIN",
    "FX_IDX_DATE",
    "FX_IDX_NAME",
    "FX_IDX_RATE",
    "FX_SOURCE",
    "FX_TRIANGLE_MAX_DEVIATION",
    "FX_USD_CNY",
    "FX_USD_HKD",
    "FxQuote",
    "HkdCnyRate",
    "fetch_hkd_cny",
    "fx_list_param",
    "parse_fx_snapshot",
    "triangle_deviation",
]
