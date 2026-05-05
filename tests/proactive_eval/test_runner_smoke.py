"""Runner smoke tests · do NOT hit live LLM (use stub LLM)."""

from __future__ import annotations

from echovessel.runtime.llm.usage import Usage
from tests.proactive_eval.runner import run_fixture
from tests.proactive_eval.schema import (
    ClockSpec,
    PersonaProfileSpec,
    ProactiveFixture,
    ProactiveSeed,
)


class StubLLM:
    provider_name = "stub"

    def __init__(self, response: str = '{"events": []}') -> None:
        self._response = response

    def model_for(self, role: str) -> str:
        return f"stub-{role}"

    async def complete(self, system, user, *, model_role="main", **kw):
        return self._response, Usage(
            input_tokens=10,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

    async def stream(self, *args, **kw):
        yield self._response


# Project pytest config sets asyncio_mode = "auto" — no decorator needed.


async def test_runner_smoke_minimal_fixture() -> None:
    """No turns, no expectations · runner returns empty result without error."""
    fix = ProactiveFixture(
        fixture_id="smoke",
        scenario="smoke",
        clock=ClockSpec(fixed_now="2026-05-05 12:00:00"),
        turns=[],
    )
    result = await run_fixture(fix, llm=StubLLM())
    assert result.events == []
    assert result.audit_per_phase == {}


async def test_runner_seeds_persona_profile() -> None:
    fix = ProactiveFixture(
        fixture_id="seed_pp",
        scenario="seed pp",
        clock=ClockSpec(fixed_now="2026-05-05 12:00:00"),
        turns=[],
        seed=ProactiveSeed(
            persona_profile=PersonaProfileSpec(style_summary="温柔", quiet_hours=[22, 8])
        ),
    )
    result = await run_fixture(fix, llm=StubLLM())
    assert result.events == []
