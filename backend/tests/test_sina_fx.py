"""``app.plugins.sina.fx`` 单测 —— 港元→人民币汇率 (AH 溢价的唯一换算依据)。

🔴 这个插件的正确性直接决定 AH 溢价页面上**每一个数字**的对错, 而汇率错 1% 只会
让所有溢价率平移 1%, 在页面上完全看不出来。所以测试的重点不是「能解析」, 而是
**每一条 fail-closed 路径都真的会抛错**, 绝不静默返回兜底值 (§4)。

夹具里的 18 字段布局与 2026-11-21 的真实响应逐位对齐 (字段序见模块 docstring 坑 1:
该端点无文档, 字段 1 是汇率这件事是靠美元三角自检证明的, 不是猜的)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.plugins.sina import fx
from app.plugins.sina.client import HQ_BASE, SinaError


def _line(code: str, rate: str, name: str = "港元/人民币", day: str = "2026-11-21") -> str:
    """拼一行 18 字段的新浪汇率快照。

    字段布局 (0-based): 0=时间, **1=汇率**, 2=买价, 3=卖价, 4=占位(恒 0),
    5~8=高/低/开/昨收, **9=中文名**, 10=涨跌幅, 11=涨跌额, 12=占位, 13=归属,
    14/15=52 周高低, 16=空, **17=日期 (横杠分隔)**。
    """
    fields = [
        "06:59:30",  # 0
        rate,  # 1 ← 要取的就是这个
        "0.8518",  # 2
        "0.8521",  # 3
        "0",  # 4
        "0.8530",  # 5
        "0.8500",  # 6
        "0.8519",  # 7
        "0.8520",  # 8
        name,  # 9
        "0.05",  # 10
        "0.0004",  # 11
        "0",  # 12
        "新浪财经",  # 13
        "0.8800",  # 14
        "0.8200",  # 15
        "",  # 16
        day,  # 17
    ]
    assert len(fields) == fx.FX_FIELD_COUNT
    return f'var hq_str_{code}="{",".join(fields)}";'


#: 三个货币对的真实量级快照。美元对只用于三角自检:
#: ``usd_cny / usd_hkd = 6.6714 / 7.8389 = 0.851066``, 与 ``fx_shkdcny`` 的
#: ``0.8519701810`` 相差 0.106% —— 这正是真实探测到的偏差 (两个快照取数时刻不同
#: 造成的残差), 远小于 1% 上限。
SNAPSHOT_OK = "\n".join(
    [
        _line("fx_shkdcny", "0.8519701810", "港元/人民币"),
        _line("fx_susdcny", "6.6714", "美元/人民币"),
        _line("fx_susdhkd", "7.8389", "美元/港元"),
    ]
)


class FakeSina:
    """顶替 ``SinaClient``: 只提供 ``fetch_hkd_cny`` 用到的 ``_get_text``。"""

    def __init__(self, text: str = "", *, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []
        self.closed = False

    def _get_text(self, base, path, params=None, *, encoding=None, referer=None):
        self.calls.append(
            {"base": base, "path": path, "params": params, "encoding": encoding, "referer": referer}
        )
        if self.error is not None:
            raise self.error
        return self.text

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------

def test_list_param_packs_all_codes_into_one_request():
    """三个货币对**必须**合并成一次请求。

    新浪按请求频次封 IP, 拆成 3 次与合并 1 次的封禁风险不是一个量级。
    """
    assert fx.fx_list_param(("fx_shkdcny", "fx_susdcny")) == "fx_shkdcny,fx_susdcny"


def test_fetch_uses_hq_base_gbk_and_no_referer_override():
    """打到 ``hq.sinajs.cn``、用 GBK 解码、**不**覆盖 Referer。

    🔴 Referer 这条是回归闸: ``SinaClient`` 的默认头里已经带了
    ``https://finance.sina.com.cn/``, 与 ``hk_index`` 不同 (那个端点需要显式覆盖)。
    这里多传一个 referer 不会报错, 但会让「哪个端点需要什么头」这件事变得不可读。
    """
    client = FakeSina(SNAPSHOT_OK)
    fx.fetch_hkd_cny(client)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["base"] == HQ_BASE
    assert call["encoding"] == fx.FX_ENCODING == "gbk"
    assert call["referer"] is None
    # 一次请求打包全部三个代码
    assert call["path"] == "/list=fx_shkdcny,fx_susdcny,fx_susdhkd"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def test_parse_snapshot_extracts_rate_name_and_dash_date():
    """字段 1 是汇率、字段 9 是名称、字段 17 是**横杠**日期。

    🔴 字段 17 的分隔符与 ``hk_index`` 的同位字段**不同** (那个是斜杠
    ``2026-11-13``): 同一台主机、同一种响应形状、不同分隔符。这是实测出来的,
    所以两种格式都要能解析, 但夹具必须钉住横杠这一种是真实形态。
    """
    quotes = fx.parse_fx_snapshot(SNAPSHOT_OK)
    assert set(quotes) == {"fx_shkdcny", "fx_susdcny", "fx_susdhkd"}
    hkd = quotes["fx_shkdcny"]
    assert hkd.rate == pytest.approx(0.8519701810)
    assert hkd.name == "港元/人民币"
    assert hkd.as_of == dt.date(2026, 11, 21)
    assert quotes["fx_susdcny"].rate == pytest.approx(6.6714)
    assert quotes["fx_susdhkd"].rate == pytest.approx(7.8389)


def test_parse_snapshot_accepts_slash_date_too():
    """斜杠日期也能解析 —— ``hk_index`` 用的就是这种, 源格式漂移时不至于全挂。"""
    quotes = fx.parse_fx_snapshot(_line("fx_shkdcny", "0.85", day="2026/11/21"))
    assert quotes["fx_shkdcny"].as_of == dt.date(2026, 11, 21)


def test_parse_snapshot_drops_short_row_instead_of_padding():
    """字段数不足 → **丢弃并告警**, 不补位不猜测 (§4)。

    补位会让下游拿到一个「看起来正常」的汇率, 而真相是源格式变了。
    """
    short = 'var hq_str_fx_shkdcny="06:59:30,0.8519,0.8518";'
    assert fx.parse_fx_snapshot(short) == {}


def test_parse_snapshot_drops_non_numeric_rate():
    """汇率字段非数值 → 丢弃该行, 而不是留一个 ``rate=None`` 的壳。

    留着壳会让 ``fetch_hkd_cny`` 以为「源有这个货币对」却拿不到值。
    """
    bad = _line("fx_shkdcny", "-")
    assert fx.parse_fx_snapshot(bad) == {}


def test_parse_snapshot_skips_empty_body():
    """空 body = 该货币对新浪无数据, 属正常情况, 不算传输错误。"""
    assert fx.parse_fx_snapshot('var hq_str_fx_shkdcny="";') == {}


def test_parse_snapshot_ignores_unrelated_lines():
    """响应里混进别的 ``hq_str_`` 变量 (股票/指数) 时不受影响。"""
    text = 'var hq_str_s_sh000001="上证指数,3533.79";\n' + SNAPSHOT_OK
    assert set(fx.parse_fx_snapshot(text)) == {"fx_shkdcny", "fx_susdcny", "fx_susdhkd"}


def test_to_date_returns_none_on_garbage_and_never_today():
    """解析失败返回 ``None``, **绝不**兜底成 ``date.today()``。

    兜底会把过期汇率伪装成当日汇率, 而 AH 溢价对汇率误差是线性敏感的。
    """
    assert fx._to_date("2026年11月21日") is None
    assert fx._to_date("") is None
    assert fx._to_date(None) is None


def test_to_float_rejects_empty_dash_and_junk():
    assert fx._to_float("") is None
    assert fx._to_float("-") is None
    assert fx._to_float("abc") is None
    assert fx._to_float(None) is None
    assert fx._to_float("0.85") == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# 美元三角自检
# ---------------------------------------------------------------------------

def test_triangle_deviation_matches_live_observation():
    """用真实探测值验证公式: ``|hkd_cny − usd_cny/usd_hkd| / hkd_cny``。

    2026-11-21 实测偏差 0.106447% (两个快照取数时刻不同造成的残差)。
    这是**唯一**能证明「字段 1 真的是汇率」的证据。
    """
    dev = fx.triangle_deviation(0.8519701810, 6.6714, 7.8389)
    assert dev == pytest.approx(0.00106447, abs=1e-6)
    assert dev < fx.FX_TRIANGLE_MAX_DEVIATION


def test_triangle_deviation_is_none_when_any_input_non_positive():
    """算不出就返回 ``None``, 不伪造 0 (0 会被读成「完全一致」)。"""
    assert fx.triangle_deviation(0.0, 6.67, 7.84) is None
    assert fx.triangle_deviation(0.85, 0.0, 7.84) is None
    assert fx.triangle_deviation(0.85, 6.67, -1.0) is None


def test_triangle_deviation_catches_wrong_field():
    """取错字段 (例如误取买价 0.8518 之外的量级) 时偏差会爆表。

    用「把美元/人民币当成港元汇率」模拟: 6.6714 vs 0.851066 → 偏差 87%。
    """
    assert fx.triangle_deviation(6.6714, 6.6714, 7.8389) > fx.FX_TRIANGLE_MAX_DEVIATION


# ---------------------------------------------------------------------------
# fetch_hkd_cny: 正常路径
# ---------------------------------------------------------------------------

def test_fetch_happy_path_returns_rate_asof_source_and_deviation():
    got = fx.fetch_hkd_cny(FakeSina(SNAPSHOT_OK))
    assert got.rate == pytest.approx(0.8519701810)
    assert got.as_of == dt.date(2026, 11, 21)
    assert got.source == fx.FX_SOURCE == "sina_hq_fx"
    assert got.triangle_deviation == pytest.approx(0.00106447, abs=1e-6)


def test_fetch_without_usd_pairs_still_succeeds_but_deviation_is_none():
    """缺美元对 → 失去三角证据, 但主汇率在区间内仍放行, ``triangle_deviation=None``。

    这是**有意的降级**: 区间检查 [0.6, 1.2] 仍能挡住量级错误, 而因为自检退化就
    让整个 AH 溢价页 502 代价过高。插件会告警, 让退化在日志里可见。
    """
    text = _line("fx_shkdcny", "0.8519701810")
    got = fx.fetch_hkd_cny(FakeSina(text))
    assert got.rate == pytest.approx(0.8519701810)
    assert got.triangle_deviation is None


# ---------------------------------------------------------------------------
# fetch_hkd_cny: 每一条 fail-closed 路径
# ---------------------------------------------------------------------------

def test_fetch_raises_on_empty_response():
    """空响应必须抛错 —— 返回 ``[]``/兜底值会把故障伪装成正常 (§4)。"""
    with pytest.raises(SinaError, match="返回空"):
        fx.fetch_hkd_cny(FakeSina(""))


def test_fetch_raises_when_hkd_pair_missing():
    """有别的货币对但没有 ``fx_shkdcny`` → 抛错, 且错误信息里列出实到了什么。"""
    text = _line("fx_susdcny", "6.6714", "美元/人民币")
    with pytest.raises(SinaError, match="fx_shkdcny"):
        fx.fetch_hkd_cny(FakeSina(text))


def test_fetch_raises_on_transport_error():
    """传输异常原样上抛, 不在插件里吞掉。"""
    with pytest.raises(SinaError):
        fx.fetch_hkd_cny(FakeSina("", error=SinaError("连接超时")))


def test_fetch_raises_when_triangle_deviation_exceeds_limit():
    """三角偏差 > 1% → 抛错。字段序变了就会走到这里。"""
    text = "\n".join(
        [
            _line("fx_shkdcny", "0.8519701810"),
            # 美元对整体错一个量级 → implied = 66.714/7.8389 = 8.51, 偏差爆表
            _line("fx_susdcny", "66.714", "美元/人民币"),
            _line("fx_susdhkd", "7.8389", "美元/港元"),
        ]
    )
    with pytest.raises(SinaError, match="三角自检偏差"):
        fx.fetch_hkd_cny(FakeSina(text))


@pytest.mark.parametrize(
    "rate",
    ["0.5", "0.59", "1.21", "8.5", "85.19"],
)
def test_fetch_raises_when_rate_outside_plausible_band(rate):
    """汇率落在 [0.6, 1.2] 之外 → 抛错 (疑似取错字段或量级错误)。

    港元联系汇率制锚定 7.8 港元/美元, 人民币对港元长期在 0.85~0.92 区间;
    0.5 或 8.5 这种值只可能是解析错位。
    """
    with pytest.raises(SinaError, match="合理区间"):
        fx.fetch_hkd_cny(FakeSina(_line("fx_shkdcny", rate)))


def test_strict_false_returns_value_but_only_for_tests():
    """``strict=False`` 时自检不过只告警不抛 —— 仅供测试构造退化场景。

    生产路径不得关闭: 关掉就等于允许一个错 17% 的汇率进入溢价计算。
    """
    text = "\n".join(
        [
            _line("fx_shkdcny", "0.8519701810"),
            _line("fx_susdcny", "66.714", "美元/人民币"),
            _line("fx_susdhkd", "7.8389", "美元/港元"),
        ]
    )
    got = fx.fetch_hkd_cny(FakeSina(text), strict=False)
    assert got.rate == pytest.approx(0.8519701810)
    assert got.triangle_deviation > fx.FX_TRIANGLE_MAX_DEVIATION


def test_strict_false_disables_all_self_checks_not_just_the_triangle():
    """``strict=False`` 关闭**全部**自检 (三角 + 区间), 而不是只关三角那一项。

    这条钉住的是语义的统一性: 若日后有人把它改成半开状态 (例如「区间仍然硬拦」),
    这里会失败, 从而逼出一次明确的决定而不是悄悄改变测试专用开关的含义。
    """
    got = fx.fetch_hkd_cny(FakeSina(_line("fx_shkdcny", "8.5")), strict=False)
    assert got.rate == pytest.approx(8.5)


def test_as_of_is_none_when_date_unparseable_but_rate_still_returned():
    """日期解析不出来 → ``as_of=None``, 汇率照常返回。

    刻意不因为日期缺失就抛错: 汇率本身通过了三角与区间两道自检, 而 ``as_of``
    只是展示用的元信息。但**不得**兜底成今天 (见 ``test_to_date_*``)。
    """
    got = fx.fetch_hkd_cny(FakeSina(_line("fx_shkdcny", "0.8519701810", day="21/11/2026")))
    assert got.rate == pytest.approx(0.8519701810)
    assert got.as_of is None
