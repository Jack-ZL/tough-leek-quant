"""腾讯港股实时快照归一化 — ``qt.gtimg.cn`` 的 ``hk`` 前缀代码 (方案 §7)。

为什么需要这个模块 (而不是复用 :mod:`app.plugins.tencent.provider`):
东财 push2 集群对本机 IP 级封禁 (2026-11 实测), ``eastmoney.hk.fetch_hk_spot``
这条港股实时链路在封禁解除前不可用; 腾讯 ``qt.gtimg.cn`` 同一端点实测可返回港股
全字段快照, 是港股实时行情的**当前唯一可用源**。

🔴 港股快照与 A 股快照**同端点、同分隔符, 但字段布局不同** (实测 A 股 88 列 /
港股 78 列), 直接套用 :mod:`provider` 的 A 股索引表会静默取错值。逐位差异
(2026-11-13 实测 ``hk00700`` / ``hk09988`` / ``sh600000`` 三行比对):

============ ============================ ============================
索引         A 股                         港股
============ ============================ ============================
``[6]/[36]`` 成交量 **手**                 成交量 **股** (🔴 差 100 倍)
``[37]``     成交额 **万元**               成交额 **元 (HKD)** (🔴 差 1e4 倍)
``[30]``     ``YYYYMMDDHHMMSS``            ``YYYY/MM/DD HH:MM:SS`` (🔴 格式不同)
``[35]``     组合串 ``价/量/额``           只有最新价 (🔴 不可当组合串解析)
``[38]``     换手率 %                      恒 ``0`` (港股换手率在 ``[59]``)
``[46]``     数值字段                      英文名 (``TENCENT``)
``[48]/[49]`` 数值字段                     52 周高 / 52 周低
``[63]``     ``GP-A`` 等                   ``GP`` 股票 / ``ZS`` 指数
``[75]``     数值字段                      币种 (``HKD``)
``[9..28]``  买卖五档价量                  恒 ``0`` (🔴 港股无五档)
============ ============================ ============================

``[1]``名称 ``[2]``代码 ``[3]``最新价 ``[4]``昨收 ``[5]``今开 ``[31]``涨跌额
``[32]``涨跌幅% ``[33]``高 ``[34]``低 ``[43]``振幅% 这几位两市场**一致**。

单位口径 (CONTRIBUTING §3):
- ``change_pct`` / ``amplitude`` / ``turnover_rate`` 源给百分数 → 产出**小数**;
- ``volume`` 产出**股**, 与 :func:`app.plugins.eastmoney.hk.map_hk_spot_row`
  同口径 (A 股是手)。下游换算必须走 :func:`app.market.shares_per_volume_unit`,
  🔴 不得再乘 100;
- ``amount`` 产出**元 (HKD)**。🔴 报告货币非统一 CNY 的问题只影响财务数据,
  行情金额本就是港币计价, 不做汇率换算 (换算即伪造)。

刻意不做:
- **不处理港股指数** (``hkHSI`` 等, ``[63]='ZS'``)。指数已有新浪链路
  (:func:`app.plugins.sina.hk_index.fetch_hk_index_spot`) 且带白名单闸门,
  两处都产出指数行情会造成双份真相。本模块只认 5 位数字股票代码,
  其余一律 :data:`None` (fail closed, 不静默映射成畸形 symbol)。
- **不产出五档**。港股该端点五档恒 0, 伪造盘口比没有盘口危险。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.market import Market

from .client import SNAPSHOT_BATCH, TencentError

if TYPE_CHECKING:  # pragma: no cover
    from .client import TencentClient

logger = logging.getLogger(__name__)

#: 腾讯港股代码前缀。与新浪港股指数的前缀同形但**各自独立定义**: 两个源的代码
#: 空间将来可能分叉, 共用常量会让一处改动静默波及另一处 (同
#: ``index_sync_hk._TENCENT_INDEX_PREFIX`` 的理由)。
HK_SPOT_CODE_PREFIX = "hk"

#: 港股内部 symbol 的代码段: 5 位数字, 且以 ``0``/``8`` 开头
#: (``0`` = 主板/GEM, ``8`` = 人民币柜台)。``1xxxx``/``6xxxx`` 是窝轮牛熊证,
#: 与 :data:`app.plugins.eastmoney.hk._HK_EQUITY_CODE_PREFIXES` 同一过滤口径。
_HK_EQUITY_CODE_PREFIXES = ("0", "8")

#: 港股行情时间戳的墙钟时区。HKT 与北京同为 UTC+8, 但语义上行情归属港交所
#: 交易时段, 用 ``Asia/Hong_Kong`` 表达意图 (与 ``hk_pipeline.HK_SYNC_TIMEZONE`` 一致)。
_HK_TZ = ZoneInfo("Asia/Hong_Kong")

#: 🔴 港股时间戳格式与 A 股的 ``YYYYMMDDHHMMSS`` **不同** (见模块 docstring 表)。
#: 实测分隔符是**斜杠** (``ord('/')==47`` 逐字节确认)。同时接受横杠写法:
#: 上游对指数行/不同集群偶有混用, 两种都能解析比只认一种稳
#: (同 ``sina.hk_index._DATE_FORMATS`` 的处理)。
_HK_TIME_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")

# 快照字段索引 (0 起, ~ 分隔; 2026-11-13 实测 hk00700/hk09988/hkHSI 三行)
_HK_F_NAME, _HK_F_CODE, _HK_F_LAST, _HK_F_PREV, _HK_F_OPEN = 1, 2, 3, 4, 5
_HK_F_TIME, _HK_F_CHG, _HK_F_PCT, _HK_F_HIGH, _HK_F_LOW = 30, 31, 32, 33, 34
_HK_F_VOL, _HK_F_AMT, _HK_F_AMPLITUDE = 36, 37, 43
#: 🔴 港股换手率在 59, 不是 A 股的 38 (港股 38 恒 0)。
#: 交叉验证: 腾讯 31493353 股 / 9092644829 总股本 = 0.346% ≈ 字段 59 的 0.35;
#: 阿里 158411438 / 19901959514 = 0.796% ≈ 字段 59 的 0.80。
_HK_F_TURNOVER = 59
#: 证券类别: ``GP`` 股票 / ``ZS`` 指数。本模块只接受 ``GP``。
_HK_F_KIND = 63
_HK_KIND_EQUITY = "GP"


# ---------------------------------------------------------------------------
# 纯函数: 代码互转
# ---------------------------------------------------------------------------

def hk_spot_code(symbol: str) -> str | None:
    """内部港股 symbol → 腾讯带前缀代码。``00700.HK`` → ``hk00700``。

    非港股、代码非 5 位数字、或代码段属于窝轮牛熊证 → ``None`` (调用方跳过并
    记日志), **不抛错** —— 与 :func:`app.plugins.tencent.provider.symbol_to_tx_code`
    同一约定。

    5 位长度是硬校验: 放过 ``700.HK`` 会拼出 ``hk700``, 腾讯可能不报错却解析到
    **另一只**标的 —— 静默取错数据比取不到数据危险得多。
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    if suffix != Market.HK.value or len(code) != 5 or not code.isdigit():
        return None
    if not code.startswith(_HK_EQUITY_CODE_PREFIXES):
        return None
    return f"{HK_SPOT_CODE_PREFIX}{code}"


def hk_spot_symbol(tx_code: str) -> str | None:
    """腾讯港股代码 → 内部 symbol。``hk00700`` → ``00700.HK``; 非法返回 ``None``。

    指数代码 (``hkHSI``) 的代码段不是数字 → ``None``, 这是刻意的 (见模块 docstring
    「刻意不做」)。
    """
    s = (tx_code or "").strip().lower()
    if not s.startswith(HK_SPOT_CODE_PREFIX):
        return None
    code = s[len(HK_SPOT_CODE_PREFIX):]
    if len(code) != 5 or not code.isdigit():
        return None
    if not code.startswith(_HK_EQUITY_CODE_PREFIXES):
        return None
    return f"{code}.{Market.HK.value}"


# ---------------------------------------------------------------------------
# 纯函数: 字段解析
# ---------------------------------------------------------------------------

def _to_float(value: str | None) -> float | None:
    """腾讯缺值返回空串; 停牌/指数占位返回 ``0``。空串 → ``None``, 0 交由调用方判定。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _field(fields: list[str], idx: int) -> str | None:
    """安全取字段: 越界返回 ``None`` (港股 78 列, 但上游可能截尾)。"""
    return fields[idx] if idx < len(fields) else None


def hk_timestamp_ms(raw: str | None, fetched_ms: int) -> int:
    """港股字段 30 (``YYYY/MM/DD HH:MM:SS``, 港交所墙钟) → epoch 毫秒。

    缺失/非法退回本地拉取时刻 ``fetched_ms`` —— 行情归属时间优先服务端,
    与 :func:`app.plugins.tencent.provider._timestamp_ms` 同一策略, 只是格式不同。
    """
    text = (raw or "").strip()
    if text:
        for fmt in _HK_TIME_FORMATS:
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=_HK_TZ)
            except ValueError:
                continue
            return int(dt.timestamp() * 1000)
        logger.debug("港股快照时间戳无法解析: %r, 退回拉取时刻", raw)
    return fetched_ms


def map_hk_spot_fields(tx_code: str, fields: list[str], fetched_ms: int) -> dict | None:
    """腾讯港股快照字段 → 内部 realtime record。

    与 :func:`app.plugins.eastmoney.hk.map_hk_spot_row` **同一套输出键**
    (``quote_service._process_full_market_records`` 契约), 由
    ``tests/test_hk_tencent_spot.py`` 逐键钉住 —— 两个源可互换的前提是 schema 一致。

    返回 ``None`` 的情形 (全部 fail closed, 不伪造):
    - 代码反解失败 (非 5 位数字股票代码, 含指数);
    - ``[63]`` 不是 ``GP`` (指数 ``ZS`` 等);
    - 最新价为 ``0``/缺失 (停牌或收盘清算占位) —— 与东财 ``"-"`` 同语义,
      但**这里连行一起丢弃**: 自选行情缺价的行没有意义, 且下游 ``change_pct``
      补算会因除零产生脏数据 (同 ``sina.hk_index.map_hk_index_quote`` 的处理)。
    """
    symbol = hk_spot_symbol(tx_code)
    if symbol is None:
        return None

    kind = (_field(fields, _HK_F_KIND) or "").strip().upper()
    if kind and kind != _HK_KIND_EQUITY:
        logger.debug("腾讯港股快照 %s 类别为 %s (非股票), 丢弃", tx_code, kind)
        return None

    last = _to_float(_field(fields, _HK_F_LAST))
    if not last:
        logger.debug("腾讯港股快照 %s 最新价缺失/为 0 (停牌?), 丢弃该行", tx_code)
        return None

    prev = _to_float(_field(fields, _HK_F_PREV))

    change_amount = _to_float(_field(fields, _HK_F_CHG))
    if change_amount is None and prev is not None:
        change_amount = last - prev

    pct = _to_float(_field(fields, _HK_F_PCT))
    change_pct = pct / 100.0 if pct is not None else None
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    amp = _to_float(_field(fields, _HK_F_AMPLITUDE))
    tr = _to_float(_field(fields, _HK_F_TURNOVER))
    return {
        "symbol": symbol,
        # 快照含中文名, 直接透传 (字段 46 是英文名, 不用)
        "name": (_field(fields, _HK_F_NAME) or "").strip() or None,
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(_field(fields, _HK_F_OPEN)),
        "high": _to_float(_field(fields, _HK_F_HIGH)),
        "low": _to_float(_field(fields, _HK_F_LOW)),
        # 股 (不是手) — 见模块 docstring 的字段差异表
        "volume": _to_float(_field(fields, _HK_F_VOL)),
        "amount": _to_float(_field(fields, _HK_F_AMT)),  # 元 (HKD), 不需 ×10000
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": amp / 100.0 if amp is not None else None,
        "turnover_rate": tr / 100.0 if tr is not None else None,
        "timestamp": hk_timestamp_ms(_field(fields, _HK_F_TIME), fetched_ms),
    }


# ---------------------------------------------------------------------------
# 拉取 (触网)
# ---------------------------------------------------------------------------

def fetch_hk_spot(
    client: TencentClient,
    symbols: Iterable[str],
    *,
    raise_on_partial: bool = False,
) -> list[dict]:
    """批量拉取港股实时快照 → 已归一化的行情记录列表。

    ``symbols``: 内部 symbol (``00700.HK``)。非港股/衍生品代码被跳过并计数告警。
    返回: ``list[dict]``, 键见 :func:`map_hk_spot_fields`。顺序按上游响应,
        **不保证**与入参一致 —— 调用方需按 ``symbol`` 建索引。
    抛出: :class:`TencentError` —— 传输/HTTP/重试耗尽/熔断, 以及**全部批次失败**。

    ⚠️ 单批失败只丢该批 (记 warning 继续), 但**全部失败必须抛错**而不是返回
    ``[]``: 上游靠异常与「成功但确实没有数据」区分。返回 ``[]`` 会让调用方
    误判为「港股今天没有行情」并写进空态缓存 (同 ``sina.hk_index.fetch_hk_index_spot``
    与 ``eastmoney.hk.fetch_hk_spot`` 的契约)。

    ⚠️ 无效代码上游**不报错也不返回对应行** (实测 60 只请求返回 47 行),
    所以「返回行数 < 请求行数」是常态, 只记 debug 不告警。

    ``raise_on_partial=True`` (PR6 港股维表换源新增) 把「部分批失败」也升级为抛错。
    🔴 只给**把返回值当白名单用**的调用方开: 维表同步拿本函数结果当「在市闸门」,
    此时「没返回」既可能是退市/停牌, 也可能是那一批请求失败 —— 两者不可区分,
    默认行为 (丢一批继续) 会把 300 只在市股票静默写成退市。行情展示类调用方
    (自选/详情) 缺一批只是少显示几只, 下一轮轮询自愈, 保持默认 ``False``。
    """
    wanted = sorted({s for s in symbols if s and s.strip()})
    if not wanted:
        raise TencentError("港股快照: 请求 symbol 列表为空")

    codes: list[str] = []
    skipped: list[str] = []
    for symbol in wanted:
        code = hk_spot_code(symbol)
        if code is None:
            skipped.append(symbol)
        else:
            codes.append(code)
    if skipped:
        logger.warning(
            "港股快照: %d 个 symbol 不是可识别的港股股票代码, 已跳过 %s",
            len(skipped), skipped[:5],
        )
    if not codes:
        raise TencentError(f"港股快照: {len(wanted)} 个 symbol 全部无法映射为腾讯代码")

    fetched_ms = int(time.time() * 1000)
    total_batches = math.ceil(len(codes) / SNAPSHOT_BATCH)
    records: list[dict] = []
    failed_batches = 0
    for i in range(0, len(codes), SNAPSHOT_BATCH):
        chunk = codes[i: i + SNAPSHOT_BATCH]
        try:
            batch = client.snapshot(chunk)
        except TencentError as e:
            failed_batches += 1
            logger.warning("腾讯港股快照批 %d-%d 失败: %s", i, i + len(chunk), e)
            continue
        for tx_code, fields in batch.items():
            rec = map_hk_spot_fields(tx_code, fields, fetched_ms)
            if rec is not None:
                records.append(rec)

    if failed_batches == total_batches:
        raise TencentError(
            f"港股快照: 全部 {total_batches} 批失败 (请求 {len(codes)} 只), 本轮空手而归"
        )
    if raise_on_partial and failed_batches:
        raise TencentError(
            f"港股快照: {failed_batches}/{total_batches} 批失败 (请求 {len(codes)} 只, "
            f"已得 {len(records)} 条) — 调用方要求全批成功, 缺批时无法区分「退市」与「请求失败」"
        )
    logger.info(
        "腾讯港股快照拉取完成: %d 条 (请求 %d 只, %d 批, 失败 %d 批)",
        len(records), len(codes), total_batches, failed_batches,
    )
    return records
