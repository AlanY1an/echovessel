"""`core_block_appends` audit row written when import adds persona_traits.

Verifies that:
- `append_to_core_block` writes both the core_blocks update and the
  audit row atomically
- `import_content(persona_traits)` creates a core_block_appends row
- Relationship facts and user identity facts also produce audit rows
- Invalid content_type raises ValueError (tracker §3 #10)
- Invalid label raises ValueError
"""

from __future__ import annotations

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.memory import (
    CoreBlock,
    CoreBlockAppend,
    Persona,
    User,
    append_to_core_block,
    create_all_tables,
    create_engine,
    import_content,
)


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def test_import_persona_traits_writes_audit_row():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        result = import_content(
            db,
            source="book-hash-aaa",
            content_type="persona_traits",
            payload={
                "persona_id": "p_test",
                "user_id": "self",
                "content": "她很怕鬼,但对她爱的人极其坚定。",
                "source_label": "Novel ch.3",
                "chunk_index": 5,
            },
        )
        assert result.total_writes == 1
        assert len(result.core_block_append_ids) == 1

        appends = list(db.exec(select(CoreBlockAppend)))
        assert len(appends) == 1
        row = appends[0]
        assert row.label == "persona"
        # persona_block is shared → user_id is NULL
        assert row.user_id is None
        assert "怕鬼" in row.content
        assert row.provenance_json["imported_from"] == "book-hash-aaa"
        assert row.provenance_json["source_label"] == "Novel ch.3"
        assert row.provenance_json["chunk_index"] == 5

        # And the core_blocks row itself was updated with the text
        blocks = list(
            db.exec(
                select(CoreBlock).where(
                    CoreBlock.persona_id == "p_test",
                    CoreBlock.label == "persona",
                )
            )
        )
        assert len(blocks) == 1
        assert "怕鬼" in blocks[0].content


def test_append_appends_not_replaces():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)

        import_content(
            db,
            source="file-1",
            content_type="persona_traits",
            payload={
                "persona_id": "p_test",
                "user_id": "self",
                "content": "first trait",
            },
        )
        import_content(
            db,
            source="file-2",
            content_type="persona_traits",
            payload={
                "persona_id": "p_test",
                "user_id": "self",
                "content": "second trait",
            },
        )

        appends = list(db.exec(select(CoreBlockAppend)))
        assert len(appends) == 2

        blocks = list(
            db.exec(
                select(CoreBlock).where(
                    CoreBlock.persona_id == "p_test",
                    CoreBlock.label == "persona",
                )
            )
        )
        assert len(blocks) == 1
        # Both traits appear
        assert "first trait" in blocks[0].content
        assert "second trait" in blocks[0].content


def test_import_user_identity_facts_writes_audit():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        result = import_content(
            db,
            source="chat-hash-bb",
            content_type="user_identity_facts",
            payload={
                "persona_id": "p_test",
                "user_id": "self",
                "content": "用户有一只叫 Mochi 的猫",
                "category": "pet",
            },
        )
        assert len(result.core_block_append_ids) == 1
        appends = list(db.exec(select(CoreBlockAppend)))
        assert appends[0].label == "user"
        assert appends[0].user_id == "self"
        assert appends[0].provenance_json["category"] == "pet"


def test_import_relationship_facts_content_type_is_rejected():
    """v0.5 · ``relationship_facts`` was dropped from the legal
    content_type whitelist (plan §1). The dispatcher must hard-fail
    instead of silently accepting it.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        with pytest.raises(ValueError, match="unknown content_type"):
            import_content(
                db,
                source="diary-ccc",
                content_type="relationship_facts",  # type: ignore[arg-type]
                payload={
                    "persona_id": "p_test",
                    "user_id": "self",
                    "content": "Alan 是用户的弟弟",
                    "person_label": "Alan",
                },
            )


def test_unknown_content_type_raises():
    """Tracker §3 #10: unknown content_type MUST raise ValueError."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        with pytest.raises(ValueError, match="unknown content_type"):
            import_content(
                db,
                source="x",
                content_type="nonsense_bucket",  # type: ignore[arg-type]
                payload={"persona_id": "p_test", "user_id": "self"},
            )


def test_unknown_label_raises():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        with pytest.raises(ValueError, match="unknown label"):
            append_to_core_block(
                db,
                persona_id="p_test",
                user_id=None,
                label="nonsense_label",
                content="x",
                provenance={},
            )


def test_empty_content_rejected():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        with pytest.raises(ValueError):
            append_to_core_block(
                db,
                persona_id="p_test",
                user_id=None,
                label="persona",
                content="   ",
                provenance={},
            )
