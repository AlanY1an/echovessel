"""Runtime.build wires the embedding-model sync.

- Default build runs ``ensure_embedding_model`` and tags the DB with
  the configured ``[memory].embedder``.
- ``sync_embedder=False`` (the ``--no-embedder`` launcher path) skips
  the sync so an injected placeholder embedder never overwrites real
  vectors or claims the configured model's tag.
"""

from __future__ import annotations

from sqlalchemy import text

from echovessel.memory.embedding_sync import EMBEDDING_MODEL_KEY
from echovessel.runtime import Runtime, build_zero_embedder, load_config_from_str


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "embed-sync"
display_name = "EmbedSync"

[memory]
db_path = ":memory:"
embedder = "fake/test-model"

[llm]
provider = "stub"
api_key_env = ""
"""


def _read_tag(engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT value FROM memory_meta WHERE key = :k"),
            {"k": EMBEDDING_MODEL_KEY},
        ).scalar()


def test_build_tags_db_with_configured_embedder(tmp_path):
    cfg = load_config_from_str(_toml(str(tmp_path)))
    rt = Runtime.build(None, config_override=cfg, embed_fn=build_zero_embedder())
    assert _read_tag(rt.ctx.engine) == "fake/test-model"


def test_build_with_sync_disabled_writes_no_tag(tmp_path):
    cfg = load_config_from_str(_toml(str(tmp_path)))
    rt = Runtime.build(
        None,
        config_override=cfg,
        embed_fn=build_zero_embedder(),
        sync_embedder=False,
    )
    with rt.ctx.engine.connect() as conn:
        has_meta = (
            conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_meta'")
            ).first()
            is not None
        )
    assert not has_meta
