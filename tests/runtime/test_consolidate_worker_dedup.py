"""ConsolidateWorker dedup regression tests.

Pin the behaviour that replaced the in-memory ``_seen`` set with
``Session.extracted`` as the single source of truth for idempotency
(see ``develop-docs/initiatives/_active/
2026-04-consolidate-worker-dedup-cleanup/00-plan.md``).

Three invariants:

1. Flipping a closed session back to ``status='closing', extracted=0``
   and re-draining the SAME worker instance triggers a fresh
   consolidation. No daemon restart required.
2. If a session with ``extracted=True`` is forcibly appended to the
   queue (e.g. by a buggy upstream), ``_process_one`` short-circuits
   without calling the extractor.
3. A session in ``status=FAILED`` is NOT auto-picked-up by the poller.
   The CLOSING filter still excludes it; retries remain a deliberate
   human action.
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Session as DbSession

from echovessel.core.types import MessageRole, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import ExtractedEvent, ExtractionResult
from echovessel.runtime.loops.consolidate_worker import ConsolidateWorker


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _add_closing_session(engine, sid: str, message_contents: list[str]) -> None:
    with DbSession(engine) as db:
        sess = Session(
            id=sid,
            persona_id="p",
            user_id="self",
            channel_id="t",
            status=SessionStatus.CLOSING,
            message_count=len(message_contents),
            total_tokens=sum(len(c) for c in message_contents),
        )
        db.add(sess)
        db.commit()

        for i, c in enumerate(message_contents):
            db.add(
                RecallMessage(
                    session_id=sid,
                    persona_id="p",
                    user_id="self",
                    channel_id="t",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=c,
                    day=date.today(),
                    token_count=len(c),
                )
            )
        db.commit()


async def _noop_reflect(nodes, reason):
    return []


async def test_flipping_extracted_false_triggers_retry_same_worker():
    """The load-bearing property: after SQL-flipping a closed session
    back to ``status=CLOSING, extracted=False``, the SAME worker
    instance (no restart) picks it up on the next drain and runs
    consolidation again. This is what ``_seen`` used to prevent.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_retry",
        [
            "今天我家狗子跑丢了",
            "还好邻居帮忙找到",
            "虚惊一场",
            "回家就给他奖励了肉干",
            "差点吓死我",
        ],
    )

    extract_calls: list[int] = []

    async def extractor(msgs):
        extract_calls.append(len(msgs))
        return ExtractionResult(
            events=[
                ExtractedEvent(
                    description=f"run {len(extract_calls)}",
                    emotional_impact=3,
                )
            ]
        )

    def _db():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
    )

    # First drain: session gets consolidated → extracted=True, status=closed
    processed_1 = await worker.drain_once()
    assert processed_1 == 1
    assert len(extract_calls) == 1
    with DbSession(engine) as db:
        sess = db.get(Session, "s_retry")
        assert sess is not None
        assert sess.extracted is True
        assert sess.status == SessionStatus.CLOSED

    # SQL-flip back to closing + extracted=False — simulates operator
    # re-triggering after a config change or a crash fix.
    with DbSession(engine) as db:
        sess = db.get(Session, "s_retry")
        assert sess is not None
        sess.status = SessionStatus.CLOSING
        sess.extracted = False
        sess.extracted_events = False
        db.add(sess)
        db.commit()

    # Second drain on the SAME worker: without the _seen removal, this
    # would skip the session entirely. With the removal, it re-runs.
    processed_2 = await worker.drain_once()
    assert processed_2 == 1
    assert len(extract_calls) == 2, (
        "Expected extractor to be called a second time after SQL-flip; "
        f"got {len(extract_calls)} total calls. The _seen set is still "
        "blocking retries."
    )
    with DbSession(engine) as db:
        sess = db.get(Session, "s_retry")
        assert sess is not None
        assert sess.extracted is True
        assert sess.status == SessionStatus.CLOSED


async def test_extracted_session_in_queue_short_circuits():
    """Idempotency should come from the persistent flag, not in-memory
    state. If someone force-appends an already-extracted session id to
    the queue, ``_process_one`` must short-circuit without calling the
    extractor.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(engine, "s_done", ["a", "b", "c", "d", "e"])

    # Pre-mark the session as already extracted — simulates a session
    # that a prior drain finished, or a race where the queue still has
    # the id after the extract flag flipped.
    with DbSession(engine) as db:
        sess = db.get(Session, "s_done")
        assert sess is not None
        sess.extracted = True
        sess.status = SessionStatus.CLOSED
        db.add(sess)
        db.commit()

    extract_calls: list[int] = []

    async def extractor(msgs):
        extract_calls.append(len(msgs))
        return ExtractionResult()

    def _db():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        initial_session_ids=("s_done",),  # force it onto the queue
    )

    # Belt-and-braces: also append manually a second time.
    worker._queue.append("s_done")  # noqa: SLF001

    await worker.drain_once()

    assert extract_calls == [], (
        "Extractor should NOT have been called for an already-extracted "
        "session even when the id is on the queue twice. "
        "Persistent idempotency flag is not short-circuiting."
    )


async def test_failed_session_not_auto_retried():
    """Deleting ``_seen`` must not accidentally turn FAILED sessions into
    auto-retry targets. The poller filter ``WHERE status=CLOSING`` still
    excludes FAILED. Recovery remains a deliberate flip back to CLOSING.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(engine, "s_failed", ["a", "b", "c", "d", "e"])

    # Mark the session as previously failed.
    with DbSession(engine) as db:
        sess = db.get(Session, "s_failed")
        assert sess is not None
        sess.status = SessionStatus.FAILED
        sess.extracted = False  # a real failure leaves extracted=False
        sess.close_trigger = "manual|failed:stub"
        db.add(sess)
        db.commit()

    extract_calls: list[int] = []

    async def extractor(msgs):
        extract_calls.append(len(msgs))
        return ExtractionResult()

    def _db():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
    )

    processed = await worker.drain_once()
    assert processed == 0, (
        "FAILED sessions must not be picked up automatically. "
        "The poll query filter for status=CLOSING should continue to "
        "exclude them even after the _seen set is gone."
    )
    assert extract_calls == [], "Extractor should not run on a FAILED session."
