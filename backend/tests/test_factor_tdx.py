"""通达信公式支持契约测试 (P1-17 第 1 批)。

覆盖:
  - 别名翻译正确性 (通达信名 → DSL 算子名, 含大小写不敏感/注释/中文变量名);
  - **数值黄金对拍**: 与独立的 numpy 手算参照逐元素比对 (CONTRIBUTING §9 —
    不复用被测的 Polars 路径自证);
  - **TDX ≡ DSL 零分歧**: 同一公式的通达信档与等价 DSL 档输出逐位相同,
    证明翻译层没有引入第二套语义;
  - 退化窗口归一 (MA(X,1)=X, REF(X,0)=X) 与负偏移硬拒 (REF(X,-1) → E005);
  - 拒绝清单逐条命中正确错误码 (未来函数 E304, 其余 E305);
  - 多语句 `:=` 中间变量在翻译层内联展开 (第 3 批), 无输出 → E310, 变量名冲突 → E309;
  - 未知标识符透传给 DSL 的 E001/E002 兜底;
  - 报错偏移映射回**用户写的通达信原文**位置;
  - 存储链路 (to_spec 注册) 与物化链路 (scoring 物化) 贯通;
  - 不变量锁: 映射表永远不得收录未来函数, 且每个映射目标必须真实存在于 DSL。
"""
from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import polars as pl
import pytest

from app.factors import dsl
from app.factors.dsl import BASE_COLUMNS, FACTOR_COLUMN, compile_formula
from app.factors.registry import unregister_factor
from app.factors.store import register_definition, to_spec
from app.factors.tdx.builtins import (
    COLUMN_ALIASES,
    FUNCTION_ARITY,
    FUNCTION_MAP,
    MACRO_ARITY,
    REJECT_FUTURE,
    REJECTED_FUNCTIONS,
    compatibility_table,
)
from app.factors.tdx.parser import parse_formula
from app.factors.tdx.translate import compile_tdx_formula, compile_tdx_formula_cached, translate_tdx

# ------------------------------------------------------------- 测试面板

def _panel(n: int = 60, seed: int = 7) -> pl.DataFrame:
    """单 symbol 随机游走面板; 数值对拍用 (窗口足够长以覆盖 warmup)。"""
    rng = np.random.default_rng(seed)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    high = close + rng.uniform(0.01, 0.3, n)
    low = close - rng.uniform(0.01, 0.3, n)
    open_ = close + rng.normal(0, 0.1, n)
    volume = rng.uniform(1e5, 9e5, n)
    return pl.DataFrame({
        "symbol": ["S"] * n,
        "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": volume * close,
        "turnover_rate": rng.uniform(0.5, 5.0, n),
    })


def _two_symbol_panel() -> pl.DataFrame:
    """两 symbol x 6 日, 验证 over("symbol") 不串组 (与 test_factor_dsl 同形)。"""
    rows = []
    for symbol, closes in (("A", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
                           ("B", [100.0, 90.0, 80.0, 70.0, 60.0, 50.0])):
        for index, close in enumerate(closes):
            rows.append({
                "symbol": symbol, "date": date(2026, 1, index + 1),
                "open": close, "high": close + 1.0, "low": close - 1.0,
                "close": close, "volume": 1000.0 + index * 100,
                "amount": (1000.0 + index * 100) * close,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _eval_tdx(formula: str, panel: pl.DataFrame | None = None) -> np.ndarray:
    compiled = compile_tdx_formula(formula)
    assert compiled.ok, [error.to_dict() for error in compiled.errors]
    assert compiled.frame_transform is not None
    frame = compiled.frame_transform(panel if panel is not None else _panel())
    assert frame is not None
    return frame[FACTOR_COLUMN].to_numpy()


def _eval_dsl(formula: str, panel: pl.DataFrame | None = None) -> np.ndarray:
    compiled = compile_formula(formula)
    assert compiled.ok, [error.to_dict() for error in compiled.errors]
    assert compiled.frame_transform is not None
    frame = compiled.frame_transform(panel if panel is not None else _panel())
    assert frame is not None
    return frame[FACTOR_COLUMN].to_numpy()


# numpy 独立参照 (手写滚动, 不经 Polars)
def _np_ma(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(w - 1, len(x)):
        out[i] = x[i - w + 1:i + 1].mean()
    return out


def _np_std_ddof1(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(w - 1, len(x)):
        out[i] = x[i - w + 1:i + 1].std(ddof=1)
    return out


def _np_ref(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if k:
        out[k:] = x[:len(x) - k]
    else:
        out[:] = x
    return out


def _np_roll(x: np.ndarray, w: int, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(w - 1, len(x)):
        out[i] = fn(x[i - w + 1:i + 1])
    return out


def _bool_or_none(values: np.ndarray) -> list[bool | None]:
    """把因子列转成三值布尔。

    FACTOR_COLUMN 是 Float64, null 落到 numpy 变成 nan —— 而 **nan.astype(bool)
    是 True**! 布尔类结果一律走这个函数, 不能直接 astype(bool), 否则预热期的
    空洞会被读成一堆假信号。
    """
    return [None if np.isnan(v) else bool(v) for v in values.tolist()]


def _k_and(a: bool | None, b: bool | None) -> bool | None:
    """SQL/Kleene 三值与: False 吸收未知, 未知遇 True 仍是未知。"""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return bool(a and b)


def _np_cross3(a: np.ndarray, b: np.ndarray) -> list[bool | None]:
    """CROSS 的三值参考: 第 0 根没有前一根, 结果取决于 Kleene 吸收。"""
    out: list[bool | None] = []
    for i in range(len(a)):
        current = bool(a[i] > b[i])
        previous = None if i == 0 else bool(a[i - 1] <= b[i - 1])
        out.append(_k_and(current, previous))
    return out


def _np_longcross3(a: np.ndarray, b: np.ndarray, n: int) -> list[bool | None]:
    """LONGCROSS 的三值参考: 前置 N 根窗口不满时结果未知, 除非 CROSS 已确定为假。"""
    crossed = _np_cross3(a, b)
    out: list[bool | None] = []
    for i in range(len(a)):
        stayed_below = None if i - n < 0 else bool((a[i - n:i] < b[i - n:i]).all())
        out.append(_k_and(crossed[i], stayed_below))
    return out


# ------------------------------------------------- 翻译正确性 (文本层)

@pytest.mark.parametrize("tdx, expected_dsl", [
    ("MA(C,5)", "ts_mean(close, 5)"),
    ("ma(c,5)", "ts_mean(close, 5)"),                      # 大小写不敏感
    ("Ma(Close,5)", "ts_mean(close, 5)"),
    ("REF(C,1)", "ts_delay(close, 1)"),
    ("HHV(H,20)", "ts_max(high, 20)"),
    ("LLV(L,20)", "ts_min(low, 20)"),
    ("SUM(V,5)", "ts_sum(volume, 5)"),
    ("STD(C,20)", "ts_std(close, 20)"),
    ("IF(C>O,1,0)", "if_else(close > open, 1, 0)"),
    ("IFF(C>O,1,0)", "if_else(close > open, 1, 0)"),       # IFF 是 IF 的同义写法
    ("MAX(H,L)", "max(high, low)"),
    ("MIN(H,L)", "min(high, low)"),
    ("ABS(C)", "abs(close)"),
    ("LOG(C)", "log(close)"),
    ("SQRT(C)", "sqrt(close)"),
    ("AMOUNT", "amount"),
    ("VOL", "volume"),
    ("V", "volume"),
    # 运算符归一
    ("C=REF(C,1)", "close == ts_delay(close, 1)"),          # `=` 是相等比较
    ("C<>REF(C,1)", "close != ts_delay(close, 1)"),         # `<>` 是不等
    ("C>1 AND V>2", "close > 1 and volume > 2"),            # 关键字转小写
    ("C>1 OR V>2", "close > 1 or volume > 2"),
    ("NOT(C>1)", "not (close > 1)"),
    # 退化窗口归一
    ("MA(C,1)", "close"),
    ("REF(C,0)", "close"),
    ("HHV(H,1)", "high"),
    ("LLV(L,1)", "low"),
    ("SUM(V,1)", "volume"),
])
def test_translation_text(tdx: str, expected_dsl: str) -> None:
    result = translate_tdx(tdx)
    assert result.ok, [e.to_dict() for e in result.errors]
    assert result.dsl_text == expected_dsl


@pytest.mark.parametrize("tdx, expected_dsl", [
    ("{这是注释}MA(C,5)", "ts_mean(close, 5)"),
    ("MA(C,5){尾注释}", "ts_mean(close, 5)"),
    ("(*块注释*)MA(C,5)", "ts_mean(close, 5)"),
    ("MA(C,5);", "ts_mean(close, 5)"),                     # 结尾分号可选
    ("  MA( C , 5 )  ", "ts_mean(close, 5)"),               # 空白无关
])
def test_comments_and_whitespace(tdx: str, expected_dsl: str) -> None:
    result = translate_tdx(tdx)
    assert result.ok, [e.to_dict() for e in result.errors]
    assert result.dsl_text == expected_dsl


def test_operator_precedence_preserved() -> None:
    """括号必须保住优先级与结合性: `A+B*C` 与 `(A+B)*C`、`A-B-C` 与 `A-(B-C)` 不能同形。"""
    assert translate_tdx("C+V*2").dsl_text == "close + volume * 2"
    assert translate_tdx("(C+V)*2").dsl_text == "(close + volume) * 2"
    assert translate_tdx("C-V-1").dsl_text == "close - volume - 1"
    assert translate_tdx("C-(V-1)").dsl_text == "close - (volume - 1)"
    assert translate_tdx("C/V/2").dsl_text == "close / volume / 2"
    assert translate_tdx("C/(V/2)").dsl_text == "close / (volume / 2)"
    assert translate_tdx("-(C+V)").dsl_text == "-(close + volume)"
    assert translate_tdx("-C").dsl_text == "-close"
    # OR/AND 优先级: AND 绑得更紧, 少括号也不能改变分组
    assert translate_tdx("C>1 OR V>2 AND C<9").dsl_text == "close > 1 or volume > 2 and close < 9"
    # not 的操作数必须括号 (DSL 的 not 在 unary 层, 比比较绑得紧)
    assert translate_tdx("NOT(C>1)").dsl_text == "not (close > 1)"


@pytest.mark.parametrize("tdx, fully_parenthesized_dsl", [
    ("C+V*2", "close + (volume * 2)"),
    ("(C+V)*2", "(close + volume) * 2"),
    ("C-V-1", "(close - volume) - 1"),
    ("C-(V-1)", "close - (volume - 1)"),
    ("C/V/2", "(close / volume) / 2"),
    ("C/(V/2)", "close / (volume / 2)"),
    ("C>1 AND V>2", "(close > 1) and (volume > 2)"),
    ("C>1 OR V>2 AND C<9", "(close > 1) or ((volume > 2) and (close < 9))"),
    ("NOT(C>1)", "not (close > 1)"),
    ("NOT(C>1) AND V>2", "(not (close > 1)) and (volume > 2)"),
    ("C*-V", "close * (-volume)"),
    ("-(C+V)", "-(close + volume)"),
    ("C>HHV(H,20)*0.98 AND V>MA(V,5)",
     "(close > (ts_max(high, 20) * 0.98)) and (volume > ts_mean(volume, 5))"),
    ("(C>O) AND (V>MA(V,5)) OR C=REF(C,1)",
     "((close > open) and (volume > ts_mean(volume, 5))) or (close == ts_delay(close, 1))"),
    ("NOT(NOT(C>1))", "not (not (close > 1))"),
    ("MA(C,5)/REF(MA(C,5),1)-1", "(ts_mean(close, 5) / ts_delay(ts_mean(close, 5), 1)) - 1"),
    ("(H-L)/(2*REF(C,1))+ABS(C-O)", "((high - low) / (2 * ts_delay(close, 1))) + abs(close - open)"),
])
def test_minimal_parens_same_semantics(tdx: str, fully_parenthesized_dsl: str) -> None:
    """差分锁: 少括号优化后的输出必须与手写全括号参照**逐位相同** (rtol=atol=0)。

    优先级感知加括号是为了省 dsl.MAX_TOKENS 预算; 这条测试保证省括号没有改变语义。
    """
    panel = _panel()
    optimized = compile_tdx_formula(tdx)
    reference = compile_formula(fully_parenthesized_dsl)
    assert optimized.ok, [e.to_dict() for e in optimized.errors]
    assert reference.ok, [e.to_dict() for e in reference.errors]
    got = optimized.frame_transform(panel)[FACTOR_COLUMN].to_numpy()
    want = reference.frame_transform(panel)[FACTOR_COLUMN].to_numpy()
    assert np.allclose(got, want, equal_nan=True, rtol=0, atol=0), tdx


def test_chinese_identifier_passthrough() -> None:
    """中文变量名在通达信侧能切词并原样透传; DSL 词法只认 ASCII 标识符, 故报 E014。

    第 1 批没有用户变量 (`:=` 未开放), 中文标识符只可能是未知列, 报错即正确行为。
    """
    statements, error = parse_formula("均线>1")
    assert error is None
    assert statements[0]["children"][0]["children"][0]["value"] == "均线"
    compiled = compile_tdx_formula("均线>1")
    assert not compiled.ok
    assert compiled.errors[0].code == "E014"


# ------------------------------------------- 数值黄金对拍 (numpy 独立参照)

def test_numeric_golden_against_numpy() -> None:
    """每个 A 类函数都与手写 numpy 参照逐元素比对 (CONTRIBUTING §9)。"""
    panel = _panel()
    close = panel["close"].to_numpy()
    high = panel["high"].to_numpy()
    low = panel["low"].to_numpy()
    open_ = panel["open"].to_numpy()
    volume = panel["volume"].to_numpy()

    cases: list[tuple[str, np.ndarray]] = [
        ("MA(C,5)", _np_ma(close, 5)),
        ("MA(CLOSE,10)", _np_ma(close, 10)),
        ("REF(C,3)", _np_ref(close, 3)),
        ("HHV(H,7)", _np_roll(high, 7, np.max)),
        ("LLV(L,7)", _np_roll(low, 7, np.min)),
        ("SUM(V,4)", _np_roll(volume, 4, np.sum)),
        # 通达信 STD 是样本标准差 (ddof=1); ddof=0 会差一截, 这里锁死口径
        ("STD(C,10)", _np_std_ddof1(close, 10)),
        ("MA(C,1)", close),
        ("REF(C,0)", close),
        ("ABS(C-REF(C,1))", np.abs(close - _np_ref(close, 1))),
        ("LOG(C)", np.log(close)),
        ("SQRT(C)", np.sqrt(close)),
        ("MAX(H,L)", np.maximum(high, low)),
        ("MIN(H,L)", np.minimum(high, low)),
        ("IF(C>O,1,0)", np.where(close > open_, 1.0, 0.0)),
        ("(C-O)/(H-L+0.0001)", (close - open_) / (high - low + 0.0001)),
        ("MA(C,5)/REF(MA(C,5),1)-1", _np_ma(close, 5) / _np_ref(_np_ma(close, 5), 1) - 1),
    ]
    for formula, reference in cases:
        got = _eval_tdx(formula, panel)
        assert np.allclose(got, reference, equal_nan=True, rtol=1e-9, atol=1e-9), formula


def test_std_uses_sample_stddev_not_population() -> None:
    """口径锁: STD 必须是 ddof=1。若哪天误改成 ddof=0, 这条会红。"""
    panel = _panel(n=30)
    close = panel["close"].to_numpy()
    got = _eval_tdx("STD(C,10)", panel)
    population = _np_roll(close, 10, lambda w: w.std(ddof=0))
    assert not np.allclose(got, population, equal_nan=True), "STD 退化成了总体标准差 (ddof=0)"
    assert np.allclose(got, _np_std_ddof1(close, 10), equal_nan=True, rtol=1e-12)


def test_boolean_null_propagation() -> None:
    """首 bar 的 REF 无值 → 比较结果必须是 null, 不能静默当 False。"""
    panel = _panel(n=20)
    close = panel["close"].to_numpy()
    prev = _np_ref(close, 1)
    valid = ~np.isnan(prev)

    def reference(mask) -> np.ndarray:
        out = np.full(len(close), np.nan)
        out[valid] = np.where(mask(valid), 1.0, 0.0)
        return out

    for formula, mask in (
        ("C>REF(C,1)", lambda v: close[v] > prev[v]),
        ("NOT(C>REF(C,1))", lambda v: ~(close[v] > prev[v])),
        ("C=REF(C,1)", lambda v: close[v] == prev[v]),
        ("C<>REF(C,1)", lambda v: close[v] != prev[v]),
    ):
        assert np.allclose(_eval_tdx(formula, panel), reference(mask), equal_nan=True), formula
    assert np.isnan(_eval_tdx("C>REF(C,1)", panel)[0])


@pytest.mark.parametrize("tdx, equivalent_dsl", [
    ("MA(C,5)", "ts_mean(close, 5)"),
    ("MA(C,5)>REF(MA(C,5),1)", "ts_mean(close, 5) > ts_delay(ts_mean(close, 5), 1)"),
    ("C>HHV(H,20)*0.98 AND V>MA(V,5)",
     "(close > (ts_max(high, 20) * 0.98)) and (volume > ts_mean(volume, 5))"),
    ("IF(C>O,1,0)", "if_else(close > open, 1, 0)"),
    ("STD(C,20)/MA(C,20)", "ts_std(close, 20) / ts_mean(close, 20)"),
    ("NOT(C>REF(C,1))", "not (close > ts_delay(close, 1))"),
    ("MAX(H,L)/MIN(H,L)", "max(high, low) / min(high, low)"),
])
def test_tdx_matches_dsl_bitwise(tdx: str, equivalent_dsl: str) -> None:
    """零分歧锁: 通达信档与等价 DSL 档输出必须逐位相同 (含 NaN 位置)。"""
    panel = _panel()
    assert np.allclose(_eval_tdx(tdx, panel), _eval_dsl(equivalent_dsl, panel), equal_nan=True)


def test_no_cross_symbol_leak() -> None:
    """窗口算子必须 over("symbol"), 不能跨股票串数据。"""
    panel = _two_symbol_panel()
    got = _eval_tdx("MA(C,3)", panel)
    # A: [nan, nan, 11, 12, 13, 14]; B: [nan, nan, 90, 80, 70, 60]
    assert np.isnan(got[0]) and np.isnan(got[1])
    assert got[2] == pytest.approx(11.0)
    assert got[5] == pytest.approx(14.0)
    assert got[8] == pytest.approx(90.0)
    assert got[11] == pytest.approx(60.0)


def test_dependencies_and_warmup_inferred() -> None:
    """依赖与预热从翻译后的 DSL 推断, 与直接写 DSL 完全一致。"""
    compiled = compile_tdx_formula("MA(C,20)/REF(MA(V,10),3)")
    assert compiled.ok
    assert compiled.dependencies == {"close", "volume"}
    assert compiled.warmup_bars == compile_formula(
        "ts_mean(close, 20) / ts_delay(ts_mean(volume, 10), 3)"
    ).warmup_bars
    assert compiled.warmup_bars >= 20  # 至少覆盖最长窗口 (与等价 DSL 完全一致)


def test_degenerate_window_warmup_drops() -> None:
    """MA(C,1) 归一成 close 后没有窗口, warmup 不该白白多要。"""
    assert compile_tdx_formula("MA(C,1)").warmup_bars == compile_formula("close").warmup_bars


def test_formula_text_keeps_user_source() -> None:
    """formula_text 必须是用户写的通达信原文, 不是生成的 DSL (编辑/展示/缓存键都靠它)。"""
    source = "MA(C,5)>REF(MA(C,5),1)"
    compiled = compile_tdx_formula(source)
    assert compiled.formula_text == source
    assert translate_tdx(source).dsl_text != source


def test_cached_variant_agrees() -> None:
    source = "MA(C,5)"
    assert compile_tdx_formula_cached(source).ok is compile_tdx_formula(source).ok
    assert compile_tdx_formula_cached(source).formula_text == source
    # 缓存命中要返回同一对象 (lru_cache 语义)
    assert compile_tdx_formula_cached(source) is compile_tdx_formula_cached(source)


# ------------------------------------------------------------- 错误码

@pytest.mark.parametrize("formula, code", [
    ("", "E301"),
    ("   ", "E301"),
    ("\t\n", "E301"),
    ("MA(C,5", "E303"),                # 缺右括号
    ("MA(C,5))", "E303"),              # 多余记号
    ("C >", "E303"),                   # 意外结束
    ("C @ 5", "E303"),                 # 无法识别的字符
    ("AND C", "E303"),                 # 关键字不能当操作数
    ("A:=MA(C,5);", "E310"),              # 全是中间变量, 没有输出值 (第 3 批)
    ("C:=CLOSE+1; C>REF(C,1)", "E309"),   # 变量名撞列别名
    ("MA:=CLOSE; MA>1", "E309"),          # 变量名撞函数名
    ("A:=A+1; A", "E309"),                # 自引用无法翻译成单表达式
    ("A>REF(B,1); B:=MA(C,5)", "E309"),   # 前向引用: 定义必须写在前面
    ("REF(C,-1)", "E005"),             # 负偏移 = 未来函数 (DSL 门控)
    ("MA(C,-5)", "E004"),              # 负窗口落在窗口纪律里, 不是偏移门控
    ("FOOBAR(C,5)", "E002"),           # 未知函数透传给 DSL
    ("ZZZZ>1", "E001"),                # 未知列透传给 DSL
    ("MA(C,0)", "E004"),               # 窗口下限
    ("MA(C,9999)", "E004"),            # 窗口上限
    ("SUM(C,0)", "E004"),              # 通达信累积和 v1 不支持
    ("MA(C,2.5)", "E004"),             # 窗口必须整数
    ("1+1", "E016"),                   # 常量表达式
])
def test_error_codes(formula: str, code: str) -> None:
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok, formula
    assert compiled.errors[0].code == code, (formula, compiled.errors[0].to_dict())


def test_source_too_long() -> None:
    compiled = compile_tdx_formula("MA(C,5)+" * 900 + "MA(C,5)")
    assert not compiled.ok
    assert compiled.errors[0].code in ("E302", "E007")  # 源码超长或生成的 DSL token 超限


def test_string_literal_rejected() -> None:
    """DSL 没有字符串类型, 字符串常量必须明确拒绝而不是静默丢弃。"""
    compiled = compile_tdx_formula("DRAWTEXT(C>0,H,'买')")
    assert not compiled.ok
    # DRAWTEXT 先命中绘图拒绝 (E305); 单独构造一个映射函数的字符串参数
    assert compiled.errors[0].code == "E305"
    compiled2 = compile_tdx_formula("IF(C>'x',1,0)")
    assert not compiled2.ok
    assert compiled2.errors[0].code == "E307"


def test_never_raises() -> None:
    """编译入口 fail-closed: 任何垃圾输入都返回错误对象, 不抛裸异常。"""
    for junk in ("", " ", ";", ";;", "(", ")", ",", "MA(", "MA(,)", "'''", "{{{", "(*", "\x00", "C" * 5000):
        compiled = compile_tdx_formula(junk)
        assert isinstance(compiled.ok, bool)
        if not compiled.ok:
            assert compiled.errors and compiled.errors[0].code


# ------------------------------------------- 拒绝清单 (逐条 + 不变量锁)

def test_every_rejected_function_is_blocked() -> None:
    """拒绝清单里每个函数都必须被挡住, 且错误码与类别对得上。"""
    for name, (category, reason) in REJECTED_FUNCTIONS.items():
        compiled = compile_tdx_formula(f"{name}(C,5)")
        assert not compiled.ok, f"{name} 竟然编译通过了"
        code = compiled.errors[0].code
        expected = "E304" if category == REJECT_FUTURE else "E305"
        assert code == expected, (name, category, code)
        assert reason in compiled.errors[0].message, name


def test_rejected_functions_case_insensitive() -> None:
    """黑名单不能因为大小写绕过 (通达信标识符大小写不敏感)。"""
    for variant in ("ZIG(C,5)", "zig(c,5)", "Zig(C,5)", "zIg(C,5)"):
        compiled = compile_tdx_formula(variant)
        assert not compiled.ok and compiled.errors[0].code == "E304", variant


def test_future_function_cannot_hide_inside_expression() -> None:
    """未来函数嵌在深层子表达式里也必须被扫出来。"""
    compiled = compile_tdx_formula("MA(C,5) + HHV(ZIG(C,3), 10) * IF(C>O,1,0)")
    assert not compiled.ok
    codes = {e.code for e in compiled.errors}
    assert "E304" in codes


def test_negative_ref_is_future_function() -> None:
    """REF(X,-1) 取的是未来 bar, 必须硬拒 (与 P1-16 的 E005 门控同口径)。"""
    compiled = compile_tdx_formula("REF(C,-1)")
    assert not compiled.ok
    assert compiled.errors[0].code == "E005"
    assert "未来函数" in compiled.errors[0].message


# 不变量锁: 下面几条是「表本身写错了」的防线, 与运行时行为无关

def test_invariant_no_future_function_in_map() -> None:
    """映射表永远不得收录未来函数 —— 否则等于给重绘开了后门。"""
    future = {name for name, (cat, _) in REJECTED_FUNCTIONS.items() if cat == REJECT_FUTURE}
    assert future & set(FUNCTION_MAP) == set()
    assert future & set(COLUMN_ALIASES) == set()


def test_invariant_map_targets_exist_in_dsl() -> None:
    """每个映射目标必须真实存在于 DSL 算子表, 防止改名后静默失效。"""
    missing = {dsl_name for dsl_name in FUNCTION_MAP.values() if dsl_name not in dsl.OPERATORS}
    assert not missing, f"映射到不存在的 DSL 算子: {sorted(missing)}"


def test_invariant_column_aliases_are_base_columns() -> None:
    """列别名必须落在 DSL 的 BASE_COLUMNS 里。"""
    invalid = {v for v in COLUMN_ALIASES.values() if v not in BASE_COLUMNS}
    assert not invalid, f"别名指向非基础列: {sorted(invalid)}"


def test_invariant_reject_and_map_disjoint() -> None:
    """同一个名字不能既被支持又被拒绝 (否则行为取决于查表顺序)。"""
    assert set(FUNCTION_MAP) & set(REJECTED_FUNCTIONS) == set()


def test_invariant_alias_keys_uppercase() -> None:
    """两张表的键必须全大写 —— 查表前会 upper(), 小写键永远命中不了。"""
    assert all(k == k.upper() for k in FUNCTION_MAP), "FUNCTION_MAP 有非大写键"
    assert all(k == k.upper() for k in COLUMN_ALIASES), "COLUMN_ALIASES 有非大写键"
    assert all(k == k.upper() for k in REJECTED_FUNCTIONS), "REJECTED_FUNCTIONS 有非大写键"


def test_compatibility_table_covers_both_tables() -> None:
    """兼容表必须与三张源表同源, 不能漏项 (前端展示靠它)。"""
    table = compatibility_table()
    assert len(table) == len(FUNCTION_MAP) + len(MACRO_ARITY) + len(REJECTED_FUNCTIONS)
    supported = {row["tdx"] for row in table if row["kind"] == "operator"}
    macros = {row["tdx"] for row in table if row["kind"] == "macro"}
    rejected = {row["tdx"] for row in table if row["status"] == "rejected"}
    assert supported == set(FUNCTION_MAP)
    assert macros == set(MACRO_ARITY)
    assert rejected == set(REJECTED_FUNCTIONS)
    assert all(row.get("reason") for row in table if row["status"] == "rejected")
    # 宏没有单一对应算子, 必须给出等价写法, 否则前端那一列是空的
    assert all(row.get("maps_to") for row in table if row["kind"] == "macro")
    # 三类互不重叠: 同名出现在两类里, 前端会展示出自相矛盾的兼容表
    assert not (supported & macros) and not ((supported | macros) & rejected)


# ------------------------------------------------- 报错偏移指回原文

@pytest.mark.parametrize("formula, offset, fragment", [
    ("MA(C,5) + REF(C,-1)", 10, "REF"),
    ("MA(C,5) + FOOBAR(C,3)", 10, "FOOBAR"),
    ("MA(C,5) + ZZZZ", 10, "ZZZZ"),
    ("HHV(H,10) * 2 + MA(C,9999)", 16, "MA"),
    ("MA(C,5) + ZIG(C,3)", 10, "ZIG"),
])
def test_error_offset_points_into_tdx_source(formula: str, offset: int, fragment: str) -> None:
    """用户看到的偏移必须落在**他写的通达信原文**上, 不是生成的中间 DSL 文本。"""
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok
    error = compiled.errors[0]
    assert error.offset == offset, (formula, error.to_dict())
    assert formula[offset:].startswith(fragment)


# ------------------------------------------------- 存储 / 物化链路

def test_store_roundtrip_lang_tdx() -> None:
    """to_spec 必须认 lang=tdx, 且 formula_text 存原文、依赖/预热正确。"""
    spec = to_spec({
        "id": "uf_tdx_a", "label": "通达信均线", "kind": "custom", "lang": "tdx",
        "formula": "MA(C,20)>REF(MA(C,20),1)", "status": "active",
    })
    try:
        assert spec.lang == "tdx"
        assert spec.formula_text == "MA(C,20)>REF(MA(C,20),1)"
        assert spec.dependencies == frozenset({"close"})
        assert spec.warmup_bars >= 21
        assert spec.stability == "stable"
    finally:
        unregister_factor("uf_tdx_a")


def test_store_rejects_unknown_lang() -> None:
    with pytest.raises(ValueError, match="未知 lang"):
        to_spec({"id": "uf_tdx_b", "label": "x", "kind": "custom", "lang": "tdxx", "formula": "MA(C,5)"})


def test_store_rejects_invalid_tdx_formula() -> None:
    """编译不过的通达信公式必须在注册期就 fail-closed, 且错误码可见。"""
    with pytest.raises(ValueError, match="E304"):
        to_spec({"id": "uf_tdx_c", "label": "x", "kind": "custom", "lang": "tdx", "formula": "ZIG(C,5)"})
    with pytest.raises(ValueError, match="E310"):
        to_spec({"id": "uf_tdx_d", "label": "x", "kind": "custom", "lang": "tdx", "formula": "A:=MA(C,5);"})


def test_register_and_materialize_chain() -> None:
    """注册 → scoring 物化: 通达信因子必须能真的算出列 (端到端)。"""
    from app.strategy.scoring import materialize_scoring_columns

    register_definition({
        "id": "uf_tdx_mat", "label": "通达信物化", "kind": "custom", "lang": "tdx",
        "formula": "MA(C,5)/REF(MA(C,5),1)-1", "status": "active",
    })
    try:
        panel = _panel(n=40)
        out = materialize_scoring_columns(panel, ["uf_tdx_mat"])
        assert "uf_tdx_mat" in out.columns
        got = out["uf_tdx_mat"].to_numpy()
        close = panel["close"].to_numpy()
        reference = _np_ma(close, 5) / _np_ref(_np_ma(close, 5), 1) - 1
        assert np.allclose(got, reference, equal_nan=True, rtol=1e-9)
        # 物化结果必须与直接编译一致 (走的是同一条 cached 编译路径)
        assert np.allclose(got, _eval_tdx("MA(C,5)/REF(MA(C,5),1)-1", panel), equal_nan=True)
    finally:
        unregister_factor("uf_tdx_mat")


def test_tdx_factor_usable_as_signal_operand() -> None:
    """因子 id 是自定义信号的合法操作数 (因子→信号→回测链路的入口)。"""
    from app.strategy.custom_signals import allowed_fields

    register_definition({
        "id": "uf_tdx_sig", "label": "信号用", "kind": "custom", "lang": "tdx",
        "formula": "MA(C,5)", "status": "active",
    })
    try:
        assert "uf_tdx_sig" in allowed_fields()
    finally:
        unregister_factor("uf_tdx_sig")


# ------------------------------------------- DSL 侧 `not` 回归 (本批附带修复)

def test_dsl_unary_not_supported() -> None:
    """`not` 此前在 _KEYWORDS 里却没有产生式 (死关键字); 本批补齐, 锁住不回退。"""
    panel = _two_symbol_panel()
    got = _eval_dsl("not (close > 12)", panel)
    # A: close=[10..15] → close>12 为 [F,F,F,T,T,T] → 取反 [1,1,1,0,0,0]
    # B: close=[100..50] → 全部 >12 → 取反后全 0
    assert list(got[:6]) == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    assert list(got[6:]) == [0.0] * 6


def test_dsl_unary_not_composes() -> None:
    panel = _two_symbol_panel()
    assert list(_eval_dsl("not not (close > 12)", panel)[:6]) == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]
    assert list(_eval_dsl("not not (close > 12)", panel)[6:]) == [1.0] * 6
    # close>11 为 [F,F,T,T,T,T] (11>11 是 False), not(close>13) 为 [T,T,T,T,F,F]
    # → and = [F,F,T,T,F,F]
    assert list(_eval_dsl("close > 11 and not (close > 13)", panel)[:6]) == [0.0, 0.0, 1.0, 1.0, 0.0, 0.0]


def test_dsl_unary_minus_unaffected() -> None:
    """补 not 不能碰坏原有的一元负号。"""
    panel = _two_symbol_panel()
    assert list(_eval_dsl("-close", panel)[:3]) == [-10.0, -11.0, -12.0]
    assert list(_eval_dsl("-(close)", panel)[:3]) == [-10.0, -11.0, -12.0]


# ------------------------------------------------- API 层 / 端到端链路

@pytest.fixture()
def cleanup_registry():
    """用例后注销测试注册的因子, 不污染全局注册表 (快照测试依赖固定基线)。"""
    created: set[str] = set()
    yield created
    for fid in created:
        unregister_factor(fid)


class _FakeEngine:
    """合成面板: 单只股票缓涨, 供保存前试算 (与 test_factor_api 同形)。"""

    def __init__(self, n_days: int = 40) -> None:
        rows = []
        end = date.today()
        for index in range(n_days):
            day = end - timedelta(days=n_days - 1 - index)
            close = 10.0 * 1.01 ** index
            rows.append({
                "symbol": "A", "date": day, "open": close, "high": close * 1.01,
                "low": close * 0.99, "close": close, "volume": 1e6 + index * 1e4,
                "amount": close * 1e6, "turnover_rate": 1.5,
            })
        self.panel = pl.DataFrame(rows).sort(["symbol", "date"])

    def load_panel(self, symbols, start, end, *, columns=None, asset_type="stock", **_kwargs):
        frame = self.panel
        if columns is not None:
            for column in columns:
                if column not in frame.columns:
                    frame = frame.with_columns(pl.lit(None).cast(pl.Float64).alias(column))
            frame = frame.select(columns)
        return frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _client(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.factors import router

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def test_api_validate_lang_tdx() -> None:
    response = _client(None).post("/api/factors/validate", json={"formula": "MA(C,5)>REF(MA(C,5),1)", "lang": "tdx"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["dependencies"] == ["close"]
    assert payload["warmup_bars"] >= 6


def test_api_validate_rejects_unknown_lang() -> None:
    response = _client(None).post("/api/factors/validate", json={"formula": "MA(C,5)", "lang": "excel"})
    assert response.status_code == 422  # pydantic pattern 挡住


def test_api_validate_tdx_error_payload() -> None:
    """校验接口要把通达信错误码与原文偏移透出去, 前端才能定位。"""
    response = _client(None).post("/api/factors/validate", json={"formula": "MA(C,5)+ZIG(C,3)", "lang": "tdx"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "E304"
    # `MA(C,5)+ZIG(C,3)` 里 ZIG 起于第 8 个字符 —— 偏移必须指回通达信原文
    assert payload["errors"][0]["position"]["offset"] == 8


def test_api_create_persists_lang_tdx(tmp_path, cleanup_registry) -> None:
    """端到端: 建因子 → 落盘带 lang=tdx + 原文 → 注册表 spec 正确 → 能物化。"""
    from app.factors import store
    from app.factors.registry import get_factor
    from app.strategy.scoring import materialize_scoring_columns

    client = _client(tmp_path)
    source = "MA(C,5)/REF(MA(C,5),1)-1"
    created = client.post("/api/factors/custom", json={
        "id": "uf_tdx_api", "label": "通达信动量", "formula": source, "lang": "tdx",
    })
    assert created.status_code == 200, created.text
    cleanup_registry.add(created.json()["id"])

    on_disk = next(d for d in store.load_all(tmp_path) if d["id"] == "uf_tdx_api")
    assert on_disk["lang"] == "tdx"
    assert on_disk["formula"] == source          # 存原文, 不是生成的 DSL

    store.load_into_registry(tmp_path)
    spec = get_factor("uf_tdx_api")
    assert spec is not None and spec.lang == "tdx" and spec.formula_text == source

    panel = _panel(n=40)
    out = materialize_scoring_columns(panel, ["uf_tdx_api"])
    assert "uf_tdx_api" in out.columns
    close = panel["close"].to_numpy()
    assert np.allclose(
        out["uf_tdx_api"].to_numpy(),
        _np_ma(close, 5) / _np_ref(_np_ma(close, 5), 1) - 1,
        equal_nan=True, rtol=1e-9,
    )


def test_api_create_rejects_future_function(tmp_path, cleanup_registry) -> None:
    """未来函数必须在保存门禁就被挡住, 不能落盘 (fail-closed)。"""
    response = _client(tmp_path).post("/api/factors/custom", json={
        "id": "uf_tdx_bad", "label": "重绘", "formula": "ZIG(C,5)>0.02", "lang": "tdx",
    })
    assert response.status_code == 400
    assert "E304" in response.text
    from app.factors import store
    assert not [d for d in store.load_all(tmp_path) if d["id"] == "uf_tdx_bad"]


def test_api_create_accepts_multi_statement(tmp_path, cleanup_registry) -> None:
    """第 3 批: 带 `:=` 中间变量的多语句公式必须能建, 且落盘存的是用户原文。"""
    from app.factors import store

    source = "A:=MA(C,5); A>REF(A,1)"
    created = _client(tmp_path).post("/api/factors/custom", json={
        "id": "uf_tdx_ms", "label": "多语句", "formula": source, "lang": "tdx",
    })
    assert created.status_code == 200, created.text
    cleanup_registry.add(created.json()["id"])
    on_disk = next(d for d in store.load_all(tmp_path) if d["id"] == "uf_tdx_ms")
    assert on_disk["formula"] == source      # 存原文, 不是内联展开后的 DSL


def test_api_create_rejects_output_less_formula(tmp_path, cleanup_registry) -> None:
    """全是 `:=` 没有输出值 → E310, 不能落盘 (通达信这种公式也不画线)。"""
    response = _client(tmp_path).post("/api/factors/custom", json={
        "id": "uf_tdx_noout", "label": "无输出", "formula": "A:=MA(C,5);", "lang": "tdx",
    })
    assert response.status_code == 400
    assert "E310" in response.text
    from app.factors import store
    assert not [d for d in store.load_all(tmp_path) if d["id"] == "uf_tdx_noout"]


def test_api_update_keeps_lang_when_not_sent(tmp_path, cleanup_registry) -> None:
    """编辑时不传 lang 必须沿用已存语言, 不能把 tdx 因子静默降级成 dsl。"""
    from app.factors import store

    client = _client(tmp_path)
    created = client.post("/api/factors/custom", json={
        "id": "uf_tdx_keep", "label": "保持语言", "formula": "MA(C,5)", "lang": "tdx",
    })
    assert created.status_code == 200, created.text
    cleanup_registry.add("uf_tdx_keep")

    updated = client.post("/api/factors/custom/uf_tdx_keep/update", json={
        "label": "保持语言", "formula": "MA(C,10)",   # 故意不带 lang
    })
    assert updated.status_code == 200, updated.text
    on_disk = next(d for d in store.load_all(tmp_path) if d["id"] == "uf_tdx_keep")
    assert on_disk["lang"] == "tdx"
    assert on_disk["formula"] == "MA(C,10)"


def test_tdx_factor_flows_into_custom_signal() -> None:
    """因子 → 自定义信号 → 可求值表达式: 通达信因子能真的当信号条件用。"""
    from app.strategy import custom_signals

    register_definition({
        "id": "uf_tdx_sig2", "label": "信号条件用", "kind": "custom", "lang": "tdx",
        "formula": "MA(C,5)/REF(MA(C,5),1)-1", "status": "active",
    })
    try:
        assert "uf_tdx_sig2" in custom_signals.allowed_fields()
        sig = {
            "id": "tdx_momentum", "name": "通达信动量", "kind": "entry", "enabled": True,
            "conditions": [{"left": "uf_tdx_sig2", "op": ">", "right": "0",
                            "leftDays": 0, "rightDays": 0}],
        }
        custom_signals.validate(sig)  # 不抛错即通过白名单
        exprs = custom_signals.build_expressions([sig])
        column = custom_signals.column_name("tdx_momentum")
        assert column in exprs

        # 物化因子列后表达式必须能真的算出布尔结果
        panel = _panel(n=40)
        prepared = custom_signals.materialize_factor_columns(panel, exprs)
        assert "uf_tdx_sig2" in prepared.columns
        out = prepared.with_columns(exprs[column].alias("_sig"))
        factor = out["uf_tdx_sig2"].to_numpy()
        signal = out["_sig"].to_list()
        # 信号必须逐 bar 等于「因子 > 0」, 且暖机期为 null (不能静默当 False)
        expected = [None if np.isnan(v) else bool(v > 0) for v in factor]
        assert signal == expected
        assert any(v is True for v in signal), "信号从未触发, 链路可能断了"
        assert any(v is False for v in signal), "信号恒为真, 比较可能没生效"
        # 信号列名带 csg_ 前缀 → 回测 _normalize_signal_name 认它
        assert column.startswith("csg_")
    finally:
        unregister_factor("uf_tdx_sig2")


# ============================================ 第 2 批: 递归算子与结构展开宏
#
# 新增 9 个通达信函数, 分两类:
#   A 类 (映射到新 DSL 算子): EMA SMA BARSLAST BARSSINCE
#   B 类 (结构展开宏, 不新增算子): CROSS LONGCROSS COUNT EVERY EXIST
# 数值口径全部与**独立手写参考**对照, 参考不复用被测代码。

def _np_ewm(x: np.ndarray, alpha: float) -> np.ndarray:
    """指数平滑独立参考: Y0=X0, Yt=a*Xt+(1-a)*Y(t-1)。通达信 EMA/SMA 的口径。"""
    out = np.full(len(x), np.nan)
    prev = np.nan
    for i, v in enumerate(x):
        if np.isnan(v):
            continue                      # null 不推进递归 (对齐 ewm_mean(ignore_nulls=True))
        prev = v if np.isnan(prev) else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def _np_count(cond: np.ndarray, n: int) -> np.ndarray:
    """COUNT 独立参考: 最近 n 根 (含当根) 里条件成立的次数; 窗口不满 → NaN。"""
    out = np.full(len(cond), np.nan)
    for i in range(n - 1, len(cond)):
        out[i] = float(cond[i - n + 1:i + 1].sum())
    return out


def _np_barslast(cond: np.ndarray, first: bool = False) -> np.ndarray:
    """BARSLAST/BARSSINCE 独立参考。first=True 时数首次成立至今。"""
    out = np.full(len(cond), np.nan)
    mark = None
    for i, v in enumerate(cond):
        if v and (mark is None or not first):
            mark = i
        if mark is not None:
            out[i] = float(i - mark)
    return out


def _null_panel() -> pl.DataFrame:
    """带空洞的面板: 前导 null + 中段 null, 锁递归算子的 null 口径。

    通达信本身没有 null 概念 (只有「未上市/停牌无数据」), 但我们的面板有 ——
    预热期和数据缺失都会产生 null, 所以必须明确定义行为。
    """
    n = 20
    close: list[float | None] = [float(100 + i) for i in range(n)]
    close[0] = close[1] = None      # 前导空洞
    close[9] = close[10] = None     # 中段空洞
    return pl.DataFrame({
        "symbol": ["S"] * n,
        "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": [float(100 + i) for i in range(n)],
        "high": [float(102 + i) for i in range(n)],
        "low": [float(98 + i) for i in range(n)],
        "close": close,
        "volume": [1e5] * n, "amount": [1e6] * n, "turnover_rate": [1.0] * n,
    }, schema={"symbol": pl.Utf8, "date": pl.Date, "open": pl.Float64,
               "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
               "volume": pl.Float64, "amount": pl.Float64, "turnover_rate": pl.Float64})


def _cross_panel() -> pl.DataFrame:
    """专门造「两线先相等、下一根上穿」的面板 —— 锁 CROSS 的 <= 口径。"""
    n = 12
    fast = [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 10.0, 10.0, 10.5, 9.0]
    slow = [11.0, 10.9, 10.8, 10.7, 10.6, 10.5, 10.4, 10.3, 10.2, 10.0, 10.0, 10.0]
    # 第 9 根两线**恰好相等**, 第 10 根 fast 反超 → CROSS 必须在第 10 根触发
    return pl.DataFrame({
        "symbol": ["S"] * n,
        "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": slow, "high": [max(a, b) + 1 for a, b in zip(fast, slow, strict=True)],
        "low": [min(a, b) - 1 for a, b in zip(fast, slow, strict=True)],
        "close": fast, "volume": [1e5] * n, "amount": [1e6] * n,
        "turnover_rate": [1.0] * n,
    })


# ------------------------------------------------- A 类: 翻译文本

@pytest.mark.parametrize("tdx, expected_dsl", [
    ("EMA(C,5)", "ts_ema(close, 5)"),
    ("SMA(C,10,2)", "ts_sma(close, 10, 2)"),
    ("BARSLAST(C>O)", "ts_barslast(close > open)"),
    ("BARSSINCE(C>O)", "ts_barssince(close > open)"),
    # 退化归一: alpha=1 时递归退化成原序列
    ("EMA(C,1)", "close"),
    ("SMA(C,1,1)", "close"),
    # 嵌套也要能翻译
    ("EMA(MA(C,5),10)", "ts_ema(ts_mean(close, 5), 10)"),
    ("BARSLAST(CROSS(MA(C,5),MA(C,10)))",
     "ts_barslast(((ts_mean(close, 5)) > (ts_mean(close, 10)) and "
     "ts_delay(ts_mean(close, 5), 1) <= ts_delay(ts_mean(close, 10), 1)))"),
])
def test_batch2_translation_text(tdx: str, expected_dsl: str) -> None:
    result = translate_tdx(tdx)
    assert result.ok, [e.to_dict() for e in result.errors]
    assert result.dsl_text == expected_dsl


@pytest.mark.parametrize("tdx, expected_dsl", [
    ("CROSS(C,O)",
     "((close) > (open) and ts_delay(close, 1) <= ts_delay(open, 1))"),
    ("COUNT(C>O,10)",
     "ts_sum(if_else(close > open, 1, 0), 10)"),
    ("EVERY(C>O,5)",
     "(ts_sum(if_else(close > open, 1, 0), 5) == 5)"),
    ("EXIST(C>O,5)",
     "(ts_sum(if_else(close > open, 1, 0), 5) > 0)"),
    # N==1 退化: DSL 窗口下限是 2, 不归一会 E004 误拒
    ("COUNT(C>O,1)", "if_else(close > open, 1, 0)"),
    ("EVERY(C>O,1)", "(if_else(close > open, 1, 0) == 1)"),
    ("EXIST(C>O,1)", "(if_else(close > open, 1, 0) > 0)"),
])
def test_batch2_macro_translation_text(tdx: str, expected_dsl: str) -> None:
    result = translate_tdx(tdx)
    assert result.ok, [e.to_dict() for e in result.errors]
    assert result.dsl_text == expected_dsl


def test_macro_composite_expansion_is_parenthesized() -> None:
    """展开成复合表达式的宏必须自带外层括号。

    _precedence() 对 call 节点一律返回原子级, 上层不会替它补括号 ——
    少了这层括号, `NOT CROSS(A,B)` 会被解析成 `(not A) > B and ...`, 语义全错。
    """
    result = translate_tdx("NOT CROSS(C,O)")
    assert result.ok, [e.to_dict() for e in result.errors]
    assert result.dsl_text.startswith("not ("), result.dsl_text
    # 括号必须包住整个 and 表达式, 而不是只包住左半边
    assert result.dsl_text.endswith("))"), result.dsl_text
    # 反证: 去掉外层括号后语义确实变了 (not 只作用于第一个比较)
    unparenthesized = result.dsl_text[len("not "):].strip()[1:-1]
    assert compile_formula(unparenthesized).ok


def test_macro_as_and_operand_keeps_grouping() -> None:
    """宏当 and/or 的操作数时, 分组不能被拆散。"""
    result = translate_tdx("CROSS(C,O) AND C>REF(C,1)")
    assert result.ok
    assert result.dsl_text == (
        "((close) > (open) and ts_delay(close, 1) <= ts_delay(open, 1))"
        " and close > ts_delay(close, 1)"
    ), result.dsl_text
    # 语义对照: 必须等于「CROSS 参考」与「C>REF(C,1)」的逐元素与
    panel = _panel()
    close = panel["close"].to_numpy()
    open_ = panel["open"].to_numpy()
    expected = [_k_and(c, bool(close[i] > close[i - 1]) if i else None)
                for i, c in enumerate(_np_cross3(close, open_))]
    assert _bool_or_none(_eval_tdx("CROSS(C,O) AND C>REF(C,1)", panel)) == expected


# ------------------------------------------------- A 类: 数值对照

@pytest.mark.parametrize("tdx, alpha", [
    ("EMA(C,5)", 2.0 / 6), ("EMA(C,12)", 2.0 / 13), ("EMA(C,60)", 2.0 / 61),
    ("SMA(C,10,2)", 2.0 / 10), ("SMA(C,12,3)", 3.0 / 12), ("SMA(C,9,9)", 1.0),
])
def test_ema_sma_match_recursive_reference(tdx: str, alpha: float) -> None:
    """EMA/SMA 必须逐 bar 等于独立手写的递归参考 (不是简单移动平均!)。"""
    close = _panel()["close"].to_numpy()
    got = _eval_tdx(tdx)
    expected = _np_ewm(close, alpha)
    mask = ~np.isnan(expected)
    assert mask.any(), "参考全 NaN, 对照无意义"
    np.testing.assert_allclose(got[mask], expected[mask], rtol=1e-12, atol=1e-12)
    assert np.all(np.isnan(got[~mask])), "参考为 NaN 处实现却有值"


def test_sma_is_not_simple_moving_average() -> None:
    """口径锁: 通达信 SMA(X,N,M) 是**指数平滑**, 不是 N 期简单均线。

    名字极具误导性 —— 这条测试防止后人"顺手修正"成 ts_mean。
    """
    close = _panel()["close"].to_numpy()
    sma = _eval_tdx("SMA(C,10,1)")
    ma = _np_ma(close, 10)
    mask = ~np.isnan(ma)
    assert np.nanmax(np.abs(sma[mask] - ma[mask])) > 1e-6, "SMA 竟然等于 MA, 口径被改坏了"


def test_ema_equals_sma_with_m2_n_plus_1() -> None:
    """口径锁: EMA(X,N) ≡ SMA(X,N+1,2) (alpha 都是 2/(N+1))。"""
    np.testing.assert_allclose(_eval_tdx("EMA(C,10)"), _eval_tdx("SMA(C,11,2)"), rtol=1e-12)


@pytest.mark.parametrize("tdx, alpha", [
    ("EMA(C,5)", 2.0 / 6), ("EMA(C,12)", 2.0 / 13),
    ("SMA(C,10,2)", 2.0 / 10), ("SMA(C,9,9)", 1.0),
])
def test_ema_sma_null_does_not_advance_recursion(tdx: str, alpha: float) -> None:
    """口径锁: null bar **不推进**递归, 且该 bar 输出 null。

    对应 polars 的 ewm_mean(ignore_nulls=True)。若误用 ignore_nulls=False,
    null 会被当成一个真实观测参与平滑, 空洞之后的整条曲线都会偏移 ——
    而空洞在预热期和停牌数据里非常常见, 错了是系统性偏差。
    """
    panel = _null_panel()
    close = panel["close"].to_numpy()
    assert np.isnan(close[0]) and np.isnan(close[9]), "面板必须真的有空洞"
    got = _eval_tdx(tdx, panel)
    expected = _np_ewm(close, alpha)
    mask = ~np.isnan(expected)
    assert mask.any()
    np.testing.assert_allclose(got[mask], expected[mask], rtol=1e-12, atol=1e-12)
    # 空洞处必须输出 null, 不能拿前值填充冒充有数据
    assert np.all(np.isnan(got[~mask])), f"{tdx} 在 null bar 上给出了值"
    # 空洞之后递归要接着走 (不是从空洞处重新起算)
    assert not np.isnan(got[11]), "空洞之后曲线断了"


def test_ema_null_semantics_differ_from_zero_fill() -> None:
    """反证: 把 null 当 0 参与平滑会得到完全不同的曲线, 证明上面那条不是空转。"""
    panel = _null_panel()
    close = panel["close"].to_numpy()
    got = _eval_tdx("EMA(C,5)", panel)
    zero_filled = _np_ewm(np.nan_to_num(close, nan=0.0), 2.0 / 6)
    mask = ~np.isnan(got)
    assert np.nanmax(np.abs(got[mask] - zero_filled[mask])) > 1.0, "null 被当成 0 了"


def test_barslast_null_condition_is_not_a_hit() -> None:
    """null 条件不算命中: BARSLAST 要跨过空洞继续数上一次真命中的距离。"""
    panel = _null_panel()
    got = _eval_tdx("BARSLAST(C>105)", panel)
    close = panel["close"].to_list()
    expected = _np_barslast(np.array([bool(v is not None and v > 105) for v in close]))
    mask = ~np.isnan(expected)
    np.testing.assert_allclose(got[mask], expected[mask], rtol=0, atol=0)
    # 空洞当根 (close 为 null) 条件为假, 计数继续递增而不是归零
    assert got[10] == got[8] + 2, (got[8], got[10])


@pytest.mark.parametrize("tdx, n", [("EMA(C,5)", 5), ("EMA(C,12)", 12), ("SMA(C,10,2)", 10)])
def test_recursive_warmup_is_quadruple_span(tdx: str, n: int) -> None:
    """递归算子有无限记忆, warmup 必须放大到 4n 而不是通用的 n+1。

    残差权重 (1-alpha)^k, k=4n 时约 e^-8 ≈ 3.4e-4, 再加大收益极小。
    """
    assert compile_tdx_formula(tdx).warmup_bars == 4 * n


@pytest.mark.parametrize("tdx, expected", [
    ("BARSLAST(C>O)", 250),
    ("BARSSINCE(C>O)", 250),
    # CROSS 展开后含 ts_delay(...,1): 250 根扫描窗口里每根还要再往前多看 1 根,
    # 所以地板要**加上**条件的回看 (第 3 批暖机口径: 沿路径求和), 不是停在 250。
    ("BARSLAST(CROSS(C,O))", 251),
])
def test_barslast_warmup_has_floor(tdx: str, expected: int) -> None:
    """BARSLAST/BARSSINCE 回看距离无上界, warmup 必须给到 250 的地板。"""
    assert compile_tdx_formula(tdx).warmup_bars == expected


def test_barslast_floor_does_not_shrink_larger_window() -> None:
    """地板只能抬高不能压低: 同式里有更长窗口时取大的那个。"""
    assert compile_tdx_formula("BARSLAST(C>O) + MA(C,300)").warmup_bars == 301


def test_barslast_barssince_match_reference() -> None:
    panel = _panel()
    close = panel["close"].to_numpy()
    open_ = panel["open"].to_numpy()
    cond = close > open_
    for tdx, expected in (("BARSLAST(C>O)", _np_barslast(cond)),
                          ("BARSSINCE(C>O)", _np_barslast(cond, first=True))):
        got = _eval_tdx(tdx, panel)
        mask = ~np.isnan(expected)
        assert mask.any()
        np.testing.assert_allclose(got[mask], expected[mask], rtol=0, atol=0)
        assert np.all(np.isnan(got[~mask])), f"{tdx} 在首次成立前就该是 null"


def test_barslast_counts_up_between_hits() -> None:
    """语义锁: 命中当根是 0, 之后逐根 +1, 再次命中归零。"""
    got = _eval_tdx("BARSLAST(C>O)", _panel())
    values = [v for v in got.tolist() if not np.isnan(v)]
    assert 0.0 in values, "从没命中过, 测试数据不合适"
    for previous, current in pairwise(values):
        assert current == 0.0 or current == previous + 1.0, (previous, current)


def test_barslast_accepts_non_boolean_condition() -> None:
    """通达信里非零即真, BARSLAST(C) 这种写法不能报错。

    布尔强制发生在 DSL 侧 (_compile_call 给非布尔入参补 `!= 0`), 翻译层原样
    透传 —— 所以这里锁**语义**而不是锁文本。
    """
    result = compile_tdx_formula("BARSLAST(C)")
    assert result.ok, [e.to_dict() for e in result.errors]
    assert translate_tdx("BARSLAST(C)").dsl_text == "ts_barslast(close)"
    # 价格恒非零 → 每根都算命中 → BARSLAST 恒为 0
    got = _eval_tdx("BARSLAST(C)", _panel())
    assert np.nanmin(got) == 0.0 and np.nanmax(got) == 0.0, got[:10]
    # 反证: 恒假条件则永远没有命中, 全程 null
    always_false = _eval_tdx("BARSLAST(C<0)", _panel())
    assert np.all(np.isnan(always_false))


def test_barssince_never_resets() -> None:
    """BARSSINCE 数的是**首次**成立至今, 必须单调不减 (与 BARSLAST 相反)。"""
    got = _eval_tdx("BARSSINCE(C>O)", _panel())
    values = [v for v in got.tolist() if not np.isnan(v)]
    assert all(b >= a for a, b in pairwise(values)), values


# ------------------------------------------------- B 类: 数值对照

def test_cross_matches_reference() -> None:
    panel = _panel()
    got = _bool_or_none(_eval_tdx("CROSS(C,O)", panel))
    expected = _np_cross3(panel["close"].to_numpy(), panel["open"].to_numpy())
    assert got == expected
    assert any(v is True for v in expected), "测试数据里一次上穿都没有, 对照无意义"


def test_cross_fires_when_previous_bar_is_exactly_equal() -> None:
    """口径锁: 前一根**恰好相等**也算上穿 (通达信用 <=, 不是 <)。

    均线金叉前一刻两线常常正好相等, 用 < 会漏掉这一天 —— 这是最常见的
    金叉写法, 漏了等于信号系统性偏少。
    """
    panel = _cross_panel()
    got = _bool_or_none(_eval_tdx("CROSS(C,O)", panel))
    fast = panel["close"].to_list()
    slow = panel["open"].to_list()
    assert fast[9] == slow[9], "测试面板第 9 根必须两线相等"
    assert fast[10] > slow[10], "测试面板第 10 根必须反超"
    assert got[10] is True, "相等后上穿没触发 —— <= 被改成 < 了"
    assert got[9] is not True, "相等当根不该算上穿"


def test_cross_strict_less_would_miss_the_equal_case() -> None:
    """反证: 把 <= 换成 < 就会漏掉上面那个信号, 证明这条口径是必需的。"""
    panel = _cross_panel()
    strict = translate_tdx("CROSS(C,O)").dsl_text.replace("<=", "<")
    compiled = compile_formula(strict)
    assert compiled.ok and compiled.frame_transform is not None
    frame = compiled.frame_transform(panel.sort(["symbol", "date"]))
    assert frame is not None
    got = _bool_or_none(frame[FACTOR_COLUMN].to_numpy())
    assert got[10] is not True, "严格 < 竟然也触发了, 反证不成立"


def test_cross_never_fires_on_first_bar() -> None:
    """第 0 根没有前一根, 绝不能凭空报出一次上穿。

    具体落点是 False 还是 null 取决于 Kleene 吸收 (当根 A>B 为假时
    `False and 未知 = False`, 为真时 `True and 未知 = 未知`)。两者都不会
    产生信号, 所以这里锁「不为真」而不是锁死某一种 —— 锁死会把一个
    依赖数据的巧合当成契约。
    """
    for panel in (_panel(), _cross_panel()):
        got = _bool_or_none(_eval_tdx("CROSS(C,O)", panel))
        assert got[0] is not True, got[:3]
        assert bool(got[0] is False) == (panel["close"][0] <= panel["open"][0])


def test_longcross_requires_n_bars_below() -> None:
    """LONGCROSS(A,B,N) = 上穿 + 上穿**之前**连续 N 根都在下方。

    前置窗口不满时结果是三值的: CROSS 已确定为假 → 假 (Kleene 吸收),
    CROSS 为真但前置未知 → null。参考实现必须同样三值, 否则预热期对不上。
    """
    panel = _panel()
    fast = panel["close"].to_numpy()
    slow = panel["open"].to_numpy()
    for n in (3, 5):
        got = _bool_or_none(_eval_tdx(f"LONGCROSS(C,O,{n})", panel))
        assert got == _np_longcross3(fast, slow, n), f"N={n}"


def test_longcross_is_strict_subset_of_cross() -> None:
    """LONGCROSS 只能过滤 CROSS, 不能凭空多出信号; N 越大越严。"""
    panel = _panel()
    def fired(formula: str) -> set[int]:
        return {i for i, v in enumerate(_bool_or_none(_eval_tdx(formula, panel))) if v is True}

    cross, short, long_ = fired("CROSS(C,O)"), fired("LONGCROSS(C,O,3)"), fired("LONGCROSS(C,O,5)")
    assert cross, "测试数据里一次上穿都没有"
    assert short <= cross and long_ <= cross
    assert long_ <= short
    assert len(short) < len(cross), "LONGCROSS 没过滤掉任何信号, 前置条件可能没生效"


def test_longcross_warmup_covers_precondition_window() -> None:
    """warmup 必须覆盖「前置 N 根 + 上穿要看的 1 根 + 均线窗口」。"""
    warmup = compile_tdx_formula("LONGCROSS(MA(C,5),MA(C,10),3)").warmup_bars
    assert warmup >= 5 + 3 + 1, warmup


@pytest.mark.parametrize("n", [1, 2, 3, 8])
def test_longcross_small_and_large_n_match_reference(n: int) -> None:
    """N 取边界值也要对: N=1 走 ts_sum 退化路径, N=8 在测试数据里一次都不触发。"""
    panel = _panel()
    got = _bool_or_none(_eval_tdx(f"LONGCROSS(C,O,{n})", panel))
    assert got == _np_longcross3(panel["close"].to_numpy(), panel["open"].to_numpy(), n), f"N={n}"


@pytest.mark.parametrize("formula", [
    "CROSS(ZIG(C,3),O)", "COUNT(BACKSET(C,5)>0,10)",
    "EVERY(C>PEAK(C,3),5)", "BARSLAST(C>TROUGH(C,3))",
    "LONGCROSS(MA(C,5),ZIG(C,3),3)",
])
def test_future_function_inside_macro_argument_is_blocked(formula: str) -> None:
    """宏会把参数复制多份展开, 未来函数不能因此在扫描里漏网。"""
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok, formula
    assert "E304" in {e.code for e in compiled.errors}, [e.to_dict() for e in compiled.errors]


@pytest.mark.parametrize("formula", [
    "COUNT(C>O,9999)", "EVERY(C>O,0)", "EXIST(C>O,-3)",
    "LONGCROSS(C,O,9999)", "EMA(C,9999)",
])
def test_batch2_window_bounds_enforced(formula: str) -> None:
    """宏展开出的窗口同样受 DSL 的 [2,512] 纪律约束, 不能绕过。"""
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok, formula
    assert compiled.errors[0].code == "E004", [e.to_dict() for e in compiled.errors]


def test_count_every_exist_match_reference() -> None:
    panel = _panel()
    cond = panel["close"].to_numpy() > panel["open"].to_numpy()
    for n in (2, 5, 10):
        counted = _np_count(cond, n)
        got_count = _eval_tdx(f"COUNT(C>O,{n})", panel)
        mask = ~np.isnan(counted)
        np.testing.assert_allclose(got_count[mask], counted[mask], rtol=0, atol=0)
        assert np.all(np.isnan(got_count[~mask])), f"COUNT(·,{n}) 窗口不满时该是 null"

        got_every = _eval_tdx(f"EVERY(C>O,{n})", panel)
        np.testing.assert_array_equal(
            got_every[mask].astype(bool), (counted[mask] == n))
        assert np.all(np.isnan(got_every[~mask]))

        got_exist = _eval_tdx(f"EXIST(C>O,{n})", panel)
        np.testing.assert_array_equal(
            got_exist[mask].astype(bool), (counted[mask] > 0))
        assert np.all(np.isnan(got_exist[~mask]))


def test_every_implies_exist() -> None:
    """恒等式锁: EVERY 为真必然 EXIST 为真, 且 COUNT==N。"""
    panel = _panel()
    every = _eval_tdx("EVERY(C>O,5)", panel)
    exist = _eval_tdx("EXIST(C>O,5)", panel)
    count = _eval_tdx("COUNT(C>O,5)", panel)
    for e, x, c in zip(every, exist, count, strict=True):
        if np.isnan(e):
            continue
        if e:
            assert x and c == 5.0


def test_count_window_one_degenerates_correctly() -> None:
    """N==1 退化后语义必须与 N==2 的口径一致 (只是窗口短一根)。"""
    panel = _panel()
    got = _eval_tdx("COUNT(C>O,1)", panel)
    expected = (panel["close"].to_numpy() > panel["open"].to_numpy()).astype(float)
    np.testing.assert_allclose(got, expected, rtol=0, atol=0)
    assert not np.isnan(got).any(), "N==1 不该有 warmup 空洞"


def test_composite_condition_inside_macro() -> None:
    """宏的参数本身是复合条件时, 括号不能被拆散。"""
    panel = _panel()
    got = _eval_tdx("COUNT(C>O AND C>REF(C,1),10)", panel)
    close = panel["close"].to_numpy()
    open_ = panel["open"].to_numpy()
    cond = (close > open_) & (close > _np_ref(close, 1))
    expected = _np_count(np.nan_to_num(cond.astype(float)).astype(bool), 10)
    mask = ~np.isnan(expected)
    np.testing.assert_allclose(got[mask], expected[mask], rtol=0, atol=0)


# ------------------------------------------------- 错误码

@pytest.mark.parametrize("formula", [
    "CROSS(C)", "CROSS(C,O,5)", "LONGCROSS(C,O)", "LONGCROSS(C,O,3,2)",
    "COUNT(C)", "COUNT(C>O,5,2)", "EVERY(C>O)", "EVERY(C>O,5,2)",
    "EXIST(C>O)", "EXIST(C>O,5,2)",
    "EMA(C)", "EMA(C,5,2)", "SMA(C,10)", "SMA(C,10,2,3)",
    "BARSLAST(C,5)", "BARSLAST()", "BARSSINCE(C,5)", "BARSSINCE()",
])
def test_batch2_arity_rejected_with_e308(formula: str) -> None:
    """参数个数不符必须报 E308, 而且用**通达信函数名**报错。

    透传给 DSL 的话用户会看到「函数 ts_sma 参数数量不符」, 根本不知道
    ts_sma 是什么 —— 那是内部实现名。
    """
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok, formula
    error = compiled.errors[0]
    assert error.code == "E308", (formula, error.to_dict())
    name = formula.split("(")[0]
    assert name in error.message, (formula, error.message)
    assert "ts_" not in error.message, f"内部算子名泄漏给用户: {error.message}"


def test_e308_offset_points_into_tdx_source() -> None:
    """E308 的偏移也要指回用户写的通达信原文。"""
    formula = "MA(C,5) + CROSS(C)"
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok
    error = compiled.errors[0]
    assert error.code == "E308"
    assert error.offset == 10, error.to_dict()
    assert formula[error.offset:].startswith("CROSS")


def test_e308_does_not_leak_into_valid_formula() -> None:
    """E308 只能由参数个数触发, 合法公式不能被误伤。"""
    for formula in ("CROSS(C,O)", "LONGCROSS(C,O,3)", "COUNT(C>O,5)",
                    "EVERY(C>O,5)", "EXIST(C>O,5)", "EMA(C,5)",
                    "SMA(C,10,2)", "BARSLAST(C>O)", "BARSSINCE(C>O)"):
        compiled = compile_tdx_formula(formula)
        assert compiled.ok, (formula, [e.to_dict() for e in compiled.errors])
        assert all(e.code != "E308" for e in compiled.errors), formula


@pytest.mark.parametrize("formula, fragment", [
    ("SMA(C,10,20)", "m 必须是"),      # m > n
    ("SMA(C,10,0)", "m 必须是"),        # m < 1
    ("SMA(C,10,2.5)", "m 必须是"),      # m 非整数
    ("EMA(C,1)", ""),                   # 退化归一, 不该报错
])
def test_sma_weight_validation(formula: str, fragment: str) -> None:
    """SMA 的权重 M 必须满足 1 ≤ M ≤ N 且为整数, 否则 alpha=M/N 越界。"""
    compiled = compile_tdx_formula(formula)
    if not fragment:
        assert compiled.ok, [e.to_dict() for e in compiled.errors]
        return
    assert not compiled.ok, formula
    error = compiled.errors[0]
    assert error.code == "E004", (formula, error.to_dict())
    assert fragment in error.message, error.message


def test_sma_weight_error_offset_points_into_source() -> None:
    """DSL 侧的 E004 也要经过偏移重映射, 指回通达信原文。"""
    formula = "EMA(C,5) + SMA(C,10,20)"
    compiled = compile_tdx_formula(formula)
    assert not compiled.ok
    error = compiled.errors[0]
    assert error.code == "E004"
    assert error.offset == 11, error.to_dict()
    assert formula[error.offset:].startswith("SMA")


# ------------------------------------------------- 不变量锁

def test_batch2_functions_no_longer_rejected() -> None:
    """第 2 批的 9 个函数必须从拒绝清单里彻底移除。"""
    batch2 = {"EMA", "SMA", "CROSS", "LONGCROSS", "COUNT",
              "EVERY", "EXIST", "BARSLAST", "BARSSINCE"}
    assert not (batch2 & set(REJECTED_FUNCTIONS)), sorted(batch2 & set(REJECTED_FUNCTIONS))
    for name in sorted(batch2):
        assert name in FUNCTION_MAP or name in MACRO_ARITY, name


def test_invariant_macro_tables_consistent() -> None:
    """宏的三张表必须自洽: 键大写、互不重叠、展开提示不缺项。"""
    from app.factors.tdx.builtins import MACRO_EXPANSION_HINT
    from app.factors.tdx.translate import _MACROS

    assert all(k == k.upper() for k in MACRO_ARITY), "MACRO_ARITY 有非大写键"
    assert set(MACRO_ARITY) == set(_MACROS), "arity 表与展开实现不同步"
    assert set(MACRO_EXPANSION_HINT) == set(MACRO_ARITY), "兼容表提示缺项"
    # 同名既在映射表又在宏表 → 行为取决于查表顺序
    assert not (set(MACRO_ARITY) & set(FUNCTION_MAP))
    assert not (set(MACRO_ARITY) & set(REJECTED_FUNCTIONS))
    assert not (set(FUNCTION_ARITY) & set(MACRO_ARITY))


def test_invariant_every_macro_compiles() -> None:
    """每个登记的宏都必须真能编译通过 —— 防止表里加了名字但展开写错。"""
    samples = {
        "CROSS": "CROSS(C,O)", "LONGCROSS": "LONGCROSS(C,O,3)",
        "COUNT": "COUNT(C>O,5)", "EVERY": "EVERY(C>O,5)", "EXIST": "EXIST(C>O,5)",
    }
    for name in MACRO_ARITY:
        formula = samples[name]
        compiled = compile_tdx_formula(formula)
        assert compiled.ok, (name, [e.to_dict() for e in compiled.errors])


def test_invariant_arity_bounds_are_sane() -> None:
    """arity 表里的上下界必须合法, 否则任何参数都会被判不符。"""
    for name, (low, high) in {**MACRO_ARITY, **FUNCTION_ARITY}.items():
        assert 0 <= low <= high, (name, low, high)


def test_invariant_no_future_function_in_batch2() -> None:
    """第 2 批新增的函数一个都不能是未来函数 (与第 1 批同一条红线)。"""
    future = {n for n, (cat, _) in REJECTED_FUNCTIONS.items() if cat == REJECT_FUTURE}
    assert not (future & set(MACRO_ARITY))
    for name in ("EMA", "SMA", "BARSLAST", "BARSSINCE"):
        assert name not in future


# ------------------------------------------------- 预算与压力

@pytest.mark.parametrize("formula", [
    "LONGCROSS(MA(C,5),MA(C,10),3)",
    "LONGCROSS(MA(C,5),MA(C,10),3) AND CROSS(MA(C,5),MA(C,20))",
    "IF(LONGCROSS(MA(C,5),MA(C,10),3) AND EXIST(CROSS(C,MA(C,20)),5),1,0)",
    "EVERY(COUNT(C>MA(C,5),10)>3,20)",
    "EMA(SMA(EMA(C,12),26,2),9)",
    "BARSLAST(CROSS(EMA(C,12),EMA(C,26)))",
    "BARSSINCE(LONGCROSS(MA(C,5),MA(C,10),3))",
])
def test_batch2_stays_within_token_and_depth_budget(formula: str) -> None:
    """宏会把参数**重复展开** (LONGCROSS 里 A/B 各出现 3 次), 必须仍在预算内。

    MAX_TOKENS=200 / MAX_AST_DEPTH=12 是防 DoS 的硬闸, 展开不能把它顶穿。
    """
    compiled = compile_tdx_formula(formula)
    assert compiled.ok, (formula, [e.to_dict() for e in compiled.errors])
    text = translate_tdx(formula).dsl_text
    assert len(text.split()) < dsl.MAX_TOKENS, (formula, len(text.split()))
    assert compiled.warmup_bars <= 512, (formula, compiled.warmup_bars)


def test_macro_expansion_does_not_explode_quadratically() -> None:
    """嵌套宏的展开长度必须是线性增长, 不能指数爆炸。

    LONGCROSS 内含 CROSS, 每层都把 A/B 复制一遍 —— 这条锁住放大倍率。
    """
    single = len(translate_tdx("CROSS(C,O)").dsl_text)
    nested = len(translate_tdx("LONGCROSS(C,O,3)").dsl_text)
    assert nested < single * 6, (single, nested)


def test_batch2_functions_survive_cache() -> None:
    """缓存变体与非缓存变体结果必须一致 (内容寻址键含 formula_text)。"""
    panel = _panel()
    for formula in ("EMA(C,5)", "SMA(C,10,2)", "CROSS(C,O)",
                    "LONGCROSS(C,O,3)", "COUNT(C>O,5)", "BARSLAST(C>O)"):
        cached = compile_tdx_formula_cached(formula)
        plain = compile_tdx_formula(formula)
        assert cached.ok and plain.ok, formula
        assert cached.warmup_bars == plain.warmup_bars, formula
        assert cached.frame_transform is not None and plain.frame_transform is not None
        frame = panel.sort(["symbol", "date"])
        np.testing.assert_array_equal(
            cached.frame_transform(frame)[FACTOR_COLUMN].to_numpy(),
            plain.frame_transform(frame)[FACTOR_COLUMN].to_numpy(),
            err_msg=formula,
        )


def test_batch2_no_cross_symbol_leak() -> None:
    """递归算子与宏都必须按 symbol 分组, 不能跨标的串数据。"""
    panel = _two_symbol_panel()
    for formula in ("EMA(C,3)", "SMA(C,5,2)", "CROSS(C,O)", "COUNT(C>O,3)",
                    "BARSLAST(C>O)", "BARSSINCE(C>O)"):
        compiled = compile_tdx_formula(formula)
        assert compiled.ok, (formula, [e.to_dict() for e in compiled.errors])
        assert compiled.frame_transform is not None
        frame = compiled.frame_transform(panel)
        assert frame is not None, formula
        combined = frame[FACTOR_COLUMN].to_list()
        for index, symbol in enumerate(("A", "B")):
            single = compiled.frame_transform(panel.filter(pl.col("symbol") == symbol))
            assert single is not None
            isolated = single[FACTOR_COLUMN].to_list()
            segment = combined[index * 6:(index + 1) * 6]
            assert segment == isolated, (formula, symbol, segment, isolated)


# =============================================== 第 3 批: 多语句 `:=` 与指标模板

def _np_ema(x: np.ndarray, n: int) -> np.ndarray:
    """通达信 EMA(X,N): alpha = 2/(N+1)。"""
    return _np_ewm(x, 2.0 / (n + 1))


def _np_sma(x: np.ndarray, n: int, m: int) -> np.ndarray:
    """通达信 SMA(X,N,M): alpha = M/N (不是简单均线, 见第 2 批口径决定)。"""
    return _np_ewm(x, m / n)


def _compare_from(actual: np.ndarray, ref: np.ndarray, start: int, label: str) -> None:
    """只比参照有值的部分; 参照有限而实际为空即视为不一致。"""
    a, r = actual[start:], ref[start:]
    finite = np.isfinite(r)
    assert finite.any(), label
    assert np.isfinite(a[finite]).all(), f"{label}: 参照有值处实际却是空"
    np.testing.assert_allclose(a[finite], r[finite], rtol=1e-9, atol=1e-9, err_msg=label)


@pytest.mark.parametrize("tdx, expected_dsl", [
    # `:=` 中间变量: 内联展开后 DSL 里不含变量名
    ("A:=MA(C,5); A>REF(A,1)",
     "ts_mean(close, 5) > ts_delay(ts_mean(close, 5), 1)"),
    # `:` 输出赋值同样可用, 且能被后续语句引用
    ("DIF:MA(C,5)", "ts_mean(close, 5)"),
    ("DIF:MA(C,5); DIF>0", "ts_mean(close, 5) > 0"),
    # 链式引用
    ("A:=C; B:=A*2; B+1", "close * 2 + 1"),
    # 多条输出: 末条为准 (通达信选股公式取最后一个输出)
    ("MA(C,5); MA(C,10)", "ts_mean(close, 10)"),
    # 重定义: 后者生效, 与通达信一致
    ("A:=MA(C,5); A:=MA(C,10); A>0", "ts_mean(close, 10) > 0"),
    # 展开必须保住优先级: B=A*2 再 +1 不能变成 A*(2+1)
    ("A:=C*2; A+1", "close * 2 + 1"),
    ("A:=C+1; A*2", "(close + 1) * 2"),
])
def test_batch3_translation_text(tdx: str, expected_dsl: str) -> None:
    result = translate_tdx(tdx)
    assert result.ok, [e.to_dict() for e in result.errors]
    assert result.dsl_text == expected_dsl


def test_batch3_inlined_equals_handwritten_single_expression() -> None:
    """内联展开不得引入第二套语义: 多语句写法与等价单表达式逐位相同。"""
    panel = _panel(400, seed=11)
    pairs = [
        ("A:=MA(C,5); A>REF(A,1)", "MA(C,5)>REF(MA(C,5),1)"),
        ("DIF:=EMA(C,12)-EMA(C,26); DEA:=EMA(DIF,9); (DIF-DEA)*2",
         "(EMA(C,12)-EMA(C,26)-EMA(EMA(C,12)-EMA(C,26),9))*2"),
        ("M:=MA(C,20); U:=M+2*STD(C,20); LO:=M-2*STD(C,20); (C-LO)/(U-LO)",
         "(C-(MA(C,20)-2*STD(C,20)))/(MA(C,20)+2*STD(C,20)-(MA(C,20)-2*STD(C,20)))"),
    ]
    for multi, single in pairs:
        np.testing.assert_array_equal(
            _bool_or_none(_eval_tdx(multi, panel)),
            _bool_or_none(_eval_tdx(single, panel)),
            err_msg=multi,
        )


@pytest.mark.parametrize("tid, start", [
    ("macd_hist", 0), ("kdj_j", 8), ("rsi_6", 1), ("boll_position", 19),
])
def test_batch3_templates_match_numpy_reference(tid: str, start: int) -> None:
    """指标模板与独立 numpy 手算参照对拍 (CONTRIBUTING §9 — 不复用被测路径自证)。"""
    from app.factors.tdx.templates import TDX_TEMPLATES

    template = next(t for t in TDX_TEMPLATES if t.id == tid)
    panel = _panel(400, seed=11)
    close = panel["close"].to_numpy()
    high = panel["high"].to_numpy()
    low = panel["low"].to_numpy()

    if tid == "macd_hist":
        dif = _np_ema(close, 12) - _np_ema(close, 26)
        ref = (dif - _np_ema(dif, 9)) * 2
    elif tid == "kdj_j":
        rsv = (close - _np_roll(low, 9, np.min)) / (
            _np_roll(high, 9, np.max) - _np_roll(low, 9, np.min)) * 100
        k = _np_sma(rsv, 3, 1)
        ref = 3 * k - 2 * _np_sma(k, 3, 1)
    elif tid == "rsi_6":
        delta = close - _np_ref(close, 1)
        ref = _np_sma(np.maximum(delta, 0.0), 6, 1) / _np_sma(np.abs(delta), 6, 1) * 100
    else:  # boll_position
        mid = _np_ma(close, 20)
        upper, lower = mid + 2 * _np_std_ddof1(close, 20), mid - 2 * _np_std_ddof1(close, 20)
        ref = (close - lower) / (upper - lower)

    _compare_from(_eval_tdx(template.formula, panel), ref, start, tid)


@pytest.mark.parametrize("tid, warmup", [
    ("macd_hist", 139), ("macd_golden_cross", 140), ("kdj_j", 32),
    ("rsi_6", 25), ("boll_position", 21), ("ma_bullish_alignment", 21),
])
def test_batch3_template_compiles_with_expected_warmup(tid: str, warmup: int) -> None:
    """每条模板都必须能编译, 且暖机值锁死 —— 暖机口径一改这里就会响。"""
    from app.factors.tdx.templates import TDX_TEMPLATES

    template = next(t for t in TDX_TEMPLATES if t.id == tid)
    compiled = compile_tdx_formula(template.formula)
    assert compiled.ok, (tid, [e.to_dict() for e in compiled.errors])
    assert compiled.warmup_bars == warmup, tid


def test_batch3_template_export_is_same_source() -> None:
    """导出接口必须与 TDX_TEMPLATES 同源, 不能各写一份。"""
    from app.factors.tdx.templates import TDX_TEMPLATES, tdx_templates

    exported = tdx_templates()
    assert [d["id"] for d in exported] == [t.id for t in TDX_TEMPLATES]
    assert set(exported[0]) == {"id", "name", "category", "kind", "formula", "description"}
    assert all(d["kind"] in ("value", "signal") for d in exported)
    # signal 类模板必须真能当布尔用 (选股条件), value 类必须是连续数值
    for d in exported:
        values = _bool_or_none(_eval_tdx(d["formula"], _panel(400, seed=11)))
        assert any(v is not None for v in values), d["id"]
        if d["kind"] == "signal":
            assert set(v for v in values if v is not None) <= {True, False}, d["id"]


def test_batch3_error_offset_points_to_definition_site() -> None:
    """错误发生在中间变量的**定义**里时, 偏移必须指回用户原文的定义处。

    内联展开会把定义体复制到每个引用点; 若偏移跟着复制走, 用户看到的行号就是错的。
    """
    source = "BAD:=REF(C,-1); MA(C,5)+BAD"
    compiled = compile_tdx_formula(source)
    assert not compiled.ok
    error = compiled.errors[0]
    assert error.code == "E005"
    # 偏移必须落在定义段 (`;` 之前), 而不是末尾那个引用点
    assert error.offset < source.index(";"), (error.offset, source)
    assert source[error.offset:error.offset + 3] == "REF", (error.offset, source)


def test_batch3_expansion_blowup_rejected() -> None:
    """内联展开按引用次数相乘, 必须有规模闸门, 且报错点名是哪个变量炸的。"""
    parts = ["A0:=C;"]
    parts += [f"A{i}:=" + "+".join([f"A{i - 1}"] * 5) + ";" for i in range(1, 8)]
    parts.append("A7")
    compiled = compile_tdx_formula(" ".join(parts))
    assert not compiled.ok
    assert compiled.errors[0].code == "E302"
    assert "A" in compiled.errors[0].message


def test_batch3_unused_variable_is_not_semantically_checked() -> None:
    """没被用到的中间变量不参与输出, 因此不做语义校验 (口径决定, 见 translate.py)。

    这是**有意**的: 用不到的变量影响不了因子值; 而只要它被引用, 就会被拼进最终
    AST 从而照常过 E304/E305 门禁 —— 下面第二条断言锁住这一点。
    """
    assert compile_tdx_formula("BAD:=ZIG(C,3); MA(C,5)").ok
    rejected = compile_tdx_formula("BAD:=ZIG(C,3); MA(C,5)+BAD")
    assert not rejected.ok
    assert rejected.errors[0].code == "E304"


def test_batch3_warmup_sums_along_nested_path() -> None:
    """嵌套窗口沿路径求和, 并列窗口取最大 (第 3 批修正 dsl.py 的全局 max)。"""
    # 嵌套: 外层要的是已算好的内层值, 内层自己还要再往前看
    assert compile_formula("ts_mean(ts_mean(close,5),20)").warmup_bars == 26
    assert compile_formula("ts_std(ts_mean(close,5),10)").warmup_bars == 16
    assert compile_formula("ts_ema(ts_ema(close,26),9)").warmup_bars == 139
    # 并列: 各自独立回看, 取 max 就够, 不能相加
    assert compile_formula("ts_mean(close,5)-ts_mean(close,20)").warmup_bars == 21
    # 单层不受影响
    assert compile_formula("ts_mean(close,20)").warmup_bars == 21
    assert compile_formula("ts_ema(close,26)").warmup_bars == 104


def test_batch3_nested_window_not_null_at_reported_warmup() -> None:
    """回归锁: 旧的全局 max 让嵌套窗口在自报暖机处直接是 null。

    `ts_mean(ts_mean(close,5),20)` 旧口径报 21, 但内层首个有效值在下标 4,
    外层要 20 个有效内层值 → 真实需要 24 根; 给 21 根时末根是空的。
    """
    formula = "ts_mean(ts_mean(close,5),20)"
    compiled = compile_formula(formula)
    assert compiled.frame_transform is not None
    panel = _panel(compiled.warmup_bars, seed=3)
    values = compiled.frame_transform(panel)[FACTOR_COLUMN].to_list()
    assert values[-1] is not None, f"暖机 {compiled.warmup_bars} 根仍算不出值"


def test_api_tdx_meta_exposes_compatibility_and_templates(tmp_path) -> None:
    """/tdx/meta 一次下发兼容表与模板, 且必须与翻译层同源 (不另抄一份)。"""
    from app.factors.tdx.builtins import compatibility_table
    from app.factors.tdx.templates import TDX_TEMPLATES

    response = _client(tmp_path).get("/api/factors/tdx/meta")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["compatibility"] == compatibility_table()
    assert [t["id"] for t in payload["templates"]] == [t.id for t in TDX_TEMPLATES]
    # 三种 kind 都要在, 否则前端「支持 / 宏展开 / 不支持」分区会有空的
    assert {"operator", "macro", "rejected"} <= {r["kind"] for r in payload["compatibility"]}
    # 下发的示例必须真能用: 编译不过的模板比没有模板更糟
    for template in payload["templates"]:
        assert compile_tdx_formula(template["formula"]).ok, template["id"]


def test_batch3_self_reference_message_is_specific() -> None:
    """自引用必须给出「不能引用自己」的专属提示, 不能只靠 E309 蒙混。

    只断言错误码是不够的: 前向引用守卫也会先抓到 `A:=A+1` 并报同一个 E309, 但消息
    会说「在定义之前被引用」—— 用户明明就写在这一句里, 那是误导。
    """
    compiled = compile_tdx_formula("A:=A+1; A")
    assert not compiled.ok
    assert compiled.errors[0].code == "E309"
    assert "自己" in compiled.errors[0].message, compiled.errors[0].message


# ── P1-17 第 4 批: 前端要的两处契约 (翻译预览 + 列表带 lang) ──────────────────
def test_batch4_validate_returns_tdx_translation_preview() -> None:
    """lang=tdx 的校验响应必须附带翻译后的 DSL 文本, 且与翻译层同源。"""
    from app.factors.tdx.translate import translate_tdx

    source = "DIF:=EMA(C,12)-EMA(C,26); DEA:=EMA(DIF,9); MACD柱:(DIF-DEA)*2;"
    response = _client(None).post(
        "/api/factors/validate", json={"formula": source, "lang": "tdx"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    # 断言同源而不是抄一份期望字符串: 翻译层改了口径, 这里自动跟着变
    assert payload["dsl_text"] == translate_tdx(source).dsl_text
    assert "ts_ema(close, 12)" in payload["dsl_text"]


def test_batch4_validate_preview_survives_compile_failure() -> None:
    """翻译成功但 DSL 编译失败时仍要给预览 —— 那正是排错最需要的东西。

    `MA(C,0)` 译成 `ts_mean(close, 0)`, 窗口下限 2 被 E004 拒; 只报错误码的话
    用户看不出「0」是被翻译层原样带过去的, 会怀疑翻译器改了自己的参数。
    """
    response = _client(None).post(
        "/api/factors/validate", json={"formula": "MA(C,0)", "lang": "tdx"}
    )
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "E004"
    assert payload["dsl_text"] == "ts_mean(close, 0)"


def test_batch4_validate_preview_empty_for_dsl_and_untranslatable() -> None:
    """非 tdx 档恒为空串; tdx 档翻译本身就失败时也是空串 (不半截糊弄)。"""
    dsl = _client(None).post(
        "/api/factors/validate", json={"formula": "rank(close)", "lang": "dsl"}
    ).json()
    assert dsl["ok"] is True and dsl["dsl_text"] == ""

    # ZIG 是未来函数, 翻译层直接拒 → 没有 DSL 可预览
    rejected = _client(None).post(
        "/api/factors/validate", json={"formula": "MA(C,5)+ZIG(C,3)", "lang": "tdx"}
    ).json()
    assert rejected["ok"] is False and rejected["dsl_text"] == ""


def test_batch4_list_exposes_lang(tmp_path, cleanup_registry) -> None:
    """因子列表必须带 lang: 编辑器靠它恢复语言档位。

    少了这个字段, 重新打开一个通达信因子会被当成 DSL 校验 —— 同一段文本在两种
    语言下语义不同, 那是静默错译, 比报错危险。
    """
    client = _client(tmp_path)
    created = client.post("/api/factors/custom", json={
        "id": "uf_tdx_lang_list", "label": "通达信动量", "formula": "MA(C,5)", "lang": "tdx",
    })
    assert created.status_code == 200, created.text
    cleanup_registry.add(created.json()["id"])

    listing = client.get("/api/factors")
    assert listing.status_code == 200, listing.text
    by_id = {item["id"]: item for item in listing.json()["factors"]}
    assert by_id["uf_tdx_lang_list"]["lang"] == "tdx"
    # 原文照存 (不是生成的 DSL), 否则切回 tdx 档编辑会看到被翻译过的文本
    assert by_id["uf_tdx_lang_list"]["formula"] == "MA(C,5)"
    # 内置因子全是 dsl — 顺带锁住"字段对所有因子都存在", 前端不必判空
    assert all(item["lang"] == "dsl" for item in by_id.values() if item["kind"] != "custom")
