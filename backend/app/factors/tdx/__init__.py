"""通达信公式支持 (P1-17)。

三档公式语言的第三档: lang="tdx"。
翻译到因子 DSL 后复用既有编译/求值/缓存链路, 不新造求值引擎。
"""
from __future__ import annotations

from app.factors.tdx.builtins import (
    COLUMN_ALIASES,
    FUNCTION_MAP,
    REJECTED_FUNCTIONS,
    compatibility_table,
)
from app.factors.tdx.parser import parse_formula
from app.factors.tdx.translate import (
    TranslateResult,
    compile_tdx_formula,
    compile_tdx_formula_cached,
    translate_tdx,
)

__all__ = [
    "COLUMN_ALIASES",
    "FUNCTION_MAP",
    "REJECTED_FUNCTIONS",
    "TranslateResult",
    "compatibility_table",
    "compile_tdx_formula",
    "compile_tdx_formula_cached",
    "parse_formula",
    "translate_tdx",
]
