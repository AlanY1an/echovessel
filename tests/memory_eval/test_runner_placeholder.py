"""Runner-level tests for the persona placeholder hook + per-turn channel.

When a persona turn's content is the ``"..."`` sentinel the runner
delegates to the production ``assemble_turn`` so the LLM produces a
real reply — without this hook, L1 fixtures would ingest a literal
"..." into L2 and consolidation would have nothing to extract from.
"""

from __future__ import annotations

import os

from echovessel.runtime.llm.stub import StubProvider
from tests.memory_eval.harness import Fixture, FixtureSeed, FixtureTurn, run_fixture
from tests.memory_eval.schema import FixtureRetrieve

# Force keyword-axis embedder so tests don't pull the 90 MB
# sentence-transformers model on first run.
os.environ.setdefault("ECHOVESSEL_EVAL_EMBEDDER", "keyword")


def _fixture(turns: list[FixtureTurn], **overrides) -> Fixture:
    base = {
        "fixture_id": "t",
        "version": "scripted",
        "generated_at": None,
        "model": None,
        "scenario": "",
        "seed": FixtureSeed(persona_block="你是陪伴"),
        "turns": turns,
        "retrieve": None,
        "invariants": {},
        "judge_prompts": [],
    }
    base.update(overrides)
    return Fixture(**base)


class _SpyProvider(StubProvider):
    """StubProvider that records every system prompt it was asked to
    complete. Lets a test prove assemble_turn was invoked (placeholder
    hook fired) versus a plain ingest_message of the "..." literal
    (which never calls the LLM)."""

    def __init__(self, *, fallback: str) -> None:
        super().__init__(fallback=fallback)
        self.system_prompts: list[str] = []

    async def complete(self, system, user, **kwargs):
        self.system_prompts.append(system)
        return await super().complete(system, user, **kwargs)


async def test_placeholder_persona_turn_invokes_assemble_turn() -> None:
    """The placeholder "..." persona turn must trigger assemble_turn,
    which invokes the LLM. A plain ingest of the literal would never
    call the provider.
    """
    fixture = _fixture(
        turns=[
            FixtureTurn(role="user", content="你能介绍一下你自己吗？"),
            FixtureTurn(role="persona", content="..."),
        ],
    )
    spy = _SpyProvider(fallback="我是欧阳老师，住在台北。")

    result = await run_fixture(fixture, llm=spy)

    # assemble_turn's system prompt has a distinctive "You are Eval"
    # opener (build_system_prompt §1). If the placeholder hook fired,
    # at least one captured system prompt starts with that line.
    assemble_invocations = [
        s for s in spy.system_prompts if s.startswith("You are Eval")
    ]
    assert assemble_invocations, (
        f"placeholder hook did NOT fire · captured: {spy.system_prompts}"
    )
    # User + LLM-generated persona reply both reach L2.
    assert result.recall_count == 2


async def test_non_placeholder_persona_turn_does_not_invoke_assemble_turn() -> None:
    """A non-"..." persona turn must NOT invoke assemble_turn — the
    runner ingests it verbatim. Keeps the L3-L6 fixtures deterministic
    where the persona reply text is part of the test contract.

    The "You are Eval" prefix is unique to assemble_turn's system
    prompt; extract / reflect prompts have their own wording.
    """
    fixture = _fixture(
        turns=[
            FixtureTurn(role="user", content="你住哪里？"),
            FixtureTurn(role="persona", content="台北"),
        ],
    )
    spy = _SpyProvider(fallback="should-not-be-called")

    await run_fixture(fixture, llm=spy)

    assemble_invocations = [
        s for s in spy.system_prompts if s.startswith("You are Eval")
    ]
    assert assemble_invocations == [], (
        f"assemble_turn fired unexpectedly · system prompts: {spy.system_prompts}"
    )


async def test_per_turn_channel_routes_through_runner() -> None:
    """Turns ingested under different channels still surface through
    retrieval (which has no channel arg) — D4 铁律.
    """
    fixture = _fixture(
        turns=[
            FixtureTurn(role="user", content="hello", channel="discord"),
            FixtureTurn(role="persona", content="hi", channel="discord"),
        ],
        retrieve=FixtureRetrieve(query="hello", top_k=3),
    )
    stub = StubProvider(fallback="hi")
    result = await run_fixture(fixture, llm=stub)
    assert result.recall_count == 2
