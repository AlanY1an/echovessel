"""Parametric eval runner · one test per YAML fixture.

Marked ``@pytest.mark.eval_proactive`` so pytest default-skips them — run
``uv run pytest -m eval_proactive`` to pay for a live LLM pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.proactive_eval.harness import (
    build_live_llm,
    check_invariants,
    discover_fixtures,
    judge_prompts,
    load_fixture,
    render_evidence,
    run_fixture,
)


def _fixture_id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


_FIXTURES = discover_fixtures()


def _build_param(path: Path):
    fix = load_fixture(path)
    if fix.xfail:
        return pytest.param(path, marks=pytest.mark.xfail(reason=fix.xfail.reason, strict=False))
    return path


@pytest.mark.eval_proactive
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

    all_prompts: list[str] = list(fixture.judge_prompts)
    for phase_name, ep in fixture.expect.phases.items():
        for jp in ep.judge_message_prompts:
            all_prompts.append(f"[phase={phase_name}] {jp}")

    verdicts = []
    if all_prompts:
        verdicts = await judge_prompts(llm=llm, evidence=evidence, prompts=all_prompts)
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
