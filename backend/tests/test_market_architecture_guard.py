"""架构守卫: 市场差异必须住在规则表里, 不得散落成 ``if market == ...`` 分支。

为什么需要这条守卫
------------------
``app/market.py`` 的 ``MarketTradingRules`` 是市场差异的**唯一**住所: 时段、
每手股数、币种、是否有涨跌停、探针篮子、时区、开盘缓冲窗……新增市场 = 往表里
加一行, 而不是去 344 个文件里找 ``if`` 加分支。

一旦 ``if market == "HK":`` 这类分支被允许存在, 就会发生:

* **漏改**: 加第三个市场时, 没人能枚举出全仓有多少处分支 (grep 也找不全 ——
  写法有 ``==`` / ``in`` / ``match`` / 三元 / 字典 get 默认值……)。
* **静默错答**: 漏改的分支不会报错, 只会走 ``else``, 用 A 股规则算出一个
  看起来合理的错误数字。这正是 QuantMind 港股印花税静默归零的形状。
* **测试覆盖假象**: 每个分支都要 × 市场数, 而分支是散落的, 没人知道覆盖率。

为什么用 AST 而不是 CI 里的 grep
--------------------------------
grep 会命中注释与文档字符串 —— ``app/market.py`` 和 ``app/services/trading_day.py``
的 docstring 里就**引用了这个反模式作为警示**, 裸 grep 会被自己的文档绊倒。
AST 只看真实的 ``Compare`` 节点, 天然跳过注释/字符串, 也不需要改 CI workflow
(pytest 已经在跑)。

豁免机制
--------
确有正当理由时, 把 ``"相对路径:行号"`` 加进 ``ALLOWLIST`` 并写明原因。
**不要**放宽匹配规则本身 —— 放宽一次, 守卫就永久失效了。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# 豁免清单: "相对 app/ 的路径:行号" -> 原因。当前为空 —— 守卫必须是零豁免起步,
# 否则第一次加豁免就没有阻力了。
ALLOWLIST: dict[str, str] = {}

_CMP_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — 语法树异常不该让守卫崩掉
        return ""


def _is_market_expr(node: ast.expr) -> bool:
    """表达式是否在谈论 market (``market`` / ``rules.market`` / ``row["market"]``)。"""
    text = _unparse(node)
    return "market" in text.lower()


def _is_market_literal(node: ast.expr) -> bool:
    """是否是市场字面量 (``"HK"``)、``Market.HK`` 枚举成员, 或**含它们的容器**。

    容器必须展开: ``market in ("HK", "US")`` 的右侧是 ``ast.Tuple`` 而不是
    ``ast.Constant``, 不展开就会漏掉 ``in`` —— 而 ``in`` 恰恰是加第三个市场时
    最常见的分支写法。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().upper() in {"CN", "HK"}
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id == "Market" and node.attr in {"CN", "HK"}
    if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        return any(_is_market_literal(element) for element in node.elts)
    return False


def _literal_side_of_compare(node: ast.Compare) -> bool:
    """比较的一侧是市场字面量, 另一侧在谈论 market。"""
    operands = [node.left, *node.comparators]
    has_literal = any(_is_market_literal(operand) for operand in operands)
    has_market = any(_is_market_expr(operand) for operand in operands)
    return has_literal and has_market


def _iter_app_sources():
    yield from sorted(APP_ROOT.rglob("*.py"))


def _collect_violations() -> list[str]:
    violations: list[str] = []
    for path in _iter_app_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - 语法错由别的门拦
            continue
        relative = path.relative_to(APP_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, _CMP_OPS) for op in node.ops):
                continue
            if not _literal_side_of_compare(node):
                continue
            key = f"{relative}:{node.lineno}"
            if key in ALLOWLIST:
                continue
            violations.append(f"{key}  {ast.unparse(node).strip()}")
    return violations


def test_no_market_literal_branches_in_app():
    """``app/`` 内不得出现 ``market`` 与市场字面量的比较。

    失败时的正确修法**不是**加豁免, 而是把差异搬进 ``MarketTradingRules``:
    给规则表加一个字段, 然后读 ``rules.<字段>``。
    """
    violations = _collect_violations()
    assert not violations, (
        "发现按市场字面量分支的代码 —— 市场差异必须住在 app/market.py 的规则表里:\n  "
        + "\n  ".join(violations)
        + "\n修法: 给 MarketTradingRules 加字段, 改成读 rules.<字段>; "
        "确有正当理由才加进本文件的 ALLOWLIST 并写明原因。"
    )


def test_guard_itself_is_not_vacuous():
    """守卫自检: 它必须真的能抓到违规, 否则上面那条通过毫无意义。

    用一段合成的违规源码喂给同一套判定逻辑 —— 若哪天 ``_is_market_expr``
    或 ``_is_market_literal`` 被改松, 这条会先失败。
    """
    samples = [
        'if market == "HK":\n    pass\n',
        'if rules.market != "CN":\n    pass\n',
        'if market in ("HK", "US"):\n    pass\n',
        "if market is Market.HK:\n    pass\n",
        'x = 1 if market == "HK" else 2\n',
        'if row["market"] == "CN":\n    pass\n',
    ]
    for source in samples:
        tree = ast.parse(source)
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(isinstance(op, _CMP_OPS) for op in node.ops)
            and _literal_side_of_compare(node)
        ]
        assert found, f"守卫漏掉了违规写法: {source!r}"


@pytest.mark.parametrize(
    "benign",
    [
        'if symbol == "600519.SH":\n    pass\n',          # 不是 market
        'if status == "CN":\n    pass\n',                 # 谈论的不是 market
        "if market is None:\n    pass\n",                 # 没有市场字面量
        'if currency == "CNY":\n    pass\n',              # 别的维度
        "if len(markets) == 2:\n    pass\n",              # 数字比较
    ],
)
def test_guard_does_not_flag_benign_comparisons(benign):
    """守卫不得误伤: 误报会逼人加豁免, 豁免一多守卫就死了。"""
    tree = ast.parse(benign)
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, _CMP_OPS) for op in node.ops)
        and _literal_side_of_compare(node)
    ]
    assert not found, f"误报: {benign!r}"


def test_allowlist_entries_still_exist():
    """豁免清单不得留死条目 (代码搬走后行号失效, 豁免会静默失去意义)。"""
    stale = []
    for key in ALLOWLIST:
        relative, _, lineno = key.rpartition(":")
        path = APP_ROOT / relative
        if not path.exists():
            stale.append(f"{key} (文件不存在)")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not (1 <= int(lineno) <= len(lines)):
            stale.append(f"{key} (行号越界, 文件只有 {len(lines)} 行)")
    assert not stale, "ALLOWLIST 有失效条目, 请复核后删除或更新行号:\n  " + "\n  ".join(stale)


def test_rules_table_covers_every_market():
    """规则表必须覆盖枚举里的每个市场 —— 漏一个就会在运行时 KeyError。

    这是守卫的正面配套: 不许写分支, 那就必须保证表是全的。
    """
    from app.market import RULES_BY_MARKET, Market

    assert set(RULES_BY_MARKET) == set(Market)
    for market, rules in RULES_BY_MARKET.items():
        assert rules.market is market, f"{market} 的规则表条目 market 字段指错了"
