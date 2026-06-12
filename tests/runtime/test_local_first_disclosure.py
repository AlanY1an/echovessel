"""Startup must log a local-first disclosure line (spec §13.1)."""

from __future__ import annotations

import logging

from echovessel.runtime import Runtime, build_zero_embedder, load_config_from_str
from echovessel.runtime.llm import StubProvider

TOML = """
[runtime]
data_dir = "/tmp/echovessel-disclosure-test"

[persona]
id = "disclosure"
display_name = "D"

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""
"""


async def test_disclosure_log_contains_auditable_fields(caplog):
    cfg = load_config_from_str(TOML)
    rt = Runtime.build(
        None, config_override=cfg, llm=StubProvider(fallback=""), embed_fn=build_zero_embedder()
    )
    with caplog.at_level(logging.INFO, logger="echovessel.runtime.app"):
        await rt.start(register_signals=False)
        try:
            pass
        finally:
            await rt.stop()

    messages = [rec.getMessage() for rec in caplog.records]
    combined = "\n".join(messages)
    assert "EchoVessel runtime started" in combined
    assert "data_dir=" in combined
    assert "llm_provider=stub" in combined
    assert "llm_model(large)=" in combined
    assert "channels=" in combined
    assert "embedder=intfloat/multilingual-e5-small" in combined
    assert "local-first disclosure" in combined
