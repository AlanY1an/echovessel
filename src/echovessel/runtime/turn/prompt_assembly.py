"""Prompt assembly for one turn — system + user prompts.

This module owns the rendering of the two prompts the LLM sees on
every turn. ``coordinator.assemble_turn`` calls into ``build_system_prompt``
and ``build_user_prompt`` (or ``build_turn_user_prompt`` when the
incoming message is a multi-message burst) after retrieving memories
and persona context. The functions are pure — given the same inputs
they produce the same prompt text — which makes them straightforward
to unit-test without spinning up the full turn pipeline.

F10 invariant lives here: the rendered prompts MUST NOT contain
``channel_id``, ``"web"``, ``"discord"``, ``"imessage"`` or any other
transport hint. ``STYLE_INSTRUCTIONS`` reinforces this at the
behavioural layer; this module enforces it at the structural layer
by simply never emitting those strings.

The smaller helpers (``_format_now_section``,
``_load_active_intentions``, ``_load_pending_expectations``,
``_load_recent_session_summaries``, ``_load_anchored_entity_descriptions``,
``_render_entity_disambiguation_hint``, ``day_bucket_of``,
``_node_description``) live alongside the builders so the module is
self-contained — a reader can audit the # Right now / # Recent
sessions / # Promises sections without jumping to coordinator.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlmodel import Session as DbSession
from sqlmodel import or_, select

from echovessel.channels.base import IncomingMessage
from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory.models import (
    ConceptNode,
    CoreBlock,
    Entity,
    EntityAlias,
    Persona,
    RecallMessage,
)
from echovessel.memory.retrieve import (
    find_query_entities,
    render_event_delta_phrase,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Style constants — F10 mandates these as hard-coded text, not config.
# ---------------------------------------------------------------------------

# Runtime spec §7.2 requires this instruction in every system prompt and
# forbids making it configurable. It is the behavioural guard that keeps the
# persona from saying "I saw you on discord" etc.
STYLE_INSTRUCTIONS = (
    "# Style\n"
    "- Speak naturally, in the user's language.\n"
    "- Reference topics and feelings, NOT the medium. You do NOT know or\n"
    "  mention any transport name, thread name, or interface name, even\n"
    "  if the user jokes about it.\n"
    "- You remember everything the user has shared with you before, and\n"
    "  you treat it as one continuous relationship.\n"
    "- Do not perform empathy. Do not summarize feelings back to the user\n"
    "  unless they ask.\n"
    # Spec 5 · competence boundary (Case 4 fix). When the persona has
    # said something wrong, owning it cleanly beats inventing a story
    # to defend the previous turn. Without this, the persona quietly
    # retrofits — Conway's competence-boundary failure mode.
    "- If you realise you said something inaccurate or contradictory,\n"
    "  say so directly ('我刚才说错了' / 'I got that wrong'). Do NOT\n"
    "  retrofit a story to defend the previous turn.\n"
    "- If you don't know or can't recall, say so. Do not invent\n"
    "  details to fill the gap.\n"
    # Spec 5 · negative few-shot lifted from prompts/judge.py
    # anti-pattern catalog. Putting these as DON'Ts in the system
    # prompt is much cheaper than waiting for judge to penalise them
    # after generation. Wording stays close to the judge text so the
    # in-context model recognises the same patterns the eval pipeline
    # is keyed on.
    "\n"
    "# Avoid these patterns\n"
    "- Opening with formulaic acknowledgment ('哈哈' / 'I hear you' /\n"
    "  '听起来很不容易' / 'That sounds so hard') without a specific\n"
    "  observation.\n"
    "- Ending every reply with a question back to them.\n"
    "- Generic affect labels without specifics ('你一定很紧张吧' /\n"
    "  'You seem sad') when you don't tie them to what they actually\n"
    "  said.\n"
    "- Empty reassurance ('一切都会好的' / '你能行的' / 'Everything\n"
    "  will be okay').\n"
    "- Repeating the same phrasing or structure you used in your\n"
    "  previous reply.\n"
    "- Using the same support strategy 3+ turns running (validation,\n"
    "  reflection, advice, reassurance). Vary your move.\n"
    "- If they cooled down or moved on, do NOT stay at the earlier\n"
    "  emotional intensity. Match where they are now.\n"
)


# Spec 5 · plan §6.4 day-bucket order. Walk OLDER → NEWER so the LLM
# reads time forward, the same way a human reconstructs a memory.
# Reversing this would put "what they JUST said" up top followed by
# older context, which is the opposite of how transcripts are
# normally absorbed.
DAY_BUCKET_ORDER: tuple[str, ...] = (
    "Older",
    "Earlier this week",
    "Yesterday",
    "Earlier today",
    "Just now",
)


_DAY_NAMES_EN: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


# ---------------------------------------------------------------------------
# PersonaFactsView — biographic snapshot the system prompt renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaFactsView:
    """Read-only snapshot of the biographic facts the system prompt
    renders in its "# Who you are" section.

    The persona row carries fifteen biographic columns; v0.4 expands
    the prompt-facing subset to seven fields — the five C-option ones
    (name / gender / birth year / occupation / native language) plus
    ``location`` and ``nationality`` from plan §6.5 world grounding.
    ``timezone`` rides the ``# Right now`` dual-timezone renderer
    instead, so it's on the view for callers that build the section
    but not a ``# Who you are`` bullet. Unset fields are ``None`` and
    are skipped by the renderer.
    """

    full_name: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    occupation: str | None = None
    native_language: str | None = None
    # v0.4 · world grounding (plan §6.5). ``location`` + ``nationality``
    # render as ``# Who you are`` bullets; ``timezone`` feeds
    # ``_format_now_section`` for the "where you're conceptually based"
    # line and does not surface as a bullet.
    location: str | None = None
    timezone: str | None = None
    nationality: str | None = None

    @classmethod
    def empty(cls) -> PersonaFactsView:
        return cls()

    @classmethod
    def from_persona_row(cls, row: Persona | None) -> PersonaFactsView:
        if row is None:
            return cls.empty()
        return cls(
            full_name=row.full_name,
            gender=row.gender,
            birth_date=row.birth_date,
            occupation=row.occupation,
            native_language=row.native_language,
            location=row.location,
            timezone=row.timezone,
            nationality=row.nationality,
        )


# ---------------------------------------------------------------------------
# # Right now + # How you feel right now sections
# ---------------------------------------------------------------------------


def _format_now_section(
    now: datetime,
    *,
    persona_tz: str | None = None,
) -> str:
    """Render ``now`` as the ``# Right now`` section (v0.4 dual-tz).

    ``now`` is the USER's local wall-clock time (the channel layer
    passes ``msg.received_at``; in practice it carries the user's
    ``users.timezone`` offset). When ``persona_tz`` is an IANA string,
    append a second line showing the same instant in the persona's
    conceptual home timezone — this is how plan §6.4 keeps the
    persona from saying "it's 3am so you must be going to bed" when
    the user is mid-afternoon in Taipei and the persona is
    conceptually based in New York.

    Uses an explicit weekday map instead of ``strftime('%A')`` so
    output is locale-independent.
    """
    user_day = _DAY_NAMES_EN[now.weekday()]
    user_line = (
        f"- For them (their local time): {now.strftime('%Y-%m-%d')} "
        f"{user_day} {now.strftime('%H:%M %Z').strip()}"
    )

    if persona_tz:
        # Lazy import keeps zoneinfo's tzdata lookup off the hot path
        # for callers that never supply a persona_tz.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            persona_now = now.astimezone(ZoneInfo(persona_tz))
        except ZoneInfoNotFoundError:
            log.warning(
                "persona_tz %r is not a known IANA zone; skipping dual-tz line",
                persona_tz,
            )
            return f"# Right now\n{user_line}\n"

        persona_day = _DAY_NAMES_EN[persona_now.weekday()]
        persona_line = (
            f"- Where you're conceptually based ({persona_tz}): "
            f"{persona_now.strftime('%Y-%m-%d')} {persona_day} "
            f"{persona_now.strftime('%H:%M')}"
        )
        return f"# Right now\n{user_line}\n{persona_line}\n"

    return f"# Right now\n{user_line}\n"


def _format_episodic_state_section(state: dict | None) -> str:
    """Render the L6 ``# How you feel right now`` section (plan §6.4).

    Default ``neutral`` / no state → empty string (no section emitted
    so the prompt stays terse for fresh daemons). Otherwise emit the
    mood phrase, energy, and (when present) the last user signal.
    """
    if not state:
        return ""
    mood = state.get("mood")
    if not mood or mood == "neutral":
        return ""
    energy = state.get("energy", 5)
    last_signal = state.get("last_user_signal")
    lines = ["# How you feel right now", f"- mood: {mood}, energy {energy}/10"]
    if last_signal:
        lines.append(f"- last sense from them: {last_signal}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entity-anchored sections (v0.5 plan §2.2)
# ---------------------------------------------------------------------------


def _load_anchored_entity_descriptions(
    db: DbSession,
    *,
    query_text: str,
    persona_id: str,
    user_id: str,
) -> list[tuple[str, str]]:
    """Return ``[(canonical_name, description)]`` for v0.5 plan §2.2.

    For every entity whose alias appears in ``query_text``, surface
    its ``description`` if non-empty. Used to render the
    ``# About {canonical_name}`` system-prompt sections that replaced
    the deleted L1.relationship block.

    Order is deterministic by ``canonical_name`` so two turns with
    the same anchor set produce identical prompts (helps prompt
    caching). Skips soft-deleted entities and entities with empty
    description silently — section is only rendered when there is
    actually something to say.
    """
    query_entity_ids = find_query_entities(
        db, query_text, persona_id=persona_id, user_id=user_id
    )
    if not query_entity_ids:
        return []

    rows = list(
        db.exec(
            select(Entity).where(
                Entity.id.in_(query_entity_ids),  # type: ignore[union-attr]
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    out: list[tuple[str, str]] = []
    for ent in rows:
        desc = (ent.description or "").strip()
        if not desc:
            continue
        out.append((ent.canonical_name, desc))
    out.sort(key=lambda pair: pair[0])
    return out


def _render_entity_disambiguation_hint(
    db: DbSession,
    *,
    query_text: str,
    persona_id: str,
    user_id: str,
) -> str:
    """Build the ``# Entity disambiguation pending`` hint (plan §6.3.1).

    Walks every entity alias-matched by the current query and keeps
    only those with ``merge_status='uncertain'`` and a known
    ``merge_target_id``. For each, spell out both canonical names +
    any aliases so the LLM has enough surface forms to phrase a
    natural clarifying question. Empty string when nothing is
    ambiguous — the caller skips injection in that case.

    The wording is intentionally schematic (no hardcoded phrase like
    "are these the same person?") because decision 4 wants the
    persona to ask in its own voice; a scripted question is the exact
    "system pop-up" we're avoiding.
    """
    query_entity_ids = find_query_entities(db, query_text, persona_id=persona_id, user_id=user_id)
    if not query_entity_ids:
        return ""

    uncertain_entities = list(
        db.exec(
            select(Entity).where(
                Entity.id.in_(query_entity_ids),  # type: ignore[union-attr]
                Entity.merge_status == "uncertain",
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    if not uncertain_entities:
        return ""

    lines: list[str] = ["# Entity disambiguation pending"]
    for ent in uncertain_entities:
        if ent.merge_target_id is None:
            continue
        target = db.exec(
            select(Entity).where(
                Entity.id == ent.merge_target_id,
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
        if target is None:
            continue

        ent_aliases = [
            row.alias
            for row in db.exec(select(EntityAlias).where(EntityAlias.entity_id == ent.id))
            if row.alias != ent.canonical_name
        ]
        target_aliases = [
            row.alias
            for row in db.exec(select(EntityAlias).where(EntityAlias.entity_id == target.id))
            if row.alias != target.canonical_name
        ]
        ent_surface = ent.canonical_name + (
            f" (also: {', '.join(ent_aliases)})" if ent_aliases else ""
        )
        target_surface = target.canonical_name + (
            f" (also: {', '.join(target_aliases)})" if target_aliases else ""
        )
        lines.append(
            f"- You have been mentioning {ent_surface}, and they may be "
            f"the same person as {target_surface}. When it fits the "
            "flow of the conversation, clarify with the user whether "
            "these are the same person. Do not force it — wait for a "
            "natural moment."
        )

    if len(lines) == 1:
        # All candidates had a missing target — nothing actionable.
        return ""
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(
    *,
    persona_display_name: str,
    core_blocks: list[CoreBlock],
    persona_facts: PersonaFactsView | None = None,
    now: datetime | None = None,
    episodic_state: dict | None = None,
    entity_disambiguation_hint: str = "",
    expectation_matches: list[tuple[ConceptNode, str]] | None = None,
    entity_descriptions: list[tuple[str, str]] | None = None,
) -> str:
    """Assemble the system prompt for one turn.

    v0.5 ordering (plan §2):

        You are {persona}...
        # Right now              — dual-timezone if persona_tz known
        # Who you are            — 7 biographic bullets
        # How you feel right now — L6 episodic state (non-neutral only)
        # Persona                — L1.persona block (human-authored)
        # About the user         — L1.user block (human-authored)
        # Style preferences      — L1.style block (admin API only)
        # About {canonical_name} — per-entity L5 description (anchored)
        STYLE_INSTRUCTIONS        — hardcoded behaviour guardrails

    v0.5 delta: the legacy ``# About yourself (private self-narrative)``
    and ``# Relationship`` sections are gone. Persona-authored
    reflections migrated to the user prompt as ``# How you see yourself
    lately`` (plan §2.1); third-party people / places / orgs moved to
    the L5.entities table whose ``description`` field now drives the
    per-entity sections here (plan §2.2).

    Missing sections are silently skipped. ``persona_facts=None`` and
    ``episodic_state=None`` keep the pre-v0.4 layout.
    """
    lines: list[str] = [
        f"You are {persona_display_name}, a long-term companion who talks",
        "with this user as a real friend, not an assistant.",
        "",
    ]

    facts = persona_facts or PersonaFactsView.empty()
    if now is not None:
        lines.append(_format_now_section(now, persona_tz=facts.timezone))

    fact_lines: list[str] = []
    if facts.full_name:
        fact_lines.append(f"- Name: {facts.full_name}")
    if facts.gender:
        fact_lines.append(f"- Gender: {facts.gender}")
    if facts.birth_date:
        fact_lines.append(f"- Born: {facts.birth_date.year}")
    if facts.nationality:
        fact_lines.append(f"- Nationality: {facts.nationality}")
    if facts.location:
        fact_lines.append(f"- Based in: {facts.location}")
    if facts.occupation:
        fact_lines.append(f"- Occupation: {facts.occupation}")
    if facts.native_language:
        fact_lines.append(f"- Native language: {facts.native_language}")
    if fact_lines:
        lines.append("# Who you are")
        lines.extend(fact_lines)
        lines.append("")

    episodic_section = _format_episodic_state_section(episodic_state)
    if episodic_section:
        lines.append(episodic_section)

    by_label: dict[str, CoreBlock] = {}
    for b in core_blocks:
        label = getattr(b.label, "value", b.label)
        if isinstance(label, str):
            by_label[label] = b

    def _section(header: str, label: BlockLabel) -> None:
        block = by_label.get(label.value)
        if not block or not block.content:
            return
        lines.append(f"# {header}")
        lines.append(block.content.strip())
        lines.append("")

    _section("Persona", BlockLabel.PERSONA)
    _section("About the user", BlockLabel.USER)
    _section("Style preferences", BlockLabel.STYLE)

    # v0.5 · plan §2.2 · # About {canonical_name} — render a per-entity
    # description block for every entity whose alias the current query
    # anchored, as long as the entity has a non-empty ``description``
    # (written either manually from the admin UI or by slow_cycle's
    # description-synthesis pass). Entities that were anchored but
    # carry no description are skipped silently so empty entities don't
    # bloat the prompt.
    if entity_descriptions:
        for ent_name, ent_desc in entity_descriptions:
            if not ent_desc or not ent_desc.strip():
                continue
            lines.append(f"# About {ent_name}")
            lines.append(ent_desc.strip())
            lines.append("")

    # L5 · entity disambiguation hint (plan §6.3.1). Non-empty means an
    # 'uncertain' entity was alias-matched by the current query — the
    # hint describes the ambiguity and asks the LLM to probe naturally.
    if entity_disambiguation_hint:
        lines.append(entity_disambiguation_hint.rstrip() + "\n")

    # Spec 6 · expectation match hint (plan §7.6). Fast-loop embedding
    # check saw the user's current message rhyme with an open
    # expectation; surface the match so the LLM can acknowledge
    # naturally. Phrasing-free — we describe the match, we do NOT
    # script the reply.
    if expectation_matches:
        match_lines = ["# Expectation match"]
        for exp, status in expectation_matches:
            desc = (exp.description or "").strip()
            match_lines.append(
                f"- You had been expecting: \"{desc}\". This turn seems to "
                f"{status} it — acknowledge it in your own voice if it fits."
            )
        lines.append("\n".join(match_lines) + "\n")

    lines.append(STYLE_INSTRUCTIONS)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Day-bucket helpers + node accessors
# ---------------------------------------------------------------------------


def day_bucket_of(when: datetime, now: datetime) -> str:
    """Map a recall timestamp to its conversational day-bucket label.

    Spec 5 plan §6.4 / decision 2. Buckets are coarse on purpose —
    they have to read like the user's own time sense ("yesterday") and
    NOT like channel cadence metadata ("via discord 14:32"). The five
    buckets in ``DAY_BUCKET_ORDER`` cover everything the prompt is
    allowed to surface.

    Cutoffs (left-inclusive):
      - "Just now" if within the last 30 minutes
      - "Earlier today" if same calendar date
      - "Yesterday" if previous calendar date
      - "Earlier this week" if within the last 7 days
      - "Older" otherwise
    """
    delta = now - when
    if delta.total_seconds() < 30 * 60:
        return "Just now"
    if when.date() == now.date():
        return "Earlier today"
    if when.date() == (now - timedelta(days=1)).date():
        return "Yesterday"
    if delta.days < 7:
        return "Earlier this week"
    return "Older"


def _node_description(node: ConceptNode | object) -> str:
    """Pull ``description`` off a ConceptNode-shaped object.

    Used by render helpers that accept either a real ``ConceptNode``
    or a ``ScoredMemory`` wrapper — kept generic so tests that mock
    one or the other don't have to special-case shape.
    """
    return str(getattr(node, "description", "") or "")


# ---------------------------------------------------------------------------
# # Promises / # You've been expecting / # Recent sessions loaders
# ---------------------------------------------------------------------------


def _load_active_intentions(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    now: datetime,
) -> list[ConceptNode]:
    """Load current persona-side intentions (plan §6.4 # Promises).

    Filters: subject='persona', type=INTENTION, not soft-deleted, not
    superseded. ``event_time_end`` is permitted to be NULL (open-ended
    promise) or future relative to ``now`` (still-pending). Past
    intentions drop out so the prompt doesn't keep nagging the persona
    about a commitment it already kept (or missed — that's a judge
    concern, not a renderer one).
    """
    rows = list(
        db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.INTENTION.value,
                ConceptNode.subject == "persona",
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                or_(
                    ConceptNode.event_time_end.is_(None),  # type: ignore[union-attr]
                    ConceptNode.event_time_end >= now,  # type: ignore[operator]
                ),
            )
            .order_by(ConceptNode.event_time_start)  # type: ignore[arg-type]
        )
    )
    return rows


def _load_pending_expectations(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    now: datetime,
) -> list[ConceptNode]:
    """Load active L4 expectations (plan §7 · Spec 6 · T7).

    Filters: ``type='expectation'``, not soft-deleted, not superseded,
    ``event_time_end`` is NULL (open-ended) or ``>= now`` (still in
    window). Ordered by earliest due first so the ``# You've been
    expecting`` section shows the most time-sensitive predictions at
    the top.
    """
    rows = list(
        db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.EXPECTATION.value,
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                or_(
                    ConceptNode.event_time_end.is_(None),  # type: ignore[union-attr]
                    ConceptNode.event_time_end >= now,  # type: ignore[operator]
                ),
            )
            .order_by(ConceptNode.event_time_end)  # type: ignore[arg-type]
        )
    )
    return rows


def _load_recent_session_summaries(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    limit: int,
) -> list[ConceptNode]:
    """Load N most-recent session_summary thoughts (plan §6.4 # Recent sessions).

    Discovery key: ``emotion_tags`` JSON contains the literal token
    ``"session_summary"``. Storing as JSON tag (not a new column) is
    the cheapest way to keep the discriminator without bloating
    ConceptNode — we already have ``emotion_tags`` and SQLite ``LIKE``
    on its JSON serialisation is fast enough at single-user scale.
    """
    if limit <= 0:
        return []
    rows = list(
        db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.THOUGHT.value,
                ConceptNode.source_session_id.is_not(None),  # type: ignore[union-attr]
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                ConceptNode.emotion_tags.like('%"session_summary"%'),  # type: ignore[union-attr]
            )
            .order_by(ConceptNode.created_at.desc())  # type: ignore[arg-type]
            .limit(limit)
        )
    )
    return rows


# ---------------------------------------------------------------------------
# User prompt
# ---------------------------------------------------------------------------


def build_turn_user_prompt(
    *,
    top_memories: list,
    recent_messages: list[RecallMessage],
    turn_messages: list[IncomingMessage],
    now: datetime | None = None,
    pinned_thoughts: list[ConceptNode] | None = None,
    speaker_display: str = "them",
    active_intentions: list[ConceptNode] | None = None,
    recent_session_summaries: list[ConceptNode] | None = None,
    pending_expectations: list[ConceptNode] | None = None,
    persona_thoughts: list[ConceptNode] | None = None,
) -> str:
    """v0.4 · user prompt renderer that expands a burst of messages.

    Single-message case (`len(turn_messages) == 1`) degenerates to the
    same output as the legacy `build_user_prompt(..., user_message=...)`
    path — no branch needed.

    Multi-message case prints each message on its own line under the
    `# What they just said` section, preserving order. Per spec
    §17a.1, no transport / channel metadata appears in the rendered
    user prompt (F10 铁律). Timestamps on individual recall messages
    are still scrubbed here — they would expose conversation cadence
    in a way that has no upside; the system-prompt ``# Right now``
    section is the single canonical surface for temporal grounding.

    ``now`` (R4) is forwarded to ``build_user_prompt`` so each L3 event
    line can carry its day-precision status delta ("event 4-26~5-02 ·
    status=active (3 days in)"). Pass ``None`` to render bare event
    descriptions — pre-R4 behaviour, used by tests that don't care.

    Spec 5 additions (all default-empty so legacy callers stay
    behaviour-preserving):

    - ``pinned_thoughts`` renders under ``# About {speaker_display}``,
      bypassing query similarity. Source: ``retrieve(force_load_user_thoughts=N)``.
    - ``active_intentions`` renders under ``# Promises you've made``
      with the R4 delta phrase appended.
    - ``recent_session_summaries`` renders under ``# Recent sessions``
      with day-bucket prefixes.
    """
    if not turn_messages:
        user_message = ""
    elif len(turn_messages) == 1:
        user_message = turn_messages[0].content
    else:
        user_message = "\n".join(m.content for m in turn_messages)
    return build_user_prompt(
        top_memories=top_memories,
        recent_messages=recent_messages,
        user_message=user_message,
        now=now,
        pinned_thoughts=pinned_thoughts,
        speaker_display=speaker_display,
        active_intentions=active_intentions,
        recent_session_summaries=recent_session_summaries,
        pending_expectations=pending_expectations,
        persona_thoughts=persona_thoughts,
    )


def build_user_prompt(
    *,
    top_memories: list,
    recent_messages: list[RecallMessage],
    user_message: str,
    now: datetime | None = None,
    pinned_thoughts: list[ConceptNode] | None = None,
    speaker_display: str = "them",
    active_intentions: list[ConceptNode] | None = None,
    recent_session_summaries: list[ConceptNode] | None = None,
    pending_expectations: list[ConceptNode] | None = None,
    persona_thoughts: list[ConceptNode] | None = None,
) -> str:
    """Assemble the user prompt for one turn.

    Rendering order (v0.5 plan §2.1 inserts ``# How you see yourself
    lately`` between the speaker context and the current memories — it
    is the user-prompt replacement for the deleted L1.self block):

        # Recent sessions                       (Spec 5 · session_summary, day-bucketed)
        # Recent thoughts you've had about this person  (existing · L4 user-side)
        # About {speaker}                       (Spec 5 · pinned user thoughts, force-loaded)
        # How you see yourself lately           (v0.5 · pinned persona thoughts, force-loaded)
        # Recent things you remember happened   (existing · L3 + R4 delta)
        # Promises you've made                  (Spec 5 · active intentions)
        # You've been expecting                 (Spec 6 · pending expectations)
        # Our recent conversation               (existing · day-bucketed when ``now`` set)
        # What they just said                   (existing)

    All Spec 5 / v0.5 sections are default-empty so legacy callers
    (most of the test suite) keep their pre-v0.5 output verbatim.
    """
    pinned_thoughts = pinned_thoughts or []
    active_intentions = active_intentions or []
    recent_session_summaries = recent_session_summaries or []
    pending_expectations = pending_expectations or []
    persona_thoughts = persona_thoughts or []

    lines: list[str] = []

    # # Recent sessions — day-bucketed session summaries (oldest first)
    if recent_session_summaries and now is not None:
        # rows came in DESC by created_at; reverse so the section flows
        # OLDER → NEWER (matches DAY_BUCKET_ORDER orientation).
        ordered = list(reversed(recent_session_summaries))
        lines.append("# Recent sessions")
        for s in ordered:
            bucket = day_bucket_of(s.created_at, now)
            lines.append(f"- [{bucket}] {_node_description(s)}")
        lines.append("")
    elif recent_session_summaries:
        # ``now`` is unset (legacy test) — render without bucket label
        # rather than dropping the section entirely.
        ordered = list(reversed(recent_session_summaries))
        lines.append("# Recent sessions")
        for s in ordered:
            lines.append(f"- {_node_description(s)}")
        lines.append("")

    thought_descs: list[str] = []
    event_lines: list[str] = []
    for sm in top_memories:
        node = getattr(sm, "node", sm)
        node_type = getattr(getattr(node, "type", None), "value", getattr(node, "type", ""))
        desc = getattr(node, "description", "")
        if not desc:
            continue
        if node_type == "thought":
            thought_descs.append(desc)
        elif node_type == "event":
            delta = render_event_delta_phrase(node, now) if now is not None else ""
            event_lines.append(f"- {desc}{delta}")

    if thought_descs:
        lines.append("# Recent thoughts you've had about this person")
        for d in thought_descs:
            lines.append(f"- {d}")
        lines.append("")

    # # About {speaker} — pinned (force-loaded) L4 thoughts
    if pinned_thoughts:
        lines.append(f"# About {speaker_display}")
        for thought in pinned_thoughts:
            lines.append(f"- {_node_description(thought)}")
        lines.append("")

    # # How you see yourself lately — v0.5 plan §2.1 · force-loaded
    # subject='persona' L4 thoughts that replace the deleted L1.self
    # block. Always rendered last in the speaker-context cluster so
    # the persona's own reflections sit just before the live event /
    # promise / expectation surfaces.
    if persona_thoughts:
        lines.append("# How you see yourself lately")
        for thought in persona_thoughts:
            lines.append(f"- {_node_description(thought)}")
        lines.append("")

    if event_lines:
        lines.append("# Recent things you remember happened")
        lines.extend(event_lines)
        lines.append("")

    # # Promises you've made — active L3 intentions, with R4 delta
    if active_intentions:
        lines.append("# Promises you've made")
        for it in active_intentions:
            delta = render_event_delta_phrase(it, now) if now is not None else ""
            lines.append(f"- {_node_description(it)}{delta}")
        lines.append("")

    # # You've been expecting — active L4 expectations (plan §7 · Spec 6)
    if pending_expectations:
        lines.append("# You've been expecting")
        for exp in pending_expectations:
            delta = render_event_delta_phrase(exp, now) if now is not None else ""
            lines.append(f"- {_node_description(exp)}{delta}")
        lines.append("")

    # # Our recent conversation — day-bucketed when ``now`` is supplied
    if recent_messages:
        lines.append("# Our recent conversation")
        if now is not None:
            grouped: dict[str, list[tuple[str, str]]] = {}
            for m in recent_messages:
                role = getattr(m.role, "value", m.role)
                if role == "user":
                    prefix = "them"
                elif role == "persona":
                    prefix = "me"
                else:
                    prefix = "note"
                bucket = day_bucket_of(m.created_at, now)
                grouped.setdefault(bucket, []).append((prefix, m.content))

            for bucket in DAY_BUCKET_ORDER:
                group = grouped.get(bucket)
                if not group:
                    continue
                lines.append(f"## {bucket}")
                for prefix, content in group:
                    lines.append(f"{prefix}: {content}")
                lines.append("")
        else:
            # Legacy / test path — flat render with no bucket headers.
            for m in recent_messages:
                role = getattr(m.role, "value", m.role)
                if role == "user":
                    prefix = "them"
                elif role == "persona":
                    prefix = "me"
                else:
                    prefix = "note"
                lines.append(f"{prefix}: {m.content}")
            lines.append("")

    lines.append("# What they just said")
    lines.append(user_message)

    return "\n".join(lines)
