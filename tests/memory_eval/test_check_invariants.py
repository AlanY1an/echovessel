"""Unit tests for ``check_invariants`` — each new invariant field has its own
test pair (positive + negative) so harness extension is TDD-driven."""

from __future__ import annotations

from tests.memory_eval.harness import (
    EvalResult,
    Fixture,
    FixtureSeed,
    check_invariants,  # noqa: F401 — used by per-field tests added in later commits
)


def _result(**kw) -> EvalResult:
    """Build a minimal EvalResult with overridable fields."""
    base: dict = {
        "events": [],
        "thoughts": [],
        "filling": [],
        "mood_block_before": "",
        "mood_block_after": "",
        "retrieved": [],
        "reflection_triggered": False,
    }
    base.update(kw)
    return EvalResult(**base)


def _fixture(invariants: dict) -> Fixture:
    return Fixture(
        fixture_id="t",
        version="scripted",
        generated_at=None,
        model=None,
        scenario="",
        seed=FixtureSeed(),
        turns=[],
        retrieve=None,
        invariants=invariants,
        judge_prompts=[],
    )
