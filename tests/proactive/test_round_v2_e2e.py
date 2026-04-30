"""End-to-end tests · proactive v2.

Covers two scenarios that thread the full v2 pipeline together — no
mocked DB, only scripted LLM closures. The migration-idempotency
e2e from spec 09 case 3 is already covered by
``tests/proactive/test_migrate_proactive_jsonl.py`` (Stage 4 commit
``875ebe8``); not duplicated here.

* **Happy path** — user mentions "下周一面试", extract_threads writes
  a ``point_event`` triplet, scan_due_threads dispatches the
  ``point_event_pre`` thread at its due time, the policy engine
  returns ``send``, the user replies in-window so engagement +=0.10,
  and finally the user volunteers "面试结束了" — close-detection
  matches one thread and sibling propagation sweeps the other two.

* **Quiet hours** — a ``point_event_on`` thread whose ``due_at``
  lands inside ``persona_profile.quiet_hours`` gets ``suppress``ed
  every time the policy runs inside the window; the same thread
  ``fire``s the moment the clock crosses out of the window.

Time travel uses an injected ``now_fn`` (lambda capturing a mutable
list) so the same Session helpers can simulate days passing without
touching the system clock or pulling in ``freezegun``.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "FollowUpThread removed in v3 · v2 e2e replaced by v3 e2e in Stage 5.3",
    allow_module_level=True,
)

from datetime import datetime, timedelta  # noqa: E402
from typing import Any  # noqa: E402

from echovessel.proactive.engines.close_detection import (  # noqa: E402
    ResolutionMatchSchema,
    detect_resolutions,
)
from echovessel.proactive.engines.extract_threads import (  # noqa: E402
    FutureBranchSchema,
    SubThreadSchema,
    extract_threads,
)
from echovessel.proactive.execution.thread_scanner import scan_due_threads  # noqa: E402
from sqlmodel import Session as DbSession  # noqa: E402
from sqlmodel import select  # noqa: E402

from echovessel.core.types import MessageRole, NodeType  # noqa: E402
from echovessel.memory import (  # noqa: E402
    Persona,
    RecallMessage,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.models import (  # noqa: E402
    ConceptNode,
    FollowUpThread,
    PersonaProfile,
    ProactiveDecision,
    ProactiveState,
)
from echovessel.proactive.core.base import (  # noqa: E402
    ActionType,
    EventType,
    ProactiveEvent,
    SkipReason,
)
from echovessel.proactive.core.config import ProactiveConfig  # noqa: E402
from echovessel.proactive.engines.policy import PolicyEngine  # noqa: E402
from echovessel.proactive.execution.audit import SQLiteAuditSink  # noqa: E402
from echovessel.proactive.execution.engagement_updater import (  # noqa: E402
    DELTA_REPLIED,
    handle_user_message_ingested,
)

PERSONA_ID = "p"
USER_ID = "self"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_engine():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    return engine


def _seed_persona(
    engine,
    *,
    quiet_hours: list[int] | None = None,
    forbidden_topics: list[str] | None = None,
    profile_at: datetime | None = None,
) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id=PERSONA_ID, display_name="Luna"))
        db.add(User(id=USER_ID, display_name="Alan"))
        db.add(
            PersonaProfile(
                persona_id=PERSONA_ID,
                style_summary="她说话很短(10-30字)，关心式开场。",
                quiet_hours=quiet_hours or [23, 7],
                forbidden_topics=forbidden_topics or [],
                voice_id=None,
                profile_generated_at=profile_at or datetime(2026, 4, 28, 12, 0),
                profile_source="llm_onboarding",
            )
        )
        db.commit()


def _seed_event(
    engine,
    *,
    description: str,
    impact: int = 4,
    when: datetime,
) -> int:
    with DbSession(engine) as db:
        node = ConceptNode(
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            type=NodeType.EVENT,
            description=description,
            emotional_impact=impact,
            created_at=when,
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        assert node.id is not None
        return node.id


class _CaptureScheduler:
    """Records every notify() call for assertion. Matches the duck type
    scan_due_threads expects."""

    def __init__(self) -> None:
        self.events: list[ProactiveEvent] = []

    def notify(self, event: ProactiveEvent) -> None:
        self.events.append(event)


def _make_policy(engine) -> PolicyEngine:
    """Real PolicyEngine wired against the test SQLite engine. The
    ``memory`` Protocol is only consulted for diagnostic snapshots
    that don't matter for the e2e assertions, so we pass a duck-typed
    stub that returns empty lists."""

    def _db():
        return DbSession(engine)

    return PolicyEngine(
        config=ProactiveConfig(persona_id=PERSONA_ID, user_id=USER_ID),
        audit=SQLiteAuditSink(db_factory=_db),
        memory=_NoopMemoryApi(),
        is_turn_in_flight=None,
        db_factory=_db,
    )


class _NoopMemoryApi:
    """Tiny Protocol-shaped stub. PolicyEngine only reads
    ``count_recent_user_messages`` / ``get_state_for`` etc. through
    its own DB-direct queries in v2 — the MemoryApi argument is held
    for backward compatibility with v1 paths and not consulted on the
    happy path."""

    def get_recent_events(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    def get_recent_messages(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


def _seed_recall_message(
    engine,
    *,
    content: str,
    role: MessageRole,
    when: datetime,
    session_id: str = "e2e-session",
) -> int:
    """Append a single message to RecallMessage so engagement_updater
    has a row to read."""
    with DbSession(engine) as db:
        msg = RecallMessage(
            session_id=session_id,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            channel_id="t",
            role=role,
            content=content,
            day=when.date(),
            token_count=len(content),
            created_at=when,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        assert msg.id is not None
        return msg.id


def _empty_recent_lookup(
    _db: DbSession, _persona_id: str, _limit: int
) -> list[str]:
    return []


# ---------------------------------------------------------------------------
# Test 1 · Full happy path · 3 thread kinds end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_proactive_v2_happy_path() -> None:
    """User says "下周一面试" → 3 point_event threads → pre fires at
    due → user replies → engagement + → user later says "面试结束了"
    → close_detection sweeps all three siblings."""

    engine = _make_engine()
    t0 = datetime(2026, 4, 29, 22, 0)  # the moment the user mentions it
    _seed_persona(engine, profile_at=t0 - timedelta(days=1))

    # 1 · L3 event the user generated when they said "下周一面试"
    source_event_id = _seed_event(
        engine, description="我下周一(05-04)有面试", impact=4, when=t0
    )

    # 2 · extract_threads(scripted point_event triplet)
    interview_day = t0 + timedelta(days=5)  # 2026-05-04 22:00

    async def _scripted_extract(**kwargs: Any) -> FutureBranchSchema:
        return FutureBranchSchema(
            has_future_branch=True,
            kind="point_event",
            confidence=0.92,
            estimated_arc_days=7,
            sub_threads=[
                SubThreadSchema(
                    kind="point_event_pre",
                    anchor_text="提前关心面试",
                    due_at=interview_day - timedelta(days=1, hours=3),  # 05-03 19:00
                    confidence=0.92,
                ),
                SubThreadSchema(
                    kind="point_event_on",
                    anchor_text="面试当天加油",
                    due_at=interview_day.replace(hour=9),  # 05-04 09:00
                    confidence=0.92,
                ),
                SubThreadSchema(
                    kind="point_event_post",
                    anchor_text="追问面试结果",
                    due_at=interview_day + timedelta(days=1, hours=3),  # 05-05 21:00
                    confidence=0.92,
                ),
            ],
        )

    with DbSession(engine) as db:
        events = list(db.exec(select(ConceptNode).where(ConceptNode.id == source_event_id)))
        result = await extract_threads(
            db,
            events=events,
            extract_fn=_scripted_extract,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            now_fn=lambda: t0,
            recent_messages_lookup=_empty_recent_lookup,
        )
        db.commit()
    assert result.events_skipped == []
    assert len(result.threads_created) == 3

    with DbSession(engine) as db:
        thread_rows = list(
            db.exec(
                select(FollowUpThread)
                .where(FollowUpThread.source_event_id == source_event_id)
                .order_by(FollowUpThread.due_at)  # type: ignore[attr-defined]
            )
        )
    assert {t.kind for t in thread_rows} == {
        "point_event_pre",
        "point_event_on",
        "point_event_post",
    }
    pre_thread = next(t for t in thread_rows if t.kind == "point_event_pre")
    on_thread = next(t for t in thread_rows if t.kind == "point_event_on")
    post_thread = next(t for t in thread_rows if t.kind == "point_event_post")

    # 3 · Time advances to just past pre's due_at
    pre_fire_time = (pre_thread.due_at or t0) + timedelta(minutes=5)
    scheduler = _CaptureScheduler()

    with DbSession(engine) as db:
        scan = await scan_due_threads(
            db,
            scheduler=scheduler,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            now_fn=lambda: pre_fire_time,
        )
        db.commit()
    assert scan.dispatched == 1
    dispatched = scheduler.events[0]
    assert dispatched.event_type == EventType.THREAD_DUE
    assert dispatched.payload["thread_id"] == pre_thread.id

    # 4 · Policy gates pass · returns send
    policy = _make_policy(engine)
    decision = policy.evaluate(
        [dispatched],
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        now=pre_fire_time,
    )
    assert decision.action == ActionType.SEND.value
    assert decision.skip_reason is None

    # 5 · Mark fired + record audit row (what the real DefaultScheduler
    # does after MessageGenerator + DeliveryRouter succeed).
    with DbSession(engine) as db:
        row = db.get(FollowUpThread, pre_thread.id)
        assert row is not None
        row.fired_at = pre_fire_time
        db.add(row)
        db.commit()

    def _db():
        return DbSession(engine)

    SQLiteAuditSink(db_factory=_db).record(decision)

    # 6 · User replies within the 24h window → engagement +0.10
    reply_at = pre_fire_time + timedelta(hours=2)
    user_msg_id = _seed_recall_message(
        engine, content="谢谢你 准备得差不多了", role=MessageRole.USER, when=reply_at
    )

    with DbSession(engine) as db:
        msg = db.get(RecallMessage, user_msg_id)
        assert msg is not None
        handle_user_message_ingested(
            db,
            msg=msg,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            now_fn=lambda: reply_at,
        )
        db.commit()

    with DbSession(engine) as db:
        state = db.get(ProactiveState, (PERSONA_ID, USER_ID))
        decision_row = db.exec(
            select(ProactiveDecision).where(
                ProactiveDecision.decision_id == decision.decision_id
            )
        ).first()
    assert state is not None
    assert state.engagement_score == pytest.approx(0.7 + DELTA_REPLIED)
    assert decision_row is not None
    assert decision_row.user_replied_at == reply_at

    # 7 · The user spontaneously says "面试结束了" — close-detection
    # sweeps the sibling triplet via shared source_event_id.
    after_interview = interview_day + timedelta(hours=4)
    resolution_event_id = _seed_event(
        engine,
        description="面试结束了 还行",
        impact=2,
        when=after_interview,
    )

    async def _scripted_close(**kwargs: Any) -> ResolutionMatchSchema:
        # LLM only matches one of the three siblings — sibling
        # propagation must close the other two on the engine side.
        return ResolutionMatchSchema(
            matched_thread_ids=[on_thread.id],
            confidence=0.88,
        )

    with DbSession(engine) as db:
        new_event = db.get(ConceptNode, resolution_event_id)
        assert new_event is not None
        closed = await detect_resolutions(
            db,
            new_events=[new_event],
            detection_fn=_scripted_close,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            now_fn=lambda: after_interview,
        )
        db.commit()
    # Pre + on + post all close (sibling propagation)
    assert sorted(closed) == sorted([pre_thread.id, on_thread.id, post_thread.id])

    with DbSession(engine) as db:
        for tid in (pre_thread.id, on_thread.id, post_thread.id):
            t = db.get(FollowUpThread, tid)
            assert t is not None
            assert t.closed_at is not None
            assert t.resolution_event_id == resolution_event_id


# ---------------------------------------------------------------------------
# Test 2 · Quiet hours blocks throughout the window then fires after
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_hours_blocks_through_window_then_fires_after() -> None:
    """A point_event_on thread whose due_at falls inside the quiet
    window: every policy.evaluate inside the window returns
    ``suppress(quiet_hours)``; the moment the clock crosses out of
    the window, the same thread fires."""

    engine = _make_engine()
    # quiet_hours = 22:00 → 08:00 (wraps midnight)
    _seed_persona(engine, quiet_hours=[22, 8])

    source_event_id = _seed_event(
        engine,
        description="今晚要彻夜赶 deadline",
        impact=3,
        when=datetime(2026, 5, 1, 18, 0),
    )

    # Manually craft the thread: due_at lands at 23:00 (inside quiet
    # window) so the first three scans must all suppress.
    with DbSession(engine) as db:
        thread = FollowUpThread(
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            source_event_id=source_event_id,
            anchor_text="赶 deadline 还顺利吗",
            kind="point_event_on",
            confidence=0.9,
            due_at=datetime(2026, 5, 1, 23, 0),
            created_at=datetime(2026, 5, 1, 18, 5),
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)
        thread_id = thread.id
    assert thread_id is not None

    policy = _make_policy(engine)

    def _due_event() -> ProactiveEvent:
        return ProactiveEvent(
            event_type=EventType.THREAD_DUE,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            payload={"thread_id": thread_id, "source_event_id": source_event_id},
            critical=False,
            created_at=datetime(2026, 5, 1, 23, 0),
        )

    # Three samples deep inside the quiet window — all suppress.
    for now_inside in (
        datetime(2026, 5, 1, 23, 5),
        datetime(2026, 5, 2, 1, 30),
        datetime(2026, 5, 2, 6, 45),
    ):
        decision = policy.evaluate(
            [_due_event()],
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            now=now_inside,
        )
        assert decision.action == ActionType.SKIP.value
        assert decision.skip_reason == SkipReason.QUIET_HOURS.value

    # 08:00 is the boundary; quiet_hours_end is exclusive in the v2
    # gate (``hour < end``), so 08:00 sharp is the first allowed tick.
    after_quiet = datetime(2026, 5, 2, 8, 0)
    decision = policy.evaluate(
        [_due_event()],
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        now=after_quiet,
    )
    assert decision.action == ActionType.SEND.value
    assert decision.skip_reason is None
