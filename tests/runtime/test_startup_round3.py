"""v0.4 startup-sequence coverage (spec §17a.4).

Verifies:
    - Step 6.5 `ensure_schema_up_to_date` runs during `Runtime.build()`
      and a migration failure fails-fast.
    - Step 12.5 registers `RuntimeMemoryObserver` via
      `memory.register_observer` on `Runtime.start()`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from echovessel.runtime import Runtime, build_zero_embedder, load_config_from_str
from echovessel.runtime.llm import StubProvider

TOML = """
[runtime]
data_dir = "/tmp/echovessel-startup-round3"
log_level = "warn"

[persona]
id = "smoke"
display_name = "Smoke"

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""


def test_startup_step_6_5_calls_ensure_schema_up_to_date():
    cfg = load_config_from_str(TOML)
    with patch("echovessel.runtime.app.ensure_schema_up_to_date") as patched:
        Runtime.build(
            None,
            config_override=cfg,
            llm=StubProvider(fallback=""),
            embed_fn=build_zero_embedder(),
        )
    assert patched.call_count == 1


def test_startup_step_6_5_fail_fast():
    cfg = load_config_from_str(TOML)
    with (
        patch(
            "echovessel.runtime.app.ensure_schema_up_to_date",
            side_effect=RuntimeError("migration exploded"),
        ),
        pytest.raises(RuntimeError, match="migration exploded"),
    ):
        Runtime.build(
            None,
            config_override=cfg,
            llm=StubProvider(fallback=""),
            embed_fn=build_zero_embedder(),
        )


async def test_startup_step_12_5_observer_registered():
    cfg = load_config_from_str(TOML)
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback=""),
        embed_fn=build_zero_embedder(),
    )
    with patch("echovessel.runtime.app.register_observer") as patched:
        await rt.start(channels=[], register_signals=False)
        try:
            assert patched.call_count == 1
            assert rt._memory_observer is not None
        finally:
            await rt.stop()
