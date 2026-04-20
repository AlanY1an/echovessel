"""Runtime configuration schema.

Pydantic v2 model that mirrors the TOML sections documented in
docs/runtime/01-spec-v0.1.md §4. Everything is validated at load time; a
malformed config fails the daemon fast rather than silently degrading.

Public API:

    from echovessel.runtime.config import Config, LLMSection, load_config

    cfg = load_config(Path("~/.echovessel/config.toml").expanduser())

Secrets are NEVER stored in this model — the schema only carries the NAME
of the environment variable that holds the key (e.g. `api_key_env =
"OPENAI_API_KEY"`). Actual key material is read from `os.environ` at the
point of use.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class RuntimeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Field(default_factory=lambda: Path("~/.echovessel"))
    log_level: Literal["debug", "info", "warn", "error"] = "info"
    # Per-turn wall-clock cap for the serial TurnDispatcher handler.
    # A handler exceeding this budget (typically a hung `llm.stream`)
    # is cancelled and the dispatcher continues to the next envelope.
    # Audit P1-2. 0 disables the timeout; default is 120s to align with
    # the common LLM SDK default and leave room for long streams.
    turn_timeout_seconds: float = Field(default=120.0, ge=0)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v


class PersonaSection(BaseModel):
    """Persona identity + optional voice binding.

    `voice_id` / `voice_provider` are added in Round 2 (see Voice spec
    §6.1). Both are optional — when `voice_id` is None, VoiceService.speak
    falls back to the provider default voice.

    `voice_enabled` is added in v0.4 (runtime spec §17a.7 · review Check 3):
    a persona-level main switch that controls whether reactive replies and
    proactive nudges are delivered as text or as neutral voice. Default
    `False` so daemons that have not opted-in stay text-only. The canonical
    runtime copy lives in `RuntimeContext.persona.voice_enabled`
    (`runtime/app.py::RuntimePersonaContext`) — this field is only the
    initial value loaded from config.toml at startup; subsequent mutations
    go through `Runtime.update_persona_voice_enabled()` which writes both
    config.toml and the in-memory ctx.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = "default"
    display_name: str = "Your Companion"
    initial_core_blocks_path: str | None = None

    # Voice binding (Round 2 · Voice spec §6.1)
    voice_id: str | None = None
    voice_provider: str | None = None

    # v0.4 · main voice switch (runtime spec §17a.7 · review Check 3)
    voice_enabled: bool = False


class MemorySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: str = "memory.db"
    embedder: str = "all-MiniLM-L6-v2"
    retrieve_k: int = Field(default=10, ge=1, le=50)
    relational_bonus_weight: float = 1.0
    recent_window_size: int = Field(default=20, ge=1, le=200)


class LLMSection(BaseModel):
    """LLM provider configuration. See spec §4.4 and §6.2.2.

    The validator in this class enforces the hard rules:
    - `api_key_env` must exist in os.environ unless stub / local endpoint
    - custom base_url requires explicit `model` or `models`
    - `models` keys must be one of {fast, main, judge}

    The legacy `tier_models` field is accepted for one release but
    automatically migrated to `models` with a deprecation warning:
      SMALL → fast · MEDIUM → judge · LARGE → main
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic", "openai_compat", "stub"] = "openai_compat"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    model: str | None = None
    # New field. Keys: "fast" / "main" / "judge".
    models: dict[str, str] = Field(default_factory=dict)
    # Deprecated. Accepted for one release; migrated into `models` at load time.
    # Keys: "small" / "medium" / "large".
    tier_models: dict[str, str] = Field(default_factory=dict)
    max_tokens: int = Field(default=1024, ge=64, le=32000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=60, ge=1, le=600)

    def _is_local_base_url(self) -> bool:
        if not self.base_url:
            return False
        return any(loc in self.base_url for loc in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))

    def _has_custom_base_url(self) -> bool:
        """True if the base_url is not an official Anthropic/OpenAI endpoint."""
        if not self.base_url:
            return False
        official = ("api.anthropic.com", "api.openai.com")
        return not any(o in self.base_url for o in official)

    @model_validator(mode="after")
    def _validate_provider_config(self) -> LLMSection:
        # 0. Migrate legacy tier_models → models when only the old field is
        #    populated. Both populated → models wins, tier_models ignored with
        #    a warning. Either side's unknown keys are rejected below.
        if self.tier_models:
            import logging as _logging

            _log = _logging.getLogger(__name__)
            tier_to_role = {"small": "fast", "medium": "judge", "large": "main"}
            unknown_tiers = set(self.tier_models.keys()) - set(tier_to_role.keys())
            if unknown_tiers:
                raise ValueError(
                    f"Unknown tier names in llm.tier_models: {sorted(unknown_tiers)}. "
                    f"Allowed legacy tiers: {sorted(tier_to_role.keys())}. "
                    f"Prefer `llm.models` with keys fast/main/judge."
                )

            if not self.models:
                # Safe auto-migration. Preserve by mutating in-place; pydantic
                # allows this on `after` validators by returning self.
                migrated = {tier_to_role[k]: v for k, v in self.tier_models.items()}
                object.__setattr__(self, "models", migrated)
                _log.warning(
                    "config.llm.tier_models is deprecated · use "
                    "config.llm.models with keys fast/main/judge. "
                    "Migrated %d entries for this run (SMALL→fast · "
                    "MEDIUM→judge · LARGE→main).",
                    len(migrated),
                )
            else:
                _log.warning(
                    "config.llm has BOTH tier_models (deprecated) and "
                    "models set; using `models` and ignoring tier_models."
                )

        # 1. models keys sanity (new field)
        if self.models:
            allowed_roles = {"fast", "main", "judge"}
            unknown = set(self.models.keys()) - allowed_roles
            if unknown:
                raise ValueError(
                    f"Unknown role names in llm.models: {sorted(unknown)}. "
                    f"Allowed: {sorted(allowed_roles)}."
                )

        # 2. openai_compat / anthropic with custom base_url needs explicit
        #    model or models.
        if (
            self.provider == "openai_compat"
            and self._has_custom_base_url()
            and not self.model
            and not self.models
        ):
            raise ValueError(
                f"llm.provider='openai_compat' with a custom base_url "
                f"({self.base_url!r}) requires either `llm.model` or "
                f"`llm.models` to be set. We do not ship default role "
                f"mappings for non-official endpoints."
            )
        if (
            self.provider == "anthropic"
            and self._has_custom_base_url()
            and not self.model
            and not self.models
        ):
            raise ValueError(
                f"llm.provider='anthropic' with a custom base_url "
                f"({self.base_url!r}) requires either `llm.model` or "
                f"`llm.models`."
            )

        # 3. API key env var presence
        needs_key = self.provider != "stub" and not self._is_local_base_url()
        if needs_key:
            if not self.api_key_env:
                raise ValueError(
                    f"LLM provider {self.provider!r} requires `api_key_env` "
                    f"pointing to an environment variable."
                )
            if not os.environ.get(self.api_key_env):
                raise ValueError(
                    f"Environment variable {self.api_key_env!r} is not set "
                    f"(required by llm.provider={self.provider!r})."
                )

        return self


class ConsolidateSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trivial_message_count: int = 3
    trivial_token_count: int = 200
    reflection_hard_gate_24h: int = 3
    worker_poll_seconds: float = Field(default=5.0, gt=0.0, le=3600.0)
    worker_max_retries: int = Field(default=3, ge=0, le=10)


class IdleScannerSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)


class ProactiveSection(BaseModel):
    """Proactive scheduler configuration.

    Round 2 expands this to mirror `echovessel.proactive.config.ProactiveConfig`
    (see Proactive spec §12). `to_proactive_config()` converts to the
    proactive-layer type consumed by `build_proactive_scheduler`.

    Default is `enabled=False` because MVP daemons that have not opted
    into proactive messaging should boot silently. Flip to `true` in
    config to actually run the scheduler.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    tick_interval_seconds: int = Field(default=60, ge=10, le=3600)

    # Quiet hours (local time, 24h)
    quiet_hours_start: int = Field(default=23, ge=0, le=23)
    quiet_hours_end: int = Field(default=7, ge=0, le=23)

    # Rate limit
    max_per_24h: int = Field(default=3, ge=0, le=100)

    # Cold-user detection
    cold_user_threshold: int = Field(default=2, ge=1, le=20)
    cold_user_response_window_hours: int = Field(default=6, ge=1, le=72)

    # Long silence (gentle nudge)
    long_silence_hours: int = Field(default=48, ge=1, le=720)

    # Queue cap (spec §2.5)
    max_events_in_queue: int = Field(default=64, ge=8, le=1024)

    # Voice integration
    use_voice_when_available: bool = True

    # Audit sink (MVP only supports 'jsonl')
    audit_sink: Literal["jsonl", "memory_db"] = "jsonl"

    # Graceful stop timeout
    stop_grace_seconds: int = Field(default=10, ge=1, le=120)

    def to_proactive_config(self, *, persona_id: str, user_id: str = "self"):
        """Convert this runtime-layer config into an
        `echovessel.proactive.config.ProactiveConfig`.

        Runtime-layer keeps its own Pydantic model for uniform validation
        and TOML loading; the proactive layer has its own `ProactiveConfig`
        that the scheduler factory consumes. This method bridges the two
        without leaking proactive imports into the runtime config module
        at import time — lazy imported.
        """
        from echovessel.proactive.config import ProactiveConfig

        return ProactiveConfig(
            enabled=self.enabled,
            tick_interval_seconds=self.tick_interval_seconds,
            quiet_hours_start=self.quiet_hours_start,
            quiet_hours_end=self.quiet_hours_end,
            max_per_24h=self.max_per_24h,
            cold_user_threshold=self.cold_user_threshold,
            cold_user_response_window_hours=self.cold_user_response_window_hours,
            long_silence_hours=self.long_silence_hours,
            max_events_in_queue=self.max_events_in_queue,
            use_voice_when_available=self.use_voice_when_available,
            audit_sink=self.audit_sink,
            stop_grace_seconds=self.stop_grace_seconds,
            persona_id=persona_id,
            user_id=user_id,
        )


class VoiceSection(BaseModel):
    """Voice service configuration (Round 2 · Voice spec §6.2).

    `enabled=False` produces a runtime that boots with `voice_service=None`
    — channels that probe for voice capability will see "not available"
    and proactive runs pure-text.

    Provider names are strict Literals to catch typos at config load time.
    When a new provider lands (e.g. `fishaudio_local` in v1.0), update
    both the Literal and Voice factory.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    tts_provider: Literal["fishaudio", "stub"] = "fishaudio"
    stt_provider: Literal["whisper_api", "stub"] = "whisper_api"
    tts_api_key_env: str = "FISH_API_KEY"
    stt_api_key_env: str = "OPENAI_API_KEY"
    default_audio_format: Literal["mp3", "wav", "pcm16"] = "mp3"

    @model_validator(mode="after")
    def _validate_api_keys(self) -> VoiceSection:
        """If TTS/STT providers are non-stub and voice is enabled, the
        corresponding env var must be set. Stub providers ignore the key.

        Non-fatal at module-load time when `enabled=False`; we don't block
        on env vars the user isn't going to use.
        """
        if not self.enabled:
            return self
        if self.tts_provider != "stub":
            if not self.tts_api_key_env:
                raise ValueError(
                    "voice.enabled=true with non-stub TTS requires tts_api_key_env to be set"
                )
            if not os.environ.get(self.tts_api_key_env):
                raise ValueError(
                    f"Environment variable {self.tts_api_key_env!r} is not set "
                    f"(required by voice.tts_provider={self.tts_provider!r})."
                )
        if self.stt_provider != "stub":
            if not self.stt_api_key_env:
                raise ValueError(
                    "voice.enabled=true with non-stub STT requires stt_api_key_env to be set"
                )
            if not os.environ.get(self.stt_api_key_env):
                raise ValueError(
                    f"Environment variable {self.stt_api_key_env!r} is not set "
                    f"(required by voice.stt_provider={self.stt_provider!r})."
                )
        return self


class WebChannelSection(BaseModel):
    """Typed ``[channels.web]`` subsection (Stage 2 · web-v1).

    Stage 2 introduces a typed schema for the Web channel's HTTP server
    config because Stage 2 boots uvicorn inside the daemon event loop
    and needs host/port/debounce values validated at config-load time
    rather than chased at startup.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    channel_id: str = "web"
    host: str = "127.0.0.1"
    port: int = Field(default=7777, ge=0, le=65535)
    static_dir: str = "embedded"
    debounce_ms: int = Field(default=2000, ge=0, le=60_000)


class DiscordChannelSection(BaseModel):
    """Typed ``[channels.discord]`` subsection (Stage 6 · web-v1).

    The Discord channel is a DM-only adapter. ``token_env`` names the
    environment variable that holds the bot token (never the token
    itself — secrets MUST NOT live in ``config.toml``). ``allowed_user_ids``
    is an optional allowlist of Discord user snowflake ids; if set, only
    DMs from those users reach the persona. ``None`` accepts DMs from
    anyone who successfully opens a DM with the bot.

    ``discord.py`` is an optional runtime dependency (``pip install
    echovessel[discord]``). Runtime will log an error and skip this
    channel if the import fails at startup — the daemon does not crash.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    channel_id: str = "discord"
    token_env: str = "ECHOVESSEL_DISCORD_TOKEN"
    allowed_user_ids: list[int] | None = None
    debounce_ms: int = Field(default=2000, ge=0, le=60_000)


class IMessageChannelSection(BaseModel):
    """Typed ``[channels.imessage]`` subsection.

    The iMessage channel is a DM-only adapter that drives a long-lived
    ``imsg rpc`` subprocess (see ``develop-docs/initiatives/_active/
    2026-04-imessage-channel/00-plan.md``).

    Two deployment modes are supported:

    - **Single-account (fast path · aligns with openclaw).** Leave
      ``persona_apple_id`` empty. The destination filter is disabled;
      every inbound message addressed to the Mac's iMessage account is
      eligible. The ``allowed_handles`` allowlist is the primary access
      control. This mode is the right choice when only one Apple ID is
      signed into the Mac's Messages.app.
    - **Dual-account (same macOS user).** Set ``persona_apple_id`` to
      the dedicated persona Apple ID. Only messages whose
      ``destination_caller_id`` matches this address reach the LLM —
      the user's main-account iMessage traffic is filtered out. Use
      this when you run both a personal and a persona Apple ID inside
      the same Messages.app.

    ``cli_path`` can point at an SSH wrapper script (see plan file for
    the remote-Mac pattern) — the channel does not care whether imsg
    runs locally or over SSH, just that the stdio protocol works.

    ``db_path`` is forwarded to ``imsg rpc --db`` when non-empty; leave
    default to let imsg read ``~/Library/Messages/chat.db`` for the
    current user.

    ``imsg`` is an external CLI dependency (``brew install
    steipete/tap/imsg``). Unlike ``[channels.discord]``, there is no
    Python import to fail at startup — the runtime simply spawns the
    binary and surfaces the error if it is missing.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    channel_id: str = "imessage"
    persona_apple_id: str = ""
    cli_path: str = "imsg"
    db_path: str = ""
    allowed_handles: list[str] = Field(default_factory=list)
    default_service: str = Field(default="auto", pattern=r"^(imessage|sms|auto)$")
    region: str = "US"
    debounce_ms: int = Field(default=2000, ge=0, le=60_000)


class ChannelsSection(BaseModel):
    """Per-channel configuration blobs.

    Web, Discord and iMessage are typed sub-models; WeChat remains a
    free-form placeholder until it gets its own typed schema.
    """

    model_config = ConfigDict(extra="allow")

    web: WebChannelSection = Field(default_factory=WebChannelSection)
    discord: DiscordChannelSection = Field(default_factory=DiscordChannelSection)
    imessage: IMessageChannelSection = Field(default_factory=IMessageChannelSection)
    wechat: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    persona: PersonaSection = Field(default_factory=PersonaSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    llm: LLMSection
    consolidate: ConsolidateSection = Field(default_factory=ConsolidateSection)
    idle_scanner: IdleScannerSection = Field(default_factory=IdleScannerSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    voice: VoiceSection = Field(default_factory=VoiceSection)
    channels: ChannelsSection = Field(default_factory=ChannelsSection)


# ---------------------------------------------------------------------------
# PATCH /api/admin/config allowlist (Worker η)
# ---------------------------------------------------------------------------
#
# The admin PATCH route's allowlist lives in
# :mod:`echovessel.core.config_paths` so the channels/web admin route
# can import it without breaking the layered-architecture contract
# (channels MUST NOT import runtime). If you're reading this module
# looking for "which fields are hot-reloadable?", open
# ``src/echovessel/core/config_paths.py``.


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_config(path: Path) -> Config:
    """Read and validate a TOML config file.

    Raises:
        FileNotFoundError: the file is missing.
        ValueError: Pydantic validation failed.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            f"Run `echovessel init` to create a starter config at {path}, then edit it."
        )
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)


def load_config_from_str(toml_text: str) -> Config:
    """Parse a TOML string directly. Handy for tests."""
    raw = tomllib.loads(toml_text)
    return Config.model_validate(raw)


__all__ = [
    "Config",
    "RuntimeSection",
    "PersonaSection",
    "MemorySection",
    "LLMSection",
    "ConsolidateSection",
    "IdleScannerSection",
    "ProactiveSection",
    "VoiceSection",
    "ChannelsSection",
    "WebChannelSection",
    "DiscordChannelSection",
    "load_config",
    "load_config_from_str",
]
