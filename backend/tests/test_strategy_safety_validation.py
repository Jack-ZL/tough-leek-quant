"""策略安全校验回归 — GH #475 AST 黑名单绕过 + P1-16 不变量锁。

覆盖三层:
  1. 已报告的逃逸载荷 (别名赋值 / 字符串拼接下标 / dunder 字符串参数) 与
     同类未报告逃逸 (模块命名空间句柄 pl.os / np.lib、文件读写原语、通配导入);
  2. 合法策略代码不被误伤 (含仓库存量策略全量扫描);
  3. P1-16 第三批「不变量锁」— 直接遍历校验器所用的黑名单/白名单常量,
     逐个断言执行路径真的会拦, 并锁死白名单内容与"安全错误优先于未来函数"
     的门控顺序。新增黑名单项自动纳入覆盖, 改坏执行分支或往白名单塞危险
     模块都会立刻红。
"""
from __future__ import annotations

import textwrap

import pytest

from app.strategy.ai_generator import (
    _BENIGN_DUNDER_NAMES,
    _BENIGN_DUNDER_TEXTS,
    _FORBIDDEN_ATTR_NAMES,
    _FORBIDDEN_REF_NAMES,
    _INTROSPECTABLE_ROOTS,
    AIStrategyGenerator,
    _is_dunder,
    _module_attr_names,
)

# P1-16 第三批: 不变量锁测试直接引用类上的白名单常量
_ALLOWED_IMPORT_MODULES = AIStrategyGenerator._ALLOWED_IMPORT_MODULES


def _validate(code: str, **kwargs) -> None:
    AIStrategyGenerator._validate_safety(textwrap.dedent(code), **kwargs)


# ================================================================
# 已报告逃逸 (GH #475 PoC 与最小变体): 必须全部拦截
# ================================================================
REPORTED_ESCAPES = {
    "issue_poc_alias_and_concat": '''
        import datetime

        g = getattr
        s = "__imp" + "ort__"
        imp = __builtins__[s]
        os_mod = imp("os")
        os_mod.popen("(id; uname -srm) > /tmp/TICK_RCE_PROOF.txt 2>&1").read()

        def filter(df):
            return df

        META = {"id": "custom_poc_rce", "name": "poc", "description": "poc",
                "scoring": {"close": 1.0}}
    ''',
    "alias_getattr_assignment": "g = getattr",
    "alias_globals_then_call": "gg = globals\nv = gg()",
    "concat_subscript_import": 'imp = __builtins__["__imp" + "ort__"]',
    "dunder_subscript_constant": 'x = obj["__class__"]',
    "dunder_subscript_concat": 'x = obj["__cla" + "ss__"]',
    "dunder_name_reference": "x = __builtins__",
    "dunder_attribute_traversal": "x = (1).__class__",
    "subclasses_traversal": "x = ().__class__.__base__.__subclasses__()",
    "alias_vars": "v = vars()",
    "alias_type": "t = type(x)",
}

# ================================================================
# 同类未报告逃逸: 模块命名空间句柄与文件原语
# ================================================================
NAMESPACE_ESCAPES = {
    "polars_os_handle": "import polars as pl\npl.os.system('id')",
    "polars_contextlib": "import polars as pl\nx = pl.contextlib",
    "numpy_lib_os": "import numpy as np\nnp.lib._function_base_impl.os.system('id')",
    "numpy_ctypeslib": "import numpy as np\nnp.ctypeslib.ctypes.CDLL('libc.so.6')",
    "numpy_random_sys": "import numpy as np\nnp.random._common.sys.modules",
    "from_import_namespace_handle": "from polars import os as plos",
    "bare_import_namespace_attr": "import numpy\nnumpy.random.sys",
    "direct_system_call": "x = thing.system('id')",
    "direct_popen_call": "x = thing.popen('id')",
    "write_csv_primitive": "df.write_csv('/tmp/x.csv')",
    "read_parquet_primitive": "df = pl.read_parquet('/etc/passwd')",
    "savetxt_primitive": "np.savetxt('/tmp/x', arr)",
    "module_type_handle": "m = types.ModuleType('m')",
    "lambda_type_handle": "f = types.LambdaType(x)",
}


@pytest.mark.parametrize("code", list(REPORTED_ESCAPES.values()),
                         ids=list(REPORTED_ESCAPES))
def test_reported_escapes_are_blocked(code):
    with pytest.raises(ValueError):
        _validate(code)


@pytest.mark.parametrize("code", list(NAMESPACE_ESCAPES.values()),
                         ids=list(NAMESPACE_ESCAPES))
def test_namespace_and_file_primitive_escapes_are_blocked(code):
    with pytest.raises(ValueError):
        _validate(code)


def test_star_import_is_blocked():
    with pytest.raises(ValueError, match="通配导入"):
        _validate("from app.backtest.matrix import *")


def test_namespace_package_import_is_blocked():
    with pytest.raises(ValueError, match="白名单"):
        _validate("import app.strategy")


# ================================================================
# 合法策略不被误伤
# ================================================================
LEGITIMATE_SNIPPETS = {
    "polars_matrix_strategy": '''
        import datetime

        import numpy as np
        import polars as pl
        from app.backtest.matrix import MarketDataMatrix, SignalMatrix, valid_shift

        def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
            entry = (pl.col("close") > pl.col("open")) & (pl.col("volume") > 0)
            return entry

        META = {"id": "custom_x", "name": "x", "description": "x",
                "scoring": {"close": 1.0}}
    ''',
    "market_open_column": "entry = market.open > market.close",
    "market_volume_column": "v = market.volume.rolling_mean(5)",
    "dynamic_index_by_loop_var": "for i in range(3):\n    v = arr[i]",
    "string_key_subscript": "v = row[str('close')]",
    "tuple_type_annotation": "def f() -> tuple[str, float]:\n    return ('a', 1.0)",
    "main_guard": 'if __name__ == "__main__":\n    pass',
    "legit_get_method": "v = row.get('k', 0)",
    "legit_isinstance": "if isinstance(x, dict):\n    pass",
    "legit_str_format": 'txt = "{}".format(1)',
    "legit_read_none": "x = None",
    "dunder_mention_in_docstring": '''
        """策略说明: 不使用 __class__ 等 dunder 遍历。"""

        def filter(df):
            return df
    ''',
    "numpy_public_api": "import numpy as np\nx = np.arange(10)\ny = np.float32(1.0)",
}


@pytest.mark.parametrize("code", list(LEGITIMATE_SNIPPETS.values()),
                         ids=list(LEGITIMATE_SNIPPETS))
def test_legitimate_code_still_passes(code):
    _validate(code)


def test_existing_strategies_in_repo_all_pass():
    """仓库内全部现有策略文件必须仍通过加固后的校验 (兼容性回归)。"""
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


# ================================================================
# 纵深防御: 引擎加载层必须同样拦截 (文件被直接投放磁盘的场景)
# ================================================================
_POC_FILE = '''
import datetime

g = getattr
s = "__imp" + "ort__"
imp = __builtins__[s]
os_mod = imp("os")
os_mod.popen("id > /tmp/TICK_RCE_PROOF.txt").read()

def filter(df):
    return df

META = {"id": "custom_poc_rce", "name": "poc", "description": "poc",
        "scoring": {"close": 1.0}}
'''


def test_engine_load_file_rejects_bypass_payload(tmp_path):
    """绕过保存接口直接投放磁盘的策略文件, 加载时必须被拦截。"""
    from app.strategy.engine import StrategyEngine

    path = tmp_path / "custom_poc_rce.py"
    path.write_text(textwrap.dedent(_POC_FILE), encoding="utf-8")

    with pytest.raises(ValueError):
        StrategyEngine._load_file(path)

    assert not (tmp_path / "TICK_RCE_PROOF.txt").exists()


def test_engine_load_file_rejects_dependency_using_vars(tmp_path):
    """依赖文件不再放行 vars(): 拿到调用栈即拿到任意代码执行原料。"""
    from app.strategy.engine import StrategyEngine

    (tmp_path / "_helpers.py").write_text(
        "def f(obj):\n    return vars(obj)\n", encoding="utf-8",
    )
    main = tmp_path / "custom_dep.py"
    main.write_text(
        '"""依赖文件策略"""\n'
        "import polars as pl\n"
        "import _helpers\n\n"
        "def filter(df: pl.DataFrame, params: dict) -> pl.Expr:\n"
        "    return pl.lit(True)\n\n"
        'META = {"id": "custom_dep", "name": "dep", "description": "dep",\n'
        '        "scoring": {}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        StrategyEngine._load_file(main)


def test_engine_load_file_fails_closed_on_unreadable_dependency(tmp_path):
    """依赖读取异常时 fail-closed, 不静默跳过校验直接 exec_module。

    跨平台确定性: 不依赖 chmod 语义 (Windows 上不可靠), 改用目录占位
    触发同类 OSError (IsADirectoryError 是 OSError 子类)。
    """
    from app.strategy.engine import StrategyEngine

    ghost = tmp_path / "_ghost.py"
    ghost.mkdir()  # 目录占同名路径: read_text 抛 IsADirectoryError
    (tmp_path / "custom_ok.py").write_text(
        '"""正常策略"""\n'
        "import polars as pl\n\n"
        "def filter(df: pl.DataFrame, params: dict) -> pl.Expr:\n"
        "    return pl.lit(True)\n\n"
        'META = {"id": "custom_ok", "name": "ok", "description": "ok",\n'
        '        "scoring": {}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="安全校验失败"):
        StrategyEngine._load_file(tmp_path / "custom_ok.py")


# ================================================================
# P1-16 第三批: 防御机制「不变量锁」
# ================================================================
# 上面的测试锁的是**具体载荷**(某个 PoC 被拦)。这一节锁的是**机制本身**:
# 直接遍历校验器用的那几个常量, 逐个断言执行路径真的会拦。这样
#   - 往黑名单新增一项 → 自动被覆盖, 不用记得补测试;
#   - 有人删掉/改坏执行分支 → 立刻红;
#   - 有人把危险模块塞进白名单 → 立刻红。
# 即报告 P1-16 要求的「危险模块检查」的回归锁。

# 明确危险的模块 (执行/网络/文件/反射)。注意: 白名单是**封闭式**的,
# 不在名单里的一律拒绝, 所以 math/json/re 这类"无害但没开放"的也进不来。
DANGEROUS_MODULES = (
    "os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib",
    "builtins", "pickle", "marshal", "multiprocessing", "threading",
    "pathlib", "tempfile", "requests", "urllib", "http", "ftplib", "smtplib",
    "webbrowser", "signal", "resource", "pty", "posix", "nt", "io", "runpy",
    "code", "py_compile", "asyncio", "concurrent", "inspect", "faulthandler",
    "atexit", "sysconfig", "platform", "types", "typing", "dataclasses",
    "contextlib", "weakref", "gc", "mmap", "sqlite3", "zipfile", "tarfile",
    "setuptools", "pkg_resources", "distutils", "pip", "logging",
    "ctypes.util", "os.path", "http.client", "importlib.metadata",
)


@pytest.mark.parametrize("name", sorted(_FORBIDDEN_REF_NAMES))
def test_every_forbidden_ref_name_is_rejected(name):
    """黑名单里的每个引用名都真的被拦 (机制自洽, 不靠个别用例撑场面)。"""
    with pytest.raises(ValueError, match="禁止引用"):
        _validate(f"x = {name}")


@pytest.mark.parametrize("name", sorted(_FORBIDDEN_ATTR_NAMES))
def test_every_forbidden_attr_name_is_rejected(name):
    """黑名单里的每个属性名对**任意接收者**都拦 (x.system 也拦)。"""
    with pytest.raises(ValueError, match="禁止访问属性"):
        _validate(f"x = obj.{name}")


@pytest.mark.parametrize("mod", DANGEROUS_MODULES)
def test_dangerous_module_import_is_rejected(mod):
    """危险模块两种导入形式全拦, 且错误信息点明白名单。"""
    with pytest.raises(ValueError, match="白名单"):
        _validate(f"import {mod}")
    with pytest.raises(ValueError, match="白名单"):
        _validate(f"from {mod} import anything")


def test_unlisted_benign_module_also_rejected():
    """白名单是封闭式的: 没开放的模块即使无害也进不来 (默认拒绝)。"""
    for mod in ("math", "json", "re", "itertools", "functools", "time"):
        with pytest.raises(ValueError, match="白名单"):
            _validate(f"import {mod}")


def test_whitelist_is_locked_and_contains_no_dangerous_module():
    """白名单内容被锁死 — 增删都必须是有意识的评审动作, 且不得含危险模块。"""
    expected = {
        "polars", "numpy", "app.backtest.matrix", "app.strategy.market_data",
        "app.strategy.research.factor_rank_research",
        "app.strategy.builtin.factor_rank_research", "datetime", "__future__",
    }
    assert set(_ALLOWED_IMPORT_MODULES) == expected, (
        "import 白名单发生变化 — 请确认是有意为之并同步更新本测试与文档"
    )
    dangerous_roots = {m.split(".", 1)[0] for m in DANGEROUS_MODULES}
    leaked = {m.split(".", 1)[0] for m in _ALLOWED_IMPORT_MODULES} & dangerous_roots
    assert not leaked, f"白名单里出现危险模块根: {sorted(leaked)}"


def test_no_module_handle_importable_from_any_allowed_root():
    """白名单模块自身的命名空间句柄一个都导不出来 (pl.os / np.lib / app.json ...)。

    用校验器自己的探测函数枚举, 因此上游库新增子模块也自动纳入覆盖。
    """
    offenders: list[str] = []
    for root in sorted(_INTROSPECTABLE_ROOTS):
        for attr in sorted(_module_attr_names(root)):
            try:
                _validate(f"from {root} import {attr}")
            except ValueError:
                continue
            offenders.append(f"from {root} import {attr}")
    assert not offenders, f"这些模块句柄竟然能导入: {offenders}"


def test_module_handle_attribute_access_rejected_for_every_root():
    """点号访问形式同样一个不漏 (import polars as pl 后 pl.<子模块>)。"""
    offenders: list[str] = []
    for root in sorted(_INTROSPECTABLE_ROOTS):
        for attr in sorted(_module_attr_names(root)):
            code = f"import {root}\nx = {root}.{attr}"
            try:
                _validate(code)
            except ValueError:
                continue
            offenders.append(f"{root}.{attr}")
    assert not offenders, f"这些模块句柄竟然能点出来: {offenders}"


def test_attr_blacklist_keeps_legit_market_column_names():
    """反向锁: open/type/dir 等**不得**进属性黑名单 — market.open 是合法行情列。

    (它们已在 _FORBIDDEN_REF_NAMES 里拦住裸引用, 属性形式必须放行。)
    """
    for name in ("open", "type", "dir", "close", "high", "low", "volume"):
        assert name not in _FORBIDDEN_ATTR_NAMES, f"{name} 不该进属性黑名单"
    _validate("entry = market.open > market.close")
    _validate("t = row.type")


def test_dynamic_dunder_string_cannot_be_used_even_if_unfoldable():
    """运行时才拼得出的 dunder 串本身折不出来, 但**所有使用路径**都被堵死。

    即: 光能拼出 "__import__" 字符串没用 — 要拿它做事必须经过
    __builtins__ / getattr / 下标, 这三条路全在黑名单里。
    """
    unfoldable = 'name = f"__imp{sep}ort__"'
    _validate(unfoldable + "\nx = 1")  # 单纯拼字符串不构成逃逸
    for use in (
        "imp = __builtins__[name]",          # dunder 名引用被拦
        "imp = getattr(builtins, name)",     # getattr 被拦
        "mod = __import__(name)",            # __import__ 被拦
    ):
        with pytest.raises(ValueError, match="禁止"):
            _validate(unfoldable + "\n" + use)


def test_lookahead_gate_runs_after_security_gate():
    """两道门控叠加时**安全错误优先** — 别让未来函数报错盖住真正的越权。"""
    code = 'import os\npl.col("close").shift(-1)'
    with pytest.raises(ValueError, match="白名单"):
        _validate(code)


# 关键项存在性锁: 上面的参数化遍历的是常量本身, 所以「从黑名单删掉一项」
# 只会让覆盖变少而不会变红。这里显式钉住真正构成 RCE 防线的那批名字,
# 误删/手滑立刻红。(不复制全部 130 项, 只钉关键项, 避免测试变成第二份黑名单。)
CRITICAL_REF_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "getattr", "setattr",
    "globals", "locals", "vars", "dir", "breakpoint", "input", "type", "exit",
})
CRITICAL_ATTR_NAMES = frozenset({
    "system", "popen", "spawn", "execv", "fork", "kill", "getoutput",
    "subprocess", "os", "sys", "socket", "shutil", "ctypes", "importlib",
    "builtins", "pickle", "marshal", "Path", "pathlib", "startfile",
    "load", "loads", "fromfile", "tofile", "loadtxt", "memmap",
    "read_parquet", "write_parquet", "read_csv", "write_csv",
})


def test_critical_blacklist_entries_cannot_be_silently_removed():
    """构成 RCE 防线的关键黑名单项一个都不能少。"""
    missing_ref = CRITICAL_REF_NAMES - set(_FORBIDDEN_REF_NAMES)
    assert not missing_ref, f"引用黑名单丢了关键项: {sorted(missing_ref)}"
    missing_attr = CRITICAL_ATTR_NAMES - set(_FORBIDDEN_ATTR_NAMES)
    assert not missing_attr, f"属性黑名单丢了关键项: {sorted(missing_attr)}"


def test_dunder_rule_and_benign_whitelist_are_locked():
    """dunder 走的是独立规则, 其良性白名单必须锁死 — 放宽即开逃逸口。"""
    assert set(_BENIGN_DUNDER_NAMES) == {"__name__"}
    assert set(_BENIGN_DUNDER_TEXTS) == {"__name__", "__main__"}
    for name in ("__class__", "__globals__", "__subclasses__", "__builtins__"):
        assert _is_dunder(name)
    assert not _is_dunder("__name__") or "__name__" in _BENIGN_DUNDER_NAMES

    # dunder 属性 / dunder 引用被拦
    for code in ("x = obj.__class__", "x = obj.__globals__",
                 "x = obj.__subclasses__()", "b = __builtins__"):
        with pytest.raises(ValueError, match="dunder"):
            _validate(code)
    # 良性 dunder 放行 (策略文件常见写法)
    _validate('if __name__ == "__main__":\n    x = 1')
