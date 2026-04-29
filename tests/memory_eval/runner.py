"""End-to-end fixture runner.

``run_fixture`` materialises a :class:`Fixture` in a fresh in-memory
SQLite DB, drives the real ``ingest → close → consolidate → retrieve``
pipeline, and returns the bag of dicts that ``check_invariants`` and
the judge prompts read from. Pipeline plumbing lives here; invariant
logic lives next door in :mod:`tests.memory_eval.invariants`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.base import IncomingMessage
from echovessel.core.types import BlockLabel, MessageRole, NodeType
from echovessel.memory import (
    CoreBlock,
    Persona,
    User,
    append_to_core_block,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import consolidate_session
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    Entity,
    RecallMessage,
    Session,
)
from echovessel.memory.retrieve import retrieve
from echovessel.memory.sessions import mark_session_closing
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.turn.coordinator import TurnContext, assemble_turn
from echovessel.runtime.wiring.prompts import make_extract_fn, make_reflect_fn
from tests.memory_eval.embedders import REAL_CONFIG_PATH, build_eval_embedder
from tests.memory_eval.schema import EvalResult, Fixture

__all__ = ["REAL_CONFIG_PATH", "render_evidence", "run_fixture"]

# Sentinel persona content that asks the runner to call the production
# assemble_turn pipeline so the LLM produces a real reply against the
# seeded prompt. Without this hook, the L1 fixtures would ingest a
# literal "..." into L2 and consolidation would extract nothing useful.
PERSONA_GENERATE_SENTINEL = "..."


async def run_fixture(fixture: Fixture, *, llm: LLMProvider) -> EvalResult:
    """Materialise the fixture in a fresh SQLite DB, run the pipeline,
    and return a plain-dict summary of everything produced.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    persona_id = "p_eval"
    user_id = "u_eval"

    embed_fn = build_eval_embedder()
    extract_fn = make_extract_fn(llm)
    reflect_fn = make_reflect_fn(llm)

    # 1. seed persona + user + core blocks
    with DbSession(engine) as db:
        persona_kwargs: dict[str, Any] = {
            "id": persona_id,
            "display_name": "Eval",
            "timezone": fixture.seed.persona_timezone,
            "location": fixture.seed.persona_location,
        }
        if fixture.seed.episodic_state_initial is not None:
            persona_kwargs["episodic_state"] = dict(
                fixture.seed.episodic_state_initial
            )
        db.add(Persona(**persona_kwargs))
        db.add(User(id=user_id, display_name="User"))
        db.commit()

        for label, content in [
            (BlockLabel.PERSONA, fixture.seed.persona_block),
            (BlockLabel.USER, fixture.seed.user_block),
            (BlockLabel.STYLE, fixture.seed.style_block),
        ]:
            if not content:
                continue
            row_user_id = None if label in (
                BlockLabel.PERSONA, BlockLabel.STYLE,
            ) else user_id
            append_to_core_block(
                db,
                persona_id=persona_id,
                user_id=row_user_id,
                label=label.value,
                content=content,
                provenance={"source": "eval_seed"},
            )

        now = datetime.now()

        # 2. pre-seed events (E5 / E6)
        for se in fixture.seed.seed_events:
            created = now + timedelta(hours=se.created_at_offset_hours)
            node = ConceptNode(
                persona_id=persona_id,
                user_id=user_id,
                type=NodeType.EVENT,
                description=se.description,
                emotional_impact=se.emotional_impact,
                emotion_tags=se.emotion_tags,
                relational_tags=se.relational_tags,
                created_at=created,
            )
            db.add(node)
            db.commit()
            db.refresh(node)
            backend.insert_vector(node.id, embed_fn(se.description))

        # 2b. pre-seed thoughts (L4 hard-limit + forget fixtures)
        for st in fixture.seed.seed_thoughts:
            created = now + timedelta(hours=st.created_at_offset_hours)
            node = ConceptNode(
                persona_id=persona_id,
                user_id=user_id,
                type=NodeType.THOUGHT,
                description=st.description,
                emotional_impact=st.emotional_impact,
                emotion_tags=st.emotion_tags,
                relational_tags=st.relational_tags,
                created_at=created,
            )
            db.add(node)
            db.commit()
            db.refresh(node)
            backend.insert_vector(node.id, embed_fn(st.description))

            # filling: link this thought (parent in the schema) to evidence
            # events (child) that were seeded above.
            for spec in st.filling:
                evidence = next(
                    (
                        n
                        for n in db.exec(
                            select(ConceptNode).where(
                                ConceptNode.persona_id == persona_id,
                                ConceptNode.type == NodeType.EVENT.value,
                                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                            )
                        )
                        if spec.parent_description_contains in n.description
                    ),
                    None,
                )
                if evidence is not None:
                    db.add(
                        ConceptNodeFilling(
                            parent_id=node.id,
                            child_id=evidence.id,
                            orphaned=False,
                        )
                    )
            db.commit()

    core_block_snapshot_before = _snapshot_core_blocks(engine, persona_id)
    episodic_state_before = _read_episodic_state(engine, persona_id)

    # 3. ingest turns
    #
    # Two paths:
    #   - turn.content == "..." on a persona turn → delegate to the
    #     production ``assemble_turn`` so the LLM actually generates a
    #     reply against the seeded prompt. assemble_turn ingests BOTH the
    #     user message and the persona reply with one call, so we skip
    #     the explicit ingest_message for the immediately-preceding user
    #     turn in that case.
    #   - all other turns → straight ingest_message under the per-turn
    #     channel (defaults to "web").
    session_id: str | None = None
    with DbSession(engine) as db:
        i = 0
        while i < len(fixture.turns):
            turn = fixture.turns[i]
            next_turn = fixture.turns[i + 1] if i + 1 < len(fixture.turns) else None
            generate_next = (
                turn.role == "user"
                and next_turn is not None
                and next_turn.role == "persona"
                and next_turn.content == PERSONA_GENERATE_SENTINEL
            )
            if generate_next:
                ctx = TurnContext(
                    persona_id=persona_id,
                    persona_display_name="Eval",
                    db=db,
                    backend=backend,
                    embed_fn=embed_fn,
                )
                envelope = IncomingMessage(
                    channel_id=turn.channel,
                    user_id=user_id,
                    content=turn.content,
                    received_at=datetime.now(),
                )
                await assemble_turn(ctx, envelope, llm)
                # After assemble_turn we need the open session so the
                # later consolidate step can close it.
                latest = db.exec(
                    select(Session)
                    .where(
                        Session.persona_id == persona_id,
                        Session.user_id == user_id,
                        Session.channel_id == turn.channel,
                    )
                    .order_by(Session.last_message_at.desc())  # type: ignore[union-attr]
                ).first()
                if latest is not None:
                    session_id = latest.id
                i += 2
                continue

            role = MessageRole.USER if turn.role == "user" else MessageRole.PERSONA
            result = ingest_message(
                db,
                persona_id,
                user_id,
                turn.channel,
                role,
                turn.content,
            )
            session_id = result.session.id
            i += 1
        db.commit()

    reflection_triggered = False

    # 4. consolidate: if we have turns, close the session + consolidate
    if session_id is not None and fixture.turns:
        with DbSession(engine) as db:
            sess = db.get(Session, session_id)
            assert sess is not None
            mark_session_closing(db, sess, trigger="eval")
            db.add(sess)
            db.commit()

        with DbSession(engine) as db:
            sess = db.get(Session, session_id)
            assert sess is not None
            cons = await consolidate_session(
                db=db,
                backend=backend,
                session=sess,
                extract_fn=extract_fn,
                reflect_fn=reflect_fn,
                embed_fn=embed_fn,
            )
        reflection_triggered = cons.reflection_reason is not None

    core_block_snapshot_after = _snapshot_core_blocks(engine, persona_id)
    episodic_state_after = _read_episodic_state(engine, persona_id)

    # 4b. forget actions — applied after consolidate so seeded events with
    # filling links to seed thoughts can be deleted via ORPHAN/CASCADE.
    if fixture.post_consolidate_actions:
        from echovessel.memory.forget import DeletionChoice, delete_concept_node

        with DbSession(engine) as db:
            for act in fixture.post_consolidate_actions:
                target = next(
                    (
                        n
                        for n in db.exec(
                            select(ConceptNode).where(
                                ConceptNode.persona_id == persona_id,
                                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                            )
                        )
                        if act.target_description_contains in n.description
                    ),
                    None,
                )
                if target is not None:
                    delete_concept_node(
                        db=db,
                        node_id=target.id,
                        choice=DeletionChoice[act.choice],
                        backend=backend,
                    )

    # 5. retrieve (E6). Vector hits come first; FTS fallback rows are
    # appended after so callers that index by position can rely on
    # "vector before FTS." ``relevance: 0.0`` on FTS rows is a sentinel
    # — there's no vector score to report — and ``source`` lets callers
    # disambiguate without a separate field.
    retrieved: list[dict[str, Any]] = []
    if fixture.retrieve is not None:
        with DbSession(engine) as db:
            r = retrieve(
                db=db,
                backend=backend,
                persona_id=persona_id,
                user_id=user_id,
                query_text=fixture.retrieve.query,
                embed_fn=embed_fn,
                top_k=fixture.retrieve.top_k,
                force_load_user_thoughts=fixture.retrieve.force_load_user_thoughts,
            )
        retrieved = [
            {
                "id": m.node.id,
                "description": m.node.description,
                "relevance": m.relevance,
                "source": "vector",
            }
            for m in r.memories
        ]
        retrieved.extend(
            {
                "id": rm.id,
                "description": rm.content,
                "relevance": 0.0,
                "source": "fts",
            }
            for rm in r.fts_fallback
        )
        retrieved.extend(
            {
                "id": n.id,
                "description": n.description,
                "relevance": 0.0,
                "source": "pinned",
            }
            for n in r.pinned_thoughts
        )

    # 6. collect everything we just wrote
    with DbSession(engine) as db:
        events = [
            _serialise_node(n)
            for n in db.exec(
                select(ConceptNode).where(
                    ConceptNode.persona_id == persona_id,
                    ConceptNode.type == NodeType.EVENT.value,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        ]
        # Only the events created by THIS consolidate pass — seeded events
        # have no source_session_id (we inserted them directly).
        events_from_session = [
            e for e in events if e["source_session_id"] == session_id
        ]
        thoughts = [
            _serialise_node(n)
            for n in db.exec(
                select(ConceptNode).where(
                    ConceptNode.persona_id == persona_id,
                    ConceptNode.type == NodeType.THOUGHT.value,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        ]
        filling = [
            {"parent_id": r.parent_id, "child_id": r.child_id, "orphaned": r.orphaned}
            for r in db.exec(select(ConceptNodeFilling))
        ]
        entities = [
            {
                "id": e.id,
                "canonical_name": e.canonical_name,
                "kind": getattr(e.kind, "value", e.kind),
                "merge_status": getattr(e.merge_status, "value", e.merge_status),
            }
            for e in db.exec(
                select(Entity).where(
                    Entity.persona_id == persona_id,
                    Entity.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        ]
        recall_count = len(
            list(
                db.exec(
                    select(RecallMessage).where(
                        RecallMessage.persona_id == persona_id,
                        RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
                    )
                )
            )
        )

    return EvalResult(
        events=events_from_session if fixture.turns else events,
        thoughts=thoughts,
        filling=filling,
        retrieved=retrieved,
        reflection_triggered=reflection_triggered,
        entities=entities,
        recall_count=recall_count,
        core_block_snapshot_before=core_block_snapshot_before,
        core_block_snapshot_after=core_block_snapshot_after,
        episodic_state_before=episodic_state_before,
        episodic_state_after=episodic_state_after,
    )


def _read_episodic_state(engine, persona_id: str) -> dict[str, Any]:
    with DbSession(engine) as db:
        p = db.get(Persona, persona_id)
    return dict(p.episodic_state or {}) if p else {}


def _snapshot_core_blocks(engine, persona_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with DbSession(engine) as db:
        for cb in db.exec(
            select(CoreBlock).where(
                CoreBlock.persona_id == persona_id,
                CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ):
            label = getattr(cb.label, "value", cb.label)
            key = f"{label}|{cb.user_id or '_'}"
            out[key] = hashlib.sha1(cb.content.encode()).hexdigest()[:8]
    return out


def _serialise_node(n: ConceptNode) -> dict[str, Any]:
    t = getattr(n.type, "value", n.type)
    return {
        "id": n.id,
        "type": t,
        "description": n.description,
        "emotional_impact": int(n.emotional_impact),
        "emotion_tags": list(n.emotion_tags or []),
        "relational_tags": list(n.relational_tags or []),
        "source_session_id": n.source_session_id,
        "event_time_start": (
            n.event_time_start.isoformat() if n.event_time_start else None
        ),
        "event_time_end": (
            n.event_time_end.isoformat() if n.event_time_end else None
        ),
        "subject": n.subject,
    }


def render_evidence(fixture: Fixture, result: EvalResult) -> str:
    """Render the result as a compact string the judge LLM can read."""
    lines = [
        f"Fixture: {fixture.fixture_id} ({fixture.version})",
        f"Scenario: {fixture.scenario}",
        "",
        "--- Persona block ---",
        fixture.seed.persona_block,
        "",
        "--- Turns ---",
    ]
    for t in fixture.turns:
        lines.append(f"{t.role}: {t.content}")
    lines.append("")
    lines.append(f"--- Extracted events ({len(result.events)}) ---")
    for e in result.events:
        lines.append(
            f"  impact={e['emotional_impact']:+d} rel_tags={e['relational_tags']} · "
            f"{e['description']}"
        )
    lines.append("")
    lines.append(f"--- Thoughts ({len(result.thoughts)}) ---")
    for t in result.thoughts:
        lines.append(
            f"  impact={t['emotional_impact']:+d} rel_tags={t['relational_tags']} · "
            f"{t['description']}"
        )
    if result.retrieved:
        lines.append("")
        lines.append(f"--- Retrieved (top {len(result.retrieved)}) ---")
        for m in result.retrieved:
            lines.append(
                f"  rel={m['relevance']:.2f} · {m['description']}"
            )
    return "\n".join(lines)
