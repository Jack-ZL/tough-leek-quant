"""腾讯行情免费公开接口插件 — provider 协议适配层。P1-12: 开箱即用免 Key 数据源。

数据链与单位口径 (CONTRIBUTING §3):
- realtime: qt.gtimg.cn 批量快照(≤300 只/请求, GBK) → 内部 record
  (涨跌幅/振幅/换手率 百分数→小数, volume 手透传, amount 万元→元);
  股票池来自项目维表 instruments.parquet (腾讯无全市场列表端点, 排行接口已死数据);
  指数走同一端点补拉 (sh000001 等, 五档字段恒 0 但价格/量额正常)
- depth5: 随快照免费返回 (字段 9-28 买卖五档价量, 量单位手), 无独立请求
- minute: ifzq mkline m1 逐标的 (真实 OHLC + 量手, 无成交额列 — 上游第 7/8 列含义未知不采用);
  上限 320 根 ≈ 1.3 个交易日 → 只适合当日盘中监控/应急补数, 不能做历史回补
- daily 未接入: 腾讯日K无成交额字段(实测 6 列 [日期,开,收,高,低,量]), 不满足内部契约,
  日K请用 eastmoney/baostock; adj_factor/financial 上游无对应接口
- 北交所支持 (bj 前缀, 实测 920002 全字段含五档)

设置页手动切换 (无自动回退): 默认路由仍为 tickflow, 用户在数据源卡片显式切换后生效。
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from . import client as tx_client
from .client import TencentError

logger = logging.getLogger(__name__)

# 路由数据集声明 (plugin.yaml datasets 与此保持一致; daily/adj_factor/financial 未接入不声明)
_DATASETS = ("realtime", "depth5", "minute")

_BJ_TZ = ZoneInfo("Asia/Shanghai")

# 分钟帧输出 schema (CANONICAL_MINUTE_COLS 去掉 amount — 腾讯 m1 无可靠成交额, 不伪造)
_MINUTE_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
}

# 快照字段索引 (0 起, ~ 分隔; 2026-11 实测):
#   1名称 2代码 3最新价 4昨收 5今开 6量(手)
#   9-18 买一~买五(价/量交替, 9=买一价 10=买一量) 19-28 卖一~卖五(同构)
#   30时间(YYYYMMDDHHMMSS) 31涨跌额 32涨跌幅% 33高 34低
#   35"价/量(手)/额(元)" 36量(手) 37额(万元) 38换手率% 43振幅% 44流通市值(亿) 45总市值(亿)
_F_NAME, _F_CODE, _F_LAST, _F_PREV, _F_OPEN = 1, 2, 3, 4, 5
_F_TIME, _F_CHG, _F_PCT, _F_HIGH, _F_LOW = 30, 31, 32, 33, 34
_F_COMPOSITE, _F_VOL, _F_AMT_WAN, _F_TURNOVER, _F_AMPLITUDE = 35, 36, 37, 38, 43
# 五档: 买价 9/11/13/15/17, 买量 10/12/14/16/18; 卖价 19/21/23/25/27, 卖量 20/22/24/26/28
_BID_PRICE_IDX = (9, 11, 13, 15, 17)
_BID_VOL_IDX = (10, 12, 14, 16, 18)
_ASK_PRICE_IDX = (19, 21, 23, 25, 27)
_ASK_VOL_IDX = (20, 22, 24, 26, 28)

# 前缀 ↔ 交易所后缀 (指数同规则: 000001.SH→sh000001, 399001.SZ→sz399001)
_PREFIX_TO_SUFFIX = {"sh": ".SH", "sz": ".SZ", "bj": ".BJ"}
_SUFFIX_TO_PREFIX = {v: k for k, v in _PREFIX_TO_SUFFIX.items()}


def availability() -> tuple[bool, str]:
    """loader 启动自检: 免 Key、无依赖 → 恒可用 (网络问题由运行期限速/熔断兜底)。

    不做启动网络探测: loader 在模块 import 时执行, 探测会拖慢启动。
    """
    return True, "无需 Key, 开箱即用"


# ---- 纯函数工具 (可单测, 不触网) ----
def _to_float(value) -> float | None:
    """腾讯缺值返回空串或 "0.00"(停牌/指数五档), 统一转 None 判定由调用方给阈值。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int:
    f = _to_float(value)
    return math.floor(f) if f is not None else 0


def symbol_to_tx_code(symbol: str) -> str | None:
    """内部 symbol → 腾讯带前缀代码。600000.SH→sh600000, 920002.BJ→bj920002。

    无法识别的后缀返回 None (调用方跳过并记日志)。
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    prefix = _SUFFIX_TO_PREFIX.get(f".{suffix}")
    if not code.isdigit() or prefix is None:
        return None
    return f"{prefix}{code}"


def tx_code_to_symbol(tx_code: str) -> str | None:
    """腾讯带前缀代码 → 内部 symbol。sh600000→600000.SH (响应行 key 反解)。"""
    s = (tx_code or "").strip().lower()
    suffix = _PREFIX_TO_SUFFIX.get(s[:2])
    if suffix is None or not s[2:].isdigit():
        return None
    return f"{s[2:].upper()}{suffix}"


def _field(fields: list[str], idx: int) -> str | None:
    """安全取字段: 指数/停牌行可能缺尾部字段, 越界返回 None。"""
    return fields[idx] if idx < len(fields) else None


def _timestamp_ms(raw: str | None, fetched_ms: int) -> int:
    """字段 30 (YYYYMMDDHHMMSS, 北京墙钟) → epoch 毫秒; 缺失/非法退回本地拉取时刻。"""
    if raw and len(raw) == 14 and raw.isdigit():
        try:
            dt = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=_BJ_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return fetched_ms


def _map_snapshot_row(tx_code: str, fields: list[str], fetched_ms: int) -> dict | None:
    """快照字段 → 内部 realtime record (quote_service._process_full_market_records 契约)。

    单位口径: f32/f38/f43 百分数 → 小数 (CONTRIBUTING §3.1); f36 成交量手透传;
    f37 成交额万元 → 元 (缺失时退回 f35 组合串第 3 段, 实测为精确元值)。
    停牌行最新价为 0.00 → None 保留行 (下游按停牌处理), 不伪造数据。
    """
    symbol = tx_code_to_symbol(tx_code)
    if symbol is None:
        return None
    last = _to_float(_field(fields, _F_LAST))
    if last == 0.0:
        last = None  # 停牌/未开盘: 腾讯以 0.00 占位, 转 None 与东财 "-" 同语义
    prev = _to_float(_field(fields, _F_PREV))

    change_amount = _to_float(_field(fields, _F_CHG))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    pct = _to_float(_field(fields, _F_PCT))
    change_pct = pct / 100.0 if pct is not None else None
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    amount = _to_float(_field(fields, _F_AMT_WAN))
    amount = amount * 10000.0 if amount is not None else None
    if amount is None:
        # 退回组合串 "价/量(手)/额(元)" 第 3 段 (指数行 f37 偶缺时实测可用)
        composite = _field(fields, _F_COMPOSITE) or ""
        parts = composite.split("/")
        if len(parts) >= 3:
            amount = _to_float(parts[2])

    volume = _to_float(_field(fields, _F_VOL))
    if volume is None:
        volume = _to_float(_field(fields, 6))  # f6 与 f36 同为量(手), 互为备份

    amp = _to_float(_field(fields, _F_AMPLITUDE))
    tr = _to_float(_field(fields, _F_TURNOVER))
    return {
        "symbol": symbol,
        "name": _field(fields, _F_NAME),  # 快照含名称, 直接透传
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(_field(fields, _F_OPEN)),
        "high": _to_float(_field(fields, _F_HIGH)),
        "low": _to_float(_field(fields, _F_LOW)),
        "volume": volume,  # 手, 腾讯原生单位
        "amount": amount,  # 元
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": amp / 100.0 if amp is not None else None,
        "turnover_rate": tr / 100.0 if tr is not None else None,
        # 行情归属时间优先服务端 f30; 缺失退回本地拉取时刻
        "timestamp": _timestamp_ms(_field(fields, _F_TIME), fetched_ms),
    }


def _depth_entry(fields: list[str], fallback_ms: int) -> dict | None:
    """快照字段 9-28 → 标准盘口字典 (depth_service 契约, 量单位手透传)。

    任一档价格为 0/缺失 (停牌/收盘清算/指数五档恒 0) → None 跳过该标的, 不伪造盘口。
    """
    bid_prices = [_to_float(_field(fields, i)) for i in _BID_PRICE_IDX]
    ask_prices = [_to_float(_field(fields, i)) for i in _ASK_PRICE_IDX]
    if any(not p for p in bid_prices) or any(not p for p in ask_prices):
        return None
    return {
        "bid_prices": bid_prices,
        "bid_volumes": [_to_int(_field(fields, i)) for i in _BID_VOL_IDX],
        "ask_prices": ask_prices,
        "ask_volumes": [_to_int(_field(fields, i)) for i in _ASK_VOL_IDX],
        "timestamp": _timestamp_ms(_field(fields, _F_TIME), fallback_ms),
    }


def _map_m1_row(symbol: str, row: list) -> dict | None:
    """m1 分钟K行 → 内部分钟行。列序(实测): [时间, 开, 收, 高, 低, 量(手), {}, ?]。

    注意收在第 3 列、高/低在第 4/5 列 (腾讯K线通用陷阱, 与日K同款)。
    第 7/8 列含义未知(疑似占位/换手), 不采用 → 无 amount 列 (不伪造)。
    时间为 YYYYMMDDHHMM 北京墙钟 → naive datetime (CONTRIBUTING §3.3)。
    """
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    raw_t = str(row[0] or "")
    if len(raw_t) != 12 or not raw_t.isdigit():
        return None
    try:
        dt = datetime.strptime(raw_t, "%Y%m%d%H%M")
    except ValueError:
        return None
    o, c, h, low = (_to_float(row[i]) for i in (1, 2, 3, 4))
    vol = _to_float(row[5])
    if o is None or c is None or h is None or low is None:
        return None
    return {
        "symbol": symbol,
        "datetime": dt,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": vol,
    }


@dataclass
class _TencentConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tencent"
    display_name: str = "腾讯行情"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class TencentProvider:
    """腾讯数据源。realtime/depth5 = 批量快照; minute = 逐标的 m1 (仅近 320 根)。"""

    name = "tencent"
    builtin = True

    def __init__(self) -> None:
        self.config = _TencentConfig()
        self._client: tx_client.TencentClient | None = None
        # 维表股票池缓存: (mtime, symbols) — 腾讯无全市场列表端点, 复用项目维表
        self._universe_cache: tuple[float, list[str]] | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> tx_client.TencentClient:
        if self._client is None:
            self._client = tx_client.TencentClient()
        return self._client

    # ---- 股票池 (维表复用) ----
    def _universe(self) -> list[str]:
        """全市场股票池: 读项目维表 instruments.parquet (mtime 变化时重读)。

        腾讯无全市场列表端点 (排行接口 stock.gtimg.cn 实测已死数据), 而维表由
        TickFlow/东财 instrument_sync 维护, 本插件作为备源时维表必然已就绪。
        维表缺失 → 空池 + 告警 (显式失败, 不静默伪造)。
        """
        from app.config import settings

        path = Path(settings.data_dir) / "instruments" / "instruments.parquet"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            logger.warning("腾讯实时行情: 维表缺失 (%s), 请先用主源同步标的维表", path)
            return []
        cached = self._universe_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            df = pl.read_parquet(path, columns=["symbol"])
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯实时行情: 维表读取失败 %s", e)
            return []
        symbols = [s for s in df["symbol"].to_list() if symbol_to_tx_code(s)]
        self._universe_cache = (mtime, symbols)
        return symbols

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表 (不阻断轮询)。

        ~5900 只按 300/批分片, 单批失败只丢该批 (记日志继续), 全部失败才返回 []。
        """
        symbols = self._universe()
        if not symbols:
            return []
        client = self._get_client()
        fetched_ms = int(time.time() * 1000)
        records: list[dict] = []
        failed_batches = 0
        codes = [c for c in (symbol_to_tx_code(s) for s in symbols) if c]
        for i in range(0, len(codes), tx_client.SNAPSHOT_BATCH):
            chunk = codes[i : i + tx_client.SNAPSHOT_BATCH]
            try:
                batch = client.snapshot(chunk)
            except TencentError as e:
                failed_batches += 1
                logger.warning("腾讯快照批 %d-%d 失败: %s", i, i + len(chunk), e)
                continue
            for tx_code, fields in batch.items():
                rec = _map_snapshot_row(tx_code, fields, fetched_ms)
                if rec is not None:
                    records.append(rec)
        total_batches = math.ceil(len(codes) / tx_client.SNAPSHOT_BATCH)
        if failed_batches == total_batches and total_batches:
            logger.warning("腾讯实时行情: 全部 %d 批失败, 本轮空手而归", total_batches)
            return []
        logger.info(
            "腾讯实时行情拉取完成: %d 条 (%d 批, 失败 %d 批)",
            len(records), total_batches, failed_batches,
        )
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照 (可选插件协议, quote_service 鸭子类型调用)。

        指数与个股同端点同字段布局 (实测 sh000001 五档恒 0, 价格/量额正常)。
        返回 None = 本轮失败 (上游沿用指数缓存); [] = 成功但无数据。
        """
        codes = [c for c in (symbol_to_tx_code(x) for x in symbols or []) if c]
        if not codes:
            return []
        try:
            batch = self._get_client().snapshot(codes)
        except TencentError as e:
            logger.warning("腾讯指数快照拉取失败: %s", e)
            return None
        fetched_ms = int(time.time() * 1000)
        records = [r for r in (_map_snapshot_row(c, f, fetched_ms) for c, f in batch.items()) if r]
        if not records:
            logger.warning("腾讯指数快照返回 %d 行但全部无法映射", len(batch))
        return records

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """批量快照五档盘口 → {symbol: 标准盘口字典} (量单位手透传)。

        五档随快照免费返回 — 与东财逐标的接口相比, 300 只/请求适合大规模监控。
        价格缺失/为 0 (停牌/指数) 跳过该标的; 全部缺失返回 {} → 上游记日志跳过本轮。
        """
        client = self._get_client()
        out: dict[str, dict] = {}
        fallback_ms = int(time.time() * 1000)
        pairs = [(s, c) for s in symbols for c in (symbol_to_tx_code(s),) if c]
        for i in range(0, len(pairs), tx_client.SNAPSHOT_BATCH):
            chunk = pairs[i : i + tx_client.SNAPSHOT_BATCH]
            try:
                batch = client.snapshot([c for _, c in chunk])
            except TencentError as e:
                logger.warning("腾讯五档批 %d-%d 失败: %s", i, i + len(chunk), e)
                continue
            for symbol, tx_code in chunk:
                fields = batch.get(tx_code)
                if not fields:
                    continue
                entry = _depth_entry(fields, fallback_ms)
                if entry is not None:
                    out[symbol] = entry
        return out

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """1 分钟K → 内部契约 (symbol/datetime 北京墙钟 naive/OHLC/volume 手; 无 amount 列)。

        覆盖上限 320 根/标的 (约 1.3 个交易日, 上游硬限制) — 只适合当日盘中监控/
        应急补数, 不能做历史回补; 请求窗口超出覆盖时记告警并返回可用部分。
        freq 仅支持 1m, 其余空手而归还上层回退 (tdxquant 同款约定);
        ETF/指数分钟未实测覆盖, asset_type 仅接受 stock。
        分钟源 fail-closed: 用户显式选择本源后失败不静默跨源 (kline_sync 契约)。
        """
        if freq != "1m":
            logger.info("腾讯分钟仅支持 1m (请求 %s), 空手而归还上层回退", freq)
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        if asset_type != "stock":
            logger.info("腾讯分钟未覆盖 asset_type=%s, 空手而归", asset_type)
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        stocks = [s for s in symbols if s]
        if not stocks:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        # 窗口超覆盖告警: 320 根 ≈ 1.3 交易日, 留自然日余量按 4 天判
        if start_time is not None and start_time < datetime.now() - timedelta(days=4):
            logger.warning(
                "腾讯分钟K仅覆盖近 320 根(约 1.3 个交易日), 请求起点 %s 超出覆盖, 只能返回可用部分",
                start_time,
            )
        client = self._get_client()
        rows_out: list[dict] = []
        total = len(stocks)
        for i, symbol in enumerate(stocks):
            try:
                tx_code = symbol_to_tx_code(symbol)
                if tx_code is None:
                    continue
                try:
                    raw = client.kline_m1(tx_code)
                except TencentError as e:
                    # 单标的失败只丢该标的 (与东财 iter_daily 同纪律), 不中断整批
                    logger.warning("腾讯分钟K拉取失败 %s: %s", symbol, e)
                    continue
                for row in raw:
                    rec = _map_m1_row(symbol, row)
                    if rec is None:
                        continue
                    dt = rec["datetime"]
                    if start_time is not None and dt < start_time.replace(tzinfo=None):
                        continue
                    if end_time is not None and dt > end_time.replace(tzinfo=None):
                        continue
                    rows_out.append(rec)
            finally:
                # 进度回调每标的必达 (含失败/跳过), 上游进度条不卡死
                if on_chunk_done is not None:
                    on_chunk_done(i + 1, total)
        if not rows_out:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        return pl.DataFrame(rows_out, schema=_MINUTE_SCHEMA)

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页数据源卡片「试拉」: 小样本真实调用, 返回行数/列/预览 (与东财同构)。"""
        if dataset == "minute":
            syms = [s for s in (symbols or [])][:1] or ["000001.SZ"]
            try:
                df = self.get_minute(syms, datetime.now() - timedelta(days=1), datetime.now())
            except TencentError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:  # datetime → ISO 字符串, 保证 JSON 可序列化
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
            except TencentError as e:
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
                "error": f"腾讯插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        syms = [s for s in (symbols or [])][:3] or ["000001.SZ", "600000.SH", "300750.SZ"]
        codes = [c for c in (symbol_to_tx_code(s) for s in syms) if c]
        try:
            batch = self._get_client().snapshot(codes)
        except TencentError as e:
            return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
        fetched_ms = int(time.time() * 1000)
        head = [r for r in (_map_snapshot_row(c, f, fetched_ms) for c, f in batch.items()) if r]
        return {
            "provider": self.name,
            "dataset": "realtime",
            "rows": len(head),
            "columns": list(head[0].keys()) if head else [],
            "preview": head,
        }
