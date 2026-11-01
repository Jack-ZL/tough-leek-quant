"""baostock(证券宝)免费开源数据插件 — provider 协议适配层。P1-12: 开箱即用免 Key 数据源。

数据链与单位口径 (CONTRIBUTING §3):
- daily: query_history_k_data_plus 不复权(adjustflag=3) 逐标的 →
  内部日K契约 (date/open/high/low/close/volume/amount; volume 股→手 ÷100, amount 元透传)。
  股票与指数均支持 (sh.000001 实测可用); 北交所不支持 (上游报 10004011, 直接跳过);
  ETF 无覆盖 → 空返回上层回退。
- adj_factor: query_adjust_factor 全历史复权因子链 → 相邻后复权因子比值推导单事件
  ex_factor = back_i / back_{i-1} (语义 = 前收盘/除权参考价 > 1, 与内部存储一致;
  已用项目存量 000062.SZ 2026-09-30 = 1.01385 精确对账 ✓)。首行为上市日锚点
  (back=1.0, 无前值) 跳过。事件明细列 (dividend/bonus 等) 上游不提供 → 缺省补 null。
- 未接入: realtime/depth5/minute (上游无实时接口, 收盘后更新) / financial
  (实测单查询最坏 31s, 全市场需数天, 不可用) / instruments (query_stock_basic
  服务端挂起且毒化会话, 严禁调用)。

会话纪律: baostock 为全局单 socket 会话 (匿名 bs.login()), 所有查询经模块级锁串行;
查询失败自动 logout+login 重连重试一次 (会话过期自愈)。
速度实测 ~0.15-0.6s/标的 → 全市场日K回填约 15-60 分钟, 定位是备源/兜底而非主力。

设置页手动切换 (无自动回退): 默认路由仍为 tickflow, 用户显式切换后生效。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterator

import polars as pl

from app.data_providers.normalizer import normalize_daily

logger = logging.getLogger(__name__)

# 路由数据集声明 (plugin.yaml datasets 与此保持一致)
_DATASETS = ("daily", "adj_factor")

# 日K查询字段 (顺序即返回行列序): 标准 OHLC + 量(股) + 额(元)
_DAILY_FIELDS = "date,open,high,low,close,volume,amount"
# 不复权原始价 (与东财 fqt=0 同口径; 复权由内部 adj_factor 存储链完成)
_ADJUST_FLAG_RAW = "3"
# 复权因子须查全历史链 (比值推导依赖前一行), 起点取交易所开市日
_ADJ_CHAIN_START = "1990-01-01"

# 除权因子输出 schema (规范三列; 明细列由 _align_adj_factor_frame 补 null)
_ADJ_SCHEMA = {
    "symbol": pl.String,
    "trade_date": pl.Date,
    "ex_factor": pl.Float64,
}

# 后缀 → baostock 市场前缀 (北交所无覆盖: 实测报 10004011 "股票代码未标识sh或sz")
_SUFFIX_TO_MARKET = {".SH": "sh", ".SZ": "sz"}


class BaostockError(Exception):
    """baostock 查询错误 (登录失败 / error_code 非 0 / 重连后仍失败)。"""


# ---- 全局会话管理 (baostock 为模块级单 socket, 必须串行 + 可重连) ----
_BS_LOCK = threading.RLock()  # 所有 bs.* 调用持锁 (含登录), 跨线程串行
_LOGGED_IN = False


def _import_bs():
    """延迟 import: 依赖未安装时 loader 的 availability() 已拦截, 这里兜底。"""
    try:
        import baostock as bs
    except ImportError as e:  # pragma: no cover
        raise BaostockError("baostock 依赖未安装 (设置页数据源卡片可一键安装)") from e
    return bs


def _login_locked(bs) -> None:
    """持锁登录 (幂等: 已登录直接返回)。"""
    global _LOGGED_IN
    if _LOGGED_IN:
        return
    lg = bs.login()
    if getattr(lg, "error_code", "0") != "0":
        raise BaostockError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")
    _LOGGED_IN = True


def _relogin_locked(bs) -> None:
    """持锁强制重连 (会话过期自愈): logout 吞错, 重新 login。"""
    global _LOGGED_IN
    _LOGGED_IN = False
    with contextlib.suppress(Exception):
        bs.logout()
    _login_locked(bs)


def _query_locked(query_fn, *args, **kwargs) -> list[list[str]]:
    """持锁执行一次 baostock 查询, 失败自动重连重试一次, 返回行列表。

    baostock 不抛异常而是返回 error_code — 必须显式检查;
    行数据经 rs.next()/get_row_data() 游标式收集 (官方用法)。
    """
    bs = _import_bs()

    def _once() -> tuple[str, str, list[list[str]]]:
        rs = query_fn(*args, **kwargs)
        code = str(getattr(rs, "error_code", "0") or "0")
        msg = str(getattr(rs, "error_msg", "") or "")
        rows: list[list[str]] = []
        while code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return code, msg, rows

    with _BS_LOCK:
        _login_locked(bs)
        code, msg, rows = _once()
        if code != "0":
            # 会话过期/网络抖动 → 重连重试一次; 确定性错误 (如代码格式) 重试同样失败, 代价可接受
            logger.debug("baostock 查询失败 (%s %s), 重连重试", code, msg)
            _relogin_locked(bs)
            code, msg, rows = _once()
        if code != "0":
            raise BaostockError(f"baostock 查询失败: {code} {msg}")
        return rows


def availability() -> tuple[bool, str]:
    """loader 启动自检: 依赖是否已安装 (免 Key, 装好即可用)。

    不做登录探测: loader 在模块 import 时执行, 网络登录会拖慢启动;
    会话问题由运行期 _query_locked 的自动重连兜底。
    """
    try:
        import baostock  # noqa: F401
    except ImportError:
        return False, "依赖 baostock 未安装 (点击卡片安装按钮, 或手动: uv pip install baostock)"
    return True, "无需 Key, 依赖已安装"


# ---- 纯函数工具 (可单测, 不触网) ----
def _to_float(value) -> float | None:
    """baostock 缺值为空串 (停牌日字段), 统一转 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def symbol_to_bs_code(symbol: str) -> str | None:
    """内部 symbol → baostock 代码。600000.SH→sh.600000; 北交所/无法识别 → None。"""
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    market = _SUFFIX_TO_MARKET.get(f".{suffix}")
    if not code.isdigit() or market is None:
        return None
    return f"{market}.{code}"


def _map_daily_row(symbol: str, row: list[str]) -> dict | None:
    """日K行 → 内部契约行。列序即 _DAILY_FIELDS: [date,open,high,low,close,volume,amount]。

    volume 上游为股 → ÷100 转手 (CONTRIBUTING §3.1 内部日K量纲为手); amount 元透传。
    """
    if not row or len(row) < 7 or not row[0]:
        return None
    vol = _to_float(row[5])
    return {
        "symbol": symbol,
        "date": row[0],  # YYYY-MM-DD, normalize_daily 负责 cast pl.Date
        "open": _to_float(row[1]),
        "high": _to_float(row[2]),
        "low": _to_float(row[3]),
        "close": _to_float(row[4]),
        "volume": vol / 100.0 if vol is not None else None,  # 股 → 手
        "amount": _to_float(row[6]),  # 元
    }


def derive_ex_factors(symbol: str, rows: list[list[str]]) -> list[dict]:
    """复权因子行 → 单事件 ex_factor 链。

    上游列: [code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor]。
    推导: 按除权日升序, ex_factor(d_i) = back_i / back_{i-1}
    (后复权因子累积上涨, 比值 = 前收盘/除权参考价 > 1, 与内部存储语义一致,
    已用存量数据精确对账: 000062.SZ 2026-09-30 → 1.013851 vs 存储 1.01385 ✓)。
    首行为上市日锚点 (back=1.0, 无前值可除) → 跳过。
    """
    parsed: list[tuple[str, float]] = []
    for row in rows or []:
        if len(row) < 4 or not row[1]:
            continue
        back = _to_float(row[3])
        if back is None or back <= 0:
            continue
        parsed.append((row[1], back))
    parsed.sort(key=lambda x: x[0])  # 按除权日升序 (上游顺序不保证)
    out: list[dict] = []
    for prev, cur in zip(parsed, parsed[1:]):
        factor = cur[1] / prev[1]
        if factor <= 0:
            continue
        if factor < 1.0:
            # 现金分红/送转必然 >1; <1 仅可能出现在极端配股价高于市价等场景, 保留但告警
            logger.warning("baostock 除权因子 %s %s = %.6f < 1, 请人工复核", symbol, cur[0], factor)
        out.append({
            "symbol": symbol,
            "trade_date": cur[0],  # 除权除息日 = 因子生效日
            "ex_factor": factor,
        })
    return out


@dataclass
class _BaostockConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "baostock"
    display_name: str = "baostock 证券宝"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class BaostockProvider:
    """baostock 数据源。daily/adj_factor 逐标的查询 (全局锁串行, 备源定位)。"""

    name = "baostock"
    builtin = True

    def __init__(self) -> None:
        self.config = _BaostockConfig()

    def close(self) -> None:
        """loader 重建注册表时调用: 登出全局会话 (吞错, 不影响重建流程)。"""
        global _LOGGED_IN
        with _BS_LOCK:
            if _LOGGED_IN:
                with contextlib.suppress(Exception):
                    _import_bs().logout()
                _LOGGED_IN = False

    # ---- daily ----
    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """逐标的日K → 内部契约帧流 (不复权原始价, volume 手, amount 元)。

        股票/指数同接口; ETF 无覆盖 → 空手而归还上层回退;
        北交所代码上游不支持 → 汇总告警一次并跳过 (不逐条刷屏)。
        逐标的软失败 (日志+跳过), 单标的一帧流式产出供 _persist_daily_chunks 逐批落盘。
        """
        if asset_type == "etf":
            logger.info("baostock 无 ETF 日K覆盖, 空手而归还上层回退")
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        if not symbols:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        beg = start_time.strftime("%Y-%m-%d") if start_time else _ADJ_CHAIN_START
        end = end_time.strftime("%Y-%m-%d") if end_time else date.today().isoformat()
        total = len(symbols)
        skipped_bj: list[str] = []
        for i, symbol in enumerate(symbols):
            code = symbol_to_bs_code(symbol)
            if code is None:
                if symbol.upper().endswith(".BJ"):
                    skipped_bj.append(symbol)
                else:
                    logger.warning("baostock 日K: 无法识别代码, 跳过 %s", symbol)
            else:
                try:
                    rows = _query_locked(
                        _import_bs().query_history_k_data_plus,
                        code, _DAILY_FIELDS,
                        start_date=beg, end_date=end,
                        frequency="d", adjustflag=_ADJUST_FLAG_RAW,
                    )
                except BaostockError as e:
                    logger.warning("baostock 日K拉取失败 %s [%s ~ %s]: %s", symbol, beg, end, e)
                    rows = []
                mapped = [r for r in (_map_daily_row(symbol, row) for row in rows) if r]
                if mapped:
                    df = normalize_daily(mapped, source=self.name)
                    if not df.is_empty():
                        yield df
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        if skipped_bj:
            logger.warning(
                "baostock 不支持北交所, 已跳过 %d 只 (样例: %s)", len(skipped_bj), skipped_bj[:5]
            )

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
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

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """逐标的复权因子链 → 单事件 ex_factor (symbol/trade_date/ex_factor 三列)。

        必须查全历史 (1990 起): 比值推导依赖前一锚点, 截断查询会丢首事件。
        start_time/end_time 仅用于**输出过滤** (增量窗口), 不影响链完整性。
        指数无除权事件、ETF 无覆盖、北交所不支持 → 空返回/跳过。
        """
        if asset_type != "stock" or not symbols:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        bs = _import_bs()
        lo = start_time.date() if start_time else None
        hi = end_time.date() if end_time else None
        out_rows: list[dict] = []
        total = len(symbols)
        skipped_bj: list[str] = []
        for i, symbol in enumerate(symbols):
            try:
                code = symbol_to_bs_code(symbol)
                if code is None:
                    if symbol.upper().endswith(".BJ"):
                        skipped_bj.append(symbol)
                    else:
                        logger.warning("baostock 复权因子: 无法识别代码, 跳过 %s", symbol)
                    continue
                try:
                    rows = _query_locked(
                        bs.query_adjust_factor,
                        code=code, start_date=_ADJ_CHAIN_START, end_date=date.today().isoformat(),
                    )
                except BaostockError as e:
                    logger.warning("baostock 复权因子拉取失败 %s: %s", symbol, e)
                    rows = []
                for rec in derive_ex_factors(symbol, rows):
                    # 输出窗口过滤 (链已完整推导, 只裁结果)
                    try:
                        d = date.fromisoformat(rec["trade_date"])
                    except ValueError:
                        continue
                    if lo and d < lo:
                        continue
                    if hi and d > hi:
                        continue
                    out_rows.append({**rec, "trade_date": d})
            finally:
                # 进度回调每标的必达 (含失败/跳过), 上游进度条不卡死
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
        if skipped_bj:
            logger.warning(
                "baostock 复权因子不支持北交所, 已跳过 %d 只 (样例: %s)",
                len(skipped_bj), skipped_bj[:5],
            )
        if not out_rows:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        return pl.DataFrame(out_rows, schema=_ADJ_SCHEMA)

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页数据源卡片「试拉」: 小样本真实调用, 返回行数/列/预览 (与东财同构)。"""
        if dataset == "daily":
            syms = [s for s in (symbols or [])][:1] or ["600000.SH"]
            end = datetime.now()
            try:
                df = self.get_daily(syms, end - timedelta(days=30), end)
            except BaostockError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:  # date → ISO 字符串, 保证 JSON 可序列化
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {
                "provider": self.name, "dataset": dataset, "rows": df.height,
                "columns": df.columns, "preview": head,
            }
        if dataset != "adj_factor":
            return {
                "provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"baostock 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        syms = [s for s in (symbols or [])][:1] or ["600000.SH"]
        try:
            df = self.get_adj_factors(syms)
        except BaostockError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.tail(5).to_dicts()  # 展示最近 5 次除权事件
        for row in head:
            for k, v in list(row.items()):
                if isinstance(v, (date, datetime)):
                    row[k] = v.isoformat()
        return {
            "provider": self.name, "dataset": dataset, "rows": df.height,
            "columns": df.columns, "preview": head,
        }
