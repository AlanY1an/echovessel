"""Fixture dataclasses + YAML loader.

Each ``*.yaml`` under ``fixtures/{scripted,synthesized}/`` deserialises
into a :class:`Fixture` here. The pipeline runner consumes these dicts;
no logic lives in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


@dataclass(slots=True)
class SeedEvent:
    description: str
    emotional_impact: int = 0
    emotion_tags: list[str] = field(default_factory=list)
    relational_tags: list[str] = field(default_factory=list)
    created_at_offset_hours: float = -1.0


@dataclass(slots=True)
class SeedFillingSpec:
    parent_description_contains: str


@dataclass(slots=True)
class SeedThought:
    description: str
    emotional_impact: int = 0
    emotion_tags: list[str] = field(default_factory=list)
    relational_tags: list[str] = field(default_factory=list)
    created_at_offset_hours: float = -1.0
    filling: list[SeedFillingSpec] = field(default_factory=list)


@dataclass(slots=True)
class FixtureSeed:
    persona_block: str = ""
    user_block: str = ""
    style_block: str = ""
    persona_timezone: str | None = None
    persona_location: str | None = None
    seed_events: list[SeedEvent] = field(default_factory=list)
    seed_thoughts: list[SeedThought] = field(default_factory=list)
    episodic_state_initial: dict[str, Any] | None = None


@dataclass(slots=True)
class FixtureTurn:
    role: str
    content: str


@dataclass(slots=True)
class FixtureRetrieve:
    query: str
    top_k: int = 5
    force_load_user_thoughts: int = 0


@dataclass(slots=True)
class DeleteAction:
    target_description_contains: str
    choice: str  # "ORPHAN" | "CASCADE"


@dataclass(slots=True)
class Fixture:
    fixture_id: str
    version: str
    generated_at: str | None
    model: str | None
    scenario: str
    seed: FixtureSeed
    turns: list[FixtureTurn]
    retrieve: FixtureRetrieve | None
    invariants: dict[str, Any]
    judge_prompts: list[str]
    post_consolidate_actions: list[DeleteAction] = field(default_factory=list)


@dataclass(slots=True)
class EvalResult:
    events: list[dict[str, Any]]
    thoughts: list[dict[str, Any]]
    filling: list[dict[str, Any]]
    retrieved: list[dict[str, Any]]
    reflection_triggered: bool
    entities: list[dict[str, Any]] = field(default_factory=list)
    recall_count: int = 0
    core_block_snapshot_before: dict[str, str] = field(default_factory=dict)
    core_block_snapshot_after: dict[str, str] = field(default_factory=dict)
    episodic_state_before: dict[str, Any] = field(default_factory=dict)
    episodic_state_after: dict[str, Any] = field(default_factory=dict)


def load_fixture(path: Path) -> Fixture:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    seed_raw = raw.get("seed") or {}
    episodic_initial = seed_raw.get("episodic_state_initial")
    seed = FixtureSeed(
        persona_block=seed_raw.get("persona_block", ""),
        user_block=seed_raw.get("user_block", ""),
        style_block=seed_raw.get("style_block", ""),
        persona_timezone=seed_raw.get("persona_timezone"),
        persona_location=seed_raw.get("persona_location"),
        episodic_state_initial=dict(episodic_initial) if episodic_initial else None,
        seed_events=[
            SeedEvent(
                description=e["description"],
                emotional_impact=int(e.get("emotional_impact", 0)),
                emotion_tags=list(e.get("emotion_tags") or []),
                relational_tags=list(e.get("relational_tags") or []),
                created_at_offset_hours=float(
                    e.get("created_at_offset_hours", -1.0)
                ),
            )
            for e in (seed_raw.get("seed_events") or [])
        ],
        seed_thoughts=[
            SeedThought(
                description=t["description"],
                emotional_impact=int(t.get("emotional_impact", 0)),
                emotion_tags=list(t.get("emotion_tags") or []),
                relational_tags=list(t.get("relational_tags") or []),
                created_at_offset_hours=float(
                    t.get("created_at_offset_hours", -1.0)
                ),
                filling=[
                    SeedFillingSpec(
                        parent_description_contains=f["parent_description_contains"],
                    )
                    for f in (t.get("filling") or [])
                ],
            )
            for t in (seed_raw.get("seed_thoughts") or [])
        ],
    )
    turns = [
        FixtureTurn(role=t["role"], content=t["content"])
        for t in (raw.get("turns") or [])
    ]
    retrieve_spec = None
    if raw.get("retrieve"):
        retrieve_spec = FixtureRetrieve(
            query=raw["retrieve"]["query"],
            top_k=int(raw["retrieve"].get("top_k", 5)),
            force_load_user_thoughts=int(
                raw["retrieve"].get("force_load_user_thoughts", 0)
            ),
        )
    actions: list[DeleteAction] = []
    for a in raw.get("post_consolidate_actions") or []:
        if a.get("kind") == "delete":
            actions.append(
                DeleteAction(
                    target_description_contains=a["target_description_contains"],
                    choice=a.get("choice", "ORPHAN").upper(),
                )
            )
    return Fixture(
        fixture_id=raw["fixture_id"],
        version=raw.get("version", "scripted"),
        generated_at=raw.get("generated_at"),
        model=raw.get("model"),
        scenario=raw.get("scenario", ""),
        seed=seed,
        turns=turns,
        retrieve=retrieve_spec,
        invariants=raw.get("invariants") or {},
        judge_prompts=list(raw.get("judge_prompts") or []),
        post_consolidate_actions=actions,
    )


def discover_fixtures() -> list[Path]:
    """Return every ``*.yaml`` under ``fixtures/{scripted,synthesized}/``.

    Sorted for deterministic test IDs.
    """
    out: list[Path] = []
    for sub in ("scripted", "synthesized"):
        out.extend(sorted((FIXTURE_ROOT / sub).glob("*.yaml")))
    return out


__all__ = [
    "FIXTURE_ROOT",
    "DeleteAction",
    "EvalResult",
    "Fixture",
    "FixtureRetrieve",
    "FixtureSeed",
    "FixtureTurn",
    "SeedEvent",
    "SeedFillingSpec",
    "SeedThought",
    "discover_fixtures",
    "load_fixture",
]
