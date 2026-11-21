"""腾讯 ifzq 港股**个股日K**适配器: 端点、列序、翻页方向、count 上限、脏行处理。

全部离线。不触网有两条理由, 缺一不可:

1. 测试触网不可复现 (CONTRIBUTING §9);
2. 🔴 腾讯 ``ifzq`` 是港股当前**唯一**可用日K源 —— 东财 push2 集群已对本机 IP 级
   封禁, 而在市闸门、港股指数、个股日K 三条链路全压在 ifzq 上, 不该被测试流量消耗。

样本行均为 2026-11 真机抓包 (``hk00700`` / ``hk00005``), 见各断言旁注释。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.market import SymbolMarketMismatchError
from app.plugins.tencent import client as tc
from app.plugins.tencent import hk_kline

# ---------------------------------------------------------------------------
# 假传输层 (与 test_tencent_provider 同款形状, 就地复刻避免跨测试文件耦合)
# ---------------------------------------------------------------------------

class _VirtualTime:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.status_code = 200
        self.content = text.encode()
        self.text = text


class _FakeHttp:
    """记录 ``(url, params)``; 按次序吐预置响应, 耗尽后回空 ``day``。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests: list[tuple] = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _FakeResponse(_envelope([]))

    def close(self) -> None:
        pass


def _envelope(rows, code: str = "hk00700") -> str:
    return json.dumps({"code": 0, "data": {code: {"day": rows}}})


def _tx_client(pages) -> tuple[tc.TencentClient, _FakeHttp]:
    """``pages``: 每次调用返回的行列表 (按次序); 元素为 Exception 时模拟上游失败。"""
    http = _FakeHttp([
        p if isinstance(p, Exception) else _FakeResponse(_envelope(p)) for p in pages
    ])
    vt = _VirtualTime()
    client = tc.TencentClient(
        qps=1000.0, jitter_s=(0.0, 0.0), clock=vt.now, sleeper=vt.sleep, http=http
    )
    return client, http


def _row(day: str, *, o="99.0", c="100.0", h="101.0", lo="98.0", v="1000.000") -> list:
    """腾讯日K原始行: 🔴 列序 ``[date, 开, **收**, 高, 低, 量]``, 且全是**字符串**。"""
    return [day, o, c, h, lo, v]


def _params(http: _FakeHttp) -> list[dict]:
    """把假 http 记录的 ``param`` 串拆成字段, 便于断言 beg/end/count。

    空串还原成 ``None``: 上游用空串表达「该侧不限」, 断言里写 ``None`` 更贴近意图。
    """
    out = []
    for _url, p in http.requests:
        code, _klt, beg, end, count = p["param"].split(",")
        out.append({"code": code, "beg": beg or None, "end": end or None, "count": int(count)})
    return out


# ---------------------------------------------------------------------------
# 端点与请求参数
# ---------------------------------------------------------------------------

def test_uses_raw_endpoint_never_hkfqkline():
    """🔴「落库原始价」这条契约由**结构**保证: 只打不复权端点, param 不带 qfq/hfq。

    换源前它靠服务层一个 ``fqt=0`` 入参断言守卫 (该入参已删除)。腾讯侧
    ``hkfqkline`` 给的是**等差** (加减法) 前复权, 从它反推乘性除权因子会凭空造出
    除权事件 —— 汇丰实测 207 个, 真实约 3 个 (见 ``eastmoney.hk.fetch_hk_kline``
    docstring)。等差复权价一旦落库, 污染的是全部历史且**不报错**。
    """
    client, http = _tx_client([[_row("2026-11-20")]])
    hk_kline.fetch_hk_kline_rows(client, "00700.HK")

    url, params = http.requests[0]
    assert url.endswith("/appstock/app/kline/kline")
    assert "fqkline" not in url, "打到了复权端点"
    assert params["param"] == f"hk00700,day,,,{tc.KLINE_DAILY_MAX}"
    assert "qfq" not in params["param"] and "hfq" not in params["param"]


def test_beg_end_are_sent_as_iso():
    """🔴 腾讯要 ``YYYY-MM-DD``; 东财原路径要 ``YYYYMMDD``。

    格式错的症状是**静默空返回**而非报错 —— 整轮同步「成功」写入 0 行。
    """
    client, http = _tx_client([[]])
    hk_kline.fetch_hk_kline_rows(
        client, "00700.HK", beg=date(2020, 1, 1), end=date(2026, 11, 21)
    )
    assert http.requests[0][1]["param"] == f"hk00700,day,2020-01-01,2026-11-21,{tc.KLINE_DAILY_MAX}"


def test_page_size_is_clamped_to_server_cap():
    """🔴 ``count`` 超过服务端硬上限会**静默返回 0 行** (``code=0``, 无报错)。

    实测 (2026-11-21, ``hk00700``): ``count=2000`` → 2000 行; ``count`` 取
    2500/3000/5000/9000/15000/20000 → **一律 0 行**。所以必须 clamp。

    🔴 且本模块的单页常量**直接取** :data:`KLINE_DAILY_MAX` 而非另立数字: 两者一旦
    漂移 (例如这里写 2000 而传输层仍 clamp 到 800), 翻页终止条件
    ``len(page) < size`` 会在**第一页**就误判「已到头」, 静默只落 clamp 根且不报错。
    """
    client, http = _tx_client([[]])
    hk_kline.fetch_hk_kline_rows(client, "00700.HK", page_size=99999)

    assert http.requests[0][1]["param"].split(",")[-1] == str(tc.KLINE_DAILY_MAX)
    assert tc.KLINE_DAILY_MAX == 2000, "服务端硬上限变了? 需重新实测并同步 clamp"
    assert hk_kline.HK_KLINE_PAGE_SIZE == tc.KLINE_DAILY_MAX, "两个常量必须同源"


def test_page_size_above_cap_still_pages_to_the_end():
    """🔴 插件侧的 clamp 是**承重**的, 不是传输层 clamp 的冗余副本。

    传输层 :meth:`TencentClient.kline_daily` 自己也会 clamp 到 2000, 所以「请求参数
    里写的是 2000」这条断言**杀不掉**去掉插件 clamp 的变异体。真正会坏的是翻页
    终止条件 ``len(page) < size``: ``size`` 若仍是调用方传的 99999, 服务端返回的
    满页 2000 根会被判成「不足一页 = 已到头」, 于是**第一页就停**, 静默只落 2000 根
    且不报错不告警 —— 全历史回填会被悄悄截断成最近 8 年。
    """
    full = [
        _row((date(2020, 1, 1) + timedelta(days=i)).isoformat())
        for i in range(tc.KLINE_DAILY_MAX)
    ]
    tail = [_row("2019-12-31")]
    client, http = _tx_client([full, tail])

    rows = hk_kline.fetch_hk_kline_rows(client, "00700.HK", page_size=99999)

    assert len(http.requests) == 2, "满页之后必须继续翻, 不能把 2000 根当成到头"
    assert len(rows) == tc.KLINE_DAILY_MAX + 1
    assert _params(http)[0]["count"] == tc.KLINE_DAILY_MAX


@pytest.mark.parametrize("symbol", [
    "600000.SH",   # A 股
    "000001.SZ",   # A 股
    "00700",       # 缺市场后缀
    "700.HK",      # 代码不足 5 位 → 会拼出 hk700, 可能静默解析到另一只标的
    "99880.HK",    # 窝轮 (非股票前缀 0/8)
    "",
])
def test_non_hk_symbol_is_rejected_before_any_request(symbol):
    """非港股 symbol 必须**在触网前**被拒, 且用 :class:`SymbolMarketMismatchError`。

    与东财原路径 (``hk.fetch_hk_kline`` → ``assert_symbol_market``) 同一异常类型,
    好让服务层把「维表脏数据」(永久性, 下一轮不会自愈) 与「网络失败」(下一轮自愈)
    分开告警 —— 混成一条日志运维就分不出来了。
    """
    client, http = _tx_client([[]])
    with pytest.raises(SymbolMarketMismatchError):
        hk_kline.fetch_hk_kline_rows(client, symbol)
    assert http.requests == [], "不该为非港股 symbol 触网"


# ---------------------------------------------------------------------------
# 翻页: 🔴 必须从近往远
# ---------------------------------------------------------------------------

def test_paging_walks_backward_and_returns_ascending():
    """🔴 上游对「区间 + count」返回的是区间**尾部** count 根 ⇒ 回填必须从近往远翻。

    实测 ``hk00005,day,2020-01-01,2026-11-21,800`` → 800 行但只覆盖 **2023-08-23**
    起。从远往近翻会永远拿同一段最近数据 (要么死循环, 要么静默丢掉早年历史)。
    每页把 ``end`` 改成「本页最早日 − 1 天」, 实测零重叠零缺口。
    """
    pages = [
        [_row("2026-11-19"), _row("2026-11-20")],   # 第1页: 不限 end, 拿最近 2 根
        [_row("2026-11-17"), _row("2026-11-18")],   # 第2页: end = 11-19 的前一天
        [_row("2026-11-16")],                       # 第3页: 不足 page_size → 终止
    ]
    client, http = _tx_client(pages)
    rows = hk_kline.fetch_hk_kline_rows(client, "00700.HK", page_size=2)

    assert [r[0] for r in rows] == [
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20",
    ], "产出必须按日期升序 (与东财原路径同序, 下游按序增量落盘)"
    assert [c["end"] for c in _params(http)] == [None, "2026-11-18", "2026-11-16"]


def test_paging_stops_at_beg_floor_without_extra_request():
    """翻到 ``beg`` 之前就停, 不该再发一次注定为空的请求 (2800+ 标的会放大成噪声)。"""
    pages = [
        [_row("2026-11-19"), _row("2026-11-20")],
        [_row("2026-11-17"), _row("2026-11-18")],
        [_row("2026-11-15"), _row("2026-11-16")],   # 不该被请求到
    ]
    client, http = _tx_client(pages)
    rows = hk_kline.fetch_hk_kline_rows(
        client, "00700.HK", beg=date(2026, 11, 17), page_size=2
    )
    # 第2页最早日 11-17 ⇒ 下一页 end 会是 11-16 < beg ⇒ 停
    assert len(http.requests) == 2
    assert [r[0] for r in rows] == ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    assert all(c["beg"] == "2026-11-17" for c in _params(http))


def test_max_pages_truncates_with_warning(caplog):
    """跑飞保护: 每页都满 ⇒ 触到 ``max_pages`` 就截断, 且**必须告警**。

    静默截断等于静默丢历史。触发即说明翻页终止条件失效 (上游改了区间语义),
    与 ``EastmoneyClient.datacenter_report_all`` 的 ``max_pages`` 同一纪律。
    """
    pages = [
        [_row("2026-11-19"), _row("2026-11-20")],
        [_row("2026-11-17"), _row("2026-11-18")],
        [_row("2026-11-15"), _row("2026-11-16")],
    ]
    client, http = _tx_client(pages)
    with caplog.at_level("WARNING"):
        rows = hk_kline.fetch_hk_kline_rows(client, "00700.HK", page_size=2, max_pages=2)

    assert len(http.requests) == 2, "触到 max_pages 就该停"
    assert len(rows) == 4
    assert "max_pages" in caplog.text, "截断必须告警, 不能静默丢历史"


def test_unparseable_page_date_stops_paging_with_warning(caplog):
    """首页日期解析不出来 ⇒ 停止翻页并告警, 不能拿 ``None`` 当游标继续跑。"""
    pages = [
        [_row("不是日期"), _row("2026-11-20")],
        [_row("2026-11-17"), _row("2026-11-18")],
    ]
    client, http = _tx_client(pages)
    with caplog.at_level("WARNING"):
        rows = hk_kline.fetch_hk_kline_rows(client, "00700.HK", page_size=2)

    assert len(http.requests) == 1, "游标算不出来就不该再翻"
    assert len(rows) == 2
    assert "无法解析" in caplog.text


def test_upstream_error_propagates():
    """上游失败必须**透传**给服务层 (它据此记 warning 并跳过该标的)。

    在这里吞掉异常会让「整轮同步 0 行」看起来像成功。
    """
    client, _ = _tx_client([tc.TencentError("boom")])
    with pytest.raises(tc.TencentError):
        hk_kline.fetch_hk_kline_rows(client, "00700.HK")


# ---------------------------------------------------------------------------
# 解析: 列序 / 脏行 / 类型
# ---------------------------------------------------------------------------

def test_close_is_at_index_2():
    """🔴 腾讯列序是 ``[date, 开, **收**, 高, 低, 量]`` —— 收盘在 index 2。

    与常见 OHLC 序不同 (与 ``kline_m1`` 同款反直觉排列)。按 ``row[2]`` 当最高价
    会让上下影线全错, 而数据看起来「还挺像」, 极难发现。用实测行钉死:
    ``hk00700`` 2026-11-20 = 开 341.000 / 收 344.000 / 高 349.600 / 低 339.600。
    """
    row = ["2026-11-20", "341.000", "344.000", "349.600", "339.600", "41094469.000"]
    df = hk_kline.parse_hk_kline_rows("00700.HK", [row])
    got = df.row(0, named=True)

    assert got["open"] == 341.0
    assert got["close"] == 344.0
    assert got["high"] == 349.6
    assert got["low"] == 339.6
    assert got["volume"] == 41094469.0
    assert got["date"] == date(2026, 11, 20)
    assert got["symbol"] == "00700.HK"


def test_row_with_event_dict_is_accepted():
    """有除权/回购事件的行尾部会多挂一个 dict ⇒ 判长必须用「不少于 6」而非「等于 6」。

    实测 ``hk00005`` 2026-11-18 即 7 列 (第 7 项是 ``{'cqr':…, 'FHcontent':…}``)。
    写成 ``!= 6`` 会把**所有除权日**整根丢掉 —— 而复权因子恰恰要靠这些日子。
    """
    row = [*_row("2026-11-18"), {"cqr": "1", "FHcontent": "派息"}]
    df = hk_kline.parse_hk_kline_rows("00005.HK", [row])
    assert df.height == 1
    assert df.row(0, named=True)["date"] == date(2026, 11, 18)


def test_no_amount_column_is_fabricated():
    """⚠️ 刻意**不造** ``amount``: 6 列端点不提供成交额, 填 0 或复制 volume 都是编造数据。

    与 ``index_sync_hk`` 同一决定。要成交额得改打 ``hkfqkline`` (9 列, 第 9 项为
    成交额**万元**), 但那是等差复权价, 且请求量翻倍 —— 完整权衡记在
    ``hk_kline`` 模块 docstring 的「刻意不做」里, 别重新调研一遍。
    """
    df = hk_kline.parse_hk_kline_rows("00700.HK", [_row("2026-11-20")])
    assert "amount" not in df.columns


def test_numeric_columns_are_cast_not_left_as_strings():
    """腾讯所有数值以**字符串**下发, 解析层就得 cast 成 Float64。

    🔴 留成字符串的话, 任何绕过 ``_normalize_daily`` 直接把本函数输出喂给
    ``compute_enriched`` 的调用方都会炸在字符串列上。
    🔴 且 dtype 必须是 Float64 而非 ``Null``: polars 里 ``null == 0`` 求值为 null,
    ``~null`` 仍是 null, ``filter_halt_days`` 会把**整帧**当停牌滤空
    (指数链路实测 58 行 → 0 行)。
    """
    df = hk_kline.parse_hk_kline_rows("00700.HK", [_row("2026-11-20")])
    for col in ("open", "high", "low", "close", "volume"):
        assert df.schema[col] == pl.Float64, f"{col} 未被 cast"
    assert df.schema["date"] == pl.Date
    assert df.filter(pl.col("volume").is_null()).height == 0


def test_short_row_is_skipped_with_warning(caplog):
    rows = [_row("2026-11-20"), ["2026-11-19", "1.0", "2.0"]]
    with caplog.at_level("WARNING"):
        df = hk_kline.parse_hk_kline_rows("00700.HK", rows)
    assert df.height == 1, "字段不足的行该被跳过而不是补 null"
    assert "字段数不足" in caplog.text


def test_unparseable_date_is_skipped_not_defaulted_to_today(caplog):
    """日期解析失败 ⇒ 跳过 + 告警, **绝不**兜底成今天。

    兜底会把历史数据写成当日数据; 而 ``date=null`` 会产出 ``date=null`` 分区目录
    污染存储布局。
    """
    with caplog.at_level("WARNING"):
        df = hk_kline.parse_hk_kline_rows("00700.HK", [_row("2026-11-20"), _row(" garbage ")])
    assert df.height == 1
    assert "日期无法解析" in caplog.text
    assert df.row(0, named=True)["date"] == date(2026, 11, 20)


def test_null_close_row_is_dropped(caplog):
    """收盘价 cast 后为 null 的行整根丢弃 —— 缺 close 的 K 线会污染 MA/MACD。"""
    with caplog.at_level("WARNING"):
        df = hk_kline.parse_hk_kline_rows(
            "00700.HK", [_row("2026-11-20"), _row("2026-11-19", c="")]
        )
    assert [r["date"] for r in df.iter_rows(named=True)] == [date(2026, 11, 20)]
    assert "收盘价无法解析" in caplog.text


def test_empty_input_yields_empty_frame():
    """空输入 → 空 frame (不是 ``None``, 也不是抛错): 长期停牌/未上市是正常情形。"""
    assert hk_kline.parse_hk_kline_rows("00700.HK", []).height == 0


def test_schema_inference_is_not_truncated_at_100_rows():
    """🔴 ``pl.DataFrame(list_of_dicts)`` 默认 ``infer_schema_length=100``。

    前 100 行里某列全为 ``None`` 时, 该列被推成 ``Null`` dtype, 第 101 行出现真值
    就抛 ``ComputeError``。港股全历史回填动辄数千行, 上游偶发缺字段就会踩中, 且
    症状是**整只标的同步失败**而非那一行失败。必须显式关掉推断长度限制
    (仓库先例: ``ext_data.py``、A1 的 ``instrument_sync_hk.py``)。
    """
    rows = [
        _row((date(2020, 1, 1) + timedelta(days=i)).isoformat(), o=None)
        for i in range(120)
    ]
    rows.append(_row("2020-07-01", o="12.5"))

    df = hk_kline.parse_hk_kline_rows("00700.HK", rows)

    assert df.height == 121
    assert df.schema["open"] == pl.Float64, "被推成 Null dtype 了"
    assert df.row(-1, named=True)["open"] == 12.5
