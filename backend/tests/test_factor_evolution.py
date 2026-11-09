"""公式进化 (P2-22a) 测试: 遗传算子 / 运行时 / API 三层。"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.factor_evolution import router
from app.backtest.evolution_runtime import (
    EvolutionRuntimeCancelledError,
    run_evolution_runtime,
)
from app.factors.dsl import compile_formula
from app.factors.evolution import (
    SEED_FORMULAS,
    canonical,
    crossover,
    formula_key,
    initial_population,
    mutate,
    parse_seed,
    random_tree,
    unparse,
)
from app.services.mining_jobs import MiningRunStore


@pytest.fixture(autouse=True)
def _cleanup_evo_factors():
    """发布测试会向全局注册表登记 uf_evo_* 因子; 用例后注销, 避免跨用例污染
    (同公式 → 同 id, 二次 register 会因"版本未提升"被拒)。"""
    yield
    from app.factors.registry import _active_registry, unregister_factor

    for fid in [k for k in _active_registry() if k.startswith("uf_evo_")]:
        unregister_factor(fid)


# ============================================================ 遗传算子
class TestGeneticOperators:
    def test_seeds_compile(self):
        for text in SEED_FORMULAS:
            tree = parse_seed(text)
            assert tree is not None, text
            compiled = compile_formula(unparse(tree))
            assert compiled.ok, (text, [e.code for e in compiled.errors])

    def test_unparse_roundtrip_stable(self):
        rng = random.Random(1)
        for _ in range(50):
            tree = random_tree(rng, 4)
            text = unparse(tree)
            reparsed = parse_seed(text)
            assert reparsed is not None, text
            assert canonical(reparsed) == canonical(tree), text

    def test_random_trees_compile(self):
        rng = random.Random(2)
        for _ in range(200):
            tree = random_tree(rng, 4)
            assert compile_formula(unparse(tree)).ok

    def test_canonical_commutative(self):
        assert formula_key("close + volume") == formula_key("volume + close")
        assert formula_key("close * volume") == formula_key("volume * close")
        assert formula_key("min(close, volume)") == formula_key("min(volume, close)")
        assert formula_key("ts_corr(close, volume, 10)") == formula_key("ts_corr(volume, close, 10)")
        # 非交换: a-b != b-a
        assert formula_key("close - volume") != formula_key("volume - close")

    def test_crossover_produces_valid(self):
        rng = random.Random(3)
        pop = initial_population(rng, 40)
        produced = 0
        for _ in range(200):
            a, b = rng.sample(pop, 2)
            child = crossover(a, b, rng)
            if child is None:
                continue
            assert compile_formula(unparse(child)).ok
            produced += 1
        assert produced > 0

    def test_mutate_produces_valid(self):
        rng = random.Random(4)
        pop = initial_population(rng, 40)
        produced = 0
        for _ in range(300):
            child = mutate(rng.choice(pop), rng, 4)
            if child is None:
                continue
            assert compile_formula(unparse(child)).ok
            produced += 1
        assert produced > 0

    def test_initial_population_dedup_and_size(self):
        rng = random.Random(5)
        pop = initial_population(rng, 50)
        assert len(pop) == 50
        keys = {canonical(t) for t in pop}
        assert len(keys) == 50  # 全部唯一
        # 种子公式都在种群前部
        assert len(pop) >= len(SEED_FORMULAS)

    def test_initial_population_extra_formulas(self):
        """P2-22b: 额外公式 (LLM 提案) 入种群 — 非法跳过、与种子重复去重、size 截断。"""
        rng = random.Random(5)
        pop = initial_population(
            rng, 30, 4,
            extra_formulas=["ts_rank(amount, 10)", "close +", SEED_FORMULAS[0], "ts_rank(amount, 10)"],
        )
        assert len(pop) == 30
        texts = {unparse(t) for t in pop}
        assert "ts_rank(amount, 10)" in texts  # 合法提案入选
        keys = {canonical(t) for t in pop}
        assert len(keys) == 30  # 非法/重复项未混入, 全部唯一
        # 额外公式排在随机补足之前 (种群前部 = 种子 + 提案)
        head_keys = {canonical(t) for t in pop[: len(SEED_FORMULAS) + 1]}
        assert formula_key("ts_rank(amount, 10)") in head_keys

    def test_no_bare_num_leaves(self):
        """随机树叶子只能是行情列, 不产生裸数字 (避免 E016)。"""
        rng = random.Random(6)
        for _ in range(100):
            tree = random_tree(rng, 4)
            text = unparse(tree)
            assert compile_formula(text).ok


# ============================================================ 运行时
def _synthetic_panel(n_symbols: int = 24, n_days: int = 400, seed: int = 7) -> pl.DataFrame:
    """构造带嵌入动量信号的面板: 未来收益与过去 5 日动量正相关。"""
    rng = np.random.default_rng(seed)
    start = date(2023, 1, 2)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    frames = []
    for s_index in range(n_symbols):
        sym = f"{s_index:06d}.SZ"
        base_ret = rng.normal(0, 0.02, n_days)
        mom5 = np.concatenate([[0.0] * 5, base_ret[5:]])
        signal = 0.03 * np.tanh(mom5 * 20)
        rets = base_ret * 0.5 + signal + rng.normal(0, 0.015, n_days)
        close = 10.0 * np.exp(np.cumsum(rets))
        open_ = close * (1 + rng.normal(0, 0.004, n_days))
        high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.004, n_days)))
        low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.004, n_days)))
        volume = rng.lognormal(10, 0.4, n_days)
        frames.append(pl.DataFrame({
            "symbol": [sym] * n_days,
            "date": dates,
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "amount": volume * close,
            "turnover_rate": rng.lognormal(-2, 0.4, n_days),
        }))
    return pl.concat(frames)


class _FakeEngine:
    def __init__(self, panel: pl.DataFrame) -> None:
        self._panel = panel

    def load_panel(self, symbols, start, end, columns=None, asset_type="stock", **kwargs):
        del symbols, start, end, asset_type, kwargs
        frame = self._panel
        if columns:
            frame = frame.select([c for c in columns if c in frame.columns])
        return frame


def _service(panel: pl.DataFrame) -> SimpleNamespace:
    return SimpleNamespace(engine=_FakeEngine(panel))


class TestEvolutionRuntime:
    def _payload(self, panel: pl.DataFrame, **overrides) -> dict:
        dates = panel["date"].unique().sort().to_list()
        payload = {
            "run_id": "evo_test",
            "start": dates[40].isoformat(),
            "end": dates[-1].isoformat(),
            "asset_type": "stock",
            "population_size": 24,
            "generations": 3,
            "horizon": 5,
            "max_symbols": 600,
            "seed": 42,
        }
        payload.update(overrides)
        return payload

    def test_run_succeeds_and_finds_signal(self):
        panel = _synthetic_panel()
        result = run_evolution_runtime(
            self._payload(panel), data_dir=Path("/tmp"), service=_service(panel),
        )
        assert result["status"] == "succeeded"
        assert result["algorithm_version"] == "evolution-v1"
        assert result["trials"]["evaluated"] > 0
        assert len(result["survivors"]) >= 1
        # 结果必须 JSON 可序列化 (worker 会 json.dumps)
        json.dumps(result, ensure_ascii=False, default=str)

    def test_survivors_have_holdout_stats(self):
        panel = _synthetic_panel()
        result = run_evolution_runtime(
            self._payload(panel), data_dir=Path("/tmp"), service=_service(panel),
        )
        for survivor in result["survivors"]:
            assert survivor["direction"] in ("high", "low")
            holdout = survivor["holdout"]
            for field in ("ic_mean", "ir", "coverage"):
                assert field in holdout
            # DSR / q 值可能为 None (数据不足), 但键必须存在
            assert "dsr" in holdout and "q_value" in holdout

    def test_train_holdout_disjoint(self):
        panel = _synthetic_panel()
        result = run_evolution_runtime(
            self._payload(panel), data_dir=Path("/tmp"), service=_service(panel),
        )
        info = result["panel"]
        assert info["train_end"] < info["holdout_start"]
        assert info["train_days"] + info["holdout_days"] == info["n_days"]

    def test_deterministic_with_seed(self):
        panel = _synthetic_panel()
        r1 = run_evolution_runtime(
            self._payload(panel, seed=99), data_dir=Path("/tmp"), service=_service(panel),
        )
        r2 = run_evolution_runtime(
            self._payload(panel, seed=99), data_dir=Path("/tmp"), service=_service(panel),
        )
        f1 = [s["formula"] for s in r1["survivors"]]
        f2 = [s["formula"] for s in r2["survivors"]]
        assert f1 == f2

    def test_insufficient_data_raises(self):
        panel = _synthetic_panel(n_days=60)  # < MIN_TRAIN + MIN_HOLDOUT
        dates = panel["date"].unique().sort().to_list()
        with pytest.raises(ValueError, match="数据不足"):
            run_evolution_runtime(
                self._payload(panel, start=dates[0].isoformat(), end=dates[-1].isoformat()),
                data_dir=Path("/tmp"), service=_service(panel),
            )

    def test_cancel_raises(self):
        import threading

        panel = _synthetic_panel()
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(EvolutionRuntimeCancelledError):
            run_evolution_runtime(
                self._payload(panel), data_dir=Path("/tmp"), service=_service(panel),
                cancel_check=cancel,
            )

    def test_symbol_subsample(self):
        panel = _synthetic_panel(n_symbols=60)
        result = run_evolution_runtime(
            self._payload(panel, max_symbols=50), data_dir=Path("/tmp"), service=_service(panel),
        )
        assert result["panel"]["universe_total"] == 60
        assert result["panel"]["n_symbols"] == 50

    def test_progress_callback_fires(self):
        panel = _synthetic_panel()
        events: list[dict] = []
        run_evolution_runtime(
            self._payload(panel), data_dir=Path("/tmp"), service=_service(panel),
            progress_cb=events.append,
        )
        phases = {e["phase"] for e in events}
        assert "generation" in phases
        assert "survivors" in phases

    # ------------------------------------------------ P2-22b: LLM 提议通道
    def test_llm_disabled_by_default(self):
        panel = _synthetic_panel()
        result = run_evolution_runtime(
            self._payload(panel), data_dir=Path("/tmp"), service=_service(panel),
        )
        assert result["llm"] is None
        assert result["request"]["llm_propose"] is False

    def test_llm_formulas_injected_into_population(self, monkeypatch):
        panel = _synthetic_panel()
        from app.backtest import evolution_runtime as rt
        from app.services import evolution_llm

        captured: dict = {}

        def fake_propose(**kwargs):
            captured["count"] = kwargs.get("count")
            return _fake_llm_result(formulas=["ts_rank(amount, 10)"])

        original = rt.initial_population

        def spy(rng, size, max_depth=4, extra_formulas=None):
            captured["extra"] = list(extra_formulas or [])
            return original(rng, size, max_depth, extra_formulas=extra_formulas)

        monkeypatch.setattr(evolution_llm, "propose_formulas", fake_propose)
        monkeypatch.setattr(rt, "initial_population", spy)
        run_evolution_runtime(
            self._payload(panel, llm_propose=True, llm_count=7),
            data_dir=Path("/tmp"), service=_service(panel),
        )
        assert captured["extra"] == ["ts_rank(amount, 10)"]
        assert captured["count"] == 7  # llm_count 透传

    def test_llm_phase_and_result_recorded(self, monkeypatch):
        panel = _synthetic_panel()
        from app.services import evolution_llm

        monkeypatch.setattr(
            evolution_llm, "propose_formulas",
            lambda **kwargs: _fake_llm_result(formulas=["ts_rank(amount, 10)"]),
        )
        events: list[dict] = []
        result = run_evolution_runtime(
            self._payload(panel, llm_propose=True, llm_count=8),
            data_dir=Path("/tmp"), service=_service(panel), progress_cb=events.append,
        )
        assert result["status"] == "succeeded"
        assert result["request"]["llm_propose"] is True
        assert result["request"]["llm_count"] == 8
        assert result["llm"]["accepted"][0]["formula"] == "ts_rank(amount, 10)"
        assert result["llm"]["attribution"]["model"] == "fake-model"
        llm_events = [e for e in events if e["phase"] == "llm"]
        assert len(llm_events) == 2 and "接受 1" in llm_events[-1]["label"]
        json.dumps(result, ensure_ascii=False, default=str)  # worker 序列化安全

    def test_llm_fail_open_continues_as_pure_gp(self, monkeypatch):
        panel = _synthetic_panel()
        from app.services import evolution_llm

        monkeypatch.setattr(
            evolution_llm, "propose_formulas",
            lambda **kwargs: _fake_llm_result(formulas=(), error="RuntimeError: AI API Key 未配置"),
        )
        result = run_evolution_runtime(
            self._payload(panel, llm_propose=True),
            data_dir=Path("/tmp"), service=_service(panel),
        )
        assert result["status"] == "succeeded"  # 任务不废
        assert result["llm"]["error"] and result["llm"]["accepted"] == []
        assert result["trials"]["evaluated"] > 0  # 纯 GP 照常进化


def _fake_llm_result(formulas: tuple[str, ...] = ("ts_rank(amount, 10)",), error: str | None = None) -> dict:
    """构造 propose_formulas 的假返回 (形状与 evolution_llm 真实产物一致)。"""
    return {
        "requested": 8,
        "proposed": len(formulas),
        "accepted": [
            {"formula": f, "rationale": "假理由", "direction": "high", "key": formula_key(f) or f}
            for f in formulas
        ],
        "rejected": [],
        "repair_rounds": 0,
        "attribution": {"model": "fake-model", "provider": "fake", "profile_id": ""},
        "error": error,
        "elapsed_ms": 1.0,
    }


# ============================================================ LLM 提议通道 (P2-22b)
def _marker(items: list[dict]) -> str:
    return f"前言 <!--EVOLUTION_FORMULAS: {json.dumps(items, ensure_ascii=False)}--> 后记"


class TestEvolutionLlm:
    def _patch_llm(self, monkeypatch, responses: list):
        """替换 ai_provider 出站调用; responses 按调用序返回 (Exception 项则抛出)。"""
        from app.services import ai_provider

        calls: list[list[dict]] = []

        async def fake_generate(messages, **kwargs):
            calls.append(messages)
            item = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
        monkeypatch.setattr(
            ai_provider, "current_attribution",
            lambda: {"model": "m1", "provider": "openai_compat", "profile_id": "p1"},
        )
        return calls

    def test_build_prompt_contains_vocabulary_and_spec(self):
        from app.services.evolution_llm import build_proposal_messages

        messages = build_proposal_messages(9, asset_type="etf", horizon=3)
        assert [m["role"] for m in messages] == ["system", "user"]
        user = messages[1]["content"]
        assert "ts_corr" in user and "turnover_rate" in user  # 词表来自 evolution.py 常量
        assert "EVOLUTION_FORMULAS" in user and "恰好 9 条" in user
        assert "ETF" in user and "未来 3 日" in user
        assert SEED_FORMULAS[0] in user  # 种子作范例

    def test_build_repair_prompt(self):
        from app.services.evolution_llm import build_proposal_messages

        messages = build_proposal_messages(
            5, repair=[{"formula": "ts_mean(rank(close), 5)", "reason": "E009: 截面算子不能嵌在时序窗口内", "_raw_response": "上一轮原文"}],
        )
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[2]["content"] == "上一轮原文"
        assert "E009" in messages[3]["content"] and "ts_mean(rank(close), 5)" in messages[3]["content"]
        # 空原文不产生 assistant 消息 (部分端点拒绝空内容)
        messages2 = build_proposal_messages(5, repair=[{"formula": "x", "reason": "E001: bad", "_raw_response": ""}])
        assert [m["role"] for m in messages2] == ["system", "user", "user"]

    def test_parse_proposals(self):
        from app.services.evolution_llm import parse_proposals

        text = _marker([
            {"formula": " ts_rank(amount, 10) ", "rationale": "x" * 300, "direction": "HIGH"},
            {"formula": "", "rationale": "空公式被丢"},
            "不是字典",
            {"rationale": "缺 formula 被丢"},
            {"formula": "rank(close)", "direction": "sideways"},
        ])
        parsed = parse_proposals(text)
        assert parsed is not None and len(parsed) == 2
        assert parsed[0] == {"formula": "ts_rank(amount, 10)", "rationale": "x" * 200, "direction": "high"}
        assert parsed[1]["direction"] == ""  # 非法方向归一为空

    def test_parse_proposals_last_marker_and_broken(self):
        from app.services.evolution_llm import parse_proposals

        two = _marker([{"formula": "rank(close)"}]) + "中间废话" + _marker([{"formula": "rank(volume)"}])
        parsed = parse_proposals(two)
        assert parsed is not None and [p["formula"] for p in parsed] == ["rank(volume)"]  # 取最后一处
        assert parse_proposals("没有机读块") is None
        assert parse_proposals("<!--EVOLUTION_FORMULAS: [坏json-->") is None
        assert parse_proposals("") is None

    def test_validate_proposals_accept_reject_dedup(self):
        from app.services.evolution_llm import validate_proposals

        accepted, rejected = validate_proposals([
            {"formula": "ts_rank(amount, 10)", "rationale": "a", "direction": "high"},
            {"formula": "ts_rank(amount, 10)", "rationale": "批内重复", "direction": "high"},
            {"formula": SEED_FORMULAS[2], "rationale": "撞种子", "direction": ""},
            {"formula": "ts_mean(rank(close), 5)", "rationale": "E009", "direction": "low"},
            {"formula": "close +", "rationale": "语法错", "direction": ""},
        ])
        assert [a["formula"] for a in accepted] == ["ts_rank(amount, 10)"]
        assert accepted[0]["key"] == formula_key("ts_rank(amount, 10)")
        reasons = [r["reason"] for r in rejected]
        assert reasons[0] == "重复提案"
        assert reasons[1] == "与内置种子重复"
        assert reasons[2].startswith("E009:") and reasons[3].startswith("E0")
        # forbidden_keys (修复轮撞首轮已接受项)
        acc2, rej2 = validate_proposals(
            [{"formula": "ts_rank(amount, 10)", "rationale": "", "direction": ""}],
            forbidden_keys={formula_key("ts_rank(amount, 10)")},
        )
        assert acc2 == [] and rej2[0]["reason"] == "重复提案"

    def test_propose_fail_open_on_exception(self, monkeypatch):
        from app.services.evolution_llm import propose_formulas

        self._patch_llm(monkeypatch, [RuntimeError("AI API Key 未配置, 请在设置页配置")])
        out = propose_formulas(count=5)
        assert out["accepted"] == [] and out["proposed"] == 0
        assert "RuntimeError" in out["error"] and out["attribution"] is None
        assert out["requested"] == 5 and out["elapsed_ms"] >= 0

    def test_propose_happy_no_repair(self, monkeypatch):
        from app.services.evolution_llm import propose_formulas

        calls = self._patch_llm(monkeypatch, [_marker([
            {"formula": "ts_rank(amount, 10)", "rationale": "a", "direction": "high"},
            {"formula": "ts_cov(close, volume, 10)", "rationale": "b", "direction": "low"},
        ])])
        out = propose_formulas(count=5)
        assert len(calls) == 1 and out["repair_rounds"] == 0 and out["error"] is None
        assert len(out["accepted"]) == 2 and out["proposed"] == 2
        assert out["attribution"] == {"model": "m1", "provider": "openai_compat", "profile_id": "p1"}

    def test_propose_repair_round(self, monkeypatch):
        from app.services.evolution_llm import propose_formulas

        calls = self._patch_llm(monkeypatch, [
            _marker([
                {"formula": "ts_rank(amount, 10)", "rationale": "好", "direction": "high"},
                {"formula": "ts_mean(rank(close), 5)", "rationale": "坏", "direction": "low"},
            ]),
            _marker([{"formula": "ts_corr(close, turnover_rate, 15)", "rationale": "修好的", "direction": "high"}]),
        ])
        out = propose_formulas(count=5)
        assert len(calls) == 2 and out["repair_rounds"] == 1
        assert [a["formula"] for a in out["accepted"]] == ["ts_rank(amount, 10)", "ts_corr(close, turnover_rate, 15)"]
        assert out["rejected"][0]["reason"].startswith("E009:")  # 首轮失败留痕
        repair_user = calls[1][-1]["content"]
        assert "E009" in repair_user and "ts_mean(rank(close), 5)" in repair_user
        assert out["error"] is None

    def test_propose_repair_failure_keeps_first_round(self, monkeypatch):
        from app.services.evolution_llm import propose_formulas

        self._patch_llm(monkeypatch, [
            _marker([
                {"formula": "ts_rank(volume, 5)", "rationale": "好", "direction": "high"},
                {"formula": "close +", "rationale": "坏", "direction": ""},
            ]),
            TimeoutError("llm timeout"),
        ])
        out = propose_formulas(count=5)
        assert len(out["accepted"]) == 1 and out["repair_rounds"] == 1
        assert "修复轮失败" in out["error"] and "TimeoutError" in out["error"]

    def test_propose_no_marker_no_repair(self, monkeypatch):
        from app.services.evolution_llm import propose_formulas

        calls = self._patch_llm(monkeypatch, ["模型今天不想输出机读块"])
        out = propose_formulas(count=5)
        assert len(calls) == 1 and out["error"] == "无法从 AI 响应解析出提案机读块"
        assert out["accepted"] == [] and out["repair_rounds"] == 0

    def test_propose_all_duplicates_no_repair(self, monkeypatch):
        """重复提案不可修复 (再修还是重复), 不浪费一次修复轮调用。"""
        from app.services.evolution_llm import propose_formulas

        calls = self._patch_llm(monkeypatch, [_marker([
            {"formula": SEED_FORMULAS[0], "rationale": "抄种子", "direction": "high"},
            {"formula": SEED_FORMULAS[1], "rationale": "又抄", "direction": "high"},
        ])])
        out = propose_formulas(count=5)
        assert len(calls) == 1 and out["repair_rounds"] == 0
        assert out["accepted"] == [] and len(out["rejected"]) == 2


# ============================================================ API
class _Repo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)


class _Manager:
    """假管理器: start 直接落一个 succeeded run + summary, 便于测发布。"""

    def __init__(self, data_dir: Path, summary: dict | None = None) -> None:
        self.store = MiningRunStore(data_dir, namespace="evolution")
        self._summary = summary

    def start(self, request, fingerprint, force=False, source="manual", run_id=None):
        del force
        manifest = self.store.create(request, fingerprint, run_id=run_id)
        self.store.transition_status(manifest["run_id"], "running")
        self.store.transition_status(manifest["run_id"], "succeeded")
        if self._summary is not None:
            self.store.write_summary(manifest["run_id"], self._summary)
        return self.store.get(manifest["run_id"])

    def cancel(self, run_id):
        manifest = self.store.get(run_id)
        if manifest is None:
            raise KeyError(run_id)
        return self.store.transition_status(run_id, "cancelled")


def _write_enriched(data_dir: Path, count: int, first: date = date(2020, 1, 1)) -> None:
    root = data_dir / "kline_daily_enriched"
    for index in range(count):
        value = first + timedelta(days=index)
        partition = root / f"date={value.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "part.parquet").touch()


def _summary_with_survivors(formula: str = "close / ts_delay(close, 5) - 1") -> dict:
    from app.factors.evolution import formula_key as _key

    return {
        "status": "succeeded",
        "algorithm_version": "evolution-v1",
        "trials": {"evaluated": 100, "duplicates": 5, "invalid": 2},
        "panel": {"n_symbols": 100, "n_days": 300},
        "survivors": [{
            "key": _key(formula) or formula,
            "formula": formula,
            "direction": "high",
            "train": {"ic_mean": 0.05, "ir": 1.2, "coverage": 0.9, "fitness": 1.08, "n_days": 200},
            "holdout": {"ic_mean": 0.04, "ir": 1.0, "t_nw": 3.0, "p_value": 0.003,
                        "q_value": 0.01, "dsr": 0.95, "coverage": 0.9, "n_days": 80},
        }],
        "pruned": [],
    }


def _client(tmp_path: Path, summary: dict | None = None):
    _write_enriched(tmp_path, 400)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _Repo(tmp_path)
    app.state.evolution_manager = _Manager(tmp_path, summary)
    return TestClient(app), tmp_path


class TestEvolutionApi:
    def test_start_run(self, tmp_path):
        client, _ = _client(tmp_path, _summary_with_survivors())
        resp = client.post("/api/evolution/runs", json={
            "asset_type": "stock", "population_size": 30, "generations": 3,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "succeeded"
        assert "run_id" in body

    def test_start_insufficient_data_409(self, tmp_path):
        _write_enriched(tmp_path, 50)  # < MIN_WINDOW_PARTITIONS
        app = FastAPI()
        app.include_router(router)
        app.state.repo = _Repo(tmp_path)
        app.state.evolution_manager = _Manager(tmp_path)
        client = TestClient(app)
        resp = client.post("/api/evolution/runs", json={"asset_type": "stock"})
        assert resp.status_code == 409

    def test_start_rejects_extra_field(self, tmp_path):
        client, _ = _client(tmp_path)
        resp = client.post("/api/evolution/runs", json={"bogus": 1})
        assert resp.status_code == 422

    def test_start_with_llm_params(self, tmp_path):
        client, _ = _client(tmp_path, _summary_with_survivors())
        resp = client.post("/api/evolution/runs", json={
            "generations": 2, "llm_propose": True, "llm_count": 20,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["request"]["llm_propose"] is True
        assert body["request"]["llm_count"] == 20

    def test_start_llm_count_bounds_422(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.post("/api/evolution/runs", json={"llm_propose": True, "llm_count": 100}).status_code == 422
        assert client.post("/api/evolution/runs", json={"llm_propose": True, "llm_count": 1}).status_code == 422

    def test_start_llm_propose_strict_bool_422(self, tmp_path):
        client, _ = _client(tmp_path)
        resp = client.post("/api/evolution/runs", json={"llm_propose": "yes"})
        assert resp.status_code == 422  # strict 模式拒绝字符串伪布尔

    def test_start_validates_bounds(self, tmp_path):
        client, _ = _client(tmp_path)
        resp = client.post("/api/evolution/runs", json={"population_size": 5})
        assert resp.status_code == 422  # < ge=20

    def test_list_and_get_run(self, tmp_path):
        client, _ = _client(tmp_path, _summary_with_survivors())
        run_id = client.post("/api/evolution/runs", json={"generations": 2}).json()["run_id"]
        listed = client.get("/api/evolution/runs").json()
        assert any(item["run_id"] == run_id for item in listed["items"])
        detail = client.get(f"/api/evolution/runs/{run_id}").json()
        assert detail["run_id"] == run_id
        assert "result" in detail
        assert len(detail["result"]["survivors"]) == 1

    def test_get_missing_run_404(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.get("/api/evolution/runs/nope").status_code == 404

    def test_publish_survivor(self, tmp_path):
        client, data_dir = _client(tmp_path, _summary_with_survivors())
        run_id = client.post("/api/evolution/runs", json={"generations": 2}).json()["run_id"]
        resp = client.post(f"/api/evolution/runs/{run_id}/survivors/0/publish", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] and body["id"].startswith("uf_evo_")
        # 落盘检查
        from app.factors import store as factor_store
        definitions = factor_store.load_all(data_dir)
        assert any(d["id"] == body["id"] for d in definitions)

    def test_publish_idempotent_409(self, tmp_path):
        client, _ = _client(tmp_path, _summary_with_survivors())
        run_id = client.post("/api/evolution/runs", json={"generations": 2}).json()["run_id"]
        first = client.post(f"/api/evolution/runs/{run_id}/survivors/0/publish", json={})
        assert first.status_code == 200
        second = client.post(f"/api/evolution/runs/{run_id}/survivors/0/publish", json={})
        assert second.status_code == 409

    def test_publish_index_out_of_range_404(self, tmp_path):
        client, _ = _client(tmp_path, _summary_with_survivors())
        run_id = client.post("/api/evolution/runs", json={"generations": 2}).json()["run_id"]
        resp = client.post(f"/api/evolution/runs/{run_id}/survivors/9/publish", json={})
        assert resp.status_code == 404

    def test_publish_custom_label(self, tmp_path):
        client, _ = _client(tmp_path, _summary_with_survivors())
        run_id = client.post("/api/evolution/runs", json={"generations": 2}).json()["run_id"]
        resp = client.post(
            f"/api/evolution/runs/{run_id}/survivors/0/publish",
            json={"label": "我的进化因子"},
        )
        assert resp.status_code == 200
        assert resp.json()["label"] == "我的进化因子"


def test_make_worker_task_evolution():
    from app.backtest.worker import make_worker_task

    task = make_worker_task("evolution", Path("/tmp/data"), {"request": {"generations": 3}})
    assert task["kind"] == "evolution"
    assert task["config"]["request"]["generations"] == 3
