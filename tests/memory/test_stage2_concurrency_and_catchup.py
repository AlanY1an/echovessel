"""Stage 2 add-ons · concurrent writes + catchup robustness.

Three tests pinning behaviour the existing suite never asserted:

- **2.4** Multiple actors writing to the same SQLite file at the same
  time must not raise ``OperationalError: database is locked``. This is
  the failure mode that killed catchup consolidate on real data
  yesterday — fixed by ``PRAGMA journal_mode=WAL`` +
  ``PRAGMA busy_timeout=5000`` in ``memory/db.py``.
- **2.7** A session left in ``CLOSING`` state across a daemon restart
  is picked up by the consolidate worker via ``initial_session_ids``
  and runs to completion (events written, ``extracted=1``).
- **2.8** When extraction keeps raising ``LLMTransientError`` past the
  retry budget, the worker marks the session ``FAILED`` with the cause
  appended to ``close_trigger`` instead of leaving it stuck in
  ``CLOSING``.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import date
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, NodeType, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractionResult,
)
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import ConceptNode
from echovessel.runtime.llm.errors import LLMTransientError
from echovessel.runtime.loops.consolidate_worker import ConsolidateWorker


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="P"))
    db.add(User(id="u", display_name="U"))
    db.commit()


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


# ---------------------------------------------------------------------------
# 2.4 · Concurrent writers across channels do not collide
# ---------------------------------------------------------------------------


def test_concurrent_writers_across_channels_do_not_lock() -> None:
    """Three threads, one per channel, each ingest a burst of messages.

    Without WAL + busy_timeout the second writer hits
    ``OperationalError: database is locked`` immediately. With the
    pragma fix in ``memory/db.py`` the writers serialise transparently
    and every commit lands.
    """

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "memory.db"
        engine = create_engine(db_path)
        create_all_tables(engine)

        with DbSession(engine) as db:
            _seed(db)

        errors: list[Exception] = []
        per_channel = 15

        def writer(channel: str) -> None:
            try:
                for i in range(per_channel):
                    with DbSession(engine) as db:
                        ingest_message(
                            db,
                            "p",
                            "u",
                            channel,
                            MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                            f"{channel}-{i}",
                        )
                        db.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("web",)),
            threading.Thread(target=writer, args=("discord",)),
            threading.Thread(target=writer, args=("imessage",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"writer threads raised: {errors!r}"

        with DbSession(engine) as db:
            count = db.exec(
                # Every message must have landed exactly once.
                select(func.count()).select_from(RecallMessage)
            ).one()
        assert count == per_channel * 3


# ---------------------------------------------------------------------------
# 2.7 · Worker picks up orphan CLOSING session at construction
# ---------------------------------------------------------------------------


async def test_worker_drains_orphan_closing_session_left_by_previous_run() -> None:
    """Simulate a daemon restart: a session is already in ``CLOSING``
    state with no extraction yet. The new worker is constructed with
    that session id in ``initial_session_ids`` and ``drain_once()``
    must consolidate it (write events, mark ``extracted=1``).
    """

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = Session(
            id="s_orphan",
            persona_id="p",
            user_id="u",
            channel_id="web",
            status=SessionStatus.CLOSING,
            message_count=4,
            total_tokens=400,
            close_trigger="catchup",
        )
        db.add(sess)
        for i, content in enumerate(
            ["我妈走了", "我没告诉任何人", "其实挺难受的", "好难过"]
        ):
            db.add(
                RecallMessage(
                    session_id="s_orphan",
                    persona_id="p",
                    user_id="u",
                    channel_id="web",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=content,
                    day=date.today(),
                    token_count=200,
                )
            )
        db.commit()

    extracted_event = ExtractedEvent(
        description="用户透露母亲过世且未向他人提及",
        emotional_impact=-8,
        emotion_tags=["grief"],
        relational_tags=["identity-bearing", "vulnerability"],
    )

    async def _extract(_msgs):
        return ExtractionResult(events=[extracted_event])

    async def _reflect(_nodes, _reason):
        return []

    worker = ConsolidateWorker(
        db_factory=lambda: DbSession(engine),
        backend=backend,
        extract_fn=_extract,
        reflect_fn=_reflect,
        embed_fn=_embed,
        initial_session_ids=("s_orphan",),
    )

    processed = await worker.drain_once()
    assert processed >= 1

    with DbSession(engine) as db:
        sess = db.get(Session, "s_orphan")
        assert sess is not None
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True
        assert sess.extracted_events is True

        events = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.source_session_id == "s_orphan"
                )
            )
        )
    # The extractor returned exactly one event; the orphan session is
    # consolidated, no duplicates.
    assert len(events) == 1
    assert events[0].type == NodeType.EVENT
    assert events[0].emotional_impact == -8


# ---------------------------------------------------------------------------
# 2.8 · max_retries exceeded → session marked FAILED with reason
# ---------------------------------------------------------------------------


async def test_worker_marks_session_failed_after_transient_retries_exhausted() -> None:
    """When the LLM keeps raising ``LLMTransientError`` past the retry
    budget, the worker:

    - stops retrying after ``max_retries`` attempts
    - marks the session ``FAILED``
    - stamps the cause on ``close_trigger`` (so a later debug session
      can see what killed it)
    - leaves an unrelated already-CLOSED session alone (no contagion).

    The "unrelated session" is set to CLOSED + extracted, not CLOSING,
    so the worker's own polling of CLOSING sessions does not pick it
    up — that polling is correct behaviour for the daemon and a
    different test would need a tighter harness to assert on it.
    """

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        # Doomed session — extraction will keep failing.
        db.add(
            Session(
                id="s_doomed",
                persona_id="p",
                user_id="u",
                channel_id="web",
                status=SessionStatus.CLOSING,
                message_count=4,
                total_tokens=400,
                close_trigger="catchup",
            )
        )
        # Bystander already done — proves the worker does not retroactively
        # touch CLOSED sessions when something else fails.
        db.add(
            Session(
                id="s_done",
                persona_id="p",
                user_id="u",
                channel_id="discord",
                status=SessionStatus.CLOSED,
                message_count=3,
                total_tokens=200,
                close_trigger="idle",
                extracted=True,
                extracted_events=True,
            )
        )
        for i in range(4):
            db.add(
                RecallMessage(
                    session_id="s_doomed",
                    persona_id="p",
                    user_id="u",
                    channel_id="web",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=f"doomed-{i}",
                    day=date.today(),
                    token_count=80,
                )
            )
        db.commit()

    calls: list[str] = []

    async def _flaky_extract(_msgs):
        calls.append("call")
        raise LLMTransientError("simulated upstream timeout")

    async def _reflect(_nodes, _reason):
        return []

    worker = ConsolidateWorker(
        db_factory=lambda: DbSession(engine),
        backend=backend,
        extract_fn=_flaky_extract,
        reflect_fn=_reflect,
        embed_fn=_embed,
        max_retries=1,
        initial_session_ids=("s_doomed",),
    )

    # Patch the per-retry backoff so the test does not actually sleep
    # 2 seconds between retries.
    import asyncio as _asyncio

    async def _instant_sleep(_seconds: float) -> None:
        return None

    real_sleep = _asyncio.sleep
    _asyncio.sleep = _instant_sleep  # type: ignore[assignment]
    try:
        await worker.drain_once()
    finally:
        _asyncio.sleep = real_sleep  # type: ignore[assignment]

    with DbSession(engine) as db:
        doomed = db.get(Session, "s_doomed")
        done = db.get(Session, "s_done")
        assert doomed is not None
        assert done is not None
        assert doomed.status == SessionStatus.FAILED
        assert "transient" in (doomed.close_trigger or "")
        # The bystander never moved.
        assert done.status == SessionStatus.CLOSED
        assert done.extracted is True

    # Initial attempt + max_retries=1 retry.
    assert len(calls) == 1 + 1
