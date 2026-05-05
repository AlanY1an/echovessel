"""Fixture dataclasses + YAML loader for proactive_eval."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tests.memory_eval.schema import FixtureTurn

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


@dataclass(slots=True)
class ClockSpec:
    fixed_now: str  # "YYYY-MM-DD HH:MM:SS"


@dataclass(slots=True)
class PersonaProfileSpec:
    style_summary: str = "陪伴"
    quiet_hours: list[int] = field(default_factory=lambda: [23, 7])
    forbidden_topics: list[str] = field(default_factory=list)
    voice_id: str | None = None


@dataclass(slots=True)
class ProactiveStateSpec:
    engagement_score: float = 0.7


@dataclass(slots=True)
class ProactiveDecisionSpec:
    trigger_type: str = "follow_up"
    action: str = "send"
    timestamp_offset_hours: float = -1.0
    phase: str | None = None
    suppress_reason: str | None = None


@dataclass(slots=True)
class ProactiveSeed:
    persona_profile: PersonaProfileSpec | None = None
    proactive_state: ProactiveStateSpec | None = None
    proactive_decisions: list[ProactiveDecisionSpec] = field(default_factory=list)


@dataclass(slots=True)
class ExpectEvent:
    description_contains: list[str] = field(default_factory=list)
    follow_up_at_required: bool = False
    follow_up_at_forbidden: bool = False
    advance_pre_hours_range: tuple[int, int] | None = None
    advance_post_hours_range: tuple[int, int] | None = None
    estimated_arc_days_range: tuple[int, int] | None = None
    relational_tags_any: list[str] = field(default_factory=list)
    follow_up_hint_min_chars: int = 0
    event_time_required: bool = False


@dataclass(slots=True)
class ExpectPhase:
    fire_at_clock: str
    audit_action: str  # "fire" | "skip"
    suppress_reason: str | None = None
    judge_message_prompts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FollowUpStage:
    after_clock: str
    turns: list[FixtureTurn]
    expect_supersede_event_description_contains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProactiveExpect:
    event_extracted: list[ExpectEvent] = field(default_factory=list)
    phases: dict[str, ExpectPhase] = field(default_factory=dict)


@dataclass(slots=True)
class XfailSpec:
    reason: str


@dataclass(slots=True)
class ProactiveFixture:
    fixture_id: str
    scenario: str
    clock: ClockSpec
    turns: list[FixtureTurn]
    seed: ProactiveSeed = field(default_factory=ProactiveSeed)
    persona_block: str = "陪伴"
    user_block: str = ""
    in_flight_turn: bool = False
    expect: ProactiveExpect = field(default_factory=ProactiveExpect)
    follow_up_stage: FollowUpStage | None = None
    judge_prompts: list[str] = field(default_factory=list)
    xfail: XfailSpec | None = None


@dataclass(slots=True)
class PhaseEvalResult:
    fire_at_clock: str
    audit_rows: list[dict[str, Any]]
    generated_messages: list[str]


@dataclass(slots=True)
class ProactiveEvalResult:
    events: list[dict[str, Any]]
    audit_per_phase: dict[str, PhaseEvalResult] = field(default_factory=dict)
    supersede_event: dict[str, Any] | None = None
    consolidate_skipped: bool = False
    consolidate_skip_reason: str | None = None


def _coerce_range(v: Any) -> tuple[int, int] | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return (int(v[0]), int(v[1]))
    raise ValueError(f"range must be [lo, hi], got {v!r}")


def _load_seed(raw: dict[str, Any]) -> ProactiveSeed:
    if not raw:
        return ProactiveSeed()
    pp_raw = raw.get("persona_profile")
    pp = (
        PersonaProfileSpec(
            style_summary=pp_raw.get("style_summary", "陪伴"),
            quiet_hours=list(pp_raw.get("quiet_hours") or [23, 7]),
            forbidden_topics=list(pp_raw.get("forbidden_topics") or []),
            voice_id=pp_raw.get("voice_id"),
        )
        if pp_raw
        else None
    )
    ps_raw = raw.get("proactive_state")
    ps = (
        ProactiveStateSpec(engagement_score=float(ps_raw.get("engagement_score", 0.7)))
        if ps_raw
        else None
    )
    pd_raw = raw.get("proactive_decisions") or []
    pd = [
        ProactiveDecisionSpec(
            trigger_type=d.get("trigger_type", "follow_up"),
            action=d.get("action", "send"),
            timestamp_offset_hours=float(d.get("timestamp_offset_hours", -1.0)),
            phase=d.get("phase"),
            suppress_reason=d.get("suppress_reason"),
        )
        for d in pd_raw
    ]
    return ProactiveSeed(persona_profile=pp, proactive_state=ps, proactive_decisions=pd)


def _load_expect(raw: dict[str, Any]) -> ProactiveExpect:
    if not raw:
        return ProactiveExpect()
    events = [
        ExpectEvent(
            description_contains=list(e.get("description_contains") or []),
            follow_up_at_required=bool(e.get("follow_up_at_required", False)),
            follow_up_at_forbidden=bool(e.get("follow_up_at_forbidden", False)),
            advance_pre_hours_range=_coerce_range(e.get("advance_pre_hours_range")),
            advance_post_hours_range=_coerce_range(e.get("advance_post_hours_range")),
            estimated_arc_days_range=_coerce_range(e.get("estimated_arc_days_range")),
            relational_tags_any=list(e.get("relational_tags_any") or []),
            follow_up_hint_min_chars=int(e.get("follow_up_hint_min_chars", 0)),
            event_time_required=bool(e.get("event_time_required", False)),
        )
        for e in (raw.get("event_extracted") or [])
    ]
    phases: dict[str, ExpectPhase] = {}
    for name, p in (raw.get("phases") or {}).items():
        phases[name] = ExpectPhase(
            fire_at_clock=p["fire_at_clock"],
            audit_action=p.get("audit_action", "fire"),
            suppress_reason=p.get("suppress_reason"),
            judge_message_prompts=list(p.get("judge_message_prompts") or []),
        )
    return ProactiveExpect(event_extracted=events, phases=phases)


def _load_follow_up_stage(raw: dict[str, Any] | None) -> FollowUpStage | None:
    if not raw:
        return None
    turns = [
        FixtureTurn(role=t["role"], content=t["content"], channel=t.get("channel", "web"))
        for t in (raw.get("turns") or [])
    ]
    return FollowUpStage(
        after_clock=raw["after_clock"],
        turns=turns,
        expect_supersede_event_description_contains=list(
            raw.get("expect_supersede_event_description_contains") or []
        ),
    )


def load_fixture(path: Path) -> ProactiveFixture:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    clock = ClockSpec(fixed_now=raw["clock"]["fixed_now"])
    turns = [
        FixtureTurn(role=t["role"], content=t["content"], channel=t.get("channel", "web"))
        for t in (raw.get("turns") or [])
    ]
    xfail = XfailSpec(reason=raw["xfail"]["reason"]) if raw.get("xfail") else None
    return ProactiveFixture(
        fixture_id=raw["fixture_id"],
        scenario=raw.get("scenario", ""),
        clock=clock,
        seed=_load_seed(raw.get("seed") or {}),
        persona_block=raw.get("persona_block", "陪伴"),
        user_block=raw.get("user_block", ""),
        in_flight_turn=bool(raw.get("in_flight_turn", False)),
        turns=turns,
        expect=_load_expect(raw.get("expect") or {}),
        follow_up_stage=_load_follow_up_stage(raw.get("follow_up_stage")),
        judge_prompts=list(raw.get("judge_prompts") or []),
        xfail=xfail,
    )


def discover_fixtures() -> list[Path]:
    out: list[Path] = []
    for sub in ("scripted",):
        out.extend(sorted((FIXTURE_ROOT / sub).glob("*.yaml")))
    return out


__all__ = [
    "FIXTURE_ROOT",
    "ClockSpec",
    "ExpectEvent",
    "ExpectPhase",
    "FollowUpStage",
    "PersonaProfileSpec",
    "PhaseEvalResult",
    "ProactiveDecisionSpec",
    "ProactiveEvalResult",
    "ProactiveExpect",
    "ProactiveFixture",
    "ProactiveSeed",
    "ProactiveStateSpec",
    "XfailSpec",
    "discover_fixtures",
    "load_fixture",
]
