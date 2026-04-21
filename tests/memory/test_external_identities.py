"""Tests for the ``external_identities`` alias table.

The table maps `(channel_id, external_id) -> internal_user_id` so the
memory layer can stay scoped by `internal_user_id` while channels
continue to surface transport-native identities (Discord snowflakes,
phone handles, web "self"). MVP semantics: every external id maps to
``"self"``; the table exists so future multi-user / group-chat work
can rebind without changing the memory schema.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DbSession

from echovessel.memory import (
    ExternalIdentity,
    Persona,
    User,
    create_all_tables,
    create_engine,
)


def _seed_user(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def test_external_identities_table_created():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    inspector = inspect(engine)
    assert "external_identities" in set(inspector.get_table_names())


def test_external_identity_round_trip():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed_user(engine)

    with DbSession(engine) as db:
        db.add(
            ExternalIdentity(
                channel_id="discord",
                external_id="753654474022584361",
                internal_user_id="self",
            )
        )
        db.commit()

    with DbSession(engine) as db:
        row = db.get(ExternalIdentity, ("discord", "753654474022584361"))
        assert row is not None
        assert row.internal_user_id == "self"


def test_external_identity_composite_pk_rejects_duplicate_pair():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed_user(engine)

    with DbSession(engine) as db:
        db.add(
            ExternalIdentity(
                channel_id="discord",
                external_id="999",
                internal_user_id="self",
            )
        )
        db.commit()

    with DbSession(engine) as db:
        db.add(
            ExternalIdentity(
                channel_id="discord",
                external_id="999",
                internal_user_id="self",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_external_identity_same_external_id_different_channel_allowed():
    """The same opaque string can mean different humans in different
    transports — `999` on Discord is not the same identity as `999` on
    iMessage. The composite PK must allow both rows to coexist."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed_user(engine)

    with DbSession(engine) as db:
        db.add(
            ExternalIdentity(
                channel_id="discord",
                external_id="999",
                internal_user_id="self",
            )
        )
        db.add(
            ExternalIdentity(
                channel_id="imessage",
                external_id="999",
                internal_user_id="self",
            )
        )
        db.commit()  # must not raise
