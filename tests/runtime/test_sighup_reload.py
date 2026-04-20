"""SIGHUP reload semantics — provider swap without restarting.

We exercise `Runtime.reload()` directly (no signals sent), but also verify
the in-flight turn snapshot contract: a turn that started BEFORE the
reload completes with the OLD provider reference, even after
`runtime.ctx.llm` has been replaced.
"""

from __future__ import annotations

from echovessel.runtime import Runtime, load_config_from_str
from echovessel.runtime.llm import StubProvider

SIGHUP_TOML = """
[runtime]
data_dir = "/tmp/echovessel-sighup-test"

[persona]
id = "sighup"
display_name = "Sighup"

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""
"""


async def test_reload_skipped_when_no_config_path():
    cfg = load_config_from_str(SIGHUP_TOML)
    rt = Runtime.build(None, config_override=cfg, llm=StubProvider(fallback=""))
    assert rt.ctx.config_path is None
    # reload() with no path is a logged no-op.
    await rt.reload()
    # Still same provider.
    assert rt.ctx.llm.provider_name == "stub"


async def test_reload_replaces_llm_on_config_diff(monkeypatch, tmp_path):
    # Write a config to disk so reload() has a config_path.
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(SIGHUP_TOML)

    old_stub = StubProvider(fallback="OLD")
    rt = Runtime.build(toml_path, llm=old_stub)
    # Worker ζ wraps every provider in CostTrackingProvider, so
    # ``rt.ctx.llm`` is the wrapper; the raw provider lives at
    # ``rt.ctx.llm._inner``.
    from echovessel.runtime.cost_logger import CostTrackingProvider

    assert isinstance(rt.ctx.llm, CostTrackingProvider)
    assert rt.ctx.llm._inner is old_stub  # type: ignore[attr-defined]

    # Monkey-patch build_llm_provider to return a fresh stub on next call.
    import echovessel.runtime.app as app_mod

    new_stub = StubProvider(fallback="NEW")
    monkeypatch.setattr(app_mod, "build_llm_provider", lambda cfg: new_stub)

    # Change the config on disk so equality check sees a diff.
    toml_path.write_text(
        SIGHUP_TOML.replace('provider = "stub"', 'provider = "stub"\nmax_tokens = 2048')
    )
    await rt.reload()

    # After reload the wrapper still wraps — but its _inner is the new stub.
    assert isinstance(rt.ctx.llm, CostTrackingProvider)
    assert rt.ctx.llm._inner is new_stub  # type: ignore[attr-defined]


async def test_in_flight_reference_snapshot_survives_reload():
    """A turn handler that captures `llm = runtime.ctx.llm` locally must
    retain the old provider even if ctx.llm is replaced mid-turn.

    We simulate this by taking a local snapshot, mutating ctx.llm, and
    asserting the snapshot still points at the old object. After
    Worker ζ landed, ``ctx.llm`` is a :class:`CostTrackingProvider`
    wrapper — the snapshot retains the wrapper, and the wrapper's
    ``_inner`` is the old raw provider.
    """
    from echovessel.runtime.cost_logger import CostTrackingProvider

    cfg = load_config_from_str(SIGHUP_TOML)
    old = StubProvider(fallback="OLD")
    rt = Runtime.build(None, config_override=cfg, llm=old)

    local_snapshot = rt.ctx.llm  # wrapper around `old`
    new = StubProvider(fallback="NEW")
    rt.ctx.llm = new  # bare reassignment; wrapper not re-applied here

    assert rt.ctx.llm is new
    assert isinstance(local_snapshot, CostTrackingProvider)
    assert local_snapshot._inner is old  # type: ignore[attr-defined]
    # And the snapshot still produces the old provider's output.
    text, _usage = await local_snapshot.complete("s", "u")
    assert text == "OLD"
