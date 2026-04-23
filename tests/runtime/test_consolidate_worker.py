"""ConsolidateWorker — PR6 tests."""

from __future__ import annotations

from datetime import date

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
from echovessel.memory.consolidate import ExtractedEvent, ExtractionResult
from echovessel.memory.models import ConceptNode
from echovessel.runtime.consolidate_worker import ConsolidateWorker
from echovessel.runtime.llm.errors import LLMPermanentError, LLMTransientError


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


async def test_worker_processes_closing_session_and_marks_closed():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_1",
        [
            "今天我家狗子跑丢了",
            "还好邻居帮忙找到",
            "虚惊一场",
            "回家就给他奖励了肉干",
            "差点吓死我",
        ],
    )

    async def extractor(msgs):
        return ExtractionResult(
            events=[
                ExtractedEvent(
                    description="用户的狗短暂走失后被邻居找回",
                    emotional_impact=3,
                    emotion_tags=["relief"],
                )
            ]
        )

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
    )
    processed = await worker.drain_once()
    assert processed == 1

    with DbSession(engine) as db:
        sess = db.get(Session, "s_1")
        assert sess is not None
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True


async def test_worker_idempotent_on_already_extracted():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_iden",
        [
            "闲聊一",
            "闲聊二",
            "闲聊三",
            "闲聊四",
            "闲聊五",
        ],
    )

    extract_calls: list[int] = []

    async def extractor(msgs):
        extract_calls.append(len(msgs))
        return ExtractionResult()

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
    )
    await worker.drain_once()
    # A second drain should be a no-op — session is now CLOSED.
    await worker.drain_once()

    assert extract_calls == [5]


async def test_worker_marks_failed_after_transient_exhaustion(monkeypatch):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_flake",
        ["raw" * 40, "raw2" * 40, "raw3" * 40, "raw4" * 40, "raw5" * 40],
    )

    async def flaky(msgs):
        raise LLMTransientError("boom")

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=flaky,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        max_retries=2,
    )

    import asyncio as _asyncio

    orig_sleep = _asyncio.sleep

    async def fast(_t):
        return None

    _asyncio.sleep = fast  # type: ignore[assignment]
    try:
        await worker.drain_once()
    finally:
        _asyncio.sleep = orig_sleep  # type: ignore[assignment]

    with DbSession(engine) as db:
        sess = db.get(Session, "s_flake")
        assert sess is not None
        assert sess.status == SessionStatus.FAILED


async def test_worker_marks_failed_on_permanent_error():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_perm",
        ["aaaaaaaa" * 50, "bbbbbbbb" * 50, "cccccccc" * 50, "dddddddd" * 50, "eeeeeeee" * 50],
    )

    async def perm(msgs):
        raise LLMPermanentError("auth dead")

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=perm,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
    )
    await worker.drain_once()

    with DbSession(engine) as db:
        sess = db.get(Session, "s_perm")
        assert sess is not None
        assert sess.status == SessionStatus.FAILED


async def test_worker_retries_on_sqlite_database_locked():
    """SQLite WAL contention is transient infra, not a permanent unknown
    fault. Reproduces the 2026-04-20 dogfood symptom where consolidate hit
    `sqlite3.OperationalError: database is locked` on first try and was
    marked FAILED with no retry, silently dropping a 22-message session."""
    import sqlite3

    import sqlalchemy.exc

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_lock",
        ["aaaaaaa" * 40, "bbbbbbb" * 40, "ccccccc" * 40, "ddddddd" * 40, "eeeeeee" * 40],
    )

    call_count = [0]

    async def flaky_then_ok(msgs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise sqlalchemy.exc.OperationalError(
                "SELECT 1", {}, sqlite3.OperationalError("database is locked")
            )
        return ExtractionResult(
            events=[
                ExtractedEvent(
                    description="finally extracted after lock contention",
                    emotional_impact=2,
                    emotion_tags=["relief"],
                )
            ]
        )

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=flaky_then_ok,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        max_retries=3,
    )

    import asyncio as _asyncio

    orig_sleep = _asyncio.sleep

    async def fast(_t):
        return None

    _asyncio.sleep = fast  # type: ignore[assignment]
    try:
        await worker.drain_once()
    finally:
        _asyncio.sleep = orig_sleep  # type: ignore[assignment]

    assert call_count[0] == 3
    with DbSession(engine) as db:
        sess = db.get(Session, "s_lock")
        assert sess is not None
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True


async def test_worker_does_not_retry_truly_unexpected_error():
    """Guard against over-broad transient classification: a non-infra
    exception (programming bug, schema mismatch, etc.) must still be marked
    FAILED on first hit instead of burning the retry budget."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_oops",
        ["xx" * 40, "yy" * 40, "zz" * 40, "ww" * 40, "vv" * 40],
    )

    call_count = [0]

    async def buggy(msgs):
        call_count[0] += 1
        raise ValueError("not transient — a real programming bug")

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=buggy,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        max_retries=3,
    )
    await worker.drain_once()

    assert call_count[0] == 1
    with DbSession(engine) as db:
        sess = db.get(Session, "s_oops")
        assert sess is not None
        assert sess.status == SessionStatus.FAILED


async def test_worker_does_not_duplicate_events_on_transient_reflect_failure():
    """Regression test for the P0 retry-duplication bug.

    When extraction succeeds but reflection raises ``LLMTransientError``,
    the worker retries ``consolidate_session`` from the top. Without the
    ``extracted_events`` guard, extraction runs a second time and commits
    a duplicate set of event nodes. This test pins the fixed behavior:
    one set of events regardless of how many reflection retries happen.

    See develop-docs/initiatives/_active/2026-04-consolidate-retry-safety/.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    # "走了" is a strong-emotion keyword → skips trivial, forces extraction.
    _add_closing_session(
        engine,
        "s_flaky_reflect",
        [
            "我今天心情糟透了",
            "出了一件很难接受的事情",
            "我爸突然走了",
            "我还没缓过来",
            "整个人都不对了",
        ],
    )

    extract_calls: list[int] = []

    async def extractor(_msgs):
        extract_calls.append(1)
        return ExtractionResult(
            events=[
                ExtractedEvent(
                    description="user lost father suddenly",
                    # impact >= SHOCK_IMPACT_THRESHOLD (=8) forces reflection
                    emotional_impact=9,
                    emotion_tags=["grief"],
                )
            ]
        )

    reflect_attempts: list[int] = []

    async def flaky_reflect(_nodes, _reason):
        reflect_attempts.append(1)
        if len(reflect_attempts) == 1:
            raise LLMTransientError("reflect transient fail")
        return []  # second attempt returns empty list, letting F commit

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=flaky_reflect,
        embed_fn=_embed,
        max_retries=2,
    )

    import asyncio as _asyncio

    orig_sleep = _asyncio.sleep

    async def fast(_t):
        return None

    _asyncio.sleep = fast  # type: ignore[assignment]
    try:
        await worker.drain_once()
    finally:
        _asyncio.sleep = orig_sleep  # type: ignore[assignment]

    with DbSession(engine) as db:
        events = db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == "s_flaky_reflect",
                ConceptNode.type == NodeType.EVENT,
            )
        ).all()

        # THE BUG: without the fix, extraction runs twice → 2 events.
        assert len(events) == 1, (
            f"expected 1 event after transient reflect-retry, got {len(events)} "
            f"— extraction duplicated the events on retry"
        )
        # Extraction should have run only once; reflection should have been
        # attempted twice (the transient + the successful retry).
        assert len(extract_calls) == 1
        assert len(reflect_attempts) == 2

        # Session should end up fully closed.
        sess = db.get(Session, "s_flaky_reflect")
        assert sess is not None
        assert sess.status == SessionStatus.CLOSED


async def test_worker_initial_session_ids_are_processed_first():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s_init",
        ["one two three four", "reply one", "two", "three", "four"],
    )

    called: list[str] = []

    async def extractor(msgs):
        called.append(msgs[0].session_id)
        return ExtractionResult()

    def db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=db_factory,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        initial_session_ids=("s_init",),
    )
    processed = await worker.drain_once()
    assert processed == 1
    assert called == ["s_init"]
