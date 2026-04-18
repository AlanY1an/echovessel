"""Interaction layer end-to-end tests (happy path + error handling)."""

from __future__ import annotations

from datetime import date, datetime

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import BlockLabel, MessageRole
from echovessel.memory import (
    CoreBlock,
    Persona,
    RecallMessage,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.runtime.interaction import (
    IncomingMessage,
    PersonaFactsView,
    TurnContext,
    assemble_turn,
    build_system_prompt,
    build_user_prompt,
)
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.llm.errors import LLMPermanentError, LLMTransientError


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="Sage"))
    db.add(User(id="self", display_name="Alan"))
    db.add(
        CoreBlock(
            persona_id="p",
            user_id=None,
            label=BlockLabel.PERSONA,
            content="You are Sage, calm and present.",
        )
    )
    db.commit()


async def test_assemble_turn_happy_path_ingests_both_user_and_persona():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Sage",
            db=db,
            backend=backend,
            embed_fn=_embed,
        )
        envelope = IncomingMessage(
            channel_id="web",
            user_id="self",
            content="hi there",
            received_at=datetime(2026, 4, 14, 9, 0, 0),
        )
        stub = StubProvider(fallback="hey, what's on your mind")
        result = await assemble_turn(ctx, envelope, stub)

        assert not result.skipped
        assert result.reply == "hey, what's on your mind"

        # Two messages in L2: the user turn and the persona reply.
        msgs = list(
            db.exec(
                select(RecallMessage).order_by(RecallMessage.id)
            )
        )
        assert len(msgs) == 2
        assert msgs[0].role == MessageRole.USER
        assert msgs[1].role == MessageRole.PERSONA
        assert msgs[1].content == "hey, what's on your mind"


async def test_assemble_turn_skips_on_permanent_llm_error():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    class BadProvider(StubProvider):
        async def complete(self, system, user, **kwargs):
            raise LLMPermanentError("nope")

    with DbSession(engine) as db:
        _seed(db)
        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Sage",
            db=db,
            backend=backend,
            embed_fn=_embed,
        )
        envelope = IncomingMessage(
            channel_id="web",
            user_id="self",
            content="whatever",
            received_at=datetime(2026, 4, 14, 10, 0, 0),
        )
        result = await assemble_turn(ctx, envelope, BadProvider())

        assert result.skipped
        assert result.error and "permanent" in result.error

        # User message still ingested; persona reply NOT (we skipped before
        # the persona ingest).
        msgs = list(db.exec(select(RecallMessage)))
        assert len(msgs) == 1
        assert msgs[0].role == MessageRole.USER


async def test_assemble_turn_skips_on_transient_no_retry():
    """v0.4 · review M6 + handoff §10.2: streaming does NOT retry on
    LLMTransientError. Already-streamed tokens are kept (not rolled
    back), but no second LLM call is attempted — retrying would
    duplicate tokens the user already saw and double-bill.

    This test replaces the v0.3 `retries_transient_then_succeeds`
    test (same file) because v0.4 removes the retry loop entirely.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    attempts = {"n": 0}

    class FlakyProvider(StubProvider):
        async def complete(self, system, user, **kwargs):
            attempts["n"] += 1
            raise LLMTransientError("flaky")

    with DbSession(engine) as db:
        _seed(db)
        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Sage",
            db=db,
            backend=backend,
            embed_fn=_embed,
        )
        envelope = IncomingMessage(
            channel_id="web",
            user_id="self",
            content="are you there",
            received_at=datetime(2026, 4, 14, 11, 0, 0),
        )

        result = await assemble_turn(ctx, envelope, FlakyProvider())

    assert result.skipped
    assert result.error and "transient" in result.error
    # Exactly one LLM attempt — no retry loop.
    assert attempts["n"] == 1


def test_build_system_prompt_has_style_block():
    out = build_system_prompt(persona_display_name="Test", core_blocks=[])
    assert "NOT the medium" in out
    assert "Test" in out


def test_build_system_prompt_omits_who_you_are_when_no_facts():
    out = build_system_prompt(persona_display_name="Test", core_blocks=[])
    assert "# Who you are" not in out


def test_build_system_prompt_renders_who_you_are_with_five_facts():
    facts = PersonaFactsView(
        full_name="张丽华",
        gender="female",
        birth_date=date(1962, 3, 15),
        occupation="retired_teacher",
        native_language="zh-CN",
    )
    out = build_system_prompt(
        persona_display_name="妈",
        core_blocks=[],
        persona_facts=facts,
    )
    assert "# Who you are" in out
    assert "- Name: 张丽华" in out
    assert "- Gender: female" in out
    # Only the year is rendered, not the full ISO date.
    assert "- Born: 1962" in out
    assert "1962-03-15" not in out
    assert "- Occupation: retired_teacher" in out
    assert "- Native language: zh-CN" in out


def test_build_system_prompt_skips_null_facts_individually():
    facts = PersonaFactsView(full_name="Ann", occupation="engineer")
    out = build_system_prompt(
        persona_display_name="Ann",
        core_blocks=[],
        persona_facts=facts,
    )
    assert "- Name: Ann" in out
    assert "- Occupation: engineer" in out
    # Unset fields do not emit empty bullets.
    assert "- Gender:" not in out
    assert "- Born:" not in out
    assert "- Native language:" not in out


def test_build_system_prompt_empty_view_is_equivalent_to_no_view():
    baseline = build_system_prompt(persona_display_name="X", core_blocks=[])
    with_empty = build_system_prompt(
        persona_display_name="X",
        core_blocks=[],
        persona_facts=PersonaFactsView.empty(),
    )
    assert baseline == with_empty


def test_persona_facts_view_from_persona_row_copies_five_columns():
    row = Persona(
        id="p",
        display_name="Sage",
        full_name="Full Name",
        gender="female",
        birth_date=date(2001, 5, 4),
        occupation="software_engineer",
        native_language="en-US",
        # Columns NOT in the view should be ignored.
        timezone="America/Los_Angeles",
        marital_status="single",
    )
    view = PersonaFactsView.from_persona_row(row)
    assert view.full_name == "Full Name"
    assert view.gender == "female"
    assert view.birth_date == date(2001, 5, 4)
    assert view.occupation == "software_engineer"
    assert view.native_language == "en-US"


def test_persona_facts_view_from_none_row_is_empty():
    view = PersonaFactsView.from_persona_row(None)
    assert view == PersonaFactsView.empty()


def test_build_user_prompt_just_user_message_when_empty():
    out = build_user_prompt(
        top_memories=[], recent_messages=[], user_message="hi"
    )
    assert out.endswith("hi")
    assert "What they just said" in out
