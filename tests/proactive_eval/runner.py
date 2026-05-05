"""End-to-end fixture runner for proactive_eval."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import consolidate_session
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import ConceptNode
from echovessel.memory.models import ProactiveDecision as ProactiveDecisionRow
from echovessel.memory.models import Session as MemSession
from echovessel.memory.sessions import mark_session_closing
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.core.models import PersonaProfile, ProactiveState
from echovessel.proactive.execution.audit import SQLiteAuditSink
from echovessel.proactive.execution.follow_up_scheduler import FollowUpScheduler
from echovessel.proactive.execution.observer import MemoryFollowUpObserver
from echovessel.proactive.factory import build_proactive_scheduler
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.wiring.memory import MemoryFacade
from echovessel.runtime.wiring.prompts import (
    make_extract_fn,
    make_proactive_fn,
    make_reflect_fn,
)
from tests.memory_eval.embedders import build_eval_embedder
from tests.memory_eval.schema import FixtureTurn
from tests.proactive.fakes import FakeChannel, FakeChannelRegistry, FakePersonaView
from tests.proactive_eval.schema import (
    PhaseEvalResult,
    ProactiveEvalResult,
    ProactiveFixture,
)

log = logging.getLogger(__name__)

PERSONA_ID = "p_eval"
USER_ID = "u_eval"
CHANNEL_ID = "web"


__all__ = ["run_fixture"]


async def run_fixture(fixture: ProactiveFixture, *, llm: LLMProvider) -> ProactiveEvalResult:
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    mock_now = [_parse_clock(fixture.clock.fixed_now)]

    def now_fn() -> datetime:
        return mock_now[0]

    def db_factory() -> DbSession:
        return DbSession(engine)

    _seed_personas(engine)
    _seed_persona_profile(engine, fixture, mock_now[0])
    _seed_proactive_state(engine, fixture, mock_now[0])
    _seed_proactive_decisions(engine, fixture, mock_now[0])

    embed_fn = build_eval_embedder()
    extract_fn = make_extract_fn(llm)
    reflect_fn = make_reflect_fn(llm)
    proactive_fn = make_proactive_fn(llm)

    memory_api = MemoryFacade(db_factory)
    audit_sink = SQLiteAuditSink(db_factory=db_factory)
    fake_channel = FakeChannel(name=CHANNEL_ID, channel_id=CHANNEL_ID)
    channel_registry = FakeChannelRegistry(channels=[fake_channel])

    is_turn_in_flight = (lambda: True) if fixture.in_flight_turn else None

    proactive_scheduler = build_proactive_scheduler(
        config=ProactiveConfig(
            enabled=True,
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            max_per_24h=fixture.max_per_24h,
        ),
        memory_api=memory_api,
        channel_registry=channel_registry,
        proactive_fn=proactive_fn,
        audit_sink=audit_sink,
        persona=FakePersonaView(),
        is_turn_in_flight=is_turn_in_flight,
        clock=now_fn,
        db_factory=db_factory,
    )
    follow_up_scheduler = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=proactive_scheduler,
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        now_fn=now_fn,
    )
    observer = MemoryFollowUpObserver(scheduler=follow_up_scheduler)

    await proactive_scheduler.start()
    try:
        session_id: str | None = None
        consolidate_skipped = False
        consolidate_skip_reason: str | None = None
        if fixture.turns:
            session_id = _ingest_turns(engine, fixture.turns, mock_now[0])
            cons = await _close_and_consolidate(
                engine,
                backend,
                session_id,
                extract_fn=extract_fn,
                reflect_fn=reflect_fn,
                embed_fn=embed_fn,
                observer=observer,
                now=mock_now[0],
            )
            consolidate_skipped = cons.skipped
            consolidate_skip_reason = _derive_skip_reason(cons)

        events = _snapshot_events(engine)

        audit_per_phase: dict[str, PhaseEvalResult] = {}
        sorted_phases = sorted(
            fixture.expect.phases.items(),
            key=lambda kv: _parse_clock(kv[1].fire_at_clock),
        )
        for phase_name, ep in sorted_phases:
            mock_now[0] = _parse_clock(ep.fire_at_clock)
            before_count = _count_decisions(engine)
            await follow_up_scheduler.start()
            # Stage 1: wait for the FIRST audit row to appear (= dispatch was
            # triggered). Then immediately stop follow_up_scheduler so the
            # event's _schedule_next doesn't re-arm check_N+1 / next-phase
            # timers in a rapid-fire loop while mock_clock is pinned. The
            # in-flight DefaultScheduler dispatch task lives on the proactive
            # scheduler (not follow_up), so stopping follow_up_scheduler does
            # NOT cancel the LLM call we're waiting on.
            arm_deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < arm_deadline:
                await asyncio.sleep(0.2)
                if _count_decisions(engine) > before_count:
                    break
            await follow_up_scheduler.stop()
            # Stage 2: wait for the first row's outcome to settle. Real LLM
            # generation (proactive_fn) can take 10-30s with V4 reasoning.
            outcome_deadline = asyncio.get_event_loop().time() + 60.0
            while asyncio.get_event_loop().time() < outcome_deadline:
                rows_so_far = _collect_decisions_since(engine, before_count)
                if rows_so_far:
                    latest = rows_so_far[-1]
                    if (
                        latest.get("send_ok") is True
                        or latest.get("suppress_reason")
                        or latest.get("action") == "skip"
                    ):
                        break
                await asyncio.sleep(0.5)
            rows = _collect_decisions_since(engine, before_count)
            msgs = [r.get("message_text") or "" for r in rows if r.get("action") == "send"]
            audit_per_phase[phase_name] = PhaseEvalResult(
                fire_at_clock=ep.fire_at_clock,
                audit_rows=rows,
                generated_messages=msgs,
            )

        supersede_event: dict[str, Any] | None = None
        if fixture.follow_up_stage:
            mock_now[0] = _parse_clock(fixture.follow_up_stage.after_clock)
            new_session_id = _ingest_turns(engine, fixture.follow_up_stage.turns, mock_now[0])
            await _close_and_consolidate(
                engine,
                backend,
                new_session_id,
                extract_fn=extract_fn,
                reflect_fn=reflect_fn,
                embed_fn=embed_fn,
                observer=observer,
                now=mock_now[0],
            )
            supersede_event = _find_latest_supersede_event(engine)

        return ProactiveEvalResult(
            events=events,
            audit_per_phase=audit_per_phase,
            supersede_event=supersede_event,
            consolidate_skipped=consolidate_skipped,
            consolidate_skip_reason=consolidate_skip_reason,
        )
    finally:
        await proactive_scheduler.stop()


# --- helpers ----------------------------------------------------------------


def _parse_clock(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _derive_skip_reason(cons: Any) -> str | None:
    if not cons.skipped:
        return None
    if getattr(cons.session, "trivial", False):
        return "trivial"
    return "already_closed"


def _seed_personas(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id=PERSONA_ID, display_name="Eval"))
        db.add(User(id=USER_ID, display_name="User"))
        db.commit()


def _seed_persona_profile(engine, fixture: ProactiveFixture, now: datetime) -> None:
    if not fixture.seed.persona_profile:
        return
    pp = fixture.seed.persona_profile
    with DbSession(engine) as db:
        db.add(
            PersonaProfile(
                persona_id=PERSONA_ID,
                style_summary=pp.style_summary,
                quiet_hours=list(pp.quiet_hours),
                forbidden_topics=list(pp.forbidden_topics),
                voice_id=pp.voice_id,
                profile_generated_at=now,
                profile_source="fallback_default",
            )
        )
        db.commit()


def _seed_proactive_state(engine, fixture: ProactiveFixture, now: datetime) -> None:
    if not fixture.seed.proactive_state:
        return
    with DbSession(engine) as db:
        db.add(
            ProactiveState(
                persona_id=PERSONA_ID,
                user_id=USER_ID,
                engagement_score=fixture.seed.proactive_state.engagement_score,
                last_updated=now,
            )
        )
        db.commit()


def _seed_proactive_decisions(engine, fixture: ProactiveFixture, now: datetime) -> None:
    if not fixture.seed.proactive_decisions:
        return
    with DbSession(engine) as db:
        for spec in fixture.seed.proactive_decisions:
            ts = now + timedelta(hours=spec.timestamp_offset_hours)
            db.add(
                ProactiveDecisionRow(
                    decision_id=str(uuid.uuid4()),
                    timestamp=ts,
                    persona_id=PERSONA_ID,
                    user_id=USER_ID,
                    trigger_type=spec.trigger_type,
                    action=spec.action,
                    suppress_reason=spec.suppress_reason,
                    phase=spec.phase,
                    created_at=ts,
                )
            )
        db.commit()


def _ingest_turns(engine, turns: list[FixtureTurn], now: datetime) -> str:
    session_id: str | None = None
    with DbSession(engine) as db:
        for t in turns:
            role = MessageRole.USER if t.role == "user" else MessageRole.PERSONA
            r = ingest_message(db, PERSONA_ID, USER_ID, t.channel, role, t.content, now=now)
            session_id = r.session.id
        db.commit()
    assert session_id is not None
    return session_id


async def _close_and_consolidate(
    engine,
    backend,
    session_id,
    *,
    extract_fn,
    reflect_fn,
    embed_fn,
    observer,
    now,
):
    with DbSession(engine) as db:
        sess = db.get(MemSession, session_id)
        assert sess is not None
        mark_session_closing(db, sess, trigger="eval", now=now)
        db.commit()
    with DbSession(engine) as db:
        sess = db.get(MemSession, session_id)
        # Bypass Phase A trivial gate: eval fixtures are short by design and
        # we want every session to flow into Phase B regardless of message
        # / token count. trivial_no_followup proves null follow_up_at on its
        # own merits via the LLM, not via the keyword gate.
        return await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extract_fn,
            reflect_fn=reflect_fn,
            embed_fn=embed_fn,
            now=now,
            observer=observer,
            trivial_message_count=0,
            trivial_token_count=0,
        )


def _snapshot_events(engine) -> list[dict[str, Any]]:
    with DbSession(engine) as db:
        rows = db.exec(
            select(ConceptNode).where(
                ConceptNode.persona_id == PERSONA_ID,
                ConceptNode.user_id == USER_ID,
                ConceptNode.deleted_at.is_(None),
            )
        ).all()
        return [_serialise_event(n) for n in rows]


def _serialise_event(n: ConceptNode) -> dict[str, Any]:
    t = getattr(n.type, "value", n.type)
    return {
        "id": n.id,
        "type": t,
        "description": n.description,
        "follow_up_at": n.follow_up_at.isoformat() if n.follow_up_at else None,
        "follow_up_hint": n.follow_up_hint,
        "advance_pre_hours": n.advance_pre_hours,
        "advance_post_hours": n.advance_post_hours,
        "estimated_arc_days": n.estimated_arc_days,
        "event_time_start": n.event_time_start.isoformat() if n.event_time_start else None,
        "event_time_end": n.event_time_end.isoformat() if n.event_time_end else None,
        "relational_tags": list(n.relational_tags or []),
        "emotional_impact": n.emotional_impact,
        "subject": n.subject,
        "superseded_by_id": n.superseded_by_id,
    }


def _count_decisions(engine) -> int:
    with DbSession(engine) as db:
        return len(db.exec(select(ProactiveDecisionRow)).all())


def _collect_decisions_since(engine, before_count: int) -> list[dict[str, Any]]:
    with DbSession(engine) as db:
        rows = db.exec(select(ProactiveDecisionRow).order_by(ProactiveDecisionRow.id.asc())).all()
        return [_serialise_decision(r) for r in rows[before_count:]]


def _serialise_decision(r: ProactiveDecisionRow) -> dict[str, Any]:
    # SQLiteAuditSink stores action as "fire"/"suppress" in the DB row but the
    # value-type ProactiveDecision uses "send"/"skip". Normalize back so the
    # invariant checker can compare against ActionType vocabulary.
    raw_action = r.action
    normalized = "send" if raw_action == "fire" else "skip"
    return {
        "decision_id": r.decision_id,
        "trigger_type": r.trigger_type,
        "action": normalized,
        "suppress_reason": r.suppress_reason,
        "phase": r.phase,
        "send_ok": (
            getattr(r, "sent_message_id", None) is not None if raw_action == "fire" else None
        ),
        "message_text": r.message_text,
        "voice_used": r.voice_used,
    }


def _find_latest_supersede_event(engine) -> dict[str, Any] | None:
    with DbSession(engine) as db:
        rows = db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == PERSONA_ID,
                ConceptNode.user_id == USER_ID,
                ConceptNode.deleted_at.is_(None),
            )
            .order_by(ConceptNode.id.desc())
        ).all()
        for n in rows:
            if any(m.superseded_by_id == n.id for m in rows if m.id != n.id):
                return _serialise_event(n)
    return None
