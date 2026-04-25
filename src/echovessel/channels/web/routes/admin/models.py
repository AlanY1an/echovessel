"""Pydantic request bodies for the admin routes.

Every model here is a wire format the Web admin frontend POSTs in.
The handlers themselves live in the route domain modules; keeping the
schemas in one file makes the contract easy to scan and easy to
re-validate against the TypeScript client.

``_enum_or_none`` is the soft-normalisation helper that backs the
PersonaFactsPayload field validators. Mirrors the same logic in
:func:`echovessel.prompts.persona_facts._coerce_fact` — anything outside
the enum becomes ``None`` instead of raising, so the user can fix the
mistake on the review page rather than seeing a 422.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from echovessel.prompts import (
    ENUM_EDUCATION_LEVEL,
    ENUM_GENDER,
    ENUM_HEALTH_STATUS,
    ENUM_LIFE_STAGE,
    ENUM_RELATIONSHIP_STATUS,
)


def _enum_or_none(value: str | None, allowed: tuple[str, ...]) -> str | None:
    """Coerce to lower-case and drop values outside the enum vocabulary.

    Mirrors the soft-normalisation in
    :func:`echovessel.prompts.persona_facts._coerce_fact` — anything
    outside the enum becomes ``None`` instead of raising, so the user
    can re-onboard with their mistake corrected on the review page
    rather than hitting a 422.
    """

    if value is None:
        return None
    stripped = value.strip().lower()
    if not stripped:
        return None
    if stripped in allowed:
        return stripped
    return None


class PersonaFactsPayload(BaseModel):
    """JSON shape for the 15 biographic facts carried on the persona row.

    Every field is optional — the onboarding flow lets the user leave
    as many blank as they want, and the Web admin PATCH handler accepts
    any subset. Enum-valued fields are validated against the same
    vocabularies :mod:`echovessel.prompts.persona_facts` uses, so the
    wire format matches what the LLM emits and what the DB stores.
    """

    full_name: str | None = Field(default=None, max_length=256)
    gender: str | None = Field(default=None)
    birth_date: str | None = Field(default=None, max_length=32)
    ethnicity: str | None = Field(default=None, max_length=128)
    nationality: str | None = Field(default=None, max_length=8)
    native_language: str | None = Field(default=None, max_length=32)
    locale_region: str | None = Field(default=None, max_length=128)
    education_level: str | None = Field(default=None)
    occupation: str | None = Field(default=None, max_length=128)
    occupation_field: str | None = Field(default=None, max_length=128)
    location: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    relationship_status: str | None = Field(default=None)
    life_stage: str | None = Field(default=None)
    health_status: str | None = Field(default=None)

    @field_validator(
        "full_name",
        "ethnicity",
        "nationality",
        "native_language",
        "locale_region",
        "occupation",
        "occupation_field",
        "location",
        "timezone",
    )
    @classmethod
    def _strip_empty_free_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("gender")
    @classmethod
    def _validate_gender(cls, v: str | None) -> str | None:
        return _enum_or_none(v, ENUM_GENDER)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone_iana(cls, v: str | None) -> str | None:
        """Reject non-IANA timezone strings.

        Frontend will pull the dropdown from ``Intl.supportedValuesOf('timeZone')``
        so admin writes are guaranteed IANA. LLM extraction (persona_facts.py)
        used to write free-text like "Taiwan" / "台北" — those are rejected here
        so they can only enter the system via the admin dropdown path.
        """
        if v is None or v == "":
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(
                f"timezone must be an IANA name (e.g. 'Asia/Taipei'); got {v!r}"
            ) from e
        return v

    @field_validator("education_level")
    @classmethod
    def _validate_education_level(cls, v: str | None) -> str | None:
        return _enum_or_none(v, ENUM_EDUCATION_LEVEL)

    @field_validator("relationship_status")
    @classmethod
    def _validate_relationship_status(cls, v: str | None) -> str | None:
        return _enum_or_none(v, ENUM_RELATIONSHIP_STATUS)

    @field_validator("life_stage")
    @classmethod
    def _validate_life_stage(cls, v: str | None) -> str | None:
        return _enum_or_none(v, ENUM_LIFE_STAGE)

    @field_validator("health_status")
    @classmethod
    def _validate_health_status(cls, v: str | None) -> str | None:
        return _enum_or_none(v, ENUM_HEALTH_STATUS)

    @field_validator("birth_date")
    @classmethod
    def _validate_birth_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        try:
            date.fromisoformat(stripped)
        except ValueError as e:
            raise ValueError(
                "birth_date must be ISO YYYY-MM-DD (use YYYY-01-01 for year-only)"
            ) from e
        return stripped


class OnboardingRequest(BaseModel):
    """Body for ``POST /api/admin/persona/onboarding``.

    v0.5: L1 collapsed to ``persona`` + ``user`` (+ ``style`` via the
    dedicated style endpoint). The legacy ``self_block`` and
    ``relationship_block`` fields are gone — sending either yields a
    422. The pre-1.0 ``no backcompat shims`` rule from CLAUDE.md is
    why the request model is strict (``extra='forbid'``); call sites
    that still send the obsolete fields must be updated, not silently
    accepted.

    Both required block fields are still required (the frontend sends
    them even when empty), but empty strings are accepted and
    silently skipped at write time. ``facts`` is optional — the user
    may skip every biographic field and finish onboarding with just
    the blocks.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(..., min_length=1, max_length=256)
    persona_block: str = Field(...)
    user_block: str = Field(...)
    facts: PersonaFactsPayload | None = None


class PersonaFactsUpdateRequest(BaseModel):
    """Body for ``PATCH /api/admin/persona/facts``.

    Every field is optional; the handler applies only the keys that
    are present in the request body, leaving the rest untouched. Use
    an explicit ``null`` to clear a previously-set field.
    """

    facts: PersonaFactsPayload


class PersonaExtractRequest(BaseModel):
    """Body for ``POST /api/admin/persona/extract-from-input``.

    Dispatches on ``input_type``:

    - ``blank_write`` — the user has been typing blocks directly. We
      stitch them into the LLM context and extract facts. ``upload_id``
      / ``pipeline_id`` are ignored in this mode.
    - ``import_upload`` — the caller has either just finished an
      import (``pipeline_id`` set) or is about to start one
      (``upload_id`` set). We wait for the pipeline, concatenate its
      events + thoughts as the LLM context, and extract both blocks
      and facts in one call.

    ``existing_blocks`` and ``locale`` are hints; omit or send null to
    let the LLM infer from the input alone.
    """

    input_type: str = Field(..., pattern="^(blank_write|import_upload)$")
    user_input: str | None = Field(default=None, max_length=100_000)
    existing_blocks: dict[str, str] | None = Field(default=None)
    locale: str | None = Field(default=None, max_length=16)
    persona_display_name: str | None = Field(default=None, max_length=256)
    upload_id: str | None = Field(default=None, min_length=1, max_length=64)
    pipeline_id: str | None = Field(default=None, min_length=1, max_length=64)


class PersonaUpdateRequest(BaseModel):
    """Body for ``POST /api/admin/persona``.

    v0.5 contract — only ``persona`` and ``user`` blocks may be
    written through this endpoint. ``style`` has its own dedicated
    endpoint (``POST /api/admin/persona/style``). ``self_block`` and
    ``relationship_block`` are gone (plan §1) and any payload that
    still includes them yields a 422 thanks to ``extra='forbid'``.

    Every field is optional — the server applies only the keys that
    are actually present in the request body.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=256)
    persona_block: str | None = None
    user_block: str | None = None


class VoiceToggleRequest(BaseModel):
    """Body for ``POST /api/admin/persona/voice-toggle``."""

    enabled: bool


class StyleUpdateRequest(BaseModel):
    """Body for ``POST /api/admin/persona/style`` (plan §6.6).

    Three actions:
      - ``set``     — soft-delete the prior STYLE row and write fresh.
      - ``append``  — standard ``append_to_core_block`` path; joins
                      prior content with a newline.
      - ``clear``   — soft-delete the row; ``text`` is ignored.
    """

    action: str = Field(..., pattern="^(set|append|clear)$")
    text: str = ""


class UserTimezoneRequest(BaseModel):
    """Body for ``POST /api/admin/users/timezone`` (plan decision 5).

    Web channel POSTs the browser's
    ``Intl.DateTimeFormat().resolvedOptions().timeZone`` on first
    connect. ``override=True`` overwrites an existing value (admin UI
    manual edit path).
    """

    timezone: str = Field(..., min_length=1, max_length=64)
    override: bool = False


class PersonaBootstrapRequest(BaseModel):
    """Body for ``POST /api/admin/persona/bootstrap-from-material``.

    At least one of ``upload_id`` or ``pipeline_id`` MUST be supplied:

    - ``upload_id``   — the caller has uploaded material but has not
      started a pipeline. This endpoint will start one, wait for
      ``pipeline.done``, then bootstrap.
    - ``pipeline_id`` — the caller already started a pipeline (via
      ``POST /api/admin/import/start``) and is ready to consume its
      output. This endpoint subscribes to the existing stream and
      waits for ``pipeline.done``.

    ``persona_display_name`` is an optional hint passed to the LLM so
    the generated blocks can reference the persona by name where
    natural. The ACTUAL display_name is set later via
    ``POST /api/admin/persona/onboarding``.
    """

    upload_id: str | None = Field(default=None, min_length=1, max_length=64)
    pipeline_id: str | None = Field(default=None, min_length=1, max_length=64)
    persona_display_name: str | None = Field(default=None, max_length=256)


class VoiceCloneRequest(BaseModel):
    """Body for ``POST /api/admin/voice/clone``.

    Worker λ. ``display_name`` is the user-facing label for the new
    cloned voice (e.g. "我的声音 2026-04-16"). The backend takes every
    current draft sample, concatenates the raw bytes, and passes the
    blob to :meth:`VoiceService.clone_voice_interactive`.
    """

    display_name: str = Field(..., min_length=1, max_length=128)


class VoicePreviewRequest(BaseModel):
    """Body for ``POST /api/admin/voice/preview``."""

    voice_id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=500)


class VoiceActivateRequest(BaseModel):
    """Body for ``POST /api/admin/voice/activate``."""

    voice_id: str = Field(..., min_length=1, max_length=128)


class PreviewDeleteRequest(BaseModel):
    """Body for ``POST /api/admin/memory/preview-delete``.

    ``node_id`` is the L3 event or L4 thought the admin UI wants to
    inspect before committing a delete. Used to show the user how many
    (if any) derivative thoughts would be affected and let them pick
    between cascade / orphan.
    """

    node_id: int = Field(..., ge=1)


# v0.5 hotfix · admin Persona tab Social Graph endpoints. The five
# entity-related Pydantic bodies below back the routes added at the
# bottom of ``build_admin_router``.

_ENTITY_KIND_PATTERN: str = "^(person|place|org|pet|other)$"


class EntityDescriptionPatchRequest(BaseModel):
    """Body for ``PATCH /api/admin/memory/entities/{id}``.

    Owner-edited descriptions always set ``owner_override=true``
    server-side — the client cannot opt out — so the slow_cycle
    description synthesizer permanently leaves the row alone (plan
    §2.2). An empty string is allowed (clears the description).
    """

    description: str = Field(..., max_length=4000)


class EntityCreateRequest(BaseModel):
    """Body for ``POST /api/admin/memory/entities``.

    Owner manually adds an entity from the admin UI. ``merge_status``
    is forced to ``'confirmed'`` server-side; ``owner_override`` is
    set to True iff a non-empty ``description`` is supplied.
    """

    canonical_name: str = Field(..., min_length=1, max_length=256)
    kind: str = Field(default="person", pattern=_ENTITY_KIND_PATTERN)
    description: str | None = Field(default=None, max_length=4000)
    aliases: list[str] | None = Field(default=None)


class EntityMergeRequest(BaseModel):
    """Body for ``POST /api/admin/memory/entities/{id}/merge``.

    Owner says "this entity is the same person as ``target_id``".
    Routes through the existing
    :func:`echovessel.memory.entities.apply_entity_clarification`
    with ``same=True``.
    """

    target_id: int = Field(..., ge=1)


class EntitySeparateRequest(BaseModel):
    """Body for ``POST /api/admin/memory/entities/{id}/confirm-separate``.

    Owner says "this entity and ``other_id`` are different people".
    Routes through ``apply_entity_clarification(same=False)`` and
    additionally promotes any leftover ``merge_status='uncertain'``
    rows on either side to ``'confirmed'``.
    """

    other_id: int = Field(..., ge=1)
