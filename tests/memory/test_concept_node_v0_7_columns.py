"""v0.7 schema additions for proactive follow-up."""

from datetime import datetime

from sqlmodel import Session

from echovessel.memory.db import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date
from echovessel.memory.models import ConceptNode, NodeType, Persona, User


def test_concept_node_has_v0_7_columns(tmp_path):
    engine = create_engine(tmp_path / "test.db")
    create_all_tables(engine)
    ensure_schema_up_to_date(engine)

    with Session(engine) as db:
        db.add(Persona(id="p1", display_name="Test"))
        db.add(User(id="u1", display_name="Test"))
        db.commit()

        node = ConceptNode(
            persona_id="p1",
            user_id="u1",
            type=NodeType.EVENT,
            description="user has interview Monday",
            follow_up_at=datetime(2026, 5, 4, 10, 0),
            follow_up_hint="面试结果",
            estimated_arc_days=14,
            advance_pre_hours=24,
            advance_post_hours=24,
        )
        db.add(node)
        db.commit()
        db.refresh(node)

        assert node.follow_up_at == datetime(2026, 5, 4, 10, 0)
        assert node.follow_up_hint == "面试结果"
        assert node.estimated_arc_days == 14
        assert node.advance_pre_hours == 24
        assert node.advance_post_hours == 24
        assert node.proactive_suppressed_at is None


def test_concept_node_v0_7_columns_default_null(tmp_path):
    """Existing rows without explicit values get NULL."""
    engine = create_engine(tmp_path / "test.db")
    create_all_tables(engine)
    ensure_schema_up_to_date(engine)

    with Session(engine) as db:
        db.add(Persona(id="p1", display_name="Test"))
        db.add(User(id="u1", display_name="Test"))
        db.commit()

        node = ConceptNode(
            persona_id="p1",
            user_id="u1",
            type=NodeType.EVENT,
            description="ordinary fact",
        )
        db.add(node)
        db.commit()
        db.refresh(node)

        assert node.follow_up_at is None
        assert node.follow_up_hint is None
        assert node.estimated_arc_days is None
        assert node.advance_pre_hours is None
        assert node.advance_post_hours is None
        assert node.proactive_suppressed_at is None
