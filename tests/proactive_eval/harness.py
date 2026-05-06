"""Re-export shim for proactive_eval users."""

from tests.memory_eval.embedders import (
    REAL_CONFIG_PATH,
    build_eval_embedder,
    build_live_llm,
)
from tests.memory_eval.judge import JudgeVerdict, judge_prompts
from tests.proactive_eval.invariants import check_invariants, render_evidence
from tests.proactive_eval.runner import run_fixture
from tests.proactive_eval.schema import (
    FIXTURE_ROOT,
    ClockSpec,
    ExpectEvent,
    ExpectPhase,
    FollowUpStage,
    PersonaProfileSpec,
    PhaseEvalResult,
    ProactiveDecisionSpec,
    ProactiveEvalResult,
    ProactiveExpect,
    ProactiveFixture,
    ProactiveSeed,
    ProactiveStateSpec,
    XfailSpec,
    discover_fixtures,
    load_fixture,
)

__all__ = [
    "FIXTURE_ROOT",
    "REAL_CONFIG_PATH",
    "ClockSpec",
    "ExpectEvent",
    "ExpectPhase",
    "FollowUpStage",
    "JudgeVerdict",
    "PersonaProfileSpec",
    "PhaseEvalResult",
    "ProactiveDecisionSpec",
    "ProactiveEvalResult",
    "ProactiveExpect",
    "ProactiveFixture",
    "ProactiveSeed",
    "ProactiveStateSpec",
    "XfailSpec",
    "build_eval_embedder",
    "build_live_llm",
    "check_invariants",
    "discover_fixtures",
    "judge_prompts",
    "load_fixture",
    "render_evidence",
    "run_fixture",
]
