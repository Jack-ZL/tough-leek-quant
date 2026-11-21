"""东方财富 F10 公司概况 → A/H 股配对关系 — 直连扩展模块。

**为什么需要本模块**: AH 溢价 (同一公司的 A 股相对 H 股贵多少) 是港股面板最有解释
力的估值指标之一, 但它的前提是**先知道哪只 A 股对应哪只 H 股**。项目此前没有任何
AH 关联数据 —— 六家数据源插件里都找不到, 本地 ``instruments`` / ``instruments_hk``
两张维表也各自独立、无交叉引用列。

数据源: ``datacenter-web.eastmoney.com`` 报表 ``RPT_F10_BASIC_ORGINFO`` (A 股 F10
「公司概况」), 与 :mod:`app.plugins.eastmoney.hk_dividend` / ``hsgt`` 同主机 ——
独立于 push2 的封禁池 (push2 已被集群级 IP 封禁, 见方案 §1976)。

🔴 **这条路径是试错 5 轮之后才找到的**, 把死路记下来以免后人重走:

- 方案文档指定的 ``stock_zh_ah_spot_em`` 走 push2, **已被封**, 不可用。
- 17 个 ``RPT_*AH*`` 报表名拼法**全部**返回「报表配置不存在」。
- ``RPT_HKF10_INFO_ORGPROFILE`` (港股 F10) 里**没有任何 A 股关联列**。
- ``data.eastmoney.com/hsgt/ah.html`` → 404。
- 20 余个腾讯端点要么已下线要么返回的不是 AH 关系。
- 用 ``ORG_CODE`` (公司级注册号, 实测工商银行 A/H 同为 ``10008224``、建行 H 为
  ``10008225``) 做 A↔H 连接**可行但更差**: 得到 358 对, 混入大量 B 股、权证与
  虚拟代码噪声。``STR_CODEH`` 是**直接给出的关联列**, 严格优于连接。

实测口径 (2026-11-22 活体验证):

- ⭐ **``STR_CODEH`` 直接返回本项目规范格式** ``01398.HK`` —— 与方案 §63 的
  「单一 symbol 格式」强制要求一致, **无需任何规范化**, 也不会引入代码格式分叉。
- 同族列还有 ``STR_CODEA/STR_NAMEA``、``STR_CODEB/STR_NAMEB`` (B 股)。本模块只用
  H 族: A 股代码取行自身的 ``SECUCODE`` 更可靠 (``STR_CODEA`` 在部分行上是
  ``A04021.SZ`` 这类**占位码**, 不是真实 A 股代码)。
- 过滤键 ``(SECURITY_TYPE_CODE="058001001")`` → count=**8387**, 全部
  ``SECURITY_TYPEE='A股'``; 不加过滤是 24854 行 (含 B 股/H 股/退市/基金等)。
  等价写法 ``(SECURITY_TYPEE="A股")`` 同样 8387 行。**用类型码而非中文名**:
  中文枚举值上游改文案就失效, 类型码是稳定标识。
- 🔴 **无法在服务端过滤「有 H 股的行」**, 只能全量拉回本地筛。实测全部写法失效:
  ``(STR_CODEH!="")`` → ``9201 返回数据为空`` (NULL 与空串比较结果为 unknown,
  整表被滤空); ``(STR_CODEH!='')`` → **被静默忽略** (仍返回 8387 行, 最危险的
  一种: 不报错但没生效); ``(STR_CODEH is not null)`` / ``(!=null)`` → ``9501``
  语法错误; ``(STR_CODEH like "%.HK")`` → ``9501 不支持like查询``。
- ⭐ **``pageSize`` 可以开到很大**: 实测 ``pageSize=9000`` **单页返回全部 8387
  行, 0.67s**, ``pages=1``。故本模块用 5000/页 (常态 2 页 ≈1.3s), 既避免单次
  响应过大, 又让 AH 配对这种**极稳定**的数据 (只在新 AH 股上市时才变) 不需要
  服务端缓存 —— 前端 TanStack Query 的 ``staleTime`` 足够 (同
  :mod:`app.services.hk_dividend_calendar` 的取舍)。
- ⚠️ **``STR_CODEH`` 可能是逗号连接的多值** (同一公司多个港股柜台), 实测 3 行:
  ``601899.SH 紫金矿业 → '02958.HK,02899.HK'``、``603993.SH 洛阳钼业 →
  '02957.HK,03993.HK'``、``A22212.SZ 深演智能 → '02723.HK,02947.HK'``。
  括号后缀 ``(五百)/(二千)/(三千)`` 实测就是 ``TRADE_UNIT`` (每手股数):
  ``02899.HK`` 每手 500 股 / 2003-12-23 上市, ``02958.HK`` 每手 2000 股 /
  **2026-11-13** 上市 —— 后者是刚开的第二柜台。**本模块只负责如实拆分成候选,
  不做取舍**: 消歧需要 H 股上市日, 而那是本地 ``instruments_hk`` 维表的数据,
  插件层不碰数据存储 (分层纪律), 交给 :mod:`app.services.ah_premium`。
- ⚠️ 数据里含退市/ST 主体 (``000585.SZ 东电退 → 00042.HK``、``000488.SZ ST晨鸣
  → 01812.HK``)。**刻意不在插件层剔除**: 它们本地没有行情, 会在服务层的价格
  join 里自然落空; 在插件层按名称关键字过滤是脆弱的猜测 (§13)。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, NamedTuple

from app.plugins.eastmoney.client import EastmoneyClient

logger = logging.getLogger(__name__)

#: A 股 F10「公司概况」报表。一行 = 一只 A 股。
RPT_F10_BASIC_ORGINFO = "RPT_F10_BASIC_ORGINFO"

#: A 股过滤。``058001001`` 实测等价于 ``SECURITY_TYPEE='A股'`` (8387 行),
#: 但类型码不受上游改文案影响。
AH_FILTER_A_SHARE = '(SECURITY_TYPE_CODE="058001001")'

#: 拉取列。整张报表有 57 列 (含 ``ORG_CODE``/``STR_CODEA``/``STR_CODEB`` 等),
#: 只取配对必需的 5 列 —— 响应体积差一个量级 (CONTRIBUTING §6.3)。
AH_PAIR_COLUMNS = "SECUCODE,SECURITY_NAME_ABBR,STR_CODEH,STR_NAMEH,LISTING_DATE"

#: 单页行数。实测 9000 可单页取全 (8387 行 / 0.67s); 取 5000 留一倍余量,
#: 常态 2 页。A 股数量涨过 5000×8 才会触发截断告警。
AH_PAIR_PAGE_SIZE = 5000
AH_PAIR_MAX_PAGES = 8

#: 排序键。翻页要求全序稳定; ``SECUCODE`` 天然唯一, 单键即够 (不像 ``hsgt``
#: 需要 ``MUTUAL_TYPE`` 作次键)。
AH_PAIR_SORT_COLUMNS = "SECUCODE"
AH_PAIR_SORT_TYPES = "1"

#: 真实 A 股代码形状。🔴 **必须校验**: 上游 ``SECUCODE`` 里混有 ``A04021.SZ`` /
#: ``A11047.SH`` / ``A22212.SZ`` 这类**占位码** (实测 280 个候选里 67 个不在本地
#: ``instruments`` 维表, 绝大多数是这种)。它们不是可交易标的, 放进去只会让下游
#: join 静默失配。
A_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

#: H 股代码形状。``STR_CODEH`` 实测已是 ``01398.HK`` 规范格式; 校验是为了挡住
#: 上游偶发的脏值 (空串、缺后缀), 而不是做格式转换。
H_SYMBOL_RE = re.compile(r"^\d{4,5}\.HK$")

#: 数据源标识。
AH_PAIR_SOURCE = "eastmoney_datacenter_f10_orginfo"


class AhPairCandidate(NamedTuple):
    """一条**未经消歧**的 AH 配对候选。

    ``h_candidates`` 是元组而非单值: 上游可能给逗号连接的多柜台 (实测 3 例)。
    本层如实透传全部候选, 由服务层用 H 股上市日择一 —— 插件层不碰数据存储,
    拿不到 ``instruments_hk.listing_date``, 也就无从判断哪个是原始柜台。
    """

    #: A 股代码, 规范格式如 ``601398.SH``。
    a_symbol: str
    #: A 股简称, 如 ``工商银行``。可能含 ``ST`` / ``退`` 后缀 (刻意不剔除)。
    a_name: str | None
    #: A 股上市日。服务层用它给「同一 H 股被多只 A 股认领」去重。
    a_listing_date: date | None
    #: H 股候选代码 (规范格式 ``01398.HK``), 已按逗号拆分并去空。
    h_candidates: tuple[str, ...]
    #: 与 ``h_candidates`` **按位置对应**的 H 股名称; 上游名称个数与代码个数不符
    #: 时, 缺位补 ``None`` (不猜测对应关系)。
    h_names: tuple[str | None, ...]


def _clean_str(value: Any) -> str | None:
    """上游字符串 → 去空白的 ``str``; 空/None → ``None``。"""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _split_multi(value: Any) -> list[str]:
    """拆逗号连接的多值列, 去空白与空项。

    上游用**半角逗号**连接 (实测 ``'02958.HK,02899.HK'``); 同时容全角逗号,
    因为同一报表的中文名称列里全角标点很常见, 不值得赌它一致。
    """
    s = _clean_str(value)
    if not s:
        return []
    return [part.strip() for part in s.replace("，", ",").split(",") if part.strip()]


def _parse_listing_date(value: Any) -> date | None:
    """``LISTING_DATE`` → :class:`date`; 空或无法解析 → ``None``。

    实测返回 ``'1994-03-24'`` 或 ``None`` (占位码行无上市日)。两种分隔符都接受,
    与同主机 ``hk_dividend`` / ``hsgt`` 同一纪律 —— 上游在一个主机内混用斜杠与
    横杠已有先例。
    """
    s = _clean_str(value)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19].replace("/", "-")).date()
    except ValueError:
        logger.warning("AH 配对: A 股上市日无法解析, 置空: %r", value)
        return None


def parse_ah_pair_candidates(rows: Iterable[Mapping[str, Any]]) -> list[AhPairCandidate]:
    """原始行 → AH 配对候选, 按 A 股代码升序。

    丢弃三类行 (都是**数据形状**判断, 不涉及业务取舍):

    1. ``SECUCODE`` 不符合真实 A 股形状 (占位码 ``A04021.SZ`` 等);
    2. ``STR_CODEH`` 为空 —— 绝大多数 A 股没有 H 股 (8387 行里仅约 280 行有);
    3. ``STR_CODEH`` 拆出的候选**全部**不符合 H 股代码形状。

    部分候选不合形状时**保留合形状的那些**并 warn: 上游把权证/虚拟代码混进同一
    列是已知行为, 整行丢掉会连带损失有效配对。
    """
    out: list[AhPairCandidate] = []
    for row in rows:
        a_symbol = _clean_str(row.get("SECUCODE"))
        if not a_symbol or not A_SYMBOL_RE.match(a_symbol):
            continue
        raw_h = _split_multi(row.get("STR_CODEH"))
        if not raw_h:
            continue
        raw_names = _split_multi(row.get("STR_NAMEH"))
        h_candidates: list[str] = []
        h_names: list[str | None] = []
        for idx, code in enumerate(raw_h):
            if not H_SYMBOL_RE.match(code):
                logger.warning(
                    "AH 配对: %s 的 H 股候选 %r 不符合代码形状, 已丢弃该候选", a_symbol, code
                )
                continue
            h_candidates.append(code)
            # 名称按位置对应; 上游名称个数少于代码个数时补 None, 不猜测。
            h_names.append(raw_names[idx] if idx < len(raw_names) else None)
        if not h_candidates:
            continue
        out.append(
            AhPairCandidate(
                a_symbol=a_symbol,
                a_name=_clean_str(row.get("SECURITY_NAME_ABBR")),
                a_listing_date=_parse_listing_date(row.get("LISTING_DATE")),
                h_candidates=tuple(h_candidates),
                h_names=tuple(h_names),
            )
        )
    out.sort(key=lambda item: item.a_symbol)
    return out


def fetch_ah_pair_candidates(client: EastmoneyClient) -> list[AhPairCandidate]:
    """拉取全市场 AH 配对候选 (实测约 210~280 条, 视上游脏数据量而定)。

    一次调用 2 个请求 (``pageSize=5000``), 常态 ≈1.3s。**不做服务端缓存**:
    AH 关系只在新 AH 股上市时才变, 前端 ``staleTime`` 已足够。

    :raises EastmoneyError: 报表名/列名/过滤格式错误等**非空**失败。
        整表为空 (``code=9201``) 由 client 归一成 ``[]`` —— 但对本报表而言
        8387 行的表返回空意味着上游出问题了, 服务层应据此 fail closed。
    """
    rows = client.datacenter_report_all(
        RPT_F10_BASIC_ORGINFO,
        flt=AH_FILTER_A_SHARE,
        columns=AH_PAIR_COLUMNS,
        page_size=AH_PAIR_PAGE_SIZE,
        max_pages=AH_PAIR_MAX_PAGES,
        sort_columns=AH_PAIR_SORT_COLUMNS,
        sort_types=AH_PAIR_SORT_TYPES,
    )
    return parse_ah_pair_candidates(rows)


__all__ = [
    "AH_FILTER_A_SHARE",
    "AH_PAIR_COLUMNS",
    "AH_PAIR_MAX_PAGES",
    "AH_PAIR_PAGE_SIZE",
    "AH_PAIR_SORT_COLUMNS",
    "AH_PAIR_SORT_TYPES",
    "AH_PAIR_SOURCE",
    "A_SYMBOL_RE",
    "H_SYMBOL_RE",
    "RPT_F10_BASIC_ORGINFO",
    "AhPairCandidate",
    "fetch_ah_pair_candidates",
    "parse_ah_pair_candidates",
]
