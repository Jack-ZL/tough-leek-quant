"""AI 数字忠实度闸门 — 白名单采集 (设计文档: docs/ai-number-fidelity-plan.md §6)。

生成前把"发给模型的全部数字"收进 NumberPool, 两条采集路径互补:

1. 结构化扫描 (analyzer 拼 prompt 前的原始对象: kline_tail/fins/levels/
   overview/signals…): 按字段名做形态族双口径——
   - pct 类字段 (change_pct/turnover_rate/…): 小数口径 0.0366 与百分数口径
     3.66 互保 (个股 change_pct 是小数口径、复盘指数是百分数口径, 模型两种
     写法都会输出, CONTRIBUTING §3.1);
   - 金额类字段 (amount/revenue/…): 元值与亿展示值互保;
   - 排名哨兵 999 (轮动缺失日占位, 渲染成 "—") 不入池。
2. 文本扫描 (system/user prompt 成品文本): 与闸门同一条提取管线
   (scan_text_numbers), 提示词里以什么形态展示就收什么形态, 天然覆盖
   所有格式化结果 (含 "120.5 亿"、"3.66%" 等带单位写法)。

🔴 铁律 (CONTRIBUTING §8): 白名单为空/采集失败 → NumberPool.empty_reason
非空 → 闸门打码全部非豁免数字, 绝不放行 (fail-closed)。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from typing import Any

from app.services.ai_number_gate import MULT, NumberPool, scan_text_numbers

logger = logging.getLogger(__name__)

# pct 形态族字段名关键词 (小写子串匹配, 按"字段路径"匹配: amount.total 也算)
_PCT_KEY_WORDS = ("pct", "percent", "rate", "amplitude", "proportion", "discount")
# 金额形态族字段名关键词 (元值, 展示常换算成亿)
_AMOUNT_KEY_WORDS = (
    "amount", "turnover", "revenue", "profit", "income", "asset", "liabilit",
    "cash", "balance", "cap", "flow", "gmv", "value",
)

# 递归防护: 嵌套深度与池容量上限 (Vibe 实测 30 万数字池核对 <3s, 内存可控)
_MAX_DEPTH = 12
_MAX_VALUES = 300_000


def _add_value(v: float, path: str, all_vals: set[float],
               plain_vals: set[tuple[float, str]]) -> None:
    """单个结构化数值入池: 原值 + 按字段名的形态族双口径。"""
    if not math.isfinite(v):
        return
    key = path.lower()
    if "rank" in key and v == 999:
        # 轮动排名哨兵 (concept_rotation 缺失日占位, prompt 渲染成 "—"),
        # 入池会替编造的 "999" 作证
        return
    all_vals.add(v)
    if any(w in key for w in _PCT_KEY_WORDS):
        # 双口径互保: 小数口径补百分数, 百分数口径补小数
        all_vals.add(round(v * 100, 10) if abs(v) <= 1 else round(v / 100, 10))
    if any(w in key for w in _AMOUNT_KEY_WORDS) and abs(v) >= 1e6:
        # 金额类: 补"亿"展示形态 (原样读进 plain 池, 换算读进 all 池)
        yi = v / 1e8
        all_vals.add(yi)
        plain_vals.add((yi, "亿"))


def _scan_obj(obj: Any, path: str, all_vals: set[float],
              plain_vals: set[tuple[float, str]], depth: int = 0) -> None:
    """递归扫描结构化对象。字符串值跳过 (文本形态由 texts= 参数覆盖)。"""
    if depth > _MAX_DEPTH or len(all_vals) > _MAX_VALUES:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan_obj(v, f"{path}.{k}" if path else str(k), all_vals, plain_vals, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _scan_obj(v, path, all_vals, plain_vals, depth + 1)
    elif isinstance(obj, bool) or not isinstance(obj, (int, float)):
        # bool 是 int 子类必须先排除: True 入池会替编造的 "1" 作证
        return
    else:
        _add_value(float(obj), path, all_vals, plain_vals)


def _add_reading(v: float, unit: str, all_vals: set[float],
                 plain_vals: set[tuple[float, str]]) -> None:
    """文本扫描的一个"读法"入池: 与闸门 _readings 对称, 保证两侧口径一致。"""
    if not math.isfinite(v):
        return
    all_vals.add(v)
    if unit in MULT:
        # "120.5 亿": 原样形态进 plain 池 (供回答写 "2 亿" 时同单位配对),
        # 换算值进 all 池 (供回答写 "12050000000" 或裸数字时命中)
        plain_vals.add((v, unit))
        all_vals.add(v * MULT[unit])
    elif unit == "%":
        # "3.66%": 百分数与小数口径互保
        all_vals.add(v / 100)


def collect_whitelist(objs: Iterable[Any] = (), texts: Iterable[str] = ()) -> NumberPool:
    """采集白名单池。

    objs:  发给模型的结构化数据对象 (拼 prompt 前的原始形态, 字段名可辨);
    texts: 发给模型的成品文本 (system prompt / user prompt / 工具结果 JSON 串),
           用与闸门相同的提取管线扫描, 展示什么形态就收什么形态。

    两路都为空 → empty_reason 非空 → 闸门 fail-closed 全打码。
    """
    all_vals: set[float] = set()
    plain_vals: set[tuple[float, str]] = set()
    for obj in objs:
        _scan_obj(obj, "", all_vals, plain_vals)
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for value, unit in scan_text_numbers(text):
            _add_reading(value, unit, all_vals, plain_vals)
    if not all_vals:
        logger.warning("number gate whitelist empty → fail-closed masking")
        return NumberPool((), (), empty_reason="no_numbers_collected")
    return NumberPool(tuple(sorted(all_vals)), tuple(sorted(plain_vals)), None)
