"""数字忠实度闸门 golden-vector 回归 (docs/ai-number-fidelity-plan.md §12.2)。

参照 test_stats_v2.py test_bh_fdr_golden 先例: fixture 锁定 {输入, 白名单,
期望打码文本, 期望打码数, 期望违规原因} 五元组 — 任何算法改动导致输出
漂移都会在这里显形, 改行为必须同步改 fixture (人工复核后)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ai_number_gate import gate_text
from app.services.ai_number_whitelist import collect_whitelist

_FIXTURE = Path(__file__).parent / "fixtures" / "ai_number_gate" / "golden_cases.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_golden(case):
    pool = collect_whitelist(case["objs"], texts=case["texts"])
    res = gate_text(case["input"], pool)
    assert res.text == case["expected_text"]
    assert res.masked == case["expected_masked"]
    assert sorted({v.reason for v in res.violations}) == case["expected_reasons"]
