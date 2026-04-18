"""Eval harness · load a fixture, replay it through the real memory
pipeline, and report whether the result satisfies the fixture's
hard invariants.

A single harness serves both ``scripted`` and ``synthesized`` fixture
versions — the pipeline it exercises does not care which author wrote
the YAML.

Call sites:
- :mod:`tests.memory_eval.test_eval_fixtures` parameterizes over every
  file under ``fixtures/scripted/`` and ``fixtures/synthesized/``.
- :mod:`tests.memory_eval.synthesize` runs this harness end-to-end for
  quick self-validation after generating a new synthesized fixture.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import BlockLabel, MessageRole, NodeType
from echovessel.memory import (
    CoreBlock,
    Persona,
    User,
    append_to_core_block,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    SHOCK_IMPACT_THRESHOLD,
    consolidate_session,
)
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import ConceptNode, ConceptNodeFilling, Session
from echovessel.memory.retrieve import retrieve
from echovessel.memory.sessions import mark_session_closing
from echovessel.runtime.config import load_config
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.llm.factory import build_llm_provider
from echovessel.runtime.prompts_wiring import make_extract_fn, make_reflect_fn

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
REAL_CONFIG_PATH = Path.home() / ".echovessel" / "config.toml"


# ---------------------------------------------------------------------------
# Fixture schema
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SeedEvent:
    description: str
    emotional_impact: int = 0
    emotion_tags: list[str] = field(default_factory=list)
    relational_tags: list[str] = field(default_factory=list)
    created_at_offset_hours: float = -1.0


@dataclass(slots=True)
class FixtureSeed:
    persona_block: str = ""
    self_block: str = ""
    user_block: str = ""
    mood_block: str = ""
    relationship_block: str = ""
    seed_events: list[SeedEvent] = field(default_factory=list)


@dataclass(slots=True)
class FixtureTurn:
    role: str
    content: str


@dataclass(slots=True)
class FixtureRetrieve:
    query: str
    top_k: int = 5


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


def load_fixture(path: Path) -> Fixture:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    seed_raw = raw.get("seed") or {}
    seed = FixtureSeed(
        persona_block=seed_raw.get("persona_block", ""),
        self_block=seed_raw.get("self_block", ""),
        user_block=seed_raw.get("user_block", ""),
        mood_block=seed_raw.get("mood_block", ""),
        relationship_block=seed_raw.get("relationship_block", ""),
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
    )


def discover_fixtures() -> list[Path]:
    """Return every ``*.yaml`` under ``fixtures/{scripted,synthesized}/``.

    Sorted for deterministic test IDs.
    """
    out: list[Path] = []
    for sub in ("scripted", "synthesized"):
        out.extend(sorted((FIXTURE_ROOT / sub).glob("*.yaml")))
    return out


# ---------------------------------------------------------------------------
# LLM + embedder wiring
# ---------------------------------------------------------------------------


def build_live_llm() -> LLMProvider:
    """Read ``~/.echovessel/config.toml`` to build the user's daemon
    provider. Callers should ``pytest.skip`` on KeyError/FileNotFound so
    eval tests degrade gracefully on CI or a fresh clone.
    """

    if not REAL_CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"eval needs a real LLM config at {REAL_CONFIG_PATH}"
        )
    cfg = load_config(REAL_CONFIG_PATH)
    if cfg.llm.provider == "stub":
        raise RuntimeError(
            "eval requires a non-stub provider in config.llm.provider"
        )
    if cfg.llm.api_key_env and not os.environ.get(cfg.llm.api_key_env):
        raise RuntimeError(
            f"eval needs {cfg.llm.api_key_env} set in the environment"
        )
    return build_llm_provider(cfg.llm)


def build_eval_embedder():
    """Pick the best embedder available for this eval run.

    Preference order:
      1. ``sentence-transformers`` (real semantic embeddings). First
         call downloads the ~90 MB ``all-MiniLM-L6-v2`` model into
         ``~/.echovessel/embedder.cache/``; subsequent runs are
         instantaneous. This is the same embedder the daemon uses in
         production, so retrieve-relevance invariants are meaningful.
      2. Keyword-axis fallback. Cheap + deterministic, but groups
         tokens by hand-curated axis so synonyms that were not
         enumerated miss each other. Triggered automatically when
         ``sentence-transformers`` is not installed.

    Honours ``ECHOVESSEL_EVAL_EMBEDDER=keyword`` as an override so a
    debugging run can skip the heavy load explicitly.
    """

    if os.environ.get("ECHOVESSEL_EVAL_EMBEDDER") == "keyword":
        return keyword_embedder()[0]

    try:
        from echovessel.runtime import build_sentence_transformers_embedder
    except ImportError:
        return keyword_embedder()[0]

    cache_dir = Path.home() / ".echovessel" / "embedder.cache"
    try:
        return build_sentence_transformers_embedder(
            model_name="all-MiniLM-L6-v2", cache_dir=cache_dir
        )
    except ImportError:
        # ``sentence-transformers`` extra not installed on this host.
        return keyword_embedder()[0]


def keyword_embedder() -> tuple[callable, dict[str, int]]:
    """Return ``(embed_fn, axes)`` — a deterministic keyword-axis
    embedder plus the axis map it uses.

    Used as the fallback when ``sentence-transformers`` is not
    available. Keeps eval runnable on a bare-bones clone without the
    90 MB model download.

    Each keyword gets its own axis; texts that mention multiple
    keywords land in the mean-vector. Unknown texts hash into a
    fallback slot so vectors never collapse to all-zero.
    """

    axes: dict[str, int] = {}
    dim = 384

    keywords = [
        # E1
        "张丽华", "老伴", "丧偶", "过世", "退休", "沈阳", "中学", "语文",
        "Mochi", "mochi", "黑猫", "猫", "领养", "2020",
        # E3
        "妈", "母亲", "走了", "没说",
        # E4
        "28", "32", "成都", "更正", "说错",
        # E5
        "分手", "前任", "新工作", "新城市", "失眠", "朋友",
        # E6
        "意大利面", "羽毛球", "生日", "fintech", "室友", "医院",
        "东京", "吉他",
        # E7
        "难受", "工作", "压", "没人能说", "喘不过气",
        # E8
        "画展", "印象派", "莫奈", "睡莲", "calm",
    ]
    for i, kw in enumerate(keywords):
        axes[kw.lower()] = i % (dim - 16)

    def _embed(text: str) -> list[float]:
        v = [0.0] * dim
        low = text.lower()
        matched = False
        for kw, axis in axes.items():
            if kw in low:
                v[axis] += 1.0
                matched = True
        if not matched:
            v[(abs(hash(text)) % 16) + (dim - 16)] = 1.0
        # normalise
        norm = sum(x * x for x in v) ** 0.5
        if norm > 0:
            v = [x / norm for x in v]
        return v

    return _embed, axes


# ---------------------------------------------------------------------------
# Eval run
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvalResult:
    events: list[dict[str, Any]]
    thoughts: list[dict[str, Any]]
    filling: list[dict[str, Any]]
    mood_block_before: str
    mood_block_after: str
    retrieved: list[dict[str, Any]]
    reflection_triggered: bool


async def run_fixture(fixture: Fixture, *, llm: LLMProvider) -> EvalResult:
    """Materialise the fixture in a fresh SQLite DB, run the pipeline,
    and return a plain-dict summary of everything produced.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    persona_id = "p_eval"
    user_id = "u_eval"

    embed_fn = build_eval_embedder()
    extract_fn = make_extract_fn(llm)
    reflect_fn = make_reflect_fn(llm)

    # 1. seed persona + user + core blocks
    with DbSession(engine) as db:
        db.add(Persona(id=persona_id, display_name="Eval"))
        db.add(User(id=user_id, display_name="User"))
        db.commit()

        for label, content in [
            (BlockLabel.PERSONA, fixture.seed.persona_block),
            (BlockLabel.SELF, fixture.seed.self_block),
            (BlockLabel.USER, fixture.seed.user_block),
            (BlockLabel.MOOD, fixture.seed.mood_block),
            (BlockLabel.RELATIONSHIP, fixture.seed.relationship_block),
        ]:
            if not content:
                continue
            row_user_id = None if label in (
                BlockLabel.PERSONA, BlockLabel.SELF, BlockLabel.MOOD,
            ) else user_id
            append_to_core_block(
                db,
                persona_id=persona_id,
                user_id=row_user_id,
                label=label.value,
                content=content,
                provenance={"source": "eval_seed"},
            )

        now = datetime.now()

        # 2. pre-seed events (E5 / E6)
        for se in fixture.seed.seed_events:
            created = now + timedelta(hours=se.created_at_offset_hours)
            node = ConceptNode(
                persona_id=persona_id,
                user_id=user_id,
                type=NodeType.EVENT,
                description=se.description,
                emotional_impact=se.emotional_impact,
                emotion_tags=se.emotion_tags,
                relational_tags=se.relational_tags,
                created_at=created,
            )
            db.add(node)
            db.commit()
            db.refresh(node)
            backend.insert_vector(node.id, embed_fn(se.description))

    mood_before = _read_mood(engine, persona_id)

    # 3. ingest turns
    session_id: str | None = None
    with DbSession(engine) as db:
        for turn in fixture.turns:
            role = MessageRole.USER if turn.role == "user" else MessageRole.PERSONA
            result = ingest_message(
                db,
                persona_id,
                user_id,
                "web",
                role,
                turn.content,
            )
            session_id = result.session.id
        db.commit()

    reflection_triggered = False

    # 4. consolidate: if we have turns, close the session + consolidate
    if session_id is not None and fixture.turns:
        with DbSession(engine) as db:
            sess = db.get(Session, session_id)
            assert sess is not None
            mark_session_closing(db, sess, trigger="eval")
            db.add(sess)
            db.commit()

        with DbSession(engine) as db:
            sess = db.get(Session, session_id)
            assert sess is not None
            cons = await consolidate_session(
                db=db,
                backend=backend,
                session=sess,
                extract_fn=extract_fn,
                reflect_fn=reflect_fn,
                embed_fn=embed_fn,
            )
        reflection_triggered = cons.reflection_reason is not None

    mood_after = _read_mood(engine, persona_id)

    # 5. retrieve (E6)
    retrieved: list[dict[str, Any]] = []
    if fixture.retrieve is not None:
        with DbSession(engine) as db:
            r = retrieve(
                db=db,
                backend=backend,
                persona_id=persona_id,
                user_id=user_id,
                query_text=fixture.retrieve.query,
                embed_fn=embed_fn,
                top_k=fixture.retrieve.top_k,
            )
        retrieved = [
            {"id": m.node.id, "description": m.node.description, "relevance": m.relevance}
            for m in r.memories
        ]

    # 6. collect everything we just wrote
    with DbSession(engine) as db:
        events = [
            _serialise_node(n)
            for n in db.exec(
                select(ConceptNode).where(
                    ConceptNode.persona_id == persona_id,
                    ConceptNode.type == NodeType.EVENT.value,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        ]
        # Only the events created by THIS consolidate pass — seeded events
        # have no source_session_id (we inserted them directly).
        events_from_session = [
            e for e in events if e["source_session_id"] == session_id
        ]
        thoughts = [
            _serialise_node(n)
            for n in db.exec(
                select(ConceptNode).where(
                    ConceptNode.persona_id == persona_id,
                    ConceptNode.type == NodeType.THOUGHT.value,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        ]
        filling = [
            {"parent_id": r.parent_id, "child_id": r.child_id, "orphaned": r.orphaned}
            for r in db.exec(select(ConceptNodeFilling))
        ]

    return EvalResult(
        events=events_from_session if fixture.turns else events,
        thoughts=thoughts,
        filling=filling,
        mood_block_before=mood_before,
        mood_block_after=mood_after,
        retrieved=retrieved,
        reflection_triggered=reflection_triggered,
    )


def _read_mood(engine, persona_id: str) -> str:
    with DbSession(engine) as db:
        row = db.exec(
            select(CoreBlock).where(
                CoreBlock.persona_id == persona_id,
                CoreBlock.label == BlockLabel.MOOD,
                CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
    return row.content if row else ""


def _serialise_node(n: ConceptNode) -> dict[str, Any]:
    t = getattr(n.type, "value", n.type)
    return {
        "id": n.id,
        "type": t,
        "description": n.description,
        "emotional_impact": int(n.emotional_impact),
        "emotion_tags": list(n.emotion_tags or []),
        "relational_tags": list(n.relational_tags or []),
        "source_session_id": n.source_session_id,
    }


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------


def check_invariants(fixture: Fixture, result: EvalResult) -> list[str]:
    """Return a list of human-readable invariant violations. Empty =
    all hard invariants passed.
    """
    inv = fixture.invariants
    violations: list[str] = []

    # Event count bounds — use the subset produced by the session when
    # turns exist; for retrieval-only fixtures fall back to total events.
    n_events = len(result.events)
    if inv.get("events_min") is not None and n_events < inv["events_min"]:
        violations.append(f"events_min {inv['events_min']} > produced {n_events}")
    if inv.get("events_max") is not None and n_events > inv["events_max"]:
        violations.append(f"events_max {inv['events_max']} < produced {n_events}")

    n_thoughts = len(result.thoughts)
    if inv.get("thoughts_min") is not None and n_thoughts < inv["thoughts_min"]:
        violations.append(
            f"thoughts_min {inv['thoughts_min']} > produced {n_thoughts}"
        )

    if inv.get("shock_event_present"):
        shocks = [
            e for e in result.events
            if abs(e["emotional_impact"]) >= SHOCK_IMPACT_THRESHOLD
        ]
        if not shocks:
            violations.append("shock_event_present: no |impact|>=8 event produced")

    if inv.get("reflection_triggered") and not result.reflection_triggered:
        violations.append("reflection_triggered: reflect_fn was never called")

    if (
        inv.get("mood_block_changed")
        and result.mood_block_after == result.mood_block_before
    ):
        violations.append(
            f"mood_block_changed: mood stayed {result.mood_block_before!r}"
        )

    if inv.get("must_mention_any"):
        wanted = inv["must_mention_any"]
        all_text = " ".join(e["description"] for e in result.events)
        if not any(w in all_text for w in wanted):
            violations.append(
                f"must_mention_any: no event mentions any of {wanted}"
            )

    if inv.get("must_have_relational_tag_any"):
        wanted = set(inv["must_have_relational_tag_any"])
        got = set()
        for e in result.events:
            got.update(e["relational_tags"])
        if not wanted & got:
            violations.append(
                f"must_have_relational_tag_any: wanted any of {sorted(wanted)} "
                f"· got {sorted(got)}"
            )

    if inv.get("filling_min") is not None:
        # Any SINGLE thought must cite at least ``filling_min`` events.
        by_parent: dict[int, int] = {}
        for r in result.filling:
            by_parent[r["parent_id"]] = by_parent.get(r["parent_id"], 0) + 1
        top = max(by_parent.values(), default=0)
        if top < inv["filling_min"]:
            violations.append(
                f"filling_min {inv['filling_min']} > largest chain {top}"
            )

    if inv.get("top3_relevant_min") is not None:
        must = inv.get("top3_description_contains_any") or []
        top3 = result.retrieved[:3]
        n_rel = sum(1 for m in top3 if any(tok in m["description"] for tok in must))
        if n_rel < inv["top3_relevant_min"]:
            violations.append(
                f"top3_relevant_min {inv['top3_relevant_min']} > matched {n_rel} "
                f"(top3: {[m['description'] for m in top3]})"
            )

    if inv.get("output_language") == "zh":
        # A quick heuristic: at least half of all event text must be CJK.
        all_text = "".join(e["description"] for e in result.events)
        if not all_text:
            violations.append("output_language=zh: no event descriptions to check")
        else:
            cjk = sum(1 for ch in all_text if "\u4e00" <= ch <= "\u9fff")
            if cjk * 2 < len(all_text):
                violations.append(
                    f"output_language=zh: CJK ratio {cjk}/{len(all_text)} below 50%"
                )

    return violations


def render_evidence(fixture: Fixture, result: EvalResult) -> str:
    """Render the result as a compact string the judge LLM can read."""
    lines = [
        f"Fixture: {fixture.fixture_id} ({fixture.version})",
        f"Scenario: {fixture.scenario}",
        "",
        "--- Persona block ---",
        fixture.seed.persona_block,
        "",
        "--- Turns ---",
    ]
    for t in fixture.turns:
        lines.append(f"{t.role}: {t.content}")
    lines.append("")
    lines.append(f"--- Extracted events ({len(result.events)}) ---")
    for e in result.events:
        lines.append(
            f"  impact={e['emotional_impact']:+d} rel_tags={e['relational_tags']} · "
            f"{e['description']}"
        )
    lines.append("")
    lines.append(f"--- Thoughts ({len(result.thoughts)}) ---")
    for t in result.thoughts:
        lines.append(
            f"  impact={t['emotional_impact']:+d} rel_tags={t['relational_tags']} · "
            f"{t['description']}"
        )
    if result.retrieved:
        lines.append("")
        lines.append(f"--- Retrieved (top {len(result.retrieved)}) ---")
        for m in result.retrieved:
            lines.append(
                f"  rel={m['relevance']:.2f} · {m['description']}"
            )
    lines.append("")
    lines.append("--- Mood ---")
    lines.append(f"before: {result.mood_block_before!r}")
    lines.append(f"after:  {result.mood_block_after!r}")
    return "\n".join(lines)


__all__ = [
    "FIXTURE_ROOT",
    "Fixture",
    "FixtureSeed",
    "FixtureTurn",
    "FixtureRetrieve",
    "SeedEvent",
    "EvalResult",
    "build_live_llm",
    "check_invariants",
    "discover_fixtures",
    "keyword_embedder",
    "load_fixture",
    "render_evidence",
    "run_fixture",
]
