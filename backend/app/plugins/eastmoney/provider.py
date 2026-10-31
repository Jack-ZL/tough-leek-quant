"""东方财富免费公开行情插件 — provider 协议适配层。P1-12: 开箱即用免 Key 数据源。

数据链与单位口径 (CONTRIBUTING §3):
- realtime: clist 全市场快照分页 → 内部 record (涨跌幅/振幅/换手率 百分数→小数,
  volume 手透传, amount 元); 指数走独立 clist(fs=i:) 单请求补拉
- daily: kline/get 逐标的 (fqt=0 原始价锁定 — 前复权序列事件间存在漂移, 项目内禁止落库)
  → normalize_daily; 股票/ETF/指数共用同一端点
- adj_factor: datacenter 分红事件 (RPT_SHAREBONUS_DET) + 交易所公式推导
  (单事件比值非累积, 与 fuyao/tdxquant 同口径); 配权事件接口未覆盖, 按 0 处理
- instruments: clist 快照复用 → 标的维表 (股本 = 市值/价格 反推; 缺失字段由
  instrument_sync._preserve_share_fields 从旧维表保留)
- depth5: stock/get 逐标的五档盘口 (仅适合连板梯队等小规模监控)
- minute/financial 未接入 → 不进 datasets 声明, 路由自动回退 TickFlow

设置页手动切换 (无自动回退): 默认路由仍为 tickflow, 用户在数据源卡片显式切换后生效。
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import polars as pl

from app.data_providers.normalizer import normalize_daily

from . import client as em_client
from .client import EastmoneyError

logger = logging.getLogger(__name__)

# 路由数据集声明 (plugin.yaml datasets 与此保持一致; minute/financial 未接入不声明)
_DATASETS = ("realtime", "daily", "adj_factor", "depth5")

# 除权因子输出 schema: 内部契约 3 列 + 事件明细列 (供等差显示投影/全精度因子链重建)
_ADJ_SCHEMA = {
    "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
    "dividend": pl.Float64, "bonus": pl.Float64, "allot": pl.Float64,
    "allot_price": pl.Float64, "prev_close": pl.Float64,
}

# 配价K线窗口: 首个事件前 20 自然日 (覆盖长假) ~ 末事件后 5 自然日 (自检用除权日收盘)
_PREV_CLOSE_BACKDAYS = 20
_POST_EX_DAYS = 5
# 未显式传窗口时的日K默认边界 (东财 beg/end 为 YYYYMMDD 字符串)
_KLINE_BEG_DEFAULT = "19900101"
_KLINE_END_DEFAULT = "20500101"


def availability() -> tuple[bool, str]:
    """loader 启动自检: 免 Key、无依赖 → 恒可用 (网络问题由运行期限速/熔断兜底)。

    不做启动网络探测: loader 在模块 import 时执行, 探测会拖慢启动;
    且东财对短时突发请求会触发 IP 级临时封禁 (实测, 见 client.py 模块注释)。
    """
    return True, "无需 Key, 开箱即用"


# ---- 纯函数工具 (可单测, 不触网) ----
def _to_float(value) -> float | None:
    """东财缺值返回 "-" 或空串 (停牌/无该字段), 统一转 None, 不伪造 0。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int:
    f = _to_float(value)
    return math.floor(f) if f is not None else 0


def symbol_to_secid(symbol: str) -> str | None:
    """内部 symbol → 东财 secid (股票/ETF/指数同一套前缀规则)。

    东财市场前缀: 1=上海, 0=深圳/北京。例: 600000.SH→1.600000, 000001.SZ→0.000001,
    920002.BJ→0.920002, 000001.SH(上证指数)→1.000001, 399001.SZ→0.399001。
    无法识别的后缀返回 None (调用方跳过并记日志)。
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    if not code.isdigit():
        return None
    if suffix == "SH":
        return f"1.{code}"
    if suffix in ("SZ", "BJ"):
        return f"0.{code}"
    return None


def _suffix_of(code: str, market) -> str:
    """东财市场码 (f13: 1=沪, 0=深/京) + 代码段 → 内部交易所后缀 (.SH/.SZ/.BJ)。

    北交所挂深市市场码 0, 按代码段识别: 43/83/87/88 开头及 92 段。
    """
    try:
        m = int(market)
    except (TypeError, ValueError):
        m = 0
    if m == 1:
        return ".SH"
    if code.startswith(("4", "8", "92")):
        return ".BJ"
    return ".SZ"


def _exchange_of(code: str, market) -> str:
    """同 _suffix_of, 返回维表 exchange 列取值 (SH/SZ/BJ, 无点号)。"""
    return _suffix_of(code, market).lstrip(".")


def _price_limit(symbol: str) -> float:
    """按代码前缀给涨跌停幅度 (因子自检容差用): 创业板/科创板 20%, 北交所 30%, 主板 10%。"""
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _ref_price(
    prev_close: float | None,
    dividend: float | None,
    bonus: float | None,
    allot: float | None,
    allot_price: float | None,
) -> float | None:
    """交易所除权除息参考价公式: X = (P − D + A×AP) / (1 + B + A), 0.01 四舍五入(half-up)。

    P=前收盘 D=每股派息 B=每股送转 A=每股配股 AP=配股价。与 fuyao/tdxquant 同一实现。
    """
    if prev_close is None:
        return None
    bonus = bonus or 0.0
    allot = allot or 0.0
    denom = 1.0 + bonus + allot
    if denom <= 0:
        return None
    x = (prev_close - (dividend or 0.0) + allot * (allot_price or 0.0)) / denom
    return math.floor(x * 100 + 0.5) / 100


def _date_of(value) -> date | None:
    """'2026-11-09 00:00:00' / '2026-11-09' → date; 非法值返回 None。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _map_spot_row(row: dict, fetched_ms: int) -> dict | None:
    """clist 快照行 → 内部 realtime record (quote_service._process_full_market_records 契约)。

    单位口径: f3/f7/f8 百分数 → 小数 (CONTRIBUTING §3.1); f5 成交量东财原生即手, 透传;
    f6 成交额元; fltt=2 下价格字段已是浮点元。停牌行价格为 "-" → None 保留行
    (下游按停牌处理), 不伪造数据。f124 行情时间戳(秒) 缺失时退回本地拉取时刻。
    """
    code = str(row.get("f12") or "").strip()
    if not code:
        return None
    last = _to_float(row.get("f2"))
    prev = _to_float(row.get("f18"))

    pct = _to_float(row.get("f3"))
    change_pct = pct / 100.0 if pct is not None else None
    change_amount = _to_float(row.get("f4"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    amp = _to_float(row.get("f7"))
    tr = _to_float(row.get("f8"))
    ts_s = _to_float(row.get("f124"))
    return {
        "symbol": f"{code}{_suffix_of(code, row.get('f13'))}",
        "name": row.get("f14"),  # clist 含名称, 直接透传
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("f17")),
        "high": _to_float(row.get("f15")),
        "low": _to_float(row.get("f16")),
        "volume": _to_float(row.get("f5")),  # 手, 东财原生单位
        "amount": _to_float(row.get("f6")),  # 元
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": amp / 100.0 if amp is not None else None,
        "turnover_rate": tr / 100.0 if tr is not None else None,
        # 行情归属时间优先服务端 f124(秒); 阈值过滤 "-"/异常小值
        "timestamp": int(ts_s * 1000) if (ts_s and ts_s > 1e9) else fetched_ms,
    }


def _map_kline_row(symbol: str, line: str) -> dict | None:
    """kline CSV 行 → 内部日K行 (volume 手 / amount 元, 东财原生单位透传)。

    列序(实测 2026-11): 日期,开,收,高,低,成交量,成交额,振幅%,涨跌幅%,涨跌额,换手率%
    — 注意收在第 3 列、高/低在第 4/5 列, 与常见 OHLC 序不同。
    """
    parts = (line or "").split(",")
    if len(parts) < 7:
        return None
    d = _date_of(parts[0])
    o, c, h, low = (_to_float(parts[i]) for i in (1, 2, 3, 4))
    if d is None or o is None or c is None or h is None or low is None:
        return None
    return {
        "symbol": symbol,
        "date": d,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": _to_float(parts[5]),
        "amount": _to_float(parts[6]),
    }


def _depth_entry(row: dict | None, fallback_ms: int) -> dict | None:
    """stock/get 原始字段 → 标准盘口字典 (depth_service 契约, 量单位手透传)。

    买一~买五价 = f19/f17/f15/f13/f11, 量 = f20/f18/f16/f14/f12;
    卖一~卖五价 = f31/f33/f35/f37/f39, 量 = f32/f34/f36/f38/f40。
    任一价格缺失 (停牌/收盘清算/接口未返回) → None 跳过该标的, 不伪造盘口。
    """
    if not row:
        return None
    bid_prices = [_to_float(row.get(f)) for f in ("f19", "f17", "f15", "f13", "f11")]
    ask_prices = [_to_float(row.get(f)) for f in ("f31", "f33", "f35", "f37", "f39")]
    if any(p is None for p in bid_prices) or any(p is None for p in ask_prices):
        return None
    return {
        "bid_prices": bid_prices,
        "bid_volumes": [_to_int(row.get(f)) for f in ("f20", "f18", "f16", "f14", "f12")],
        "ask_prices": ask_prices,
        "ask_volumes": [_to_int(row.get(f)) for f in ("f32", "f34", "f36", "f38", "f40")],
        "timestamp": fallback_ms,
    }


def _per_share(value) -> float:
    """东财分红接口成分口径为每 10 股 (10派1元 → PRETAX_BONUS_RMB=1) → 每股。"""
    f = _to_float(value)
    return f / 10.0 if f is not None else 0.0


def _merge_same_day(evs: list[dict]) -> list[dict]:
    """同日多事件 (派息与送转分开公告) 成分求和合并, 避免互相覆盖丢成分。

    输入行需含 _ex(date); 输出 [{_ex, _d(每股派息), _b(每股送转)}] 按日期升序。
    BONUS_IT_RATIO (送转合计) 缺失时退回 BONUS_RATIO + IT_RATIO 求和。
    """
    by_day: dict[date, dict] = {}
    for ev in evs:
        exd = ev["_ex"]
        b = _per_share(ev.get("BONUS_IT_RATIO"))
        if not b:
            b = _per_share(ev.get("BONUS_RATIO")) + _per_share(ev.get("IT_RATIO"))
        slot = by_day.setdefault(exd, {"_ex": exd, "_d": 0.0, "_b": 0.0})
        slot["_d"] += _per_share(ev.get("PRETAX_BONUS_RMB"))
        slot["_b"] += b
    return [by_day[k] for k in sorted(by_day)]


@dataclass
class _EastmoneyConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "eastmoney"
    display_name: str = "东方财富"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class EastmoneyProvider:
    """东财数据源。realtime = clist 全市场快照分页; daily/adj_factor = 逐标的限速拉取。"""

    name = "eastmoney"
    builtin = True

    def __init__(self) -> None:
        self.config = _EastmoneyConfig()
        self._client: em_client.EastmoneyClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> em_client.EastmoneyClient:
        if self._client is None:
            self._client = em_client.EastmoneyClient()
        return self._client

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表 (不阻断轮询)。"""
        try:
            rows = self._get_client().spot_all()
        except EastmoneyError as e:
            logger.warning("东财实时行情拉取失败: %s", e)
            return []
        fetched_ms = int(time.time() * 1000)
        records: list[dict] = []
        dropped = 0
        for row in rows:
            rec = _map_spot_row(row, fetched_ms)
            if rec is not None:
                records.append(rec)
            else:
                dropped += 1
        if dropped and not records:
            # 整页都识别不出 f12 代码 → 大概率接口 schema 变了, 明确告警而非静默空数据
            logger.warning("东财快照 %d 行全部缺少 f12 代码字段, 疑似接口结构变化", dropped)
            return []
        logger.info("东财实时行情拉取完成: %d 条(丢弃 %d 行)", len(records), dropped)
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照 (可选插件协议, quote_service 鸭子类型调用)。

        A 股 clist 不含指数, 指数用 fs=i: 前缀单次 clist 补拉 (核心四只 + 偏离值基准)。
        返回 None = 本轮失败 (上游沿用指数缓存); [] = 成功但无数据。
        """
        secids = [s for s in (symbol_to_secid(x) for x in symbols or []) if s]
        if not secids:
            return []
        try:
            rows = self._get_client().index_spot(secids)
        except EastmoneyError as e:
            logger.warning("东财指数快照拉取失败: %s", e)
            return None
        fetched_ms = int(time.time() * 1000)
        records = [r for r in (_map_spot_row(row, fetched_ms) for row in rows) if r]
        if not records:
            logger.warning("东财指数快照返回 %d 行但全部无法映射", len(rows))
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """iter_daily 的聚合版 (设置页试拉 / 非流式调用方使用)。"""
        chunks = [
            df
            for df in self.iter_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """逐标的 kline 拉取 → 内部日K契约 (原始价 fqt=0, volume 手, amount 元)。

        股票/ETF/指数共用同一 kline 端点 (secid 前缀区分), 三类 asset_type 均支持。
        逐标的软失败 (日志+跳过), 单标的一帧流式产出供 _persist_daily_chunks 逐批落盘。
        限速由 client 令牌桶承担: 全市场首次全量回填约 1 小时, 增量只补缺口标的。
        """
        if not symbols:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        beg = start_time.strftime("%Y%m%d") if start_time else _KLINE_BEG_DEFAULT
        end = end_time.strftime("%Y%m%d") if end_time else _KLINE_END_DEFAULT
        client = self._get_client()
        total = len(symbols)
        for i, symbol in enumerate(symbols):
            secid = symbol_to_secid(symbol)
            if secid is None:
                logger.warning("东财日K: 无法识别 secid, 跳过 %s", symbol)
            else:
                try:
                    lines = client.kline(secid, beg, end)
                except EastmoneyError as e:
                    logger.warning("东财日K拉取失败 %s [%s ~ %s]: %s", symbol, beg, end, e)
                    lines = []
                rows = [r for r in (_map_kline_row(symbol, ln) for ln in lines) if r]
                if rows:
                    df = normalize_daily(rows, source=self.name)
                    if not df.is_empty():
                        yield df
            if on_chunk_done:
                on_chunk_done(i + 1, total)

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """东财分红事件 → 内部除权因子契约 (symbol/trade_date/ex_factor, 单事件比值非累积)
        + 事件明细列 (dividend/bonus/allot/allot_price/prev_close, 可空)。

        数据链: datacenter RPT_SHAREBONUS_DET 按除权日窗口一次拉全市场 (分页, 不逐标的)
        → 过滤目标标的集 → 同日事件成分合并 → 逐标的 kline 配前收盘价 → 交易所公式推导
        → 涨跌停自检剔除异常因子。配股 (allot) 该接口不覆盖, 按 0 处理 (A 股已极罕见)。
        未来已公告事件无前收盘, 留给滚动增量窗口自然补上 (与 fuyao 同策略)。
        ETF 无分红事件接口 → 空返回, 上层回退 TickFlow。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        end_d = end_time.date() if end_time else date.today()
        start_d = start_time.date() if start_time else end_d - timedelta(days=365)
        try:
            events = self._get_client().dividend_events(start_d, end_d)
        except EastmoneyError as e:
            logger.warning("东财分红事件拉取失败: %s", e)
            return pl.DataFrame(schema=_ADJ_SCHEMA)

        # SECUCODE 自带交易所后缀 (002555.SZ), 与内部 symbol 格式一致可直接匹配
        symset = set(symbols)
        by_sym: dict[str, list[dict]] = {}
        for ev in events:
            sym = str(ev.get("SECUCODE") or "").strip().upper()
            exd = _date_of(ev.get("EX_DIVIDEND_DATE"))
            if sym in symset and exd is not None:
                by_sym.setdefault(sym, []).append({**ev, "_ex": exd})
        if not by_sym:
            return pl.DataFrame(schema=_ADJ_SCHEMA)

        client = self._get_client()
        out_rows: list[dict] = []
        syms = sorted(by_sym)
        for i, sym in enumerate(syms):
            merged = _merge_same_day(by_sym[sym])
            first_ex, last_ex = merged[0]["_ex"], merged[-1]["_ex"]
            closes = self._closes_window(
                client, sym,
                first_ex - timedelta(days=_PREV_CLOSE_BACKDAYS),
                last_ex + timedelta(days=_POST_EX_DAYS),
            )
            if not closes:
                logger.warning("东财除权因子: %s 日K为空, 跳过其 %d 个事件", sym, len(merged))
            else:
                days = sorted(closes)
                for ev in merged:
                    exd: date = ev["_ex"]
                    prev_days = [d for d in days if d < exd]
                    if not prev_days:
                        continue  # 未来事件/日K窗口未覆盖, 留给滚动增量窗口
                    p = closes[prev_days[-1]]
                    ref = _ref_price(p, ev["_d"], ev["_b"], 0.0, 0.0)
                    if ref is None or ref <= 0:
                        logger.warning(
                            "东财除权因子: %s %s 参考价无法计算(P=%s D=%s B=%s), 跳过",
                            sym, exd, p, ev["_d"], ev["_b"],
                        )
                        continue
                    factor = p / ref
                    ex_days = [d for d in days if d >= exd]
                    if ex_days:  # 涨跌停自检: 错误因子会令复权后除权日涨跌幅超出板限
                        ret = closes[ex_days[0]] / (p / factor) - 1.0
                        if abs(ret) > _price_limit(sym) + 0.02:
                            logger.warning(
                                "东财除权因子自检剔除 %s %s: 复权后除权日涨跌幅 %.1f%% 超出涨跌停",
                                sym, exd, ret * 100,
                            )
                            continue
                    out_rows.append({
                        "symbol": sym, "trade_date": exd, "ex_factor": factor,
                        "dividend": ev["_d"], "bonus": ev["_b"],
                        "allot": None, "allot_price": None, "prev_close": p,
                    })
            if on_chunk_done:
                on_chunk_done(i + 1, len(syms))
        if not out_rows:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        return (
            pl.DataFrame(out_rows, schema=_ADJ_SCHEMA)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    def _closes_window(
        self, client: em_client.EastmoneyClient, symbol: str, start_d: date, end_d: date
    ) -> dict[date, float]:
        """拉窗口内原始收盘价 → {date: close} (除权因子配价用)。软失败返回 {}。"""
        secid = symbol_to_secid(symbol)
        if secid is None:
            return {}
        try:
            lines = client.kline(secid, start_d.strftime("%Y%m%d"), end_d.strftime("%Y%m%d"))
        except EastmoneyError as e:
            logger.warning("东财除权因子: %s 配价日K拉取失败: %s", symbol, e)
            return {}
        out: dict[date, float] = {}
        for ln in lines:
            parts = (ln or "").split(",")
            if len(parts) < 7:
                continue
            d = _date_of(parts[0])
            c = _to_float(parts[2])  # 第 3 列 = 收盘 (列序见 _map_kline_row 注释)
            if d is not None and c is not None:
                out[d] = c
        return out

    # ---- instruments (非路由数据集, instrument_sync 鸭子类型调用) ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """clist 快照 → 标的维表扁平行 (仅 A 股股票; ETF/指数维表不覆盖)。

        股本反推: total_shares = f20 总市值 / f2 最新价, float_shares = f21 流通市值 / f2。
        listing_date/tick_size/涨跌停价接口不提供 → None, 由
        instrument_sync._preserve_share_fields 从旧维表保留, 不会掏空既有字段。
        """
        if asset_type != "stock":
            return []
        try:
            rows = self._get_client().spot_all()
        except EastmoneyError as e:
            logger.warning("东财标的维表拉取失败: %s", e)
            return []
        items: list[dict] = []
        for row in rows:
            code = str(row.get("f12") or "").strip()
            if not code:
                continue
            market = row.get("f13")
            price = _to_float(row.get("f2"))

            def _shares(cap_field: str) -> int | None:
                cap = _to_float(row.get(cap_field))
                if cap is None or not price:
                    return None
                return int(cap / price)  # 市值(元) / 价格(元) = 股数

            items.append({
                "symbol": f"{code}.{_exchange_of(code, market)}",
                "name": row.get("f14"),
                "code": code,
                "exchange": _exchange_of(code, market),
                "region": "CN",
                "type": "stock",
                "ext": {
                    "listing_date": None,
                    "total_shares": _shares("f20"),
                    "float_shares": _shares("f21"),
                    "tick_size": None,
                    "limit_up": None,
                    "limit_down": None,
                },
            })
        logger.info("东财标的维表: %d 只股票", len(items))
        return items

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """逐标的 stock/get 五档盘口 → {symbol: 标准盘口字典} (量单位手透传)。

        逐标的接口, 仅适合连板梯队/板块监控等小规模标的集 (depth_service 分片限速)。
        字段缺失 (停牌/接口未返回) 跳过该标的; 全部缺失返回 {} → 上游记日志跳过本轮。
        """
        client = self._get_client()
        out: dict[str, dict] = {}
        fallback_ms = int(time.time() * 1000)
        for symbol in symbols:
            secid = symbol_to_secid(symbol)
            if secid is None:
                continue
            try:
                row = client.quote_depth(secid)
            except EastmoneyError as e:
                logger.warning("东财五档拉取失败 %s: %s", symbol, e)
                continue
            entry = _depth_entry(row, fallback_ms)
            if entry is not None:
                out[symbol] = entry
        return out

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页数据源卡片「试拉」: 小样本真实调用, 返回行数/列/预览 (与 fuyao 同构)。"""
        if dataset in ("daily", "adj_factor"):
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                if dataset == "daily":
                    df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
                else:
                    df = self.get_adj_factors(
                        syms, datetime.now() - timedelta(days=365), datetime.now()
                    )
            except EastmoneyError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": df.height,
                "columns": df.columns,
                "preview": head,
            }
        if dataset == "depth5":
            syms = [s for s in (symbols or [])][:1] or ["000001.SZ"]
            try:
                data = self.get_depth_batch(syms)
            except EastmoneyError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": len(data),
                "columns": list(next(iter(data.values())).keys()) if data else [],
                "preview": data,
            }
        if dataset != "realtime":
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "error": f"东财插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        try:
            rows, count = self._get_client().spot_page(pn=1, pz=5)
        except EastmoneyError as e:
            return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
        fetched_ms = int(time.time() * 1000)
        head = [r for r in (_map_spot_row(row, fetched_ms) for row in rows) if r][:5]
        return {
            "provider": self.name,
            "dataset": "realtime",
            "rows": count or len(head),
            "columns": list(head[0].keys()) if head else [],
            "preview": head,
        }
