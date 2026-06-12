"""sweep_dead_vectors — physical search-index cleanup for long-dead nodes.

Soft delete and supersede both keep the concept_nodes row. The sweep
removes the concept_nodes_vec row (both cases) and the concept_nodes_fts
entry (soft-deleted only) once the node has been dead longer than the
retention threshold. Node rows, supersede pointers, and filling chains
survive untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory import (
    ConceptNode,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.forget import sweep_dead_vectors

NOW = datetime(2026, 6, 12, 12, 0, 0)
OLD = NOW - timedelta(days=40)
RECENT = NOW - timedelta(days=5)


def _make_world():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="P"))
        db.add(User(id="self", display_name="U"))
        db.commit()
    return engine, backend


def _add_node(
    db: DbSession,
    backend: SQLiteBackend,
    description: str,
    *,
    created_at: datetime = NOW - timedelta(days=60),
    deleted_at: datetime | None = None,
    with_vector: bool = True,
) -> ConceptNode:
    node = ConceptNode(
        persona_id="p",
        user_id="self",
        type=NodeType.EVENT,
        description=description,
        created_at=created_at,
        deleted_at=deleted_at,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    if with_vector:
        vec = [0.0] * 384
        vec[node.id % 384] = 1.0
        backend.insert_vector(node.id, vec)
    return node


def _vec_ids(engine) -> set[int]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM concept_nodes_vec")).all()
    return {r[0] for r in rows}


def _fts_match_ids(engine, token: str) -> set[int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT rowid FROM concept_nodes_fts WHERE concept_nodes_fts MATCH :q"),
            {"q": f'"{token}"'},
        ).all()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Soft-deleted nodes
# ---------------------------------------------------------------------------


def test_old_soft_deleted_node_loses_vec_and_fts_but_keeps_row():
    engine, backend = _make_world()
    with DbSession(engine) as db:
        node = _add_node(db, backend, "oldsoftdel marker text", deleted_at=OLD)
        nid = node.id

        assert nid in _vec_ids(engine)
        assert nid in _fts_match_ids(engine, "oldsoftdel")

        counts = sweep_dead_vectors(db, backend, now=NOW)

        assert counts.soft_deleted == 1
        assert nid not in _vec_ids(engine)
        assert nid not in _fts_match_ids(engine, "oldsoftdel")

        row = db.exec(select(ConceptNode).where(ConceptNode.id == nid)).one()
        assert row.deleted_at is not None, "node row must survive the sweep"


def test_recent_soft_deleted_node_is_kept():
    engine, backend = _make_world()
    with DbSession(engine) as db:
        node = _add_node(db, backend, "recentsoftdel marker text", deleted_at=RECENT)
        nid = node.id

        counts = sweep_dead_vectors(db, backend, now=NOW)

        assert counts.soft_deleted == 0
        assert nid in _vec_ids(engine)
        assert nid in _fts_match_ids(engine, "recentsoftdel")


# ---------------------------------------------------------------------------
# Superseded nodes
# ---------------------------------------------------------------------------


def test_old_superseded_node_loses_vec_keeps_row_pointer_and_fts():
    engine, backend = _make_world()
    with DbSession(engine) as db:
        old = _add_node(db, backend, "oldsuperseded marker text")
        successor = _add_node(db, backend, "successorlive marker text", created_at=OLD)
        old.superseded_by_id = successor.id
        db.add(old)
        db.commit()
        old_id, succ_id = old.id, successor.id

        counts = sweep_dead_vectors(db, backend, now=NOW)

        assert counts.superseded == 1
        assert old_id not in _vec_ids(engine)
        # Successor is live — untouched.
        assert succ_id in _vec_ids(engine)

        row = db.exec(select(ConceptNode).where(ConceptNode.id == old_id)).one()
        assert row.deleted_at is None
        assert row.superseded_by_id == succ_id, "supersede chain must stay intact"
        # Admin search filters on deleted_at only, so superseded history
        # stays searchable — FTS entry is kept.
        assert old_id in _fts_match_ids(engine, "oldsuperseded")


def test_recently_superseded_node_is_kept():
    engine, backend = _make_world()
    with DbSession(engine) as db:
        old = _add_node(db, backend, "freshsuperseded marker text")
        successor = _add_node(db, backend, "freshhead marker text", created_at=RECENT)
        old.superseded_by_id = successor.id
        db.add(old)
        db.commit()

        counts = sweep_dead_vectors(db, backend, now=NOW)

        assert counts.superseded == 0
        assert old.id in _vec_ids(engine)


# ---------------------------------------------------------------------------
# Live nodes + idempotency
# ---------------------------------------------------------------------------


def test_live_nodes_are_untouched():
    engine, backend = _make_world()
    with DbSession(engine) as db:
        live = _add_node(db, backend, "livenode marker text")
        dead = _add_node(db, backend, "deadnode marker text", deleted_at=OLD)

        sweep_dead_vectors(db, backend, now=NOW)

        assert live.id in _vec_ids(engine)
        assert live.id in _fts_match_ids(engine, "livenode")
        assert dead.id not in _vec_ids(engine)


def test_sweep_is_idempotent():
    engine, backend = _make_world()
    with DbSession(engine) as db:
        _add_node(db, backend, "idemsoftdel marker text", deleted_at=OLD)
        old = _add_node(db, backend, "idemsuperseded marker text")
        successor = _add_node(db, backend, "idemhead marker text", created_at=OLD)
        old.superseded_by_id = successor.id
        db.add(old)
        db.commit()

        first = sweep_dead_vectors(db, backend, now=NOW)
        assert first.soft_deleted == 1
        assert first.superseded == 1

        # Second run must find nothing and, critically, must NOT issue a
        # second FTS 'delete' for the same rowid (that corrupts the index).
        second = sweep_dead_vectors(db, backend, now=NOW)
        assert second.soft_deleted == 0
        assert second.superseded == 0

        assert _fts_match_ids(engine, "idemsoftdel") == set()


def test_node_without_vec_row_is_skipped_entirely():
    """A soft-deleted node whose vector was already scrubbed at delete time
    must not get an FTS 'delete' from the sweep (vec presence is the gate)."""
    engine, backend = _make_world()
    with DbSession(engine) as db:
        node = _add_node(db, backend, "novecnode marker text", deleted_at=OLD, with_vector=False)

        counts = sweep_dead_vectors(db, backend, now=NOW)

        assert counts.soft_deleted == 0
        # FTS entry stays — harmless, search filters deleted_at anyway.
        assert node.id in _fts_match_ids(engine, "novecnode")


def test_swept_fts_entries_stay_gone_after_schema_recreate():
    """create_all_tables runs an FTS backfill at startup; it must not
    resurrect entries the sweep removed."""
    engine, backend = _make_world()
    with DbSession(engine) as db:
        _add_node(db, backend, "restartcheck marker text", deleted_at=OLD)
        sweep_dead_vectors(db, backend, now=NOW)

    create_all_tables(engine)

    assert _fts_match_ids(engine, "restartcheck") == set()
