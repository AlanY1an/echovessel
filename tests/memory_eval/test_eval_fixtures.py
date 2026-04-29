"""Parameterised eval tests · one test per fixture YAML.

Every ``.yaml`` under ``fixtures/scripted/`` and
``fixtures/synthesized/`` becomes a separate parametrised test case.
Each run:

  1. Materialise the fixture in a fresh in-memory SQLite DB
  2. Replay turns through the real consolidate pipeline (live LLM!)
  3. Check the hard invariants declared in the YAML
  4. Ask the judge LLM each ``judge_prompts`` question

Marked ``@pytest.mark.eval`` so ``pytest`` default-skips them — run
``pytest -m eval`` to pay for a live LLM pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.memory_eval.harness import (
    build_live_llm,
    check_invariants,
    discover_fixtures,
    load_fixture,
    render_evidence,
    run_fixture,
)
from tests.memory_eval.judge import judge_prompts


def _fixture_id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


# Fixtures whose design depends on infrastructure that isn't wired yet.
# When the gap closes we just drop the row.
_XFAIL_FIXTURES: dict[str, str] = {
    "l5_entity_anchored_retrieve_bonus": (
        "seed-event entity resolution not wired in run_fixture; "
        "without it the retrieve cannot apply the entity-anchor boost"
    ),
}


def _build_param(path: Path):
    reason = _XFAIL_FIXTURES.get(path.stem)
    if reason:
        return pytest.param(
            path, marks=pytest.mark.xfail(reason=reason, strict=False)
        )
    return path


_FIXTURES = discover_fixtures()


@pytest.mark.eval
@pytest.mark.parametrize(
    "fixture_path",
    [_build_param(p) for p in _FIXTURES],
    ids=[_fixture_id(p) for p in _FIXTURES],
)
async def test_eval_fixture(fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)

    try:
        llm = build_live_llm()
    except (FileNotFoundError, RuntimeError) as e:
        pytest.skip(f"live LLM unavailable: {e}")

    result = await run_fixture(fixture, llm=llm)

    violations = check_invariants(fixture, result)
    evidence = render_evidence(fixture, result)

    # Always collect judge verdicts even if a hard invariant already
    # failed — the reasoning is useful debug output.
    verdicts = []
    if fixture.judge_prompts:
        verdicts = await judge_prompts(
            llm=llm,
            evidence=evidence,
            prompts=fixture.judge_prompts,
        )

    failing_judgements = [v for v in verdicts if not v.verdict]

    if violations or failing_judgements:
        report = ["\n" + evidence]
        if violations:
            report.append("\n-- Invariant violations --")
            for v in violations:
                report.append(f"  · {v}")
        if failing_judgements:
            report.append("\n-- Judge said NO --")
            for v in failing_judgements:
                report.append(f"  Q: {v.prompt}")
                report.append(f"  A: {v.reasoning}")
        pytest.fail("\n".join(report))
