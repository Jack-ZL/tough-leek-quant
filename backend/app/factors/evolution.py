"""公式进化遗传算子 (P2-22a) — 纯函数, 无 I/O, 无大模型。

树表示直接复用 DSL 的 AST dict 形状 ({"kind","value","children","offset"}),
所有遗传产物统一 unparse 回 DSL 文本、再走 compile_formula 的全部安全闸
(未来函数拦截 / 节点与深度上限 / E009 截面算子嵌套拒绝 / E016 纯常量拒绝),
本模块自身不做语义校验 — 非法产物由编译闸拒绝, 调用方丢弃重试即可。

设计约束 (与 app/backtest/evolution_runtime.py 配合):
- 叶子只允许行情列 (不生成裸数字), 从根上避免 E016 常量表达式;
- 截面算子 (rank/zscore) 只在非时序上下文生成, 避免 E009;
- 窗口常量只取 WINDOWS 白名单, 避免 E004;
- canonical() 提供交换律归一化键, 供运行时全局去重 (a+b 与 b+a 同键)。
"""

from __future__ import annotations

import copy
import random
from collections.abc import Iterator, Sequence
from typing import Any

from app.factors.dsl import (
    MAX_AST_DEPTH,
    OPERATORS,
    _ast_depth,
    parse_formula,
)

# ------------------------------------------------------------------ 词表
# 叶子列: 只用日线面板必有列 (turnover_rate 缺失时 compile 的 E013 会兜底,
# 运行时按 dependencies 过滤, 缺列公式 fitness 记 0 自然淘汰)。
LEAF_COLUMNS: tuple[str, ...] = (
    "close", "open", "high", "low", "volume", "amount", "turnover_rate",
)

# 一元算子 (1 个表达式参数, 无常量)
UNARY_OPS: tuple[str, ...] = ("log", "abs", "sign", "sqrt", "-")
# 中缀二元算子 (2 个表达式参数, 无常量)
BIN_OPS: tuple[str, ...] = ("+", "-", "*", "/")
# min/max 在 DSL 里是函数调用 (min(a,b)), 不是中缀算子
MINMAX_OPS: tuple[str, ...] = ("min", "max")
# 时序算子 (1 个表达式参数 + 窗口 n)
TS1_OPS: tuple[str, ...] = (
    "ts_mean", "ts_std", "ts_sum", "ts_max", "ts_min",
    "ts_delay", "ts_delta", "ts_rank", "ts_zscore",
    "decay_linear", "ts_ema",
)
# 时序算子 (2 个表达式参数 + 窗口 n)
TS2_OPS: tuple[str, ...] = ("ts_corr", "ts_cov")
# 截面算子 (1 个表达式参数) — 禁止出现在任何 ts 窗口内部 (E009)
CROSS_OPS: tuple[str, ...] = ("rank", "zscore")
# 窗口白名单 (交易日)
WINDOWS: tuple[int, ...] = (2, 3, 5, 10, 20, 30, 60)
# ts_quantile 的分位白名单
QUANTILES: tuple[float, ...] = (0.2, 0.5, 0.8)

# 交换律算子: canonical 键对子节点排序, 使 a+b 与 b+a 同键
COMMUTATIVE_OPS: frozenset[str] = frozenset({"+", "*", "min", "max", "ts_corr", "ts_cov"})

# 生成树默认最大深度 (DSL 硬上限是 MAX_AST_DEPTH=12, 这里远低于它;
# 交叉可能加深, 由 crossover 内部按 MAX_AST_DEPTH 拒绝)
DEFAULT_MAX_DEPTH = 4

# 随机树中叶子出现的基础概率 (深度越浅越倾向叶子)
_LEAF_PROB = 0.3

# unparse 优先级 (数字越大结合越紧)
_PREC_OR, _PREC_AND, _PREC_CMP, _PREC_ADD, _PREC_MUL, _PREC_UNARY = 1, 2, 3, 4, 5, 6
_BIN_PREC: dict[str, int] = {
    "or": _PREC_OR, "and": _PREC_AND,
    ">": _PREC_CMP, "<": _PREC_CMP, ">=": _PREC_CMP, "<=": _PREC_CMP,
    "==": _PREC_CMP, "!=": _PREC_CMP,
    "+": _PREC_ADD, "-": _PREC_ADD,
    "*": _PREC_MUL, "/": _PREC_MUL,
}

# 种子公式: 经典量价因子, 作为初始种群的一部分 (registry 基础因子的
# formula_text 是中文描述而非 DSL, 不能直接当种子, 故手工给出)。
SEED_FORMULAS: tuple[str, ...] = (
    "close / ts_delay(close, 5) - 1",
    "close / ts_mean(close, 20) - 1",
    "ts_rank(close, 20)",
    "volume / ts_mean(volume, 20) - 1",
    "-ts_std(close / ts_delay(close, 1) - 1, 20)",
    "(close - ts_min(low, 20)) / (ts_max(high, 20) - ts_min(low, 20))",
    "rank(amount)",
    "ts_corr(close, volume, 10)",
    "ts_delta(ts_mean(close, 5), 5)",
    "close / ts_max(close, 60) - 1",
    "ts_zscore(amount, 20)",
    "sign(ts_delta(close, 3)) * ts_rank(volume, 10)",
)


# ------------------------------------------------------------------ 节点构造
def node(kind: str, value: Any = None, children: list[dict] | None = None) -> dict:
    """构造 AST 节点 (offset 仅用于报错定位, 遗传产物统一置 0)。"""
    return {"kind": kind, "value": value, "children": children or [], "offset": 0}


def col_node(name: str) -> dict:
    return node("col", name)


def num_node(value: float) -> dict:
    return node("num", float(value))


def _num_text(value: Any) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


# ------------------------------------------------------------------ unparse
def _prec_of(node_: dict) -> int:
    kind = node_["kind"]
    if kind in ("col", "num"):
        return 10
    if kind == "call":
        return 9
    if kind == "unary":
        return _PREC_UNARY
    return _BIN_PREC.get(str(node_["value"]), _PREC_ADD)


def unparse(node_: dict) -> str:
    """AST → DSL 文本 (带最小必要括号, 产物可直接 compile_formula)。"""
    kind = node_["kind"]
    if kind == "col":
        return str(node_["value"])
    if kind == "num":
        return _num_text(node_["value"])
    if kind == "call":
        args = ", ".join(unparse(child) for child in node_["children"])
        return f"{node_['value']}({args})"
    if kind == "unary":
        child = node_["children"][0]
        text = unparse(child)
        if _prec_of(child) <= _PREC_UNARY:
            text = f"({text})"
        return f"-{text}" if node_["value"] == "-" else f"{node_['value']}{text}"
    # bin
    op = str(node_["value"])
    prec = _BIN_PREC.get(op, _PREC_ADD)
    left, right = node_["children"]
    left_text = unparse(left)
    if _prec_of(left) < prec:
        left_text = f"({left_text})"
    right_text = unparse(right)
    right_prec = _prec_of(right)
    # 所有二元算子均左结合: 右子同级 (如 a+(b-c)、a*(b/c)) 必须加括号,
    # 否则重解析会左结合成 ((a+b)-c), 数学等价但结构漂移, 破坏 canonical 往返。
    if right_prec <= prec:
        right_text = f"({right_text})"
    return f"{left_text} {op} {right_text}"


# ------------------------------------------------------------------ 遍历
def _expr_positions(tree: dict) -> list[tuple[tuple[int, ...], dict, bool]]:
    """枚举所有"表达式槽位": (路径, 节点, 是否处于 ts 窗口内部)。

    只返回可被遗传操作替换的位置 — call 的常量参数 (窗口 n / 分位 q)
    不是表达式槽位, 替换会破坏签名 (E003), 故排除。
    """
    out: list[tuple[tuple[int, ...], dict, bool]] = []
    stack: list[tuple[tuple[int, ...], dict, bool]] = [((), tree, False)]
    while stack:
        path, current, in_ts = stack.pop()
        out.append((path, current, in_ts))
        if current["kind"] == "call":
            n_expr = OPERATORS[str(current["value"])][0]
            child_in_ts = in_ts or str(current["value"]).startswith(("ts_", "decay_"))
            for index, child in enumerate(current["children"]):
                if index < n_expr:
                    stack.append(((*path, index), child, child_in_ts))
        elif current["kind"] in ("bin", "unary"):
            for index, child in enumerate(current["children"]):
                stack.append(((*path, index), child, in_ts))
    return out


def _node_at(tree: dict, path: tuple[int, ...]) -> dict:
    current = tree
    for index in path:
        current = current["children"][index]
    return current


def _replace_at(tree: dict, path: tuple[int, ...], replacement: dict) -> dict:
    new_tree = copy.deepcopy(tree)
    if not path:
        return copy.deepcopy(replacement)
    parent = _node_at(new_tree, path[:-1])
    parent["children"][path[-1]] = copy.deepcopy(replacement)
    return new_tree


def contains_cross_op(tree: dict) -> bool:
    stack = [tree]
    while stack:
        current = stack.pop()
        if current["kind"] == "call" and str(current["value"]) in ("rank", "zscore", "winsorize"):
            return True
        stack.extend(current["children"])
    return False


def tree_depth(tree: dict) -> int:
    return _ast_depth(tree)


# ------------------------------------------------------------------ 随机生成
def _random_window(rng: random.Random) -> float:
    return float(rng.choice(WINDOWS))


def random_tree(rng: random.Random, max_depth: int = DEFAULT_MAX_DEPTH, *, _in_ts: bool = False) -> dict:
    """随机生成一棵合法倾向的表达式树 (最终合法性仍由 compile_formula 裁决)。"""
    if max_depth <= 1 or rng.random() < _LEAF_PROB * (DEFAULT_MAX_DEPTH + 1 - max_depth) / DEFAULT_MAX_DEPTH:
        return col_node(rng.choice(LEAF_COLUMNS))

    weights: list[tuple[str, float]] = [
        ("ts1", 0.30), ("ts2", 0.08), ("tsq", 0.04),
        ("bin", 0.26), ("minmax", 0.06), ("unary", 0.10),
    ]
    if not _in_ts:
        weights.append(("cross", 0.18))
    total = sum(weight for _, weight in weights)
    pick = rng.random() * total
    choice = weights[-1][0]
    for name, weight in weights:
        pick -= weight
        if pick <= 0:
            choice = name
            break

    if choice == "ts1":
        op = rng.choice(TS1_OPS)
        return node("call", op, [random_tree(rng, max_depth - 1, _in_ts=True), num_node(_random_window(rng))])
    if choice == "ts2":
        op = rng.choice(TS2_OPS)
        return node("call", op, [
            random_tree(rng, max_depth - 1, _in_ts=True),
            random_tree(rng, max_depth - 1, _in_ts=True),
            num_node(_random_window(rng)),
        ])
    if choice == "tsq":
        return node("call", "ts_quantile", [
            random_tree(rng, max_depth - 1, _in_ts=True),
            num_node(_random_window(rng)),
            num_node(rng.choice(QUANTILES)),
        ])
    if choice == "cross":
        op = rng.choice(CROSS_OPS)
        return node("call", op, [random_tree(rng, max_depth - 1)])
    if choice == "minmax":
        op = rng.choice(MINMAX_OPS)
        return node("call", op, [
            random_tree(rng, max_depth - 1, _in_ts=_in_ts),
            random_tree(rng, max_depth - 1, _in_ts=_in_ts),
        ])
    if choice == "unary":
        op = rng.choice(UNARY_OPS)
        child = random_tree(rng, max_depth - 1, _in_ts=_in_ts)
        if op == "-":
            return node("unary", "-", [child])
        return node("call", op, [child])
    op = rng.choice(BIN_OPS)
    return node("bin", op, [
        random_tree(rng, max_depth - 1, _in_ts=_in_ts),
        random_tree(rng, max_depth - 1, _in_ts=_in_ts),
    ])


# ------------------------------------------------------------------ 交叉 / 变异
def crossover(recipient: dict, donor: dict, rng: random.Random, *, attempts: int = 8) -> dict | None:
    """子树交换交叉; 违反约束 (E009 截面嵌套 / 深度超限) 时返回 None。"""
    recipient_slots = _expr_positions(recipient)
    # 裸数字不做供体: 换进表达式槽位易产生 E016 纯常量表达式
    donor_slots = [slot for slot in _expr_positions(donor) if slot[1]["kind"] != "num"]
    if not recipient_slots or not donor_slots:
        return None
    for _ in range(attempts):
        r_path, _, r_in_ts = rng.choice(recipient_slots)
        _d_path, d_node_, _ = rng.choice(donor_slots)
        if r_in_ts and contains_cross_op(d_node_):
            continue
        child = _replace_at(recipient, r_path, d_node_)
        if tree_depth(child) > MAX_AST_DEPTH:
            continue
        return child
    return None


def mutate(tree: dict, rng: random.Random, max_depth: int = DEFAULT_MAX_DEPTH) -> dict | None:
    """单点变异: 换窗口 / 换叶子 / 同类换算子 / 子树重生 / 包裹算子。"""
    slots = _expr_positions(tree)
    if not slots:
        return None
    strategy = rng.choice(("window", "leaf", "op", "subtree", "wrap"))

    if strategy == "window":
        call_nodes: list[dict] = []
        stack = [tree]
        while stack:
            current = stack.pop()
            if current["kind"] == "call" and OPERATORS[str(current["value"])][1][:1] == ("n",):
                call_nodes.append(current)
            stack.extend(current["children"])
        if not call_nodes:
            return None
        target = rng.choice(call_nodes)
        n_expr = OPERATORS[str(target["value"])][0]
        old = float(target["children"][n_expr]["value"])
        options = [w for w in WINDOWS if float(w) != old]
        new_tree = copy.deepcopy(tree)
        _node_at(new_tree, _path_of(tree, target))["children"][n_expr] = num_node(rng.choice(options))
        return new_tree

    if strategy == "leaf":
        leaf_slots = [(p, n) for p, n, _ in slots if n["kind"] == "col"]
        if not leaf_slots:
            return None
        path, target = rng.choice(leaf_slots)
        options = [c for c in LEAF_COLUMNS if c != target["value"]]
        return _replace_at(tree, path, col_node(rng.choice(options)))

    if strategy == "op":
        call_slots = [(p, n, in_ts) for p, n, in_ts in slots if n["kind"] == "call"]
        if not call_slots:
            return None
        path, target, in_ts = rng.choice(call_slots)
        name = str(target["value"])
        for pool in (TS1_OPS, TS2_OPS, CROSS_OPS, MINMAX_OPS, ("log", "abs", "sign", "sqrt")):
            if name in pool:
                options = [op for op in pool if op != name]
                # 截面算子不能出现在 ts 上下文内 (E009)
                if in_ts:
                    options = [op for op in options if op not in CROSS_OPS]
                # ts_delay/ts_delta 允许 n<2, 其余时序算子要求 n ≥ WINDOW_MIN (E004)
                if name in TS1_OPS or name in TS2_OPS:
                    n_expr = OPERATORS[name][0]
                    window = float(target["children"][n_expr]["value"])
                    if window < 2:
                        options = [op for op in options if op in ("ts_delay", "ts_delta")]
                if not options:
                    return None
                new_tree = copy.deepcopy(tree)
                _node_at(new_tree, path)["value"] = rng.choice(options)
                return new_tree
        return None

    if strategy == "subtree":
        path, _, in_ts = rng.choice(slots)
        return _replace_at(
            tree, path,
            random_tree(rng, min(2, max(1, max_depth - len(path))), _in_ts=in_ts),
        )

    # wrap: 用 ts / 截面算子包裹一个表达式节点。
    # E009 约束: 含截面算子的子树不能再套 ts 窗口; ts 上下文内不能再套截面算子。
    candidates = [
        (p, n, in_ts) for p, n, in_ts in slots
        if n["kind"] != "num"  # 包裹裸常量只会产出 E016 废料
        and tree_depth(n) + 1 <= MAX_AST_DEPTH
        and not (in_ts and contains_cross_op(n))
    ]
    if not candidates:
        return None
    path, target, in_ts = rng.choice(candidates)
    has_cross = contains_cross_op(target)
    if has_cross or in_ts:
        # 含截面算子的子树只能再套截面算子; ts 上下文内只能套 ts 算子
        if has_cross and in_ts:
            return None
        if in_ts:
            wrapper = node("call", rng.choice(TS1_OPS), [target, num_node(_random_window(rng))])
        else:
            wrapper = node("call", rng.choice(CROSS_OPS), [target])
    elif rng.random() < 0.7:
        wrapper = node("call", rng.choice(TS1_OPS), [target, num_node(_random_window(rng))])
    else:
        wrapper = node("call", rng.choice(CROSS_OPS), [target])
    new_tree = _replace_at(tree, path, wrapper)
    if tree_depth(new_tree) > MAX_AST_DEPTH:
        return None
    return new_tree


def _path_of(tree: dict, target: dict) -> tuple[int, ...]:
    """按对象身份找节点路径 (mutate 的 window 策略用)。"""
    stack: list[tuple[tuple[int, ...], dict]] = [((), tree)]
    while stack:
        path, current = stack.pop()
        if current is target:
            return path
        for index, child in enumerate(current["children"]):
            stack.append(((*path, index), child))
    raise ValueError("node not in tree")


# ------------------------------------------------------------------ 归一化键
def canonical(tree: dict) -> str:
    """交换律归一化键: a+b 与 b+a、min(a,b) 与 min(b,a) 得到同一个键。

    只做结构级归一 (不展开数学恒等式, 如 a-b 与 a+(-b) 不同键),
    剩余语义重复由运行时的相关性剪枝兜底。
    """
    kind = tree["kind"]
    if kind == "col":
        return str(tree["value"])
    if kind == "num":
        return _num_text(tree["value"])
    if kind == "unary":
        return f"(-{canonical(tree['children'][0])})"
    if kind == "call":
        name = str(tree["value"])
        n_expr = OPERATORS[name][0]
        exprs = [canonical(child) for child in tree["children"][:n_expr]]
        consts = [_num_text(child["value"]) for child in tree["children"][n_expr:]]
        if name in COMMUTATIVE_OPS:
            exprs.sort()
        return f"{name}({','.join(exprs + consts)})"
    op = str(tree["value"])
    left, right = (canonical(child) for child in tree["children"])
    if op in COMMUTATIVE_OPS:
        left, right = sorted((left, right))
    return f"({left}{op}{right})"


def formula_key(text: str) -> str | None:
    """DSL 文本 → 归一化键; 解析失败返回 None。"""
    tree, error = parse_formula(text)
    if tree is None or error is not None:
        return None
    return canonical(tree)


# ------------------------------------------------------------------ 初始种群
def parse_seed(text: str) -> dict | None:
    tree, error = parse_formula(text)
    if tree is None or error is not None:
        return None
    return tree


def initial_population(
    rng: random.Random,
    size: int,
    max_depth: int = DEFAULT_MAX_DEPTH,
    extra_formulas: Sequence[str] | None = None,
) -> list[dict]:
    """初始种群 = 种子公式 + 额外公式 (如 LLM 提案) + 随机树补足 (canonical 去重)。

    extra_formulas 解析失败的静默跳过 (调用方已过编译闸, 此处双保险);
    种子+额外超过 size 时截断 (随机补足让位, 提案优先入种群)。
    """
    population: list[dict] = []
    seen: set[str] = set()
    for text in (*SEED_FORMULAS, *(extra_formulas or ())):
        tree = parse_seed(text)
        if tree is None:
            continue
        key = canonical(tree)
        if key in seen:
            continue
        seen.add(key)
        population.append(tree)
        if len(population) >= size:
            return population
    guard = 0
    while len(population) < size and guard < size * 20:
        guard += 1
        tree = random_tree(rng, max_depth)
        key = canonical(tree)
        if key in seen:
            continue
        seen.add(key)
        population.append(tree)
    return population


def iter_nodes(tree: dict) -> Iterator[dict]:
    stack = [tree]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current["children"])
