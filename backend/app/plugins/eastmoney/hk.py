"""东方财富港股公开接口 — 直连扩展模块 (PR2a)。

**只服务 ``.HK`` 标的**; A 股链路完全不经此模块 (方案 §4 闸1: 独立港股模块)。
共用 :mod:`app.plugins.eastmoney.client` 的令牌桶限速与熔断 —— 实测 push2 主机
短时突发请求即触发 IP 级临时封禁 (HTTP 连接直接被拒), **严禁绕过 client 直连**。

实测口径 (2026-11-15 活体验证; 完整记录见 docs/hk-market-plan.md §3.1.1 / §3.1.2):

- 列表过滤用市场码 **``m:128``**, 不是 ``m:116`` —— 后者返回 17580 行, 混入大量
  窝轮/牛熊证/债券 (样本 ``14192 中寿信证七一购A``、``69128 华虹汇丰八甲熊J``)。
  子类: ``t:3`` 主板 2621 / ``t:4`` GEM 310 / ``t:1`` REITs 493 / ``t:2`` 债券 1324。
- 单标的 secid 前缀 **``116.``** (``128.`` 实测亦可用且返回相同数据, 但统一选
  ``116`` 并由测试钉住 —— 两套前缀混用会让缓存键与去重分叉)。
- ``fs`` 分隔符 ``+`` 与空格**等价** (均返回 total=2621), 沿用既有 ``FS_ALL_A`` 的 ``+`` 写法。
- clist 字段: ``f26`` 上市日期 (YYYYMMDD 整数) / ``f100`` 恒生行业 / ``f38`` 总股本 /
  ``f124`` 行情戳 (秒)。⚠️ **``f292`` 不是每手股数** —— 中国银行 (每手 1000) 与
  阿里巴巴 (每手 100) 同为 5, 已排除。每手股数 (board lot) 需另找源, 见 ``lot_size``。
- 🔴 **成交量单位是「股」, 不是 A 股的「手」**。两条独立交叉验证: ① ``89988``
  f5=86000 × f2=76.8 ≈ f6=6679940 元; ② ``00700`` volume=31493353 对应腾讯返回的
  换手率 0.350% (31.5M / 91.9 亿股)。透传时**不得**再乘 100, 换算统一走
  :func:`app.market.shares_per_volume_unit`。
- **双柜台**: 人民币柜台代码 = 港币柜台代码首位 ``0``→``8``, 名称多 ``-R`` 后缀
  (``09988 阿里巴巴-W`` ↔ ``89988 阿里巴巴-WR``)。⚠️ 不能只按 ``89`` 开头识别,
  ``82xxx``/``83xxx``/``86xxx`` 同样是人民币柜台。
"""

from __future__ import annotations

import logging
from datetime import date

from app.market import Market, assert_symbol_market

from . import client as em_client

logger = logging.getLogger(__name__)


def _to_float(value) -> float | None:
    """东财缺值返回 "-" 或空串 (停牌/无该字段), 统一转 None, 不伪造 0。

    与 ``provider._to_float`` 逐字同义。**刻意不 import 它**: provider 需要反向
    import 本模块的 :func:`hk_secid`, 直接引用会成环。本仓插件惯例就是各自持有
    一份 (见 ``baostock/provider.py``、``tdxquant/provider.py``)。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# 端点常量 (全部经活体验证, 改动前必须重新实测)
# ---------------------------------------------------------------------------

#: 单标的 secid 前缀。``128`` 亦可用, 统一选 ``116`` (见模块 docstring)。
HK_SECID_PREFIX = "116"

#: 港股主板 (t:3)。实测 2621 只, 不含窝轮/牛熊证/债券/REITs。
FS_HK_MAIN_BOARD = "m:128+t:3"

#: 港股全市场股票 = 主板 + GEM + REITs + 债券。实测 4748 只。
#: ⚠️ 与 akshare ``stock_hk_spot_em`` 同口径。**不含** t:5 (窝轮牛熊证 5903 只)。
FS_HK_ALL = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"

#: 港股**股票**全集 = 主板 (t:3) + GEM 创业板 (t:4)。
#:
#: 这是标的维表 (:mod:`app.services.instrument_sync_hk`) 的默认口径: 个股分析要的是
#: 股票, REITs (t:1) 和债券 (t:2) 混进维表会污染选股器/标的池的分母, 且它们没有
#: 「换手率」「市盈率」这些股票语义的字段。
#:
#: ⚠️ t:3=主板 / t:4=GEM / t:1=REITs / t:2=债券 的对应关系来自一次活体探测
#: (主板单查 2621 只, 四者合查 4748 只), **改动前必须重新实测**。第二道防线是
#: :func:`partition_hk_derivatives` + ``_HK_EQUITY_CODE_PREFIXES`` 的代码段过滤:
#: GEM 代码是 ``08xxx`` (0 开头) 故保留, 窝轮牛熊证是 ``1xxxx``/``6xxxx`` 故剔除。
FS_HK_EQUITY = "m:128+t:3,m:128+t:4"

#: 快照字段。在 A 股 SPOT_FIELDS 基础上增补港股必需的:
#: f9 市盈率(动) / f10 量比 / f23 市净率 / f24 60日涨跌幅 / f25 年初至今涨跌幅 /
#: f26 上市日期 / f38 总股本 / f100 恒生行业。
HK_SPOT_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f22,f23,f24,f25,f26,f38,f100,f124"
)

#: 港股代码合法首位。主板/REITs/债券为 ``0xxxx``, GEM 为 ``08xxx``,
#: 人民币柜台为 ``8xxxx``。窝轮 (``1xxxx``/``2xxxx``) 与牛熊证 (``5xxxx``/``6xxxx``)
#: 都不在此列 —— 这是列表纯净性守卫的判据。
_HK_EQUITY_CODE_PREFIXES = ("0", "8")

#: HKEX 每手股数缺省值。**仅在 lot_size 未知时**用于展示兜底, 不得用于任何
#: 金额/换手计算 (港股 board lot 逐股不同: 腾讯 100 / 汇丰 400 / 中国银行 1000)。
HK_FALLBACK_LOT_SIZE = 100


# ---------------------------------------------------------------------------
# 纯函数: symbol / secid
# ---------------------------------------------------------------------------

def hk_secid(symbol: str) -> str | None:
    """内部港股 symbol → 东财 secid。``00700.HK`` → ``116.00700``。

    非港股 symbol、或代码不是 5 位数字, 返回 ``None`` (调用方跳过并记日志),
    **不抛错** —— 与 :func:`app.plugins.eastmoney.provider.symbol_to_secid` 同一约定。

    5 位长度是硬校验: 内部 symbol 契约就是 ``[0-9]{5}\\.HK`` (见
    :data:`app.market.Market`)。放过 ``700.HK`` 会拼出 ``116.700`` 这种畸形 secid,
    东财可能不报错却解析到**另一只**标的 —— 静默取错数据比取不到数据危险得多。
    正常链路不会触发: :func:`hk_symbol` 已做 ``zfill(5)``。
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    if suffix != "HK" or not code.isdigit() or len(code) != 5:
        return None
    return f"{HK_SECID_PREFIX}.{code}"


def hk_symbol(code: str) -> str | None:
    """东财港股代码 → 内部 symbol。``00700`` → ``00700.HK``; 非法返回 ``None``。"""
    c = str(code or "").strip()
    if not c.isdigit():
        return None
    # 港股全市场统一 5 位 (含 GEM 与人民币柜台), 不足位左补零
    c = c.zfill(5)
    if len(c) != 5:
        return None
    return f"{c}.{Market.HK.value}"


# ---------------------------------------------------------------------------
# 纯函数: 双柜台 (dual counter)
# ---------------------------------------------------------------------------

def hkd_counter_of(code: str) -> str | None:
    """人民币柜台代码 → 对应港币柜台代码 (首位 ``8``→``0``)。

    ``89988`` → ``09988``。非 ``8`` 开头返回 ``None`` (它自己就是港币柜台)。
    """
    c = str(code or "").strip()
    if len(c) != 5 or not c.isdigit() or not c.startswith("8"):
        return None
    return "0" + c[1:]


def is_cny_counter(code: str, name: str | None = None) -> bool:
    """是否人民币柜台 (双柜台中与港币柜台同一家公司的那个代码)。

    两个信号: ① 代码 5 位且 ``8`` 开头; ② 名称以 ``R`` 结尾 (HKEX 对人民币柜台
    的命名规定, 形如 ``-R`` / ``-WR`` / ``-SWR``)。

    ``name`` 给定时**两个信号必须一致**才判真 —— 单信号误判的代价不对称:
    漏判会让同一家公司在成交额/行业排名里占两席, 误判会把真实标的踢出统计。
    ``name`` 缺省时只按代码前缀判 (主板 ``8xxxx`` 即人民币柜台)。

    批量场景请优先用 :func:`hk_instrument_rows`, 它额外用「配对代码是否存在」
    这第三个信号交叉校验。
    """
    c = str(code or "").strip()
    if len(c) != 5 or not c.isdigit() or not c.startswith("8"):
        return False
    if name is None:
        return True
    return str(name).strip().upper().endswith("R")


# ---------------------------------------------------------------------------
# 纯函数: 字段解析
# ---------------------------------------------------------------------------

def parse_hk_list_date(value) -> date | None:
    """东财 ``f26`` 上市日期 → :class:`datetime.date`。

    实测为 ``YYYYMMDD`` **整数** (如 ``20230619``), 与 A 股 datacenter 的
    ``'2026-11-09 00:00:00'`` 字符串格式不同, 因此单独一个解析函数。
    缺值 (``'-'`` / ``None`` / ``0``) 与非法值一律返回 ``None``, 不伪造日期。
    """
    f = _to_float(value)
    if f is None or f <= 0:
        return None
    text = str(int(f))
    if len(text) != 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


#: HKEX 价位表 (spread table): (区间下界, 区间上界, 最小变动价位)。
#: 区间为 **左闭右开** ``[lower, upper)``, 与港交所规则一致。
_HK_SPREAD_TABLE: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.25, 0.001),
    (0.25, 0.50, 0.005),
    (0.50, 10.00, 0.010),
    (10.00, 20.00, 0.020),
    (20.00, 100.00, 0.050),
    (100.00, 200.00, 0.100),
    (200.00, 500.00, 0.200),
    (500.00, 1000.00, 0.500),
    (1000.00, 2000.00, 1.000),
    (2000.00, 5000.00, 2.000),
    (5000.00, float("inf"), 5.000),
)


def hk_tick_size(price) -> float | None:
    """按 HKEX 价位表给最小变动价位 (tick size / spread)。

    港股 tick size **随价格变动** (A 股恒为 0.01), 因此必须按标的当前价查表。
    价格缺失或非正返回 ``None`` (不猜测)。
    """
    p = _to_float(price)
    if p is None or p <= 0:
        return None
    for lower, upper, tick in _HK_SPREAD_TABLE:
        if lower <= p < upper:
            return tick
    return None


# ---------------------------------------------------------------------------
# 纯函数: 列表纯净性守卫
# ---------------------------------------------------------------------------

def partition_hk_derivatives(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """把 clist 行分成 (股票, 疑似衍生品/异常) 两堆。

    **为什么需要**: 港股市场码用错 (``m:116`` 而非 ``m:128``) 会静默混入 5903 只
    窝轮牛熊证, 污染全市场统计、涨跌幅榜与量比 —— 且不报错。判据是代码首位:
    股票为 ``0``/``8``, 窝轮为 ``1``/``2``, 牛熊证为 ``5``/``6``。

    被剔除的行**必须记 warning**, 数量非零即说明上游过滤器语义变了, 需要人工复核。
    """
    clean: list[dict] = []
    rejected: list[dict] = []
    for row in rows:
        code = str(row.get("f12") or "").strip()
        if len(code) == 5 and code.isdigit() and code.startswith(_HK_EQUITY_CODE_PREFIXES):
            clean.append(row)
        else:
            rejected.append(row)
    return clean, rejected


# ---------------------------------------------------------------------------
# 行映射: clist → 内部契约
# ---------------------------------------------------------------------------

def map_hk_spot_row(row: dict, fetched_ms: int) -> dict | None:
    """港股 clist 快照行 → 内部 realtime record。

    与 :func:`app.plugins.eastmoney.provider._map_spot_row` **同一套输出键**
    (quote_service._process_full_market_records 契约), 差异只有三处:

    1. symbol 后缀恒为 ``.HK`` (不走 ``_suffix_of`` 的 f13 推断 —— 港股 f13=128,
       用它会被误判成深市);
    2. 🔴 ``volume`` 单位是**股**, 原样透传 (A 股是手)。下游换算必须走
       :func:`app.market.shares_per_volume_unit`, 不得再乘 100;
    3. 无涨跌停字段 (港股 ``has_price_limit=False``)。

    停牌行价格为 ``"-"`` → ``None`` 并**保留该行** (下游按停牌处理), 不伪造数据。
    """
    code = str(row.get("f12") or "").strip()
    symbol = hk_symbol(code)
    if symbol is None:
        return None

    last = _to_float(row.get("f2"))
    prev = _to_float(row.get("f18"))

    pct = _to_float(row.get("f3"))
    change_pct = pct / 100.0 if pct is not None else None
    change_amount = _to_float(row.get("f4"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        change_pct = change_amount / prev

    amp = _to_float(row.get("f7"))
    tr = _to_float(row.get("f8"))
    ts_s = _to_float(row.get("f124"))
    return {
        "symbol": symbol,
        "name": row.get("f14"),
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("f17")),
        "high": _to_float(row.get("f15")),
        "low": _to_float(row.get("f16")),
        # 股 (不是手) — 见模块 docstring 的交叉验证
        "volume": _to_float(row.get("f5")),
        "amount": _to_float(row.get("f6")),  # 元 (HKD)
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": amp / 100.0 if amp is not None else None,
        "turnover_rate": tr / 100.0 if tr is not None else None,
        "timestamp": int(ts_s * 1000) if (ts_s and ts_s > 1e9) else fetched_ms,
    }


#: ``instruments_hk`` 的列契约。前 12 列与 A 股 ``instruments`` **同名同义同序**
#: (方案 §4 闸3 要求 schema 对齐, 便于共用读取代码), 差异只在取值:
#: ``limit_up``/``limit_down`` 恒为 ``None`` —— 港股无涨跌停
#: (``HK_RULES.has_price_limit=False``)。保留列而非删列是刻意的: 现有守卫按
#: 「limit_up 为 0/null 即无限制」判定, null 是安全值; 删列反而会让共用代码分叉。
#: 末尾 5 列是港股专有: ``lot_size`` / ``industry`` / ``is_cny_counter`` /
#: ``hkd_counter`` / ``is_shsc``+``is_szsc`` (后两者待证券资料源接入, 见下)。
HK_INSTRUMENT_COLUMNS = (
    "symbol", "name", "code", "exchange", "region", "type",
    "listing_date", "total_shares", "float_shares", "tick_size",
    "limit_up", "limit_down",
    "lot_size", "industry", "is_cny_counter", "hkd_counter",
)


def hk_instrument_rows(rows: list[dict]) -> list[dict]:
    """港股 clist 行 → ``instruments_hk`` 维表行。

    双柜台标记用**三个信号**判定, 比 :func:`is_cny_counter` 的单行启发式更严:
    代码 ``8`` 开头 + 名称 ``R`` 结尾 + **配对港币代码确实存在于本次列表**。
    三者不一致时记 warning (说明上游命名或代码规则变了), 并按「代码+名称」
    两信号结论落库 —— 宁可标记出来让人复核, 不静默二选一。

    ``lot_size`` 恒为 ``None``: 东财 clist **不提供**每手股数 (``f292`` 已实测排除)。
    补齐源是财务宽表的 ``PER_SHARES`` (8/8 标的精确匹配), 见
    ``app.services.financial_sync_hk.backfill_hk_lot_size`` —— 所以这里写 ``None``
    不是缺陷, 而是「clist 这一路确实没有」的如实表达; ``lot_size`` 已列入
    ``instrument_sync_hk._HK_PRESERVE_FIELDS``, 维表重同步不会抹掉回填值。
    消费方拿到 ``None`` 时**不得**假设为 100
    (见 :data:`app.market.HK_RULES` 的 lot_size 注释)。
    """
    clean, rejected = partition_hk_derivatives(rows)
    if rejected:
        logger.warning(
            "港股列表剔除 %d 行非股票标的 (代码首位非 0/8, 疑似窝轮/牛熊证): %s",
            len(rejected),
            [(r.get("f12"), r.get("f14")) for r in rejected[:5]],
        )

    codes = {str(r.get("f12") or "").strip() for r in clean}
    out: list[dict] = []
    for row in clean:
        code = str(row.get("f12") or "").strip()
        symbol = hk_symbol(code)
        if symbol is None:
            continue
        name = row.get("f14")
        last = _to_float(row.get("f2"))

        hkd = hkd_counter_of(code)
        by_code = is_cny_counter(code, name)
        by_pair = hkd is not None and hkd in codes
        if by_code != by_pair:
            logger.warning(
                "港股双柜台判定信号不一致 %s %r: 代码/名称=%s 配对存在=%s (港币柜台 %s)",
                code, name, by_code, by_pair, hkd,
            )

        shares = _to_float(row.get("f38"))
        out.append({
            "symbol": symbol,
            "name": str(name).strip() if name not in (None, "", "-") else None,
            "code": code,
            "exchange": Market.HK.value,
            "region": Market.HK.value,
            "type": "stock",
            "listing_date": parse_hk_list_date(row.get("f26")),
            "total_shares": int(shares) if shares else None,
            # 港股全流通, 流通股本 = 总股本 (实测 f38 == f39)
            "float_shares": int(shares) if shares else None,
            "tick_size": hk_tick_size(last),
            # 港股无涨跌停 → None (不是 0: 0 会被读成「限价 0 元」)
            "limit_up": None,
            "limit_down": None,
            "lot_size": None,  # 东财 clist 不提供, 见 docstring
            "industry": _clean_text(row.get("f100")),
            "is_cny_counter": by_code,
            "hkd_counter": (hk_symbol(hkd) if by_code and hkd else None),
        })
    return out


def _clean_text(value) -> str | None:
    """东财缺值占位 ``'-'`` / 空串 → ``None``, 不让占位符进维表当成真实行业名。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text != "-" else None


# ---------------------------------------------------------------------------
# 网络调用 (薄封装; 限速/熔断/重试全部由 client 负责)
# ---------------------------------------------------------------------------

def fetch_hk_spot(
    client: em_client.EastmoneyClient,
    *,
    fs: str = FS_HK_MAIN_BOARD,
    fields: str = HK_SPOT_FIELDS,
    page_size: int = 100,
    max_pages: int = 60,
) -> list[dict]:
    """分页拉港股快照全量 → 原始 clist 行。

    ⚠️ ``page_size`` 默认 **100** 而非 A 股的 1000: 实测港股 ``fs`` 下 ``pz=1000``
    只返回 100 行 (上游对港股分页有独立上限), 沿用 1000 会让 ``spot_all`` 的
    「拉满即停」判据永远不成立、白跑 30 页。

    空结果抛 :class:`EastmoneyError` (与 ``spot_all`` 同语义): 港股全市场为空
    只可能是接口异常, 不能当成「今天没有港股」静默返回空表。
    """
    out: list[dict] = []
    total = 0
    for pn in range(1, max_pages + 1):
        data = client._push2_data(
            em_client.PUSH2_BASE,
            "/api/qt/clist/get",
            {
                "pn": pn,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": fs,
                "fields": fields,
            },
        )
        if not data:
            break
        diff = data.get("diff") or []
        # 老版本接口 diff 为 {序号: 行} 字典, 新版为数组 — 两者兼容
        page_rows = list(diff.values()) if isinstance(diff, dict) else list(diff)
        if not page_rows:
            break
        out.extend(page_rows)
        total = int(data.get("total") or 0)
        if total and len(out) >= total:
            break
    if not out:
        raise em_client.EastmoneyError(f"港股快照为空 (fs={fs})")
    if total and len(out) < total:
        logger.warning("港股快照分页未拉满: %d/%d (fs=%s)", len(out), total, fs)
    return out


def fetch_hk_kline(
    client: em_client.EastmoneyClient,
    symbol: str,
    beg: str,
    end: str,
    *,
    fqt: int = 0,
) -> list[str]:
    """单只港股日K → 原始 CSV 行 (解析复用 provider 层 ``_map_kline_row``)。

    行格式与 A 股**完全一致** (实测 ``00700``: ``日期,开,收,高,低,量,额,振幅%,
    涨跌幅%,涨跌额,换手率%``), 因此解析函数可直接复用; 唯一差异是成交量单位为股。

    ``fqt``: 0=不复权 (**项目契约: 落库必须原始价**) 1=前复权 2=后复权。
    港股前复权另有腾讯 ``hkfqkline`` 直出通道, 见方案 §3.3。
    """
    assert_symbol_market(symbol, Market.HK)
    secid = hk_secid(symbol)
    if secid is None:
        raise em_client.EastmoneyError(f"非港股 symbol, 拒绝走港股K线通道: {symbol!r}")
    return client.kline(secid, beg, end, klt=101, fqt=fqt)
