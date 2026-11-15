"""实时指数核心清单 — 产品级固定契约 (单一权威, 按市场分表)。

指数展示层 (侧栏指数条 / 市场总览) 每市场固定四只核心指数, 不开放配置:
- 数据源边界: A 股 TickFlow 与 fuyao 指数快照双源均完整覆盖, 无降级分歧;
  港股走 Sina 快照 + Tencent 日K (东财指数端点在 push2 集群, 见方案 §11);
- 后端消费方 (quote_service / overview / sector_monitor) 与前端 Layout
  统一引用此处, 不得各自维护副本。

监控规则的指数标的不受此限 — quote_service 会把启用规则的指数并入显式拉取。

🔴 港股指数代码是**纯字母** (``HSI`` / ``HSTECH``), 不匹配 :mod:`app.market`
的 symbol 正则 —— 那两份正则是**股票**契约 (CN = 6 位数字 + 交易所后缀,
HK = 5 位数字 + ``.HK``), 且被 Python/DuckDB 双载体平价测试钉住, 不能为指数放宽。
后果 (实测)::

    infer_market("HSI")            -> Market.CN   # 兜底, 不是 HK
    is_valid_symbol("HSI", HK)     -> False
    assert_symbol_market("HSI")    -> 不报错      # 它只拦「确实匹配到别的市场」

所以**指数链路一律显式传 ``market``**, 绝不用 :func:`infer_market` 路由;
否则港股指数会静默写进 A 股目录 (``kline_index_daily``) 并污染全市场统计。
本模块的 :func:`core_index_names` / :func:`core_index_symbols` 对未知市场
**抛错而非回退 CN** —— 回退会让港股展示层静默显示上证综指。
"""

from app.market import Market, rules_for

CORE_INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000680.SH": "科创综指",
}

CORE_INDEX_SYMBOLS: tuple[str, ...] = tuple(CORE_INDEX_NAMES.keys())

#: 港股核心四只 (方案 §11)。代码为 Sina/Tencent 原生形 (``hkHSI`` 去掉 ``hk`` 前缀),
#: 不带 ``.HK`` 后缀 —— 港股指数没有交易所后缀这一说, 加后缀反而两个源都查不到。
HK_CORE_INDEX_NAMES: dict[str, str] = {
    "HSI": "恒生指数",
    "HSTECH": "恒生科技指数",
    "HSCEI": "恒生中国企业指数",
    "HSCCI": "恒生香港中资企业指数",
}

HK_CORE_INDEX_SYMBOLS: tuple[str, ...] = tuple(HK_CORE_INDEX_NAMES.keys())

#: 按市场查表 (方案 §4: 市场差异住在表里, 不住在控制流里)。
#: ``tests/test_market_architecture_guard.py`` 会拦 ``if market == Market.HK`` 分支。
_CORE_INDEX_NAMES_BY_MARKET: dict[Market, dict[str, str]] = {
    Market.CN: CORE_INDEX_NAMES,
    Market.HK: HK_CORE_INDEX_NAMES,
}


def core_index_names(market: object = Market.CN) -> dict[str, str]:
    """该市场的核心指数 ``{symbol: 中文名}``。

    ``market`` 经 :func:`rules_for` 归一并校验: 传 ``None`` / 未知市场**抛错**,
    不回退 CN (回退 = 港股页面显示上证综指, 属串市场事故)。
    存量调用方可继续直接用 :data:`CORE_INDEX_NAMES`, 语义逐字不变。
    """
    return _CORE_INDEX_NAMES_BY_MARKET[rules_for(market).market]


def core_index_symbols(market: object = Market.CN) -> tuple[str, ...]:
    """该市场的核心指数代码元组 (顺序即展示顺序)。"""
    return tuple(core_index_names(market))
