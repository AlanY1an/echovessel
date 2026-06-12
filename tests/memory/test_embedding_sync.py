"""Embedding-model tag + re-embed sync (``memory.embedding_sync``).

Covers:

- Fresh DB (no vectors): tag written, embed_fn never called.
- Tag matches configured model: no-op, embed_fn never called.
- Untagged DB that already has vectors: vectors are assumed to come
  from the pre-tag default (``all-MiniLM-L6-v2``) — no re-embed when
  that is the configured model, full re-embed when it isn't.
- Model change: every row that has a vec row is re-embedded (including
  soft-deleted / superseded nodes — any row in a vec table participates
  in KNN candidate selection), entity vectors use the same embed input
  as ``resolve_entity``, and the tag is updated.
- Interrupted sync: the transaction rolls back (tag + vectors keep
  their pre-sync state) and a retry redoes the full sync.

All embed functions are in-test fakes — no model download.
"""

from __future__ import annotations

import struct
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import ConceptNode, Persona, User, create_all_tables, create_engine
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.db import VECTOR_DIM
from echovessel.memory.embedding_sync import (
    EMBEDDING_MODEL_KEY,
    UNTAGGED_VECTORS_MODEL,
    ensure_embedding_model,
)
from echovessel.memory.models import Entity

PERSONA = "p1"
USER = "self"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        db.add(Persona(id=PERSONA, display_name="P"))
        db.add(User(id=USER, display_name="self"))
        db.commit()
    return engine, backend


def _make_embed(marker: float, calls: list[str] | None = None):
    """Deterministic fake embedder. ``marker`` stands in for the model
    identity so tests can tell which 'model' produced a stored blob."""

    def _embed(t: str) -> list[float]:
        if calls is not None:
            calls.append(t)
        v = [0.0] * VECTOR_DIM
        v[0] = marker
        v[1] = float(len(t) % 97)
        return v

    return _embed


def _read_tag(engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT value FROM memory_meta WHERE key = :k"),
            {"k": EMBEDDING_MODEL_KEY},
        ).scalar()


def _read_vec(engine, table: str, row_id: int) -> list[float]:
    with engine.connect() as conn:
        blob = conn.execute(
            text(f"SELECT embedding FROM {table} WHERE id = :id"),
            {"id": row_id},
        ).scalar()
    assert blob is not None
    return list(struct.unpack(f"{VECTOR_DIM}f", blob))


def _add_node(engine, backend, embed, description: str, **kwargs) -> int:
    with DbSession(engine) as db:
        node = ConceptNode(
            persona_id=PERSONA,
            user_id=USER,
            type=NodeType.EVENT,
            description=description,
            **kwargs,
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        node_id = node.id
    backend.insert_vector(node_id, embed(description))
    return node_id


def _add_entity(engine, backend, embed, canonical_name: str, description: str | None) -> int:
    with DbSession(engine) as db:
        entity = Entity(
            persona_id=PERSONA,
            user_id=USER,
            canonical_name=canonical_name,
            description=description,
        )
        db.add(entity)
        db.commit()
        db.refresh(entity)
        entity_id = entity.id
    embed_input = f"{canonical_name}. {description}" if description else canonical_name
    backend.insert_entity_vector(entity_id, embed(embed_input))
    return entity_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fresh_db_writes_tag_without_embedding():
    engine, backend = _setup()
    calls: list[str] = []

    count = ensure_embedding_model(engine, backend, "model-a", _make_embed(1.0, calls))

    assert count == 0
    assert calls == []
    assert _read_tag(engine) == "model-a"


def test_same_model_startup_is_noop():
    engine, backend = _setup()
    old_embed = _make_embed(1.0)
    node_id = _add_node(engine, backend, old_embed, "user adopted a cat")
    ensure_embedding_model(engine, backend, "model-a", old_embed)

    calls: list[str] = []
    count = ensure_embedding_model(engine, backend, "model-a", _make_embed(2.0, calls))

    assert count == 0
    assert calls == []
    assert _read_tag(engine) == "model-a"
    assert _read_vec(engine, "concept_nodes_vec", node_id)[0] == pytest.approx(1.0)


def test_untagged_db_with_vectors_assumes_legacy_default():
    engine, backend = _setup()
    node_id = _add_node(engine, backend, _make_embed(1.0), "user adopted a cat")

    calls: list[str] = []
    count = ensure_embedding_model(engine, backend, UNTAGGED_VECTORS_MODEL, _make_embed(2.0, calls))

    assert count == 0
    assert calls == []
    assert _read_tag(engine) == UNTAGGED_VECTORS_MODEL
    assert _read_vec(engine, "concept_nodes_vec", node_id)[0] == pytest.approx(1.0)


def test_untagged_db_with_vectors_reembeds_when_configured_model_differs():
    engine, backend = _setup()
    node_id = _add_node(engine, backend, _make_embed(1.0), "user adopted a cat")

    count = ensure_embedding_model(
        engine, backend, "intfloat/multilingual-e5-small", _make_embed(2.0)
    )

    assert count == 1
    assert _read_tag(engine) == "intfloat/multilingual-e5-small"
    assert _read_vec(engine, "concept_nodes_vec", node_id)[0] == pytest.approx(2.0)


def test_model_change_reembeds_every_vec_row_including_non_live_nodes():
    engine, backend = _setup()
    old_embed = _make_embed(1.0)
    live_id = _add_node(engine, backend, old_embed, "live event")
    deleted_id = _add_node(engine, backend, old_embed, "deleted event")
    superseded_id = _add_node(engine, backend, old_embed, "superseded event")
    with DbSession(engine) as db:
        deleted = db.get(ConceptNode, deleted_id)
        deleted.deleted_at = datetime.now()
        superseded = db.get(ConceptNode, superseded_id)
        superseded.superseded_by_id = live_id
        db.add(deleted)
        db.add(superseded)
        db.commit()
    entity_id = _add_entity(engine, backend, old_embed, "Scott", "coworker at the lab")

    calls: list[str] = []
    count = ensure_embedding_model(engine, backend, "model-b", _make_embed(2.0, calls))

    assert count == 4
    assert sorted(calls) == sorted(
        ["live event", "deleted event", "superseded event", "Scott. coworker at the lab"]
    )
    for node_id in (live_id, deleted_id, superseded_id):
        assert _read_vec(engine, "concept_nodes_vec", node_id)[0] == pytest.approx(2.0)
    assert _read_vec(engine, "entities_vec", entity_id)[0] == pytest.approx(2.0)
    assert _read_tag(engine) == "model-b"


def test_entity_embed_input_matches_resolve_entity_shape():
    engine, backend = _setup()
    old_embed = _make_embed(1.0)
    _add_entity(engine, backend, old_embed, "Momo", None)

    calls: list[str] = []
    ensure_embedding_model(engine, backend, "model-b", _make_embed(2.0, calls))

    # No description → bare canonical name, exactly like resolve_entity.
    assert calls == ["Momo"]


def test_interrupted_sync_rolls_back_and_retry_redoes_the_work():
    engine, backend = _setup()
    old_embed = _make_embed(1.0)
    first_id = _add_node(engine, backend, old_embed, "first event")
    second_id = _add_node(engine, backend, old_embed, "second event")
    ensure_embedding_model(engine, backend, "model-a", old_embed)

    boom_calls: list[str] = []

    def flaky(t: str) -> list[float]:
        boom_calls.append(t)
        if len(boom_calls) >= 2:
            raise RuntimeError("embedder crashed mid-sync")
        v = [0.0] * VECTOR_DIM
        v[0] = 2.0
        return v

    with pytest.raises(RuntimeError, match="mid-sync"):
        ensure_embedding_model(engine, backend, "model-b", flaky)

    # Transaction rolled back: tag and ALL vectors keep their pre-sync state.
    assert _read_tag(engine) == "model-a"
    assert _read_vec(engine, "concept_nodes_vec", first_id)[0] == pytest.approx(1.0)
    assert _read_vec(engine, "concept_nodes_vec", second_id)[0] == pytest.approx(1.0)

    # Retry re-embeds everything.
    count = ensure_embedding_model(engine, backend, "model-b", _make_embed(2.0))
    assert count == 2
    assert _read_tag(engine) == "model-b"
    assert _read_vec(engine, "concept_nodes_vec", first_id)[0] == pytest.approx(2.0)
    assert _read_vec(engine, "concept_nodes_vec", second_id)[0] == pytest.approx(2.0)
