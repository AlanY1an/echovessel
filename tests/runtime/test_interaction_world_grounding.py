"""Spec 2 · L1 world-grounding renderer tests.

Covers:

- ``_format_now_section`` single-tz and dual-tz renderings (plan §6.4).
- ``# Who you are`` expansion to location + nationality bullets
  (plan §6.5).
- ``# How you feel right now`` section rendering + suppression on
  default neutral state (plan §6.4).
- ``maybe_decay_episodic_state`` 12h decay helper.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from echovessel.memory.models import Persona
from echovessel.runtime.turn.coordinator import (
    PersonaFactsView,
    _format_episodic_state_section,
    _format_now_section,
    build_system_prompt,
    maybe_decay_episodic_state,
)


def _ny_now() -> datetime:
    return datetime(2026, 4, 23, 20, 0, tzinfo=ZoneInfo("America/New_York"))


# ---------------------------------------------------------------------------
# _format_now_section
# ---------------------------------------------------------------------------


def test_format_now_section_single_timezone():
    now = _ny_now()
    out = _format_now_section(now)
    assert out.startswith("# Right now\n")
    assert "2026-04-23" in out
    assert "Thursday" in out
    assert "20:00" in out


def test_format_now_section_dual_timezone_renders_both_lines():
    now = _ny_now()
    out = _format_now_section(now, persona_tz="Asia/Taipei")
    # User line (ET)
    assert "For them" in out
    assert "2026-04-23" in out
    # Persona line — Taipei is 12h ahead at this time of year.
    assert "Where you're conceptually based" in out
    assert "Asia/Taipei" in out
    assert "2026-04-24" in out


def test_format_now_section_unknown_persona_tz_falls_back():
    now = _ny_now()
    # Invalid IANA string should not raise; dual-tz line is just skipped.
    out = _format_now_section(now, persona_tz="Not/AZone")
    assert "# Right now" in out
    assert "Not/AZone" not in out


# ---------------------------------------------------------------------------
# # Who you are section
# ---------------------------------------------------------------------------


def test_who_you_are_renders_location_and_nationality():
    facts = PersonaFactsView(
        full_name="Luna",
        nationality="CN",
        location="New York",
        occupation="writer",
        native_language="zh-CN",
    )
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=facts,
    )
    assert "# Who you are" in prompt
    assert "Luna" in prompt
    assert "Nationality: CN" in prompt
    assert "Based in: New York" in prompt
    assert "Occupation: writer" in prompt


def test_who_you_are_skips_all_none_view():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=None,
    )
    assert "# Who you are" not in prompt


def test_dual_tz_line_uses_persona_timezone_from_facts():
    """When ``now`` and facts.timezone are both supplied, the dual-tz
    renderer uses facts.timezone for the persona-side line."""
    facts = PersonaFactsView(full_name="Luna", timezone="Asia/Taipei")
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=facts,
        now=_ny_now(),
    )
    assert "For them" in prompt
    assert "Asia/Taipei" in prompt


# ---------------------------------------------------------------------------
# # How you feel right now
# ---------------------------------------------------------------------------


def test_episodic_state_section_renders_non_neutral():
    out = _format_episodic_state_section(
        {"mood": "warm-curious", "energy": 7, "last_user_signal": "warm"}
    )
    assert "# How you feel right now" in out
    assert "warm-curious" in out
    assert "energy 7/10" in out
    assert "last sense from them: warm" in out


def test_episodic_state_section_suppresses_neutral_default():
    out = _format_episodic_state_section(
        {"mood": "neutral", "energy": 5, "last_user_signal": None}
    )
    assert out == ""


def test_episodic_state_section_missing_state_returns_empty():
    assert _format_episodic_state_section(None) == ""
    assert _format_episodic_state_section({}) == ""


def test_build_system_prompt_omits_episodic_section_when_neutral():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=None,
        episodic_state={
            "mood": "neutral",
            "energy": 5,
            "last_user_signal": None,
        },
    )
    assert "# How you feel right now" not in prompt


def test_build_system_prompt_emits_episodic_section_when_non_neutral():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=None,
        episodic_state={
            "mood": "subdued",
            "energy": 3,
            "last_user_signal": "cool",
        },
    )
    assert "# How you feel right now" in prompt
    assert "subdued" in prompt
    assert "cool" in prompt


# ---------------------------------------------------------------------------
# maybe_decay_episodic_state
# ---------------------------------------------------------------------------


def test_decay_resets_after_12_hours():
    persona = Persona(id="p", display_name="P")
    earlier = datetime.now(UTC) - timedelta(hours=13)
    persona.episodic_state = {
        "mood": "subdued",
        "energy": 3,
        "last_user_signal": "cool",
        "updated_at": earlier.isoformat(),
    }
    changed = maybe_decay_episodic_state(persona, datetime.now(UTC))
    assert changed is True
    assert persona.episodic_state["mood"] == "neutral"
    assert persona.episodic_state["energy"] == 5
    assert persona.episodic_state["last_user_signal"] is None


def test_decay_noop_when_recent():
    persona = Persona(id="p", display_name="P")
    recent = datetime.now(UTC) - timedelta(hours=2)
    persona.episodic_state = {
        "mood": "warm",
        "energy": 6,
        "last_user_signal": "warm",
        "updated_at": recent.isoformat(),
    }
    changed = maybe_decay_episodic_state(persona, datetime.now(UTC))
    assert changed is False
    assert persona.episodic_state["mood"] == "warm"


def test_decay_noop_when_updated_at_missing():
    persona = Persona(id="p", display_name="P")
    persona.episodic_state = {
        "mood": "warm",
        "energy": 6,
        "last_user_signal": None,
        "updated_at": None,
    }
    changed = maybe_decay_episodic_state(persona, datetime.now(UTC))
    assert changed is False


def test_birth_year_still_renders_not_datetime_instance():
    """Sanity check that PersonaFactsView still works with a ``date``."""
    facts = PersonaFactsView(full_name="Luna", birth_date=date(1999, 5, 1))
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        persona_facts=facts,
    )
    assert "Born: 1999" in prompt
