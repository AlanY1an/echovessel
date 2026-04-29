"""Canonical allowlists for the admin ``PATCH /api/admin/config`` route.

The constants live in the core layer (not runtime) so both the runtime
config loader and the channels/web admin route can import them without
breaking the layered-architecture contract
(``runtime → channels|proactive → memory|voice → core``).

Each path is a ``section.field`` string matching the Pydantic schema in
:mod:`echovessel.runtime.config`. Keep the two sets disjoint — a path
cannot be both hot-reloadable and restart-required.
"""

from __future__ import annotations

#: Paths the admin PATCH route applies at runtime. After the TOML write,
#: the runtime's SIGHUP-style reload + a small admin-side mirror pass
#: propagate the change to the live daemon.
HOT_RELOADABLE_CONFIG_PATHS: frozenset[str] = frozenset(
    {
        # LLM — reload() rebuilds the provider when these change.
        "llm.provider",
        "llm.model",
        "llm.api_key_env",
        "llm.base_url",
        "llm.timeout_seconds",
        "llm.temperature",
        "llm.max_tokens",
        # Persona — admin route mirrors display_name into ctx.persona
        # (and the Persona DB row) after the TOML write so subsequent
        # reads reflect the new name without a restart.
        "persona.display_name",
        # Memory tuning — consumers read ``ctx.config.memory.*`` at call
        # time, so a reload() swap is enough.
        "memory.retrieve_k",
        "memory.relational_bonus_weight",
        "memory.recent_window_size",
        # Consolidate thresholds — same read-at-use-time semantics.
        "consolidate.trivial_message_count",
        "consolidate.trivial_token_count",
        "consolidate.reflection_hard_gate_24h",
    }
)

#: Paths that ARE valid config keys but cannot be applied at runtime
#: because they steer a structural dependency (data_dir / db_path).
#: The admin PATCH route returns 400 on these, instructing the operator
#: to edit ``config.toml`` manually and restart the daemon.
RESTART_REQUIRED_CONFIG_PATHS: frozenset[str] = frozenset(
    {
        "runtime.data_dir",
        "memory.db_path",
    }
)


__all__ = [
    "HOT_RELOADABLE_CONFIG_PATHS",
    "RESTART_REQUIRED_CONFIG_PATHS",
]
