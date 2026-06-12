"""FTS sync UPDATE triggers are scoped to the indexed content column.

``recall_fts_update`` / ``concept_fts_update`` carry an ``UPDATE OF``
column list so bookkeeping writes (access_count bumps, soft-delete
timestamps) don't delete + reinsert trigram rows. Databases carrying
the unscoped trigger shape are rebuilt by ``ensure_schema_up_to_date``.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.core.types import MessageRole, NodeType, SessionStatus
from echovessel.memory import (
    ConceptNode,
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.migrations import ensure_schema_up_to_date

_OLD_CONCEPT_TRIGGER = """
CREATE TRIGGER concept_fts_update
AFTER UPDATE ON concept_nodes
BEGIN
    INSERT INTO concept_nodes_fts(concept_nodes_fts, rowid, description)
    VALUES('delete', old.id, old.description);
    INSERT INTO concept_nodes_fts(rowid, description)
    VALUES (new.id, new.description);
END
"""

_OLD_RECALL_TRIGGER = """
CREATE TRIGGER recall_fts_update
AFTER UPDATE ON recall_messages
BEGIN
    INSERT INTO recall_messages_fts(recall_messages_fts, rowid, content)
    VALUES('delete', old.id, old.content);
    INSERT INTO recall_messages_fts(rowid, content)
    VALUES (new.id, new.content);
END
"""


def _trigger_sql(engine, name: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:n"),
            {"n": name},
        ).first()
    assert row is not None, f"missing trigger {name}"
    return row[0]


def _install_old_triggers(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER concept_fts_update"))
        conn.execute(text("DROP TRIGGER recall_fts_update"))
        conn.execute(text(_OLD_CONCEPT_TRIGGER))
        conn.execute(text(_OLD_RECALL_TRIGGER))


def test_fresh_db_update_triggers_are_column_scoped():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    assert "UPDATE OF description" in _trigger_sql(engine, "concept_fts_update")
    assert "UPDATE OF content" in _trigger_sql(engine, "recall_fts_update")


def test_migration_rebuilds_unscoped_update_triggers():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _install_old_triggers(engine)

    assert "UPDATE OF" not in _trigger_sql(engine, "concept_fts_update")
    assert "UPDATE OF" not in _trigger_sql(engine, "recall_fts_update")

    ensure_schema_up_to_date(engine)

    assert "UPDATE OF description" in _trigger_sql(engine, "concept_fts_update")
    assert "UPDATE OF content" in _trigger_sql(engine, "recall_fts_update")

    # Second run is a no-op.
    ensure_schema_up_to_date(engine)
    assert "UPDATE OF description" in _trigger_sql(engine, "concept_fts_update")


def test_description_update_still_syncs_fts():
    """Narrowing the trigger must not break the actual sync: editing
    description re-indexes; bumping access_count leaves the index able
    to find the unchanged description."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        db.add(Persona(id="p_test", display_name="Test Persona"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()
        node = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="climbed the old water tower",
        )
        db.add(node)
        db.commit()
        node_id = node.id

    def fts_match(term: str) -> list[int]:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT rowid FROM concept_nodes_fts WHERE concept_nodes_fts MATCH :q"),
                {"q": f'"{term}"'},
            ).fetchall()
        return [r[0] for r in rows]

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE concept_nodes SET access_count = access_count + 1 WHERE id = :id"),
            {"id": node_id},
        )
    assert fts_match("water tower") == [node_id]

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE concept_nodes SET description = :d WHERE id = :id"),
            {"d": "adopted a grey kitten", "id": node_id},
        )
    assert fts_match("water tower") == []
    assert fts_match("grey kitten") == [node_id]


def test_soft_delete_does_not_rewrite_recall_fts():
    """deleted_at writes are bookkeeping: the trigram entry stays as-is
    (row-level filtering happens in fts_search's WHERE clause)."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        db.add(Persona(id="p_test", display_name="Test Persona"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()
        db.add(
            Session(
                id="s_001",
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                status=SessionStatus.OPEN,
            )
        )
        db.commit()
        msg = RecallMessage(
            session_id="s_001",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            role=MessageRole.USER,
            content="Mochi knocked over the plant",
            day=date.today(),
        )
        db.add(msg)
        db.commit()
        msg_id = msg.id

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE recall_messages SET deleted_at = CURRENT_TIMESTAMP WHERE id = :id"),
            {"id": msg_id},
        )

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rowid FROM recall_messages_fts WHERE recall_messages_fts MATCH '\"Mochi\"'"
            )
        ).fetchall()
    assert [r[0] for r in rows] == [msg_id]
