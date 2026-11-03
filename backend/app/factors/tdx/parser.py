"""通达信公式词法 / 语法分析 (P1-17 第 1 批)。

与 app/factors/dsl.py 的 _tokenize + _Parser 同构: 先切词, 再递归下降出 AST。
节点格式沿用 DSL 的 (kind, value, children, offset), 便于翻译期直接映射,
也让报错能指回**用户写的通达信原文位置**而不是生成的中间文本位置。

通达信与 DSL 的语法差异, 都在这里吸收:
- 注释 `{...}` 与 `(*...*)`;
- 标识符大小写不敏感, 且允许中文变量名;
- `=` 是相等比较 (不是赋值), `<>` 是不等;
- `:=` 中间变量赋值, `:` 输出赋值, `;` 结束语句;
- `AND` / `OR` / `NOT` 是关键字。

第 1 批只接受「单表达式」; 多语句与 `:=` 在第 3 批实现, 此处给明确的
E306 而不是含糊的语法错误 —— 让用户知道是功能没到, 不是自己写错了。
"""
from __future__ import annotations

import re
from typing import Any

from app.factors.dsl import DslError

_MAX_SOURCE_CHARS = 4000
_MAX_TOKENS = 300

# 顺序敏感: 双字符算子必须排在单字符之前 (:= 先于 :, <> 先于 <, >= 先于 >)
_TOKEN_RE = re.compile(
    r"(?P<comment>\{[^}]*\}|\(\*.*?\*\))"
    r"|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<str>'[^']*'|\"[^\"]*\")"
    r"|(?P<ident>[^\W\d]\w*)"
    r"|(?P<op>:=|<>|>=|<=|==|!=|[-+*/><=(),;:])",
    re.DOTALL,
)

_KEYWORDS = frozenset({"AND", "OR", "NOT"})
_CMP_OPS = frozenset({">", ">=", "<", "<=", "=", "==", "<>", "!="})

Token = tuple[str, Any, int]


def tokenize(text: str) -> tuple[list[Token], DslError | None]:
    """切词; 注释与空白丢弃。永不抛异常, 失败以 DslError 表达。"""
    if len(text) > _MAX_SOURCE_CHARS:
        return [], DslError("E302", f"公式超长: {len(text)} > {_MAX_SOURCE_CHARS} 字符")
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(text, pos)
        if match is None or match.end() == pos:
            return [], DslError("E303", f"语法错误: 无法识别的字符 '{text[pos]}'", offset=pos)
        if match.lastgroup == "comment":
            pos = match.end()
            continue
        if match.lastgroup == "num":
            tokens.append(("num", float(match.group("num")), match.start()))
        elif match.lastgroup == "str":
            # 去掉引号; 通达信字符串仅用于绘图/名称类函数, 语义层会拒 (E307)
            raw = match.group("str")
            tokens.append(("str", raw[1:-1], match.start()))
        elif match.lastgroup == "ident":
            tokens.append(("ident", match.group("ident"), match.start()))
        else:
            tokens.append(("op", match.group("op"), match.start()))
        pos = match.end()
    if len(tokens) > _MAX_TOKENS:
        return [], DslError("E302", f"规模超限: token 数 {len(tokens)} > {_MAX_TOKENS}")
    return tokens, None


class Parser:
    """递归下降。优先级由低到高: OR < AND < NOT < 比较 < 加减 < 乘除 < 一元负号。"""

    def __init__(self, tokens: list[Token], text: str) -> None:
        self._tokens = tokens
        self._text = text
        self._pos = 0

    # ── 词法游标 ────────────────────────────────────────────────
    def _peek(self, ahead: int = 0) -> Token | None:
        index = self._pos + ahead
        return self._tokens[index] if index < len(self._tokens) else None

    def _next(self) -> Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _at_op(self, *values: str) -> bool:
        token = self._peek()
        return bool(token and token[0] == "op" and token[1] in values)

    def _at_keyword(self, keyword: str) -> bool:
        token = self._peek()
        return bool(token and token[0] == "ident" and token[1].upper() == keyword)

    # ── 语句层 ──────────────────────────────────────────────────
    def parse(self) -> tuple[list[dict], DslError | None]:
        """解析整段公式, 返回语句列表。"""
        if not self._tokens:
            return [], DslError("E301", "公式为空")
        statements: list[dict] = []
        while True:
            stmt, error = self._statement()
            if error:
                return [], error
            statements.append(stmt)
            if self._at_op(";"):
                self._next()
                # 允许结尾多余的分号: `X;` 与 `X` 等价
                if self._peek() is None:
                    break
                continue
            break
        if self._peek() is not None:
            token = self._peek()
            assert token is not None
            return [], DslError("E303", f"语法错误: 多余的记号 '{token[1]}'", offset=token[2])
        return statements, None

    def _statement(self) -> tuple[dict | None, DslError | None]:
        token = self._peek()
        assert token is not None
        # `名字:=表达式` 中间变量 / `名字:表达式` 输出赋值
        if token[0] == "ident" and token[1].upper() not in _KEYWORDS:
            nxt = self._peek(1)
            if nxt and nxt[0] == "op" and nxt[1] in (":=", ":"):
                self._next()
                self._next()
                expr, error = self._expr()
                if error:
                    return None, error
                kind = "assign" if nxt[1] == ":=" else "output"
                return {"kind": kind, "value": token[1], "children": [expr], "offset": token[2]}, None
        expr, error = self._expr()
        if error:
            return None, error
        assert expr is not None
        return {"kind": "result", "value": None, "children": [expr], "offset": expr["offset"]}, None

    # ── 表达式层 ────────────────────────────────────────────────
    def _expr(self) -> tuple[dict | None, DslError | None]:
        return self._or_expr()

    def _or_expr(self) -> tuple[dict | None, DslError | None]:
        left, error = self._and_expr()
        if error:
            return None, error
        while self._at_keyword("OR"):
            token = self._next()
            right, error = self._and_expr()
            if error:
                return None, error
            assert left is not None and right is not None
            left = {"kind": "bin", "value": "or", "children": [left, right], "offset": token[2]}
        return left, None

    def _and_expr(self) -> tuple[dict | None, DslError | None]:
        left, error = self._not_expr()
        if error:
            return None, error
        while self._at_keyword("AND"):
            token = self._next()
            right, error = self._not_expr()
            if error:
                return None, error
            assert left is not None and right is not None
            left = {"kind": "bin", "value": "and", "children": [left, right], "offset": token[2]}
        return left, None

    def _not_expr(self) -> tuple[dict | None, DslError | None]:
        if self._at_keyword("NOT"):
            token = self._next()
            operand, error = self._not_expr()
            if error:
                return None, error
            assert operand is not None
            return {"kind": "unary", "value": "not", "children": [operand], "offset": token[2]}, None
        return self._cmp_expr()

    def _cmp_expr(self) -> tuple[dict | None, DslError | None]:
        left, error = self._add_expr()
        if error:
            return None, error
        token = self._peek()
        if token and token[0] == "op" and token[1] in _CMP_OPS:
            self._next()
            right, error = self._add_expr()
            if error:
                return None, error
            assert left is not None and right is not None
            # 通达信 `=` 是相等比较, `<>` 是不等 —— 归一到 DSL 的 == / !=
            op = {"=": "==", "<>": "!="}.get(token[1], token[1])
            return {"kind": "bin", "value": op, "children": [left, right], "offset": token[2]}, None
        return left, None

    def _add_expr(self) -> tuple[dict | None, DslError | None]:
        left, error = self._mul_expr()
        if error:
            return None, error
        while self._at_op("+", "-"):
            token = self._next()
            right, error = self._mul_expr()
            if error:
                return None, error
            assert left is not None and right is not None
            left = {"kind": "bin", "value": token[1], "children": [left, right], "offset": token[2]}
        return left, None

    def _mul_expr(self) -> tuple[dict | None, DslError | None]:
        left, error = self._unary()
        if error:
            return None, error
        while self._at_op("*", "/"):
            token = self._next()
            right, error = self._unary()
            if error:
                return None, error
            assert left is not None and right is not None
            left = {"kind": "bin", "value": token[1], "children": [left, right], "offset": token[2]}
        return left, None

    def _unary(self) -> tuple[dict | None, DslError | None]:
        if self._at_op("-"):
            token = self._next()
            operand, error = self._unary()
            if error:
                return None, error
            assert operand is not None
            return {"kind": "unary", "value": "-", "children": [operand], "offset": token[2]}, None
        return self._primary()

    def _primary(self) -> tuple[dict | None, DslError | None]:
        token = self._peek()
        if token is None:
            return None, DslError("E303", "语法错误: 表达式意外结束", offset=len(self._text))
        kind, value, offset = self._next()
        if kind == "num":
            return {"kind": "num", "value": value, "children": [], "offset": offset}, None
        if kind == "str":
            return {"kind": "str", "value": value, "children": [], "offset": offset}, None
        if kind == "ident":
            if value.upper() in _KEYWORDS:
                return None, DslError("E303", f"语法错误: 关键字 '{value}' 不能作为操作数", offset=offset)
            if self._at_op("("):
                return self._call(value, offset)
            return {"kind": "col", "value": value, "children": [], "offset": offset}, None
        if kind == "op" and value == "(":
            expr, error = self._expr()
            if error:
                return None, error
            if not self._at_op(")"):
                return None, DslError("E303", "语法错误: 缺少右括号 ')'", offset=offset)
            self._next()
            return expr, None
        return None, DslError("E303", f"语法错误: 意外的记号 '{value}'", offset=offset)

    def _call(self, name: str, offset: int) -> tuple[dict | None, DslError | None]:
        self._next()  # 吃掉 '('
        args: list[dict] = []
        if not self._at_op(")"):
            while True:
                arg, error = self._expr()
                if error:
                    return None, error
                assert arg is not None
                args.append(arg)
                if self._at_op(","):
                    self._next()
                    continue
                break
        if not self._at_op(")"):
            return None, DslError("E303", f"语法错误: 函数 '{name}' 缺少右括号", offset=offset)
        self._next()
        return {"kind": "call", "value": name, "children": args, "offset": offset}, None


def parse_formula(text: str) -> tuple[list[dict], DslError | None]:
    """切词 + 解析一步到位; 永不抛异常。"""
    if not isinstance(text, str) or not text.strip():
        return [], DslError("E301", "公式为空")
    tokens, error = tokenize(text)
    if error:
        return [], error
    return Parser(tokens, text).parse()
