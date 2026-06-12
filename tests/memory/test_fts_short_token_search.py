"""fts_search handles queries made only of 1-2 character tokens.

The trigram tokenizer needs >= 3 characters to produce a single
trigram, so a quoted 2-character phrase matches nothing in FTS5 — the
typical shape of a Chinese query (妈妈 / 工作 / 小猫). For all-short
queries the backend scans recall_messages with LIKE instead, under the
same persona/user/deleted_at scoping and limit.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.core.types import MessageRole, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend


def _seed(engine, contents: list[str]) -> list[int]:
    with DbSession(engine) as db:
        db.add(Persona(id="p_test", display_name="Test Persona"))
        db.add(User(id="self", display_name="Alan"))
        db.add(User(id="other", display_name="Someone"))
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
        ids = []
        for content in contents:
            msg = RecallMessage(
                session_id="s_001",
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                role=MessageRole.USER,
                content=content,
                day=date.today(),
            )
            db.add(msg)
            db.commit()
            ids.append(msg.id)
        return ids


def test_two_char_chinese_query_matches():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    (msg_id,) = _seed(engine, ["我家小猫今天特别黏人"])

    hits = backend.fts_search("小猫", "p_test", "self", top_k=5)

    assert [h.recall_message_id for h in hits] == [msg_id]


def test_multiple_short_tokens_require_all():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    both_id, one_id = _seed(engine, ["小猫在妈妈腿上睡着了", "小猫跑出去玩了"])

    hits = backend.fts_search("小猫 妈妈", "p_test", "self", top_k=5)

    assert [h.recall_message_id for h in hits] == [both_id]
    assert one_id not in [h.recall_message_id for h in hits]


def test_short_token_fallback_respects_scoping_and_deletion():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    (msg_id,) = _seed(engine, ["小猫打翻了水杯"])

    assert backend.fts_search("小猫", "p_test", "other", top_k=5) == []
    assert backend.fts_search("小猫", "p_other", "self", top_k=5) == []

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE recall_messages SET deleted_at = CURRENT_TIMESTAMP WHERE id = :id"),
            {"id": msg_id},
        )
    assert backend.fts_search("小猫", "p_test", "self", top_k=5) == []


def test_short_token_fallback_respects_top_k():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine, [f"小猫第{i}次捣乱" for i in range(5)])

    hits = backend.fts_search("小猫", "p_test", "self", top_k=2)

    assert len(hits) == 2


def test_like_wildcards_in_short_tokens_are_literal():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    pct_id, plain_id = _seed(engine, ["readiness at 9%", "readiness at 9 today"])

    hits = backend.fts_search("9%", "p_test", "self", top_k=5)

    ids = [h.recall_message_id for h in hits]
    assert pct_id in ids
    assert plain_id not in ids


def test_mixed_query_still_uses_fts():
    """A query mixing short and >= 3-char tokens keeps the FTS path —
    the long token carries the match."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    (msg_id,) = _seed(engine, ["Mochi 是只小猫"])

    hits = backend.fts_search("小猫 Mochi", "p_test", "self", top_k=5)

    assert [h.recall_message_id for h in hits] == [msg_id]


def test_empty_query_returns_nothing():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine, ["小猫睡了"])

    assert backend.fts_search("   ", "p_test", "self", top_k=5) == []
