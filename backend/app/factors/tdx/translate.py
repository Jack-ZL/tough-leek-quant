"""通达信公式 → 因子 DSL 翻译与编译入口 (P1-17 第 1 批)。

设计要点:
- **不新造求值引擎**。翻译成 DSL 文本后交给 app.factors.dsl.compile_formula,
  于是窗口纪律、E005 负偏移(未来函数)门控、依赖提取、warmup 推断、截面算子
  嵌套校验、内容寻址缓存全部自动复用, 语义与 dsl/python 两档完全一致;
- 返回的是**真正的 CompiledFormula**, 所有既有消费方 (scoring 物化、
  api validate/trial、materialize_cache 缓存键) 零改动即可用;
- 报错偏移会**映射回用户写的通达信原文**, 而不是指向生成的中间文本 ——
  用户看到的是自己那行公式的第几个字符出了问题;
- formula_text 始终保留**用户的通达信原文**, 与 lang="python" 档存 Python
  原文同一口径, 编辑/缓存键/展示都不会丢原文。

第 3 批起支持多语句与 `:=`/`:` 中间变量: 在翻译层按语句顺序**内联展开**,
展开后的 AST 里已不含变量名, 所以 dsl.py 无需任何改动 (它的 E001 只认列名
与已注册因子 id, 变量名根本到不了那一层)。
"""
from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import lru_cache

from app.factors.dsl import MAX_AST_DEPTH, CompiledFormula, DslError, compile_formula
from app.factors.tdx.builtins import (
    COLUMN_ALIASES,
    DELAY_IDENTITY_AT_0,
    FUNCTION_ARITY,
    FUNCTION_MAP,
    MACRO_ARITY,
    REJECT_CODE,
    REJECTED_FUNCTIONS,
    WINDOW_IDENTITY_AT_1,
)
from app.factors.tdx.parser import parse_formula


@dataclass(frozen=True)
class TranslateResult:
    """翻译产物。dsl_text 供前端「翻译预览」展示, 让用户看见等价公式。"""

    ok: bool
    dsl_text: str = ""
    errors: list[DslError] = field(default_factory=list)


class _Emitter:
    """累积生成 DSL 文本, 同时记录 (dsl 偏移 → 通达信原文偏移) 锚点。"""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._dsl_offsets: list[int] = []
        self._tdx_offsets: list[int] = []
        self._name_offsets: dict[str, int] = {}

    def mark(self, tdx_offset: int) -> None:
        """登记锚点: 当前生成位置对应用户原文的 tdx_offset。"""
        self._dsl_offsets.append(self._length)
        self._tdx_offsets.append(tdx_offset)

    def mark_name(self, name: str, tdx_offset: int) -> None:
        """登记标识符首次出现位置 (大小写不敏感)。

        DSL 的 E001「未知标识符」是对 sorted(set) 报错, 拿不到节点偏移 (恒为 0),
        所以这里自己记一份, 让报错仍能指回用户原文的那个名字。
        """
        self._name_offsets.setdefault(name.upper(), tdx_offset)

    def emit(self, text: str) -> None:
        self._parts.append(text)
        self._length += len(text)

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def remap(self, dsl_offset: int) -> int:
        """把 DSL 文本里的偏移换算回通达信原文偏移 (取不超过它的最近锚点)。"""
        if not self._dsl_offsets:
            return 0
        index = bisect.bisect_right(self._dsl_offsets, dsl_offset) - 1
        return self._tdx_offsets[max(index, 0)]

    def remap_error(self, error: DslError) -> DslError:
        """换算一条 DSL 错误的偏移; 无偏移的错误按标识符名回填。"""
        if error.offset:
            return replace(error, offset=self.remap(error.offset))
        name = (error.detail or {}).get("name")
        if isinstance(name, str) and name.upper() in self._name_offsets:
            return replace(error, offset=self._name_offsets[name.upper()])
        return error


# 优先级表 —— 与通达信和 DSL **两边都一致** (or < and < 比较 < 加减 < 乘除 < 一元)。
# 只在必要时加括号: 生成的 DSL 文本受 dsl.MAX_TOKENS 预算限制, 无脑全括号
# 会在长公式 (如 MACD 展开) 上白白吃掉额度。
_PRECEDENCE: dict[str, int] = {
    "or": 1, "and": 2,
    "==": 3, "!=": 3, ">": 3, ">=": 3, "<": 3, "<=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5,
    "not": 3,   # DSL 的 not 在 unary 层 (比比较绑得紧), 故其**操作数**必须括号
    "-unary": 6,
}
_ATOM_PRECEDENCE = 7


def _precedence(node: dict) -> int:
    if node["kind"] == "bin":
        return _PRECEDENCE[node["value"]]
    if node["kind"] == "unary":
        return _PRECEDENCE["not" if node["value"] == "not" else "-unary"]
    return _ATOM_PRECEDENCE


def _emit_operand(node: dict, em: _Emitter, errors: list[DslError], parent: int, right: bool) -> None:
    """按优先级决定是否给子表达式加括号。

    左结合算子: 左操作数只在优先级**更低**时加括号, 右操作数在**不更高**时加括号
    (这样 `A-(B-C)` 保住括号, 而 `(A-B)-C` 不会多余地加)。
    """
    child = _precedence(node)
    if child < parent or (right and child == parent):
        em.emit("(")
        _emit(node, em, errors)
        em.emit(")")
    else:
        _emit(node, em, errors)


def _const_int(node: dict) -> int | None:
    """取节点的字面整数常量 (支持负号), 非数字常量返回 None。"""
    if node["kind"] == "num":
        value = float(node["value"])
        return int(value) if value == int(value) else None
    if node["kind"] == "unary" and node["value"] == "-" and node["children"][0]["kind"] == "num":
        value = -float(node["children"][0]["value"])
        return int(value) if value == int(value) else None
    return None


def _emit(node: dict, em: _Emitter, errors: list[DslError]) -> None:
    kind = node["kind"]
    offset = node["offset"]

    if kind == "num":
        em.mark(offset)
        value = float(node["value"])
        # 整数不带小数点, 少占 token (DSL 有 MAX_TOKENS 预算)
        em.emit(str(int(value)) if value == int(value) else repr(value))
        return

    if kind == "str":
        errors.append(DslError(
            "E307", f"不支持字符串常量 '{node['value']}': 因子只能是数值序列", offset=offset,
        ))
        return

    if kind == "col":
        name = str(node["value"])
        mapped = COLUMN_ALIASES.get(name.upper())
        em.mark(offset)
        em.mark_name(name, offset)
        # 不在别名表里的原样透传: 可能是用户自定义因子 id, 由 DSL 的 E001 兜底
        em.emit(mapped if mapped is not None else name)
        return

    if kind == "unary":
        em.mark(offset)
        child = node["children"][0]
        if node["value"] == "-":
            em.emit("-")
            _emit_operand(child, em, errors, _PRECEDENCE["-unary"], right=False)
        else:
            em.emit("not ")
            # DSL 的 not 在 unary 层, 比比较运算绑得更紧: `not close > 1` 会被解析成
            # `(not close) > 1`。所以 not 的操作数只要不是原子就必须加括号。
            if child["kind"] in ("bin", "unary"):
                em.emit("(")
                _emit(child, em, errors)
                em.emit(")")
            else:
                _emit(child, em, errors)
        return

    if kind == "bin":
        left, right = node["children"]
        parent = _PRECEDENCE[node["value"]]
        _emit_operand(left, em, errors, parent, right=False)
        # DSL 的 and/or 是**小写关键字**, 通达信大小写不敏感, 这里统一小写
        em.emit(f" {node['value']} ")
        _emit_operand(right, em, errors, parent, right=True)
        return

    if kind == "call":
        _emit_call(node, em, errors)
        return

    errors.append(DslError("E303", f"语法错误: 未知节点类型 '{kind}'", offset=offset))


def _emit_count(args: list[dict], em: _Emitter, errors: list[DslError]) -> None:
    """COUNT(COND,N) → ts_sum(if_else(COND,1,0), N)。

    N==1 时退化成 if_else(COND,1,0) —— DSL 窗口下限是 2, 不归一会 E004 误拒。
    """
    cond, window = args
    if _const_int(window) == 1:
        em.emit("if_else(")
        _emit(cond, em, errors)
        em.emit(", 1, 0)")
        return
    em.emit("ts_sum(if_else(")
    _emit(cond, em, errors)
    em.emit(", 1, 0), ")
    _emit(window, em, errors)
    em.emit(")")


def _emit_cross(args: list[dict], em: _Emitter, errors: list[DslError]) -> None:
    """CROSS(A,B) → A>B 且前一根 A<=B (A 自下向上穿过 B)。

    前一根用 `<=` 而不是 `<`: 均线金叉前一刻两条线常**恰好相等**, 用 `<`
    会把这一天漏掉 —— 这是通达信自己的口径, 必须对齐。
    """
    left, right = args
    em.emit("(")
    _emit(left, em, errors)
    em.emit(") > (")
    _emit(right, em, errors)
    em.emit(") and ts_delay(")
    _emit(left, em, errors)
    em.emit(", 1) <= ts_delay(")
    _emit(right, em, errors)
    em.emit(", 1)")


def _emit_longcross(args: list[dict], em: _Emitter, errors: list[DslError]) -> None:
    """LONGCROSS(A,B,N) → A 在 B 下方**连续 N 根**后上穿 B。

    "连续 N 根" 数的是上穿**之前**的 N 根 (不含当根, 当根已经 A>B 了),
    所以是 REF(EVERY(A<B,N), 1) == N。
    """
    left, right, window = args
    _emit_cross([left, right], em, errors)
    below = {"kind": "bin", "value": "<", "children": [left, right], "offset": left["offset"]}
    em.emit(" and (ts_delay(")
    _emit_count([below, window], em, errors)
    em.emit(", 1) == ")
    _emit(window, em, errors)
    em.emit(")")


def _emit_every(args: list[dict], em: _Emitter, errors: list[DslError]) -> None:
    """EVERY(COND,N) → COUNT(COND,N) == N (N 根全部成立)。"""
    _emit_count(args, em, errors)
    em.emit(" == ")
    _emit(args[1], em, errors)


def _emit_exist(args: list[dict], em: _Emitter, errors: list[DslError]) -> None:
    """EXIST(COND,N) → COUNT(COND,N) > 0 (N 根内至少成立一次)。"""
    _emit_count(args, em, errors)
    em.emit(" > 0")


# 展开成**复合表达式**的宏必须自己套外层括号: _precedence() 对 call 节点一律
# 返回原子级, 上层不会替它补括号 —— `NOT CROSS(A,B)` 会被解析成
# `(not A) > B and ...`, 语义全错。COUNT 展开是单个 ts_sum(...) 调用, 属原子。
_MACROS: dict[str, tuple[Callable[[list[dict], _Emitter, list[DslError]], None], bool]] = {
    "CROSS": (_emit_cross, True),
    "LONGCROSS": (_emit_longcross, True),
    "COUNT": (_emit_count, False),
    "EVERY": (_emit_every, True),
    "EXIST": (_emit_exist, True),
}


def _emit_call(node: dict, em: _Emitter, errors: list[DslError]) -> None:
    name = str(node["value"])
    upper = name.upper()
    offset = node["offset"]
    args = node["children"]

    # 拒绝清单优先于映射表: 未来函数必须硬拒, 不能因为碰巧同名就放过
    rejected = REJECTED_FUNCTIONS.get(upper)
    if rejected is not None:
        category, reason = rejected
        prefix = "禁止使用未来函数" if category == "future" else "不支持的函数"
        errors.append(DslError(
            REJECT_CODE[category], f"{prefix}: {name} — {reason}",
            offset=offset, detail={"name": name, "category": category},
        ))
        return

    # 参数个数自查 (第 2 批): 宏展开是结构性的, 参数不够会直接 IndexError;
    # 且报错必须用**通达信函数名** —— 透传给 DSL 的话用户会看到
    # "函数 ts_sma 参数数量不符", 根本不知道 ts_sma 是什么。
    arity = MACRO_ARITY.get(upper) or FUNCTION_ARITY.get(upper)
    if arity is not None and not arity[0] <= len(args) <= arity[1]:
        errors.append(DslError(
            "E308",
            f"函数 {name} 参数个数不符: 期望 {arity[0]} 个, 实际 {len(args)} 个",
            offset=offset, detail={"name": name, "args": len(args)},
        ))
        return

    macro = _MACROS.get(upper)
    if macro is not None:
        emitter, needs_parens = macro
        em.mark(offset)
        if needs_parens:
            em.emit("(")
        emitter(args, em, errors)
        if needs_parens:
            em.emit(")")
        return

    dsl_name = FUNCTION_MAP.get(upper)
    if dsl_name is None:
        # 不在映射表也不在拒绝表: 原样透传, 让 DSL 报 E002「未知函数」,
        # 这样用户自定义的 DSL 算子名/因子函数仍可直接写。
        em.mark(offset)
        em.mark_name(name, offset)
        em.emit(name)
    else:
        # 退化窗口归一: MA(X,1)=X, REF(X,0)=X。通达信合法但 DSL 窗口下限是 2、
        # 延迟下限是 1, 不归一就会被 E004 误拒。
        if len(args) == 2:
            n_value = _const_int(args[1])
            if n_value == 1 and upper in WINDOW_IDENTITY_AT_1:
                _emit(args[0], em, errors)
                return
            if n_value == 0 and upper in DELAY_IDENTITY_AT_0:
                _emit(args[0], em, errors)
                return
        # SMA(X,1,1): alpha = M/N = 1 → 恒等。三参数, 上面的二参数分支覆盖不到。
        if len(args) == 3 and upper == "SMA" and _const_int(args[1]) == 1 and _const_int(args[2]) == 1:
            _emit(args[0], em, errors)
            return
        em.mark(offset)
        em.emit(dsl_name)

    em.emit("(")
    for index, arg in enumerate(args):
        if index:
            em.emit(", ")
        _emit(arg, em, errors)
    em.emit(")")


# ── 第 3 批: 中间变量内联展开 ──────────────────────────────────
# 变量名不能与列别名/函数名/宏名重合, 否则 `C:=CLOSE+1;` 这类写法会产生歧义:
# 到底 `C` 是收盘价还是用户自己的变量? 直接拒, 让用户换名字。
_RESERVED_NAMES = (
    frozenset(COLUMN_ALIASES) | frozenset(FUNCTION_MAP)
    | frozenset(MACRO_ARITY) | frozenset(REJECTED_FUNCTIONS)
)

# 内联展开的规模上限。`A:=...; B:=A+A; C:=B+B;` 会让展开后的节点数按引用次数
# **相乘**膨胀, 所以每展开一条就立刻量一次, 不能等全部展开完。600 远大于真实指标
# (六个内置模板实测最大 44 节点 = KDJ 的 J 值, 其余 ≤31), 只拦病态写法; 而 dsl.py
# 的 MAX_TOKENS=200 仍是真正生效的用户可见上限 —— 这里只是防止在触到它之前先吃光内存。
_MAX_EXPANDED_NODES = 600


def _col_names(node: dict) -> set[str]:
    """收集 AST 里出现的标识符名 (大写)。"""
    if node["kind"] == "col":
        return {str(node["value"]).upper()}
    found: set[str] = set()
    for child in node["children"]:
        found |= _col_names(child)
    return found


def _ast_nodes(node: dict) -> int:
    return 1 + sum(_ast_nodes(child) for child in node["children"])


def _ast_depth(node: dict) -> int:
    return 1 + max((_ast_depth(child) for child in node["children"]), default=0)


def _substitute(
    node: dict, env: dict[str, dict], declared: set[str], errors: list[DslError],
) -> dict:
    """把中间变量引用替换成它**已展开**的 AST。

    节点自带 offset, 所以内联进来的副本仍指向**定义处**的原文位置 —— 报错指回用户
    写定义的那一行, 而不是指回引用点。这与宏展开的偏移口径一致。

    叶子节点直接返回原对象 (不复制): _emit 全程只读不改, 共享子树是安全的。
    """
    if node["kind"] == "col":
        name = str(node["value"]).upper()
        hit = env.get(name)
        if hit is not None:
            return hit
        if name in declared:
            errors.append(DslError(
                "E309",
                f"中间变量 `{node['value']}` 在定义之前被引用: 公式按语句顺序求值, "
                "请把它的定义移到这一句之前",
                offset=node["offset"],
            ))
        # 既不在 env 也不在 declared: 原样透传, 由 DSL 的 E001 兜底
        # (可能是用户自定义因子 id, 也可能只是拼错了)
        return node
    if not node["children"]:
        return node
    return {**node, "children": [_substitute(c, env, declared, errors) for c in node["children"]]}


def translate_tdx(text: str) -> TranslateResult:
    """把通达信公式翻译成因子 DSL 文本; 永不抛异常, 失败以 errors 表达。"""
    return _translate_with_emitter(text)[0]


def _translate_with_emitter(text: str) -> tuple[TranslateResult, _Emitter | None]:
    """translate_tdx 的内部版: 一并返回 emitter 以便把报错偏移映射回原文。"""
    statements, parse_error = parse_formula(text)
    if parse_error is not None:
        return TranslateResult(ok=False, errors=[parse_error]), None

    # 第 3 批: 多语句与 `:=`/`:` 中间变量。按语句顺序内联展开, 最后一条语句的值就是
    # 因子的值 (与通达信「选股公式取最后一个输出」的口径一致)。
    declared = {str(s["value"]).upper() for s in statements if s["kind"] in ("assign", "output")}
    for stmt in statements:
        if stmt["kind"] in ("assign", "output") and str(stmt["value"]).upper() in _RESERVED_NAMES:
            return TranslateResult(ok=False, errors=[DslError(
                "E309",
                f"`{stmt['value']}` 是保留名 (列别名或函数名), 不能用作中间变量: 请换个名字",
                offset=stmt["offset"],
            )]), None

    env: dict[str, dict] = {}
    errors: list[DslError] = []
    value_ast: dict | None = None
    value_kind = ""
    for stmt in statements:
        raw = stmt["children"][0]
        if stmt["kind"] in ("assign", "output"):
            name = str(stmt["value"]).upper()
            if name in _col_names(raw):
                # `A:=A+1` 到底是「用上一句的 A」还是「递推」? 单个表达式两者都表达不了,
                # 与其猜不如明确拒 (绝不静默错译)。
                errors.append(DslError(
                    "E309",
                    f"中间变量 `{stmt['value']}` 不能在自己的定义里引用自己: "
                    "递推写法无法翻译成单个表达式",
                    offset=stmt["offset"],
                ))
                continue
            expanded = _substitute(raw, env, declared, errors)
            nodes, depth = _ast_nodes(expanded), _ast_depth(expanded)
            if nodes > _MAX_EXPANDED_NODES or depth > MAX_AST_DEPTH:
                errors.append(DslError(
                    "E302",
                    f"中间变量 `{stmt['value']}` 展开后规模超限: {nodes} 个节点 (上限 "
                    f"{_MAX_EXPANDED_NODES})、嵌套 {depth} 层 (上限 {MAX_AST_DEPTH})。"
                    "反复引用同一个中间变量会让公式按引用次数相乘膨胀",
                    offset=stmt["offset"],
                ))
                continue
            # 后定义覆盖先定义: 与通达信一致, 后续语句看到的是新值
            env[name] = expanded
        else:
            expanded = _substitute(raw, env, declared, errors)
        value_ast, value_kind = expanded, stmt["kind"]

    if errors:
        return TranslateResult(ok=False, errors=errors), None
    if value_kind == "assign":
        # 全是 `:=` 就没有输出值 —— 通达信里这样的公式画不出线, 因子也取不到值。
        return TranslateResult(ok=False, errors=[DslError(
            "E310",
            "公式没有输出值: 最后一条语句是中间变量赋值 `:=`, 它不参与输出。"
            "请在末尾补一条输出 (`名字:表达式;` 或直接写一个表达式)",
            offset=statements[-1]["offset"],
        )]), None
    assert value_ast is not None

    em = _Emitter()
    emit_errors: list[DslError] = []
    _emit(value_ast, em, emit_errors)
    if emit_errors:
        return TranslateResult(ok=False, errors=emit_errors), em
    return TranslateResult(ok=True, dsl_text=em.text), em


def compile_tdx_formula(text: str) -> CompiledFormula:
    """编译通达信公式; 永不抛异常, 失败以 errors 表达 (fail-closed)。

    返回标准 CompiledFormula —— 与 dsl 档完全同形, 消费方无需区分语言。
    """
    if not isinstance(text, str) or not text.strip():
        return CompiledFormula(ok=False, errors=[DslError("E301", "公式为空")], formula_text=text or "")

    result, emitter = _translate_with_emitter(text)
    if not result.ok:
        return CompiledFormula(ok=False, errors=result.errors, formula_text=text)

    compiled = compile_formula(result.dsl_text)
    # DSL 报的偏移是生成文本里的位置, 换算回用户的通达信原文位置
    if emitter is not None and compiled.errors:
        remapped = [emitter.remap_error(error) for error in compiled.errors]
    else:
        remapped = compiled.errors
    # formula_text 保留用户原文 (与 lang="python" 档存 Python 原文同一口径)
    return replace(compiled, errors=remapped, formula_text=text)


@lru_cache(maxsize=256)
def compile_tdx_formula_cached(text: str) -> CompiledFormula:
    """带 LRU 缓存的编译入口 (与 compile_formula_cached 同形)。"""
    return compile_tdx_formula(text)
