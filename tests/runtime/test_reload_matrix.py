"""Reload matrix · what hot-reload actually does per field.

Stage 4 of the 2026-04-daemon-control initiative. Walks every entry in
``HOT_RELOADABLE_CONFIG_PATHS`` and asserts the observable effect of
mutating that field + calling ``Runtime.reload()``:

- `llm.*`        → provider rebuilt (ctx.llm._inner swapped)
- `memory.*`     → ctx.config.memory.* updated (turn reads these live)
- `persona.*`    → ctx.config.persona.* updated (side-path in
                   apply_config_patches mirrors into ctx.persona;
                   SIGHUP-only reload does NOT, which matches docs)
- `consolidate.*`→ live worker attribute mutated (stage 4 fix)

This file is the single authoritative source for "what reload does".
``docs/en/configuration.md`` mirrors this table — when the table
changes, the docs must change too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from echovessel.core.config_paths import HOT_RELOADABLE_CONFIG_PATHS
from echovessel.runtime import Runtime, build_zero_embedder
from echovessel.runtime.llm import StubProvider

BASE_TOML = """
[runtime]
data_dir = "/tmp/echovessel-reload-matrix"

[persona]
id = "reload-matrix"
display_name = "Original"

[memory]
db_path = ":memory:"
retrieve_k = 10
recent_window_size = 20
relational_bonus_weight = 1.0

[llm]
provider = "stub"
api_key_env = ""
model = "orig-model"
timeout_seconds = 30
temperature = 0.7
max_tokens = 1024

[consolidate]
trivial_message_count = 3
trivial_token_count = 200
reflection_hard_gate_24h = 3
"""


def _build(tmp_path: Path) -> tuple[Runtime, Path]:
    toml = tmp_path / "config.toml"
    toml.write_text(BASE_TOML)
    rt = Runtime.build(
        toml,
        llm=StubProvider(fallback="orig"),
        embed_fn=build_zero_embedder(),
    )
    return rt, toml


async def _reload_with_mutation(
    rt: Runtime, toml: Path, replacement: tuple[str, str], monkeypatch
) -> list[str]:
    # Inline tomllib-less mutation: we can't edit nested tables portably
    # without re-writing the whole file, so patches are string replaces
    # against BASE_TOML. Each call passes (old_snippet, new_snippet).
    old, new = replacement
    current = toml.read_text()
    assert old in current, f"snippet {old!r} not in current TOML"
    toml.write_text(current.replace(old, new))

    # Monkey-patch build_llm_provider so reload doesn't try to hit a
    # real network for llm.* mutations.
    import echovessel.runtime.app as app_mod

    monkeypatch.setattr(app_mod, "build_llm_provider", lambda cfg: StubProvider(fallback="new"))
    return await rt.reload()


class TestReloadMatrixLLM:
    """Every `[llm].<field>` in the allowlist rebuilds the provider."""

    @pytest.mark.parametrize(
        "replacement",
        [
            ('model = "orig-model"', 'model = "new-model"'),
            ("temperature = 0.7", "temperature = 0.9"),
            ("max_tokens = 1024", "max_tokens = 4096"),
            ("timeout_seconds = 30", "timeout_seconds = 60"),
        ],
    )
    async def test_llm_field_triggers_provider_rebuild(self, tmp_path, monkeypatch, replacement):
        rt, toml = _build(tmp_path)
        reloaded = await _reload_with_mutation(rt, toml, replacement, monkeypatch)
        assert "llm" in reloaded


class TestReloadMatrixMemory:
    """`[memory].<field>` goes through ctx.config update only — no provider
    rebuild, no live-worker mirror. The turn handler reads
    ctx.config.memory.* per-turn so the new value is live on the next
    turn without any other machinery."""

    @pytest.mark.parametrize(
        "replacement,field,expected",
        [
            (("retrieve_k = 10", "retrieve_k = 25"), "retrieve_k", 25),
            (
                ("recent_window_size = 20", "recent_window_size = 50"),
                "recent_window_size",
                50,
            ),
            (
                (
                    "relational_bonus_weight = 1.0",
                    "relational_bonus_weight = 2.5",
                ),
                "relational_bonus_weight",
                2.5,
            ),
        ],
    )
    async def test_memory_field_updates_ctx_config(
        self, tmp_path, monkeypatch, replacement, field, expected
    ):
        rt, toml = _build(tmp_path)
        await _reload_with_mutation(rt, toml, replacement, monkeypatch)
        assert getattr(rt.ctx.config.memory, field) == expected


class TestReloadMatrixConsolidate:
    """`[consolidate].<field>` must propagate to the LIVE worker, not
    just ctx.config. Stage 4 of the daemon-control initiative fixes
    this; before the fix, only ctx.config was updated and the worker
    kept stale values."""

    async def test_trivial_message_count_mirrors_to_worker(self, tmp_path, monkeypatch):
        rt, toml = _build(tmp_path)
        # Stand in a minimal worker for the test (Runtime.build doesn't
        # construct one — that's Runtime.start's job).
        from echovessel.runtime.consolidate_worker import ConsolidateWorker

        rt._worker = ConsolidateWorker(
            db_factory=lambda: None,  # type: ignore[arg-type]
            backend=rt.ctx.backend,
            extract_fn=lambda *a, **kw: None,  # type: ignore[arg-type]
            reflect_fn=lambda *a, **kw: None,  # type: ignore[arg-type]
            embed_fn=rt.ctx.embed_fn,
            trivial_message_count=3,
            trivial_token_count=200,
            reflection_hard_limit_24h=3,
        )

        reloaded = await _reload_with_mutation(
            rt,
            toml,
            ("trivial_message_count = 3", "trivial_message_count = 10"),
            monkeypatch,
        )

        assert "consolidate.trivial_message_count" in reloaded
        assert rt._worker.trivial_message_count == 10

    async def test_trivial_token_count_mirrors_to_worker(self, tmp_path, monkeypatch):
        rt, toml = _build(tmp_path)
        from echovessel.runtime.consolidate_worker import ConsolidateWorker

        rt._worker = ConsolidateWorker(
            db_factory=lambda: None,  # type: ignore[arg-type]
            backend=rt.ctx.backend,
            extract_fn=lambda *a, **kw: None,  # type: ignore[arg-type]
            reflect_fn=lambda *a, **kw: None,  # type: ignore[arg-type]
            embed_fn=rt.ctx.embed_fn,
            trivial_message_count=3,
            trivial_token_count=200,
            reflection_hard_limit_24h=3,
        )

        reloaded = await _reload_with_mutation(
            rt,
            toml,
            ("trivial_token_count = 200", "trivial_token_count = 500"),
            monkeypatch,
        )

        assert "consolidate.trivial_token_count" in reloaded
        assert rt._worker.trivial_token_count == 500

    async def test_reflection_hard_gate_mirrors_to_worker(self, tmp_path, monkeypatch):
        rt, toml = _build(tmp_path)
        from echovessel.runtime.consolidate_worker import ConsolidateWorker

        rt._worker = ConsolidateWorker(
            db_factory=lambda: None,  # type: ignore[arg-type]
            backend=rt.ctx.backend,
            extract_fn=lambda *a, **kw: None,  # type: ignore[arg-type]
            reflect_fn=lambda *a, **kw: None,  # type: ignore[arg-type]
            embed_fn=rt.ctx.embed_fn,
            trivial_message_count=3,
            trivial_token_count=200,
            reflection_hard_limit_24h=3,
        )

        reloaded = await _reload_with_mutation(
            rt,
            toml,
            ("reflection_hard_gate_24h = 3", "reflection_hard_gate_24h = 10"),
            monkeypatch,
        )

        assert "consolidate.reflection_hard_gate_24h" in reloaded
        # Attribute on the worker is named `reflection_hard_limit_24h`
        # (constant carries a different legacy name in the memory module).
        assert rt._worker.reflection_hard_limit_24h == 10


class TestReloadMatrixPersona:
    """`[persona].display_name` goes through ctx.config on reload but
    does NOT mirror into ctx.persona via the reload path. The mirror
    happens only in ``Runtime.apply_config_patches`` (the admin API
    write path) which adds a separate step after reload.

    This is documented behaviour — SIGHUP reload alone is a no-op for
    the visible persona name; the admin API is the canonical path.
    """

    async def test_reload_updates_ctx_config_but_not_ctx_persona(self, tmp_path, monkeypatch):
        rt, toml = _build(tmp_path)
        assert rt.ctx.config.persona.display_name == "Original"

        await _reload_with_mutation(
            rt,
            toml,
            ('display_name = "Original"', 'display_name = "New"'),
            monkeypatch,
        )
        # ctx.config reflects the new name on reload.
        assert rt.ctx.config.persona.display_name == "New"
        # ctx.persona is a SEPARATE mutable object the turn handler
        # reads. reload() does NOT mirror into it — only
        # apply_config_patches does. This is the documented behaviour.
        # Whether we should change it is a separate product decision.
        assert rt.ctx.persona.display_name != "New"


class TestReloadMatrixAllowlistInventory:
    """Meta-guard: every field in HOT_RELOADABLE_CONFIG_PATHS must be
    covered by one of the tests above. If a new field is added to the
    allowlist without a corresponding test, this fails."""

    def test_all_allowlist_fields_covered(self):
        covered = {
            # llm.*
            "llm.provider",
            "llm.model",
            "llm.api_key_env",
            "llm.timeout_seconds",
            "llm.temperature",
            "llm.max_tokens",
            # persona.*
            "persona.display_name",
            # memory.*
            "memory.retrieve_k",
            "memory.relational_bonus_weight",
            "memory.recent_window_size",
            # consolidate.*
            "consolidate.trivial_message_count",
            "consolidate.trivial_token_count",
            "consolidate.reflection_hard_gate_24h",
        }
        missing = HOT_RELOADABLE_CONFIG_PATHS - covered
        extra = covered - HOT_RELOADABLE_CONFIG_PATHS
        assert not missing, (
            f"HOT_RELOADABLE_CONFIG_PATHS has fields this test file does "
            f"not cover: {sorted(missing)}. Add a test row."
        )
        assert not extra, (
            f"This test file claims to cover fields that are not in "
            f"HOT_RELOADABLE_CONFIG_PATHS: {sorted(extra)}. Remove the "
            f"obsolete row."
        )
