"""Profile derivation engine tests · Stage 1.2.

Drives ``derive_profile_from_core_blocks`` against a real in-memory
SQLite. The injected ``derivation_fn`` is a tiny async closure so the
tests stay independent of any LLM client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import pytest
from sqlmodel import Session

from echovessel.core.types import BlockLabel
from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import CoreBlock, Persona, User
from echovessel.proactive.core.models import PersonaProfile
from echovessel.proactive.engines.profile_derivation import (
    FALLBACK_STYLE_SUMMARY,
    build_fallback_profile,
    derive_profile_from_core_blocks,
)

_FIXED_NOW = datetime(2026, 4, 29, 22, 30, 0)


def _seed(db: Session, *, persona_id: str = "p1", with_blocks: bool = True) -> None:
    db.add(
        Persona(
            id=persona_id,
            display_name="Test Persona",
            native_language="zh-CN",
        )
    )
    db.add(User(id="self", display_name="Owner"))
    if with_blocks:
        db.add(
            CoreBlock(
                persona_id=persona_id,
                user_id=None,
                label=BlockLabel.PERSONA,
                content="温柔关心，问候式开场，语气轻。",
            )
        )
        db.add(
            CoreBlock(
                persona_id=persona_id,
                user_id="self",
                label=BlockLabel.USER,
                content="对方是工作日忙的工程师。",
            )
        )
    db.commit()


def _make_fake_fn(
    *,
    captured: dict[str, Any] | None = None,
    raise_exc: type[Exception] | None = None,
    style_summary: str = "她说话很短(10-30字)，关心式开场。",
    quiet_hours: list[int] | None = None,
    forbidden_topics: list[str] | None = None,
) -> Callable[..., Awaitable[PersonaProfile]]:
    async def _fn(**kwargs: Any) -> PersonaProfile:
        if captured is not None:
            captured.update(kwargs)
        if raise_exc is not None:
            raise raise_exc("simulated LLM failure")
        return PersonaProfile(
            persona_id=kwargs["persona_id"],
            style_summary=style_summary,
            quiet_hours=quiet_hours or [23, 7],
            forbidden_topics=forbidden_topics or [],
            voice_id=None,
            profile_generated_at=_FIXED_NOW,
            profile_source="llm_onboarding",
        )

    return _fn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derive_profile_calls_fn_and_persists():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with Session(engine) as db:
        _seed(db)

    captured: dict[str, Any] = {}
    fn = _make_fake_fn(captured=captured)

    with Session(engine) as db:
        result = await derive_profile_from_core_blocks(
            db,
            persona_id="p1",
            derivation_fn=fn,
            now_fn=lambda: _FIXED_NOW,
        )
        db.commit()

    assert result.persona_id == "p1"
    assert result.profile_source == "llm_onboarding"
    assert result.style_summary.startswith("她说话很短")
    assert captured["persona_id"] == "p1"
    assert captured["locale"] == "zh-CN"
    assert "persona" in captured["core_blocks"]
    assert "工程师" in captured["core_blocks"]["user"]

    with Session(engine) as db:
        row = db.get(PersonaProfile, "p1")
        assert row is not None
        assert row.profile_source == "llm_onboarding"
        assert row.quiet_hours == [23, 7]


# ---------------------------------------------------------------------------
# Fallback path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derive_profile_falls_back_when_fn_raises():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with Session(engine) as db:
        _seed(db)

    fn = _make_fake_fn(raise_exc=RuntimeError)

    with Session(engine) as db:
        result = await derive_profile_from_core_blocks(
            db,
            persona_id="p1",
            derivation_fn=fn,
            now_fn=lambda: _FIXED_NOW,
        )
        db.commit()

    assert result.persona_id == "p1"
    assert result.profile_source == "fallback_default"
    assert result.style_summary == FALLBACK_STYLE_SUMMARY
    assert result.profile_generated_at == _FIXED_NOW

    with Session(engine) as db:
        row = db.get(PersonaProfile, "p1")
        assert row is not None
        assert row.profile_source == "fallback_default"


def test_build_fallback_profile_contains_admin_hint():
    profile = build_fallback_profile("p1", lambda: _FIXED_NOW)
    assert profile.profile_source == "fallback_default"
    assert "尚未生成" in profile.style_summary
    assert profile.quiet_hours == [23, 7]
    assert profile.forbidden_topics == []
    assert profile.voice_id is None


# ---------------------------------------------------------------------------
# Inputs to the LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derive_passes_locale_and_block_dict_to_fn():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with Session(engine) as db:
        _seed(db)

    captured: dict[str, Any] = {}
    fn = _make_fake_fn(captured=captured)

    with Session(engine) as db:
        await derive_profile_from_core_blocks(
            db,
            persona_id="p1",
            derivation_fn=fn,
            now_fn=lambda: _FIXED_NOW,
        )

    blocks = captured["core_blocks"]
    assert isinstance(blocks, dict)
    assert blocks["persona"] == "温柔关心，问候式开场，语气轻。"
    assert blocks["user"].startswith("对方是")
    assert captured["locale"] == "zh-CN"


@pytest.mark.asyncio
async def test_derive_persona_not_found_raises():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    fn = _make_fake_fn()

    with Session(engine) as db, pytest.raises(ValueError):
        await derive_profile_from_core_blocks(
            db,
            persona_id="missing",
            derivation_fn=fn,
            now_fn=lambda: _FIXED_NOW,
        )


@pytest.mark.asyncio
async def test_derive_uses_default_locale_when_persona_has_none():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with Session(engine) as db:
        db.add(Persona(id="p2", display_name="No Locale"))
        db.add(User(id="self", display_name="Owner"))
        db.commit()

    captured: dict[str, Any] = {}
    fn = _make_fake_fn(captured=captured)

    with Session(engine) as db:
        await derive_profile_from_core_blocks(
            db,
            persona_id="p2",
            derivation_fn=fn,
            now_fn=lambda: _FIXED_NOW,
        )

    assert captured["locale"] == "zh-CN"
