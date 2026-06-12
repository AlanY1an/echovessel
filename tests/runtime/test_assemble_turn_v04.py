"""v0.4 · assemble_turn streaming / IncomingTurn / on_turn_done tests.

Five test cases cover:
    1. single-message turn runs through streaming + ingest
    2. multi-message burst is handed to the LLM as one connected prompt
    3. on_turn_done fires on success
    4. on_turn_done fires on transient failure (finally block)
    5. on_turn_done itself raising doesn't bubble up

All tests use `StubProvider` so no network I/O happens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import text
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
from echovessel.memory.migrations import ensure_schema_up_to_date
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.llm.errors import LLMTransientError
from echovessel.runtime.turn.coordinator import (
    IncomingMessage,
    IncomingTurn,
    TurnContext,
    assemble_turn,
)


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
            content="You are Sage.",
        )
    )
    db.commit()


def _ctx(db: DbSession, backend: SQLiteBackend) -> TurnContext:
    return TurnContext(
        persona_id="p",
        persona_display_name="Sage",
        db=db,
        backend=backend,
        embed_fn=_embed,
    )


class _TokenRecordingStub(StubProvider):
    """StubProvider whose stream() yields one character per tick so
    tests can assert the on_token callback fires multiple times.
    """

    async def stream(
        self,
        system: str,
        user: str,
        *,
        model_role: str = "main",
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        text, _usage = await self.complete(
            system,
            user,
            model_role=model_role,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        for ch in text:
            yield ch


# ---------------------------------------------------------------------------
# 1. Single-message turn
# ---------------------------------------------------------------------------


async def test_assemble_turn_single_message():
    """Single-message IncomingTurn runs the full pipeline and stamps
    the same turn_id on both user and persona L2 rows."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    tokens_seen: list[tuple[int, str]] = []

    async def _on_token(mid: int, delta: str) -> None:
        tokens_seen.append((mid, delta))

    with DbSession(engine) as db:
        _seed(db)
        msg = IncomingMessage(
            channel_id="web",
            user_id="self",
            content="hi there",
            received_at=datetime(2026, 4, 14, 9, 0, 0),
        )
        turn = IncomingTurn(
            turn_id="turn-A",
            channel_id="web",
            user_id="self",
            messages=[msg],
            received_at=datetime(2026, 4, 14, 9, 0, 1),
        )
        stub = _TokenRecordingStub(fallback="hello")
        result = await assemble_turn(_ctx(db, backend), turn, stub, on_token=_on_token)

        assert not result.skipped
        assert result.reply == "hello"
        assert len(tokens_seen) >= 1
        # All tokens share the same pending message id (opaque int).
        ids = {mid for mid, _ in tokens_seen}
        assert len(ids) == 1

        # L2 has 2 rows, both with turn_id="turn-A".
        rows = list(db.exec(select(RecallMessage).order_by(RecallMessage.id)))
        assert len(rows) == 2
        assert rows[0].role == MessageRole.USER
        assert rows[1].role == MessageRole.PERSONA
        assert rows[0].turn_id == "turn-A"
        assert rows[1].turn_id == "turn-A"


# ---------------------------------------------------------------------------
# 2. Multi-message burst
# ---------------------------------------------------------------------------


async def test_assemble_turn_multi_message_burst():
    """Three messages in one IncomingTurn → three L2 user rows + one
    persona row, all sharing the same turn_id. LLM stream is invoked
    exactly once (not three times), verified by counting call
    attempts.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    calls = {"n": 0}

    class _CountingStub(_TokenRecordingStub):
        async def complete(self, system, user, **kwargs):
            calls["n"] += 1
            # Spec §17a.1: prompt should contain all 3 messages.
            assert "one" in user
            assert "two" in user
            assert "three" in user
            return "burst-ok", None

    with DbSession(engine) as db:
        _seed(db)
        now = datetime(2026, 4, 14, 10, 0, 0)
        msgs = [
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="one",
                received_at=now,
            ),
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="two",
                received_at=now,
            ),
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="three",
                received_at=now,
            ),
        ]
        turn = IncomingTurn(
            turn_id="turn-burst",
            channel_id="web",
            user_id="self",
            messages=msgs,
            received_at=now,
        )
        result = await assemble_turn(_ctx(db, backend), turn, _CountingStub())

        assert not result.skipped
        assert result.reply == "burst-ok"
        # Only one LLM stream invocation, even though 3 user messages.
        assert calls["n"] == 1

        rows = list(db.exec(select(RecallMessage).order_by(RecallMessage.id)))
        assert len(rows) == 4
        user_rows = [r for r in rows if r.role == MessageRole.USER]
        persona_rows = [r for r in rows if r.role == MessageRole.PERSONA]
        assert len(user_rows) == 3
        assert len(persona_rows) == 1
        assert all(r.turn_id == "turn-burst" for r in rows)


# ---------------------------------------------------------------------------
# 3. on_turn_done called on success
# ---------------------------------------------------------------------------


async def test_assemble_turn_on_turn_done_called():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    done_ids: list[str] = []

    async def _on_done(turn_id: str) -> None:
        done_ids.append(turn_id)

    with DbSession(engine) as db:
        _seed(db)
        turn = IncomingTurn.from_single_message(
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="hi",
                received_at=datetime.now(),
            ),
            turn_id="t-ok",
        )
        stub = _TokenRecordingStub(fallback="sure")
        result = await assemble_turn(_ctx(db, backend), turn, stub, on_turn_done=_on_done)
        assert not result.skipped
        assert done_ids == ["t-ok"]


# ---------------------------------------------------------------------------
# 4. on_turn_done called on transient LLM failure (finally path)
# ---------------------------------------------------------------------------


async def test_assemble_turn_on_turn_done_on_error():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    done_ids: list[str] = []

    async def _on_done(turn_id: str) -> None:
        done_ids.append(turn_id)

    class _BoomStub(StubProvider):
        async def complete(self, system, user, **kwargs):
            raise LLMTransientError("boom")

    with DbSession(engine) as db:
        _seed(db)
        turn = IncomingTurn.from_single_message(
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="hi",
                received_at=datetime.now(),
            ),
            turn_id="t-err",
        )
        result = await assemble_turn(_ctx(db, backend), turn, _BoomStub(), on_turn_done=_on_done)
        # Skipped because nothing made it through the stream.
        assert result.skipped
        # on_turn_done fired even though LLM errored.
        assert done_ids == ["t-err"]


# ---------------------------------------------------------------------------
# 5. on_turn_done exception is swallowed
# ---------------------------------------------------------------------------


async def test_assemble_turn_on_turn_done_exception_swallowed():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    async def _angry_done(turn_id: str) -> None:
        raise RuntimeError("channel blew up")

    with DbSession(engine) as db:
        _seed(db)
        turn = IncomingTurn.from_single_message(
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="hi",
                received_at=datetime.now(),
            ),
            turn_id="t-swallow",
        )
        stub = _TokenRecordingStub(fallback="reply")
        # Must NOT raise — the finally block swallows on_turn_done's
        # exception and logs a warning instead.
        result = await assemble_turn(_ctx(db, backend), turn, stub, on_turn_done=_angry_done)
        assert not result.skipped
        assert result.reply == "reply"


# ---------------------------------------------------------------------------
# 6. Live turn renders exactly once in the user prompt
# ---------------------------------------------------------------------------


async def test_current_turn_text_appears_exactly_once_in_user_prompt():
    """The live turn's text is owned by `# What they just said` — the
    `# Our recent conversation` window carries only prior turns, so the
    same user text never renders twice in one prompt.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        stub = _TokenRecordingStub(fallback="sure")

        prior = IncomingTurn.from_single_message(
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="quokka sighting at the lighthouse",
                received_at=datetime(2026, 4, 14, 9, 0, 0),
            ),
            turn_id="t-prior",
        )
        await assemble_turn(_ctx(db, backend), prior, stub)

        live = IncomingTurn.from_single_message(
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="zebra pancakes for breakfast",
                received_at=datetime(2026, 4, 14, 9, 5, 0),
            ),
            turn_id="t-live",
        )
        result = await assemble_turn(_ctx(db, backend), live, stub)

        assert result.user_prompt.count("zebra pancakes for breakfast") == 1
        _, _, current_section = result.user_prompt.partition("# What they just said")
        assert "zebra pancakes for breakfast" in current_section
        # Prior exchange still shows in the recent-conversation window.
        assert "quokka sighting at the lighthouse" in result.user_prompt


async def test_burst_messages_appear_exactly_once_in_user_prompt():
    """Multi-message burst: every message of the live turn renders once."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        now = datetime(2026, 4, 14, 11, 0, 0)
        msgs = [
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content=f"burst-fragment-{i}",
                received_at=now,
            )
            for i in range(3)
        ]
        turn = IncomingTurn(
            turn_id="t-burst-once",
            channel_id="web",
            user_id="self",
            messages=msgs,
            received_at=now,
        )
        result = await assemble_turn(_ctx(db, backend), turn, _TokenRecordingStub(fallback="ok"))

        for i in range(3):
            assert result.user_prompt.count(f"burst-fragment-{i}") == 1


# ---------------------------------------------------------------------------
# 7. Latency metrics are monotonic deltas (clock-source independent)
# ---------------------------------------------------------------------------


async def test_latency_metrics_use_monotonic_deltas():
    """first_token_ms / duration_ms come from ``time.monotonic()`` deltas.

    The injected ``now_fn`` runs on an arbitrary wall clock (years away
    from real now) — if either metric mixed that wall clock with another
    clock source, the persisted values would be wildly inflated.
    """
    engine = create_engine(":memory:")
    ensure_schema_up_to_date(engine)
    create_all_tables(engine)
    ensure_schema_up_to_date(engine)
    backend = SQLiteBackend(engine)

    wall = datetime(2020, 1, 1, 0, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Sage",
            db=db,
            backend=backend,
            embed_fn=_embed,
            dev_trace_enabled=True,
        )
        turn = IncomingTurn.from_single_message(
            IncomingMessage(
                channel_id="web",
                user_id="self",
                content="hi",
                received_at=wall,
            ),
            turn_id="t-clock",
        )
        stub = _TokenRecordingStub(fallback="hello")
        result = await assemble_turn(ctx, turn, stub, now_fn=lambda: wall)
        assert not result.skipped

        row = db.execute(
            text("SELECT duration_ms, first_token_ms FROM turn_traces WHERE turn_id=:t"),
            {"t": "t-clock"},
        ).fetchone()
    assert row is not None
    duration_ms, first_token_ms = row
    assert 0 <= first_token_ms <= duration_ms < 60_000
