"""LLM 公式提议通道 (P2-22b) — 请大模型当"外援军师", 为公式进化提议种子。

与 P2-22a 的本地 GP 互补: 任务启动后、第 0 代之前, 把 DSL 词表与规则发给
当前配置的 AI, 要求返回若干候选公式; 经编译闸校验 (fail-closed) 后注入初始
种群, 之后的繁殖/训练窗筛选/保留窗裁决与纯 GP 完全一致 — 凡被评估的提案
都计入 trials 账本, DSR 多重检验校正自动覆盖 LLM 通道 (AI 没有"作弊口子")。

设计纪律:
- fail-open: 未配 AI Key / 调用失败 / 解析失败 / 提案全废 → 返回空 accepted
  + error 说明, 运行时记 warning 后退化为纯 GP 继续, 任务不废。
- 机读块解析沿用 stock_debate / ai_verdict 惯例: 标记正则 + json.loads +
  fail-honest (坏条目单独丢弃不废整批); 多处标记取最后一处。
- 编译失败的公式带错误信息回发给模型修复一轮, 仍不合格记 rejected。
- 同步入口 propose_formulas() 供 worker 子进程调用 (内部 asyncio.run;
  secrets.json 是文件式, 子进程可读 AI Key)。归属快照 (model/provider/
  profile_id) 随 run 结果落盘, 提案与拒绝原因全量持久化, 可倒查。
- LLM 给出的 direction 只是假设, 仅随报告展示; 幸存者的最终方向仍由
  训练窗 IC 符号决定 (evolution_runtime), AI 说了不算。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from app.factors.dsl import compile_formula
from app.factors.evolution import (
    BIN_OPS,
    CROSS_OPS,
    LEAF_COLUMNS,
    MINMAX_OPS,
    QUANTILES,
    SEED_FORMULAS,
    TS1_OPS,
    TS2_OPS,
    UNARY_OPS,
    WINDOWS,
    canonical,
    parse_seed,
)

logger = logging.getLogger(__name__)

# 机读提案块 (DOTALL 容忍字符串内换行; 多处标记取最后一处, 同 parse_ruling)
PROPOSALS_RE = re.compile(r"<!--\s*EVOLUTION_FORMULAS:\s*(\[.*?\])\s*-->", re.DOTALL)

LLM_TEMPERATURE = 0.7   # 提议要多样性, 比分析报告默认的 0.3 高
LLM_TIMEOUT = 240.0     # 单次调用超时 (秒); Codex CLI 通道内部自会放宽
_RATIONALE_MAX = 200    # 单条理由截断 (随 run 结果落盘, 防刷屏)
_ERROR_MAX = 300        # error/reason 字段截断
# rejected.reason 以此开头视为编译失败 (可修复); DSL 错误码形如 E001-E016
_COMPILE_ERROR_RE = re.compile(r"^E\d{3}:")


def _vocabulary_text() -> str:
    """从 evolution.py 常量生成词表说明 (单一事实源, 词表演化提示词自动跟上)。"""
    return "\n".join([
        f"- 行情列 (叶子只能用这些): {', '.join(LEAF_COLUMNS)}",
        f"- 一元算子: {', '.join(UNARY_OPS)}",
        f"- 二元算子: {', '.join(BIN_OPS)} ; 以及 min(a,b) / max(a,b) 形式: {', '.join(MINMAX_OPS)}",
        f"- 时序算子 (表达式 + 窗口 n): {', '.join(TS1_OPS)}",
        f"- 时序算子 (两个表达式 + 窗口 n): {', '.join(TS2_OPS)} ; 另有 ts_quantile(表达式, n, q)",
        f"- 截面算子: {', '.join(CROSS_OPS)}",
        f"- 窗口 n 只能取: {', '.join(str(w) for w in WINDOWS)}",
        f"- ts_quantile 的分位 q 只能取: {', '.join(str(q) for q in QUANTILES)}",
    ])


def build_proposal_messages(
    count: int,
    *,
    asset_type: str = "stock",
    horizon: int = 5,
    repair: list[dict] | None = None,
) -> list[dict[str, str]]:
    """构造提议提示词。repair 非空时为修复轮 (带上一轮的编译错误)。"""
    asset_label = "A 股股票" if asset_type == "stock" else "A 股 ETF"
    system = (
        "你是量化因子设计师, 精通 A 股量价因子。你设计的公式将进入遗传算法种群, "
        "用训练窗截面 IC 筛选, 并在从未参与筛选的保留窗上做统计裁决 (Newey-West t / "
        "BH-FDR / Deflated Sharpe)。只有真正有经济学直觉支撑的公式才可能存活, "
        "请勿输出随机堆砌算子的噪声公式。"
    )
    rules = "\n".join([
        "1. 叶子只能是行情列, 裸数字不能当操作数 (纯常量表达式会被拒绝); 数字只能作窗口 n 或分位 q 参数。",
        "2. 截面算子 rank/zscore 禁止出现在任何时序算子的窗口内部 (编译期拒绝)。",
        "3. 保持简洁: 表达式树深度不超过 6 层。",
        "4. 不存在也不需要未来函数 — DSL 只有回看窗口的时序算子, 前向收益是系统内部标签。",
        f"5. direction 填你的假设: \"high\" = 因子值越高未来 {horizon} 日收益越高, \"low\" = 反之。方向最终以训练窗 IC 符号为准, 你的假设仅作参考。",
        "6. 公式之间不得重复, 也不得与示例种子重复或仅有平凡变形 (如整体乘以常数)。",
    ])
    examples = "\n".join(f"  {text}" for text in SEED_FORMULAS)
    output_spec = (
        f"请设计 {count} 条预测未来 {horizon} 日收益的{asset_label}量价因子公式。"
        "输出格式: 只输出一个机读块, 不要输出其他任何文字:\n"
        "<!--EVOLUTION_FORMULAS: [{\"formula\": \"...\", \"rationale\": \"一句话经济学直觉\", "
        "\"direction\": \"high\"}]} -->\n"
        f"JSON 数组恰好 {count} 条; rationale 不超过 60 字。"
    )
    user = f"## DSL 词表\n{_vocabulary_text()}\n\n## 语法规则\n{rules}\n\n## 种子示例 (风格参考, 勿重复)\n{examples}\n\n## 任务\n{output_spec}"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if repair:
        failures = "\n".join(
            f"- {item['formula']}  →  {item['reason']}" for item in repair[:20]
        )
        raw_response = str(repair[0].get("_raw_response") or "")
        if raw_response.strip():  # 空 assistant 消息会被部分 OpenAI 兼容端点拒绝
            messages.append({"role": "assistant", "content": raw_response})
        messages.append({
            "role": "user",
            "content": (
                "以下公式未通过 DSL 编译闸, 请逐条修复后重新输出完整机读块 "
                f"(格式同前, 恰好 {len(repair)} 条, 只输出机读块):\n{failures}"
            ),
        })
    return messages


def parse_proposals(text: str) -> list[dict] | None:
    """从模型全文解析提案块 → list[dict] 或 None (缺失/损坏一律 None)。

    fail-honest: 合法条目清洗保留 (formula 必需; rationale/direction 缺失容忍),
    坏条目单独丢弃不废整批; 多处标记取最后一处 (模型复述示例时以末尾为准)。
    """
    last: re.Match | None = None
    for m in PROPOSALS_RE.finditer(text or ""):
        last = m
    if last is None:
        return None
    try:
        obj = json.loads(last.group(1))
    except (json.JSONDecodeError, ValueError):
        logger.warning("[evolution_llm] 提案块 JSON 损坏: %s", last.group(1)[:120])
        return None
    if not isinstance(obj, list):
        return None
    proposals: list[dict] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        formula = str(item.get("formula") or "").strip()
        if not formula:
            continue
        direction = str(item.get("direction") or "").strip().lower()
        proposals.append({
            "formula": formula,
            "rationale": str(item.get("rationale") or "").strip()[:_RATIONALE_MAX],
            "direction": direction if direction in ("high", "low") else "",
        })
    return proposals


def _seed_keys() -> set[str]:
    keys: set[str] = set()
    for text in SEED_FORMULAS:
        tree = parse_seed(text)
        if tree is not None:
            keys.add(canonical(tree))
    return keys


def validate_proposals(
    proposals: list[dict],
    *,
    forbidden_keys: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """编译闸校验 (fail-closed) → (accepted, rejected)。

    - 每条经 compile_formula 完整语义校验, 失败记 rejected 附首个错误;
    - canonical 键去重: 批内重复 / 与内置种子重复 / 撞 forbidden_keys
      (如修复轮撞第一轮已接受项) 均 rejected;
    - accepted 条目附 key, 供运行时注入与报告。
    """
    blocked = set(forbidden_keys or ())
    blocked |= _seed_keys()
    accepted: list[dict] = []
    rejected: list[dict] = []
    for item in proposals:
        formula = item["formula"]
        compiled = compile_formula(formula)
        if not compiled.ok:
            first = compiled.errors[0] if compiled.errors else None
            reason = f"{first.code}: {first.message}" if first else "编译失败"
            rejected.append({"formula": formula, "reason": reason[:_ERROR_MAX]})
            continue
        tree = parse_seed(formula)
        if tree is None:  # 理论不可达 (compile 已过), 双保险
            rejected.append({"formula": formula, "reason": "解析失败"})
            continue
        key = canonical(tree)
        if key in blocked:
            reason = "与内置种子重复" if key in _seed_keys() else "重复提案"
            rejected.append({"formula": formula, "reason": reason})
            continue
        blocked.add(key)
        accepted.append({**item, "key": key})
    return accepted, rejected


async def _apropose(count: int, asset_type: str, horizon: int) -> dict[str, Any]:
    from app.services import ai_provider  # 模块限定调用, 保证测试可 monkeypatch

    messages = build_proposal_messages(count, asset_type=asset_type, horizon=horizon)
    text = await ai_provider.generate_ai_text(
        messages, temperature=LLM_TEMPERATURE, max_tokens=None, timeout=LLM_TIMEOUT,
    )
    attribution = ai_provider.current_attribution()
    proposals = parse_proposals(text)
    if proposals is None:
        return {
            "requested": count, "proposed": 0, "accepted": [], "rejected": [],
            "repair_rounds": 0, "attribution": attribution,
            "error": "无法从 AI 响应解析出提案机读块",
        }
    accepted, rejected = validate_proposals(proposals)

    # 修复轮: 只回发编译失败项 (重复提案修复无意义), 且仍有名额缺口时才修。
    # 修复调用自身失败不吞第一轮成果 — 保留 accepted, 仅记录 error。
    repair_rounds = 0
    repairable = [item for item in rejected if _COMPILE_ERROR_RE.match(item["reason"])]
    if repairable and len(accepted) < count:
        repair_rounds = 1
        repair_payload = [{**item, "_raw_response": text} for item in repairable]
        repair_messages = build_proposal_messages(
            count, asset_type=asset_type, horizon=horizon, repair=repair_payload,
        )
        try:
            repair_text = await ai_provider.generate_ai_text(
                repair_messages, temperature=LLM_TEMPERATURE, max_tokens=None, timeout=LLM_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — 修复轮失败保留第一轮成果
            logger.warning("[evolution_llm] 修复轮调用失败, 保留首轮提案: %s", exc)
            return {
                "requested": count,
                "proposed": len(proposals),
                "accepted": accepted,
                "rejected": rejected,
                "repair_rounds": repair_rounds,
                "attribution": attribution,
                "error": f"修复轮失败: {type(exc).__name__}: {exc}"[:_ERROR_MAX],
            }
        repaired = parse_proposals(repair_text)
        if repaired:
            acc2, rej2 = validate_proposals(
                repaired, forbidden_keys={item["key"] for item in accepted},
            )
            accepted.extend(acc2)
            rejected.extend(rej2)

    return {
        "requested": count,
        "proposed": len(proposals),
        "accepted": accepted,
        "rejected": rejected,
        "repair_rounds": repair_rounds,
        "attribution": attribution,
        "error": None,
    }


def propose_formulas(*, count: int, asset_type: str = "stock", horizon: int = 5) -> dict[str, Any]:
    """同步入口 (worker 子进程用): 提议 → 校验 → 修复一轮; 永不抛异常 (fail-open)。

    返回 {"requested", "proposed", "accepted", "rejected", "repair_rounds",
          "attribution", "error", "elapsed_ms"} — 全部 JSON 安全, 随 run 结果落盘。
    """
    t0 = time.perf_counter()
    try:
        outcome = asyncio.run(_apropose(count, asset_type, horizon))
    except Exception as exc:  # noqa: BLE001 — fail-open: AI 通道任何失败都不废进化任务
        logger.warning("[evolution_llm] 提议失败, 退化为纯 GP: %s", exc)
        outcome = {
            "requested": count, "proposed": 0, "accepted": [], "rejected": [],
            "repair_rounds": 0, "attribution": None,
            "error": f"{type(exc).__name__}: {exc}"[:_ERROR_MAX],
        }
    outcome["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return outcome
