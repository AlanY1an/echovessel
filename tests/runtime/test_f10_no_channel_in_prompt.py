"""F10 guard: assemble_turn produces prompts that contain zero transport
hints — no 'web' / 'discord' / 'imessage' / 'wechat' / 'channel_id' / 'via-'.

Spec §7.2, §7.3, §13.1.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session as DbSession

from echovessel.core.types import BlockLabel
from echovessel.memory import (
    CoreBlock,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.turn.coordinator import (
    IncomingMessage,
    TurnContext,
    assemble_turn,
)

_FORBIDDEN_SUBSTRINGS = (
    "channel_id",
    '"web"',
    "'web'",
    "discord",
    "imessage",
    "wechat",
    "via-",
)


def _zero_embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _seed_persona(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="Companion"))
    db.add(User(id="self", display_name="Alan"))
    db.add(
        CoreBlock(
            persona_id="p",
            user_id=None,
            label=BlockLabel.PERSONA,
            content="You are patient and grounded.",
        )
    )
    db.add(
        CoreBlock(
            persona_id="p",
            user_id="self",
            label=BlockLabel.USER,
            content="Alan is a thoughtful engineer who values directness.",
        )
    )
    db.commit()


async def test_assemble_turn_prompts_contain_no_transport_hints():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)

        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Companion",
            db=db,
            backend=backend,
            embed_fn=_zero_embed,
        )

        envelope = IncomingMessage(
            channel_id="discord:test-guild-abc",
            user_id="self",
            content="今天有点无聊，不知道干啥",
            received_at=datetime(2026, 4, 14, 10, 0, 0),
        )

        stub = StubProvider(fallback="那我们聊点轻的，最近有什么小事让你笑过吗？")
        result = await assemble_turn(ctx, envelope, stub)

        assert not result.skipped
        assert result.reply

        combined = result.system_prompt + "\n" + result.user_prompt
        # F10: no transport hint leaks into either prompt.
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in combined, (
                f"F10 violation: prompt contains forbidden substring "
                f"{forbidden!r}\n--- system ---\n{result.system_prompt}\n"
                f"--- user ---\n{result.user_prompt}"
            )

        # System prompt MUST carry the style guard instructing "reference
        # topics and feelings, NOT the medium".
        assert "NOT the medium" in result.system_prompt


async def test_assemble_turn_prompts_also_clean_with_l2_history():
    from echovessel.core.types import MessageRole
    from echovessel.memory.ingest import ingest_message

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)

        # Plant two L2 messages with explicit channel_id values in the row
        # — interaction must NOT render them into the prompt.
        ingest_message(
            db,
            persona_id="p",
            user_id="self",
            channel_id="discord:dev",
            role=MessageRole.USER,
            content="hey you there",
        )
        ingest_message(
            db,
            persona_id="p",
            user_id="self",
            channel_id="discord:dev",
            role=MessageRole.PERSONA,
            content="always listening",
        )

        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Companion",
            db=db,
            backend=backend,
            embed_fn=_zero_embed,
        )
        envelope = IncomingMessage(
            channel_id="discord:dev",
            user_id="self",
            content="今天好累",
            received_at=datetime(2026, 4, 14, 12, 0, 0),
        )

        stub = StubProvider(fallback="说说发生了什么")
        result = await assemble_turn(ctx, envelope, stub)
        assert not result.skipped

        combined = result.system_prompt + "\n" + result.user_prompt
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in combined, (
                f"F10 violation even with L2 history: {forbidden!r} present"
            )
