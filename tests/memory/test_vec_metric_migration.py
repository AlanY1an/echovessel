"""vec0 cosine-metric rebuild in ``ensure_schema_up_to_date``.

The vec tables are declared with ``distance_metric=cosine`` so that
sqlite-vec reports cosine distance (``1 - cosine_similarity``) — the
metric every distance→similarity conversion in the codebase assumes.
Databases whose vec tables predate the declared metric get rebuilt in
place: same table name, same rows, cosine DDL.

Covers:

- Fresh DBs get the metric straight from ``create_all_tables``.
- A legacy vec table (no metric in its DDL) is rebuilt by
  ``ensure_schema_up_to_date`` with all rows surviving.
- Post-rebuild distances are cosine: identical → 0.0, orthogonal → 1.0.
- Re-running the migration is a no-op (idempotent).
"""

from __future__ import annotations

import struct

from sqlalchemy import text

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.db import VECTOR_DIM
from echovessel.memory.migrations import ensure_schema_up_to_date

VEC_TABLES = ("concept_nodes_vec", "entities_vec")


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _axis_vec(idx: int) -> list[float]:
    v = [0.0] * VECTOR_DIM
    v[idx] = 1.0
    return v


def _vec_ddl(engine, table: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": table},
        ).first()
    assert row is not None, f"missing vec table {table}"
    return row[0]


def _seed_legacy_vec_tables(engine) -> None:
    """Create both vec tables WITHOUT the metric and insert two rows each."""
    with engine.begin() as conn:
        for table in VEC_TABLES:
            conn.exec_driver_sql(
                f"CREATE VIRTUAL TABLE {table} USING vec0("
                f"id INTEGER PRIMARY KEY, embedding FLOAT[{VECTOR_DIM}])"
            )
            conn.exec_driver_sql(
                f"INSERT INTO {table}(id, embedding) VALUES (1, ?)",
                (_pack(_axis_vec(0)),),
            )
            conn.exec_driver_sql(
                f"INSERT INTO {table}(id, embedding) VALUES (2, ?)",
                (_pack(_axis_vec(1)),),
            )


def test_fresh_db_vec_tables_declare_cosine_metric():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    for table in VEC_TABLES:
        assert "distance_metric=cosine" in _vec_ddl(engine, table)


def test_legacy_vec_tables_rebuilt_with_cosine_metric_and_rows_survive():
    engine = create_engine(":memory:")
    _seed_legacy_vec_tables(engine)
    for table in VEC_TABLES:
        assert "distance_metric=cosine" not in _vec_ddl(engine, table)

    ensure_schema_up_to_date(engine)

    with engine.connect() as conn:
        for table in VEC_TABLES:
            assert "distance_metric=cosine" in _vec_ddl(engine, table)
            # No scratch table left behind.
            leftover = conn.execute(
                text("SELECT name FROM sqlite_master WHERE name=:n"),
                {"n": f"{table}_rebuild"},
            ).first()
            assert leftover is None

            rows = conn.exec_driver_sql(
                f"SELECT id, distance FROM {table} "
                f"WHERE embedding MATCH ? AND k = 2 ORDER BY distance",
                (_pack(_axis_vec(0)),),
            ).fetchall()
            assert [r[0] for r in rows] == [1, 2], f"rows lost during {table} rebuild"
            # Cosine distances: identical → 0.0, orthogonal → 1.0.
            assert abs(rows[0][1] - 0.0) < 1e-6
            assert abs(rows[1][1] - 1.0) < 1e-6


def test_vec_metric_rebuild_is_idempotent():
    engine = create_engine(":memory:")
    _seed_legacy_vec_tables(engine)
    ensure_schema_up_to_date(engine)
    ensure_schema_up_to_date(engine)

    with engine.connect() as conn:
        for table in VEC_TABLES:
            count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
            assert count == 2
