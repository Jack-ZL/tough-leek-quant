"""P1-16 静态未来函数门控回归 — 负偏移位移调用拦截。

覆盖: polars 方法形式 / 矩阵后端函数形式 / 关键字参数 / 常量折叠
(一元负号、算术、常量名、pl.lit 包装) 必须拦截; 正偏移、动态偏移、
numpy 首参为数组等合法写法不得误伤; 存量策略文件全部仍通过。
"""
from __future__ import annotations

import ast
import textwrap

import pytest

from app.strategy.ai_generator import AIStrategyGenerator, _detect_lookahead


def _validate(code: str, **kwargs) -> None:
    AIStrategyGenerator._validate_safety(textwrap.dedent(code), **kwargs)


# ================================================================
# 必须拦截: 可证明为负的偏移
# ================================================================
BLOCKED = {
    "polars_shift_negative": 'pl.col("close").shift(-1)',
    "polars_shift_and_fill_negative": 'pl.col("close").shift_and_fill(-2)',
    "polars_diff_negative": 'pl.col("close").diff(-1)',
    "polars_shift_keyword_negative": 'pl.col("close").shift(n=-1)',
    "matrix_shift_negative": "shift(close_arr, -1)",
    "matrix_valid_shift_negative": "valid_shift(close_arr, -3)",
    "matrix_valid_shift_keyword_negative": "valid_shift(close_arr, periods=-1)",
    "unary_minus_paren": 'pl.col("close").shift(-(1))',
    "arith_mult": 'pl.col("close").shift(-1 * 2)',
    "arith_sub": 'pl.col("close").shift(1 - 3)',
    "const_name_negative": 'N = -1\npl.col("close").shift(N)',
    "const_name_ann_assign": 'N: int = -2\npl.col("close").shift(N)',
    "const_name_arith": 'LAG = 1 - 4\npl.col("close").shift(LAG)',
    "pl_lit_wrapped": 'pl.col("close").shift(pl.lit(-1))',
    "bare_lit_wrapped": 'from polars import lit\npl.col("close").shift(lit(-2))',
    "nested_in_expression": 'entry = (pl.col("close") > pl.col("open").shift(-1))',
}


@pytest.mark.parametrize("code", list(BLOCKED.values()), ids=list(BLOCKED))
def test_negative_offset_is_blocked(code):
    with pytest.raises(ValueError, match="未来函数"):
        _validate(code)


def test_full_strategy_with_lookahead_is_blocked():
    """完整策略文件里的未来函数同样拦截, 报错带行号。"""
    code = '''
        import polars as pl

        def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
            # 偷看明天收盘价 — 回测作弊
            return pl.col("close") < pl.col("close").shift(-1)

        META = {"id": "custom_lookahead", "name": "x", "description": "x",
                "scoring": {"close": 1.0}}
    '''
    with pytest.raises(ValueError, match=r"第 6 行.*shift\(-1\)"):
        _validate(code)


def test_detect_lookahead_directly_reports_negative():
    tree = ast.parse('pl.col("close").shift(-5)')
    with pytest.raises(ValueError, match="look-ahead"):
        _detect_lookahead(tree)


# ================================================================
# 不得误伤: 正偏移 / 判不准的动态偏移 / 首参非偏移的调用
# ================================================================
LEGITIMATE = {
    "polars_shift_positive": 'pl.col("close").shift(1)',
    "polars_shift_zero": 'pl.col("close").shift(0)',
    "polars_shift_keyword_positive": 'pl.col("close").shift(n=2)',
    "polars_diff_positive": 'pl.col("close").diff(1)',
    "chained_positive": 'pl.col("volume").shift(1).rolling_mean(10).over("symbol")',
    "matrix_shift_positive": "shift(close_arr, 1)",
    "matrix_valid_shift_positive": "valid_shift(close_arr, 5)",
    "matrix_valid_shift_keyword_positive": "valid_shift(close_arr, periods=2)",
    "dynamic_param_offset": "def f(k):\n    return pl.col('close').shift(k)",
    "dynamic_params_dict": "def f(params):\n    return pl.col('close').shift(params['lag'])",
    "numpy_diff_array_first_arg": "import numpy as np\nnp.diff(close_arr, 2)",
    "numpy_diff_negative_is_numpy_problem": "import numpy as np\nnp.diff(close_arr, -1)",
    "conflicting_const_unresolvable": (
        "def a():\n    N = 1\n    return pl.col('close').shift(N)\n"
        "def b():\n    N = -1\n    return pl.col('close').shift(N)"
    ),
    "string_offset_not_int": 'pl.col("close").shift("x")',
    "shift_as_column_name": 'entry = pl.col("shift") > 0',
    "method_named_shift_on_unknown_receiver_positive": "row.shift(1)",
    "pl_lit_positive": 'pl.col("close").shift(pl.lit(3))',
}


@pytest.mark.parametrize("code", list(LEGITIMATE.values()), ids=list(LEGITIMATE))
def test_legitimate_offsets_pass(code):
    _validate(code)


def test_conflicting_const_is_dropped_not_flagged():
    """同名常量赋值冲突 → 不可解析 → 放行 (宁漏勿误伤, evidence 兜底)。"""
    code = 'N = 1\nN = -1\npl.col("close").shift(N)'
    _validate(code)


# ================================================================
# 兼容性回归: 存量策略与提示词示例不得被误伤
# ================================================================
def test_existing_strategies_in_repo_all_pass():
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    project_root = backend_dir.parent
    roots = [
        project_root / "data" / "strategies",
        backend_dir / "app" / "strategy" / "research",
    ]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(sorted(root.rglob("*.py")))
    files = [f for f in files if f.name != "__init__.py"]
    if not files:
        pytest.skip("no strategy files present")

    for path in files:
        AIStrategyGenerator._validate_safety(path.read_text(encoding="utf-8"))


def test_prompt_example_snippets_pass():
    """提示词指南里的示例位移写法 (全是正偏移) 不得被新门控误伤。"""
    from pathlib import Path

    prompts = Path(__file__).resolve().parents[1] / "app" / "strategy" / "prompts"
    checked = 0
    for md in sorted(prompts.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for block in _python_blocks(text):
            try:
                tree = ast.parse(block)
            except SyntaxError:
                continue
            _detect_lookahead(tree)
            checked += 1
    assert checked > 0, "提示词里应有可解析的 python 示例块"


def _python_blocks(markdown: str):
    import re

    for match in re.finditer(r"```python\n(.*?)```", markdown, re.DOTALL):
        yield match.group(1)


# ================================================================
# 纵深防御: 引擎加载层同样拦截 (文件被直接投放磁盘的场景)
# ================================================================
_LOOKAHEAD_FILE = '''
import polars as pl

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.col("close") < pl.col("close").shift(-1)

META = {"id": "custom_la_file", "name": "la", "description": "la",
        "scoring": {"close": 1.0}}
'''


def test_engine_load_file_rejects_lookahead_strategy(tmp_path):
    from app.strategy.engine import StrategyEngine

    path = tmp_path / "custom_la_file.py"
    path.write_text(textwrap.dedent(_LOOKAHEAD_FILE), encoding="utf-8")

    with pytest.raises(ValueError, match="未来函数"):
        StrategyEngine._load_file(path)


# ================================================================
# P1-16 第二批: 错误回喂模型自动修复的判据
# ================================================================
def test_needs_code_repair_recognizes_lookahead_error():
    """未来函数错误属于"模型能按错误信息自己改对"的类别 → 值得回喂修一次。"""
    from app.strategy.ai_generator import (
        _POLARS_ENTRYPOINT_ERROR,
        is_lookahead_error,
        needs_code_repair,
    )

    lookahead = {"valid": False, "error": "禁止未来函数: 第 5 行 shift(-1) 偏移为负..."}
    assert is_lookahead_error(lookahead) is True
    assert needs_code_repair(lookahead) is True

    # 结构错误同样可修复 (原有行为不得回退)
    assert needs_code_repair(
        {"valid": False, "error": _POLARS_ENTRYPOINT_ERROR}) is True

    # 安全违规 / 校验通过 → 不回喂, 直接失败上报, 不浪费 LLM 调用
    assert needs_code_repair({"valid": False, "error": "禁止导入模块: os"}) is False
    assert needs_code_repair({"valid": True, "error": None}) is False
    assert is_lookahead_error({"valid": True, "error": None}) is False


def test_lookahead_error_message_is_actionable():
    """错误文案必须自带正确写法 — 回喂修复和前端 toast 都靠它, 不能只说"不行"。"""
    with pytest.raises(ValueError) as exc_info:
        _validate('pl.col("close").shift(-1)')
    message = str(exc_info.value)
    assert "禁止未来函数" in message
    assert "shift(1)" in message  # 给出正确替代写法
    assert "look-ahead" in message


# ================================================================
# P1-16 第二批: 提示词必须提前告知, 别等门控拦下才说
# ================================================================
@pytest.mark.parametrize("filename", [
    "strategy-guide.md",
    "strategy-guide-compact.md",
])
def test_generation_prompts_warn_against_lookahead(filename):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "app" / "strategy" / "prompts"
            / filename).read_text(encoding="utf-8")
    assert "未来函数" in text, f"{filename} 缺少未来函数禁令"
    assert "不得为负" in text, f"{filename} 未说明偏移量不得为负"
    assert "shift(1)" in text, f"{filename} 应给出正偏移的正确写法"


def test_example_and_builder_prompts_annotate_positive_shift():
    """示例/脚手架是模型直接照抄的对象, 就地标注正偏移要求。"""
    from pathlib import Path

    prompts = Path(__file__).resolve().parents[1] / "app" / "strategy" / "prompts"
    for filename in ("strategy-example.md", "strategy-builder-step1.md"):
        text = (prompts / filename).read_text(encoding="utf-8")
        assert "未来数据" in text, f"{filename} 的 shift 示例缺少负偏移警示注释"
