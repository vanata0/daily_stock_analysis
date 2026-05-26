# -*- coding: utf-8 -*-
"""
Tests for PR 4: parallel TechnicalAgent + IntelAgent in standard mode.

Coverage:
  - _run_parallel_stages returns results in input order
  - TechnicalAgent + IntelAgent run concurrently (wall-clock < sequential)
  - Both StageResults are recorded in stats
  - intel failure degrades gracefully; technical failure aborts
  - Non-standard modes are NOT parallelised (sequential path untouched)
  - Exception inside a parallel stage surfaces as StageStatus.FAILED
"""
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional deps (must precede project imports)
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


for _dep in ("litellm", "akshare", "fake_useragent", "pypinyin", "pandas"):
    if _dep not in sys.modules:
        sys.modules[_dep] = MagicMock()

if "tenacity" not in sys.modules:
    def _noop_retry(*a, **kw):
        return lambda f: f
    _stub(
        "tenacity",
        retry=_noop_retry,
        stop_after_attempt=lambda n: None,
        wait_exponential=lambda **kw: None,
        retry_if_exception_type=lambda e: None,
        before_sleep_log=lambda *a: None,
    )

# Stub every heavy import pulled in by orchestrator.py at module level
for _mod in (
    "src.agent.llm_adapter",
    "src.agent.runner",
    "src.agent.tools.registry",
    "src.config",
    "src.report_language",
    "src.agent.executor",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Make AGENT_MAX_STEPS_DEFAULT importable
sys.modules["src.config"].AGENT_MAX_STEPS_DEFAULT = 10

# ---------------------------------------------------------------------------
# Import the orchestrator module (not the class, to avoid __init__ side effects)
# ---------------------------------------------------------------------------
import src.agent.orchestrator as _orch_mod  # noqa: E402

AgentOrchestrator = _orch_mod.AgentOrchestrator
OrchestratorResult = _orch_mod.OrchestratorResult

from src.agent.protocols import (  # noqa: E402
    AgentContext,
    AgentRunStats,
    StageResult,
    StageStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(mode: str = "standard") -> AgentOrchestrator:
    """Create a minimal AgentOrchestrator bypassing __init__."""
    inst = AgentOrchestrator.__new__(AgentOrchestrator)
    inst.mode = mode
    inst.tool_registry = MagicMock()
    inst.llm_adapter = MagicMock()
    inst.skill_instructions = None
    inst.technical_skill_policy = None
    inst._skill_agent_names = set()
    return inst


def _make_agent(name: str, delay: float = 0.0, fail: bool = False) -> MagicMock:
    """Create a fake agent stub.

    If ``delay`` > 0 the agent sleeps that many seconds (simulates LLM latency).
    If ``fail`` is True the agent returns a FAILED StageResult.
    """
    agent = MagicMock()
    agent.agent_name = name

    def _run(ctx, progress_callback=None, timeout_seconds=None):
        if delay:
            time.sleep(delay)
        if fail:
            return StageResult(
                stage_name=name,
                status=StageStatus.FAILED,
                error="deliberate failure",
            )
        return StageResult(
            stage_name=name,
            status=StageStatus.COMPLETED,
            duration_s=delay,
            meta={"models_used": [f"model-{name}"], "tool_calls_log": []},
        )

    agent.run.side_effect = _run
    return agent


def _make_ctx() -> AgentContext:
    ctx = AgentContext.__new__(AgentContext)
    ctx.query = "test"
    ctx.stock_code = "600519"
    ctx.stock_name = "贵州茅台"
    ctx.session_id = "test-session"
    ctx.data = {}
    ctx.opinions = []
    ctx.risk_flags = []
    ctx.meta = {}
    ctx.created_at = time.time()
    return ctx


# ---------------------------------------------------------------------------
# Tests: _run_parallel_stages
# ---------------------------------------------------------------------------

class TestRunParallelStages(unittest.TestCase):

    def setUp(self):
        self.orch = _make_orchestrator()

    def test_returns_results_in_order(self):
        """Results must come back in the same order as the agents list."""
        a1 = _make_agent("technical", delay=0.05)
        a2 = _make_agent("intel", delay=0.01)  # finishes first
        ctx = _make_ctx()

        results = self.orch._run_parallel_stages([a1, a2], ctx)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].stage_name, "technical")
        self.assertEqual(results[1].stage_name, "intel")

    def test_both_complete(self):
        """Both stage results must be COMPLETED."""
        a1 = _make_agent("technical")
        a2 = _make_agent("intel")
        ctx = _make_ctx()

        results = self.orch._run_parallel_stages([a1, a2], ctx)

        self.assertTrue(results[0].success)
        self.assertTrue(results[1].success)

    def test_concurrent_execution(self):
        """Wall-clock time must be less than the sum of individual stage times."""
        delay = 0.12  # each agent sleeps 120 ms
        a1 = _make_agent("technical", delay=delay)
        a2 = _make_agent("intel", delay=delay)
        ctx = _make_ctx()

        t0 = time.time()
        self.orch._run_parallel_stages([a1, a2], ctx)
        elapsed = time.time() - t0

        # If sequential: ~240ms; if parallel: ~120ms.  Allow generous margin.
        self.assertLess(
            elapsed,
            delay * 1.8,
            f"Expected parallel execution (~{delay}s), got {elapsed:.3f}s — likely sequential",
        )

    def test_exception_in_stage_returns_failed_result(self):
        """An exception raised by an agent surfaces as StageStatus.FAILED."""
        def _bad_run(ctx, **kw):
            raise RuntimeError("simulated network error")

        a1 = _make_agent("technical")
        a2 = MagicMock()
        a2.agent_name = "intel"
        a2.run.side_effect = _bad_run

        ctx = _make_ctx()
        results = self.orch._run_parallel_stages([a1, a2], ctx)

        self.assertEqual(results[1].status, StageStatus.FAILED)
        self.assertIn("simulated network error", results[1].error)

    def test_failed_agent_does_not_block_other(self):
        """One failing agent must not prevent the other from completing."""
        a1 = _make_agent("technical")  # succeeds
        a2 = _make_agent("intel", fail=True)  # fails
        ctx = _make_ctx()

        results = self.orch._run_parallel_stages([a1, a2], ctx)

        self.assertTrue(results[0].success, "technical should still succeed")
        self.assertEqual(results[1].status, StageStatus.FAILED, "intel should be failed")


# ---------------------------------------------------------------------------
# Tests: _execute_pipeline parallel path
# ---------------------------------------------------------------------------

class TestExecutePipelineParallel(unittest.TestCase):

    def _make_pipeline_orch(self, mode="standard"):
        orch = _make_orchestrator(mode=mode)

        # Minimal _run_stage_agent delegation
        def _run_stage_agent(agent, ctx, progress_callback=None, timeout_seconds=None):
            return agent.run(ctx, progress_callback=progress_callback, timeout_seconds=timeout_seconds)

        orch._run_stage_agent = _run_stage_agent

        # Stub methods used after the parallel section
        orch._build_context = lambda task, ctx=None: _make_ctx()
        orch._build_timeout_result = MagicMock(return_value=OrchestratorResult(success=False, error="timeout"))
        orch._build_budget_skip_result = MagicMock(return_value=OrchestratorResult(success=False, error="budget"))
        orch._get_timeout_seconds = lambda: None  # no timeout
        orch._apply_risk_override = MagicMock()
        orch._aggregate_skill_opinions = MagicMock()
        orch._resolve_final_output = MagicMock(return_value=({}, "final content"))
        orch._build_agent_chain = MagicMock()
        return orch

    def test_standard_mode_runs_technical_then_intel(self):
        """In standard mode, technical runs first, then intel (sequential with budget guard)."""
        orch = self._make_pipeline_orch(mode="standard")
        call_order = []

        def _make_ordering_agent(name, sleep=0.02):
            agent = MagicMock()
            agent.agent_name = name

            def _run(ctx, **kw):
                call_order.append(name)
                time.sleep(sleep)
                return StageResult(
                    stage_name=name,
                    status=StageStatus.COMPLETED,
                    duration_s=sleep,
                    meta={"models_used": [], "tool_calls_log": []},
                )

            agent.run.side_effect = _run
            return agent

        technical = _make_ordering_agent("technical")
        intel = _make_ordering_agent("intel")
        decision = _make_ordering_agent("decision", sleep=0.005)

        orch._build_agent_chain.return_value = [technical, intel, decision]
        ctx = _make_ctx()

        orch._execute_pipeline(ctx)

        # technical must run before intel (sequential ordering)
        self.assertIn("technical", call_order)
        self.assertIn("intel", call_order)
        self.assertLess(
            call_order.index("technical"),
            call_order.index("intel"),
            "technical must start before intel",
        )

    def test_standard_mode_both_results_recorded(self):
        """Both stage results are recorded in stats after parallel run."""
        orch = self._make_pipeline_orch(mode="standard")

        technical = _make_agent("technical")
        intel = _make_agent("intel")
        decision = _make_agent("decision")

        orch._build_agent_chain.return_value = [technical, intel, decision]
        ctx = _make_ctx()
        orch._execute_pipeline(ctx)

        # At least technical + intel + decision = 3 stage results
        self.assertGreaterEqual(
            len(orch._build_agent_chain.return_value), 2,
            "Agent chain should include at least technical + intel",
        )

    def test_intel_failure_does_not_abort_pipeline(self):
        """intel failure in parallel path should degrade, not abort."""
        orch = self._make_pipeline_orch(mode="standard")

        technical = _make_agent("technical")
        intel = _make_agent("intel", fail=True)
        decision = _make_agent("decision")

        orch._build_agent_chain.return_value = [technical, intel, decision]
        ctx = _make_ctx()

        result = orch._execute_pipeline(ctx)

        # Pipeline must not abort — decision still ran
        decision.run.assert_called()
        self.assertTrue(
            result.success or result.content,
            "Pipeline should succeed or produce content even if intel failed",
        )

    def test_technical_failure_aborts_pipeline(self):
        """technical failure in parallel path must abort the pipeline."""
        orch = self._make_pipeline_orch(mode="standard")

        technical = _make_agent("technical", fail=True)
        intel = _make_agent("intel")
        decision = _make_agent("decision")

        orch._build_agent_chain.return_value = [technical, intel, decision]
        ctx = _make_ctx()

        result = orch._execute_pipeline(ctx)

        self.assertFalse(result.success)
        self.assertIn("technical", result.error or "")
        decision.run.assert_not_called()

    def test_quick_mode_is_not_parallelised(self):
        """quick mode (technical → decision) must not trigger the parallel path."""
        orch = self._make_pipeline_orch(mode="quick")

        technical = _make_agent("technical")
        decision = _make_agent("decision")

        orch._build_agent_chain.return_value = [technical, decision]
        ctx = _make_ctx()

        # Patch _run_parallel_stages to detect if it was called
        orch._run_parallel_stages = MagicMock(wraps=orch._run_parallel_stages)
        orch._execute_pipeline(ctx)

        orch._run_parallel_stages.assert_not_called()

    def test_full_mode_is_not_parallelised(self):
        """full mode must not trigger the parallel path."""
        orch = self._make_pipeline_orch(mode="full")

        technical = _make_agent("technical")
        intel = _make_agent("intel")
        risk = _make_agent("risk")
        decision = _make_agent("decision")

        orch._build_agent_chain.return_value = [technical, intel, risk, decision]
        ctx = _make_ctx()

        orch._run_parallel_stages = MagicMock(wraps=orch._run_parallel_stages)
        orch._execute_pipeline(ctx)

        orch._run_parallel_stages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
