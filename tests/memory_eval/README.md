# Memory eval suite

End-to-end behavior tests for EchoVessel's memory pipeline. Each YAML fixture
under `fixtures/scripted/` runs the real `ingest → close → consolidate →
retrieve` pipeline against a real LLM and asserts a set of invariants on the
output.

## Coverage

See [COVERAGE.md](./COVERAGE.md) for the full layer × behavior matrix.

## Running

The suite is gated on `~/.echovessel/config.toml` having a non-stub LLM
provider with a working API key. On CI or a fresh clone, the eval suite is
deselected automatically.

To run locally:

```bash
# 1. Make sure ~/.echovessel/config.toml has a real LLM and the API key env var is set
uv run pytest tests/memory_eval/ -v

# 2. Run a single fixture by ID
uv run pytest tests/memory_eval/test_eval_fixtures.py -v -k "l3_vocative"

# 3. Use the keyword embedder fallback (no sentence-transformers download)
ECHOVESSEL_EVAL_EMBEDDER=keyword uv run pytest tests/memory_eval/ -v
```

Each fixture run takes ~15-30s depending on the LLM. The full suite is ~10
minutes wall-clock; expect ~$0.30-1.00 per full run depending on provider.

## Adding a new fixture

1. Pick a row from [COVERAGE.md](./COVERAGE.md) marked `➕`, or add a new row
   if the behavior is missing.
2. Drop a YAML in `fixtures/scripted/` named `<layer>_<behavior_id>.yaml`.
   Schema is documented in `schema.py:Fixture`.
3. Run only that fixture: `uv run pytest -v -k <fixture_id>`.
4. If the fixture asserts something the existing invariants can't express,
   extend `invariants.py:check_invariants()` with a new field and add unit tests in
   `test_check_invariants.py`.
5. Update COVERAGE.md to flip the row's status.

## Module layout

- `schema.py` — fixture dataclasses (`Fixture`, `FixtureSeed`, `SeedThought`, etc.) + `load_fixture` / `discover_fixtures`
- `embedders.py` — `build_live_llm`, `build_eval_embedder`, `keyword_embedder`
- `runner.py` — `run_fixture()` end-to-end runner + `render_evidence()`
- `invariants.py` — `check_invariants()` with all hard-invariant fields
- `harness.py` — re-export shim for backward-compatible imports

## Two eval systems — when to use which

| | `tests/eval/` (scenario.py) | `tests/memory_eval/` (this dir) |
|---|---|---|
| Tests | pipeline machinery on a 14-day corpus | LLM behavior quality on focused fixtures |
| LLM | stub with canned recordings | real LLM via config |
| Embedder | hash stub | sentence-transformers / keyword |
| Determinism | fully deterministic | LLM non-determinism (judges via JSON) |
| Speed | seconds | ~30s per fixture |
| CI | runs by default if private corpus available | skipped (needs config + API key) |

Use `tests/eval/` when verifying retrieve/consolidate plumbing won't drop
events. Use this suite when verifying the LLM-driven steps (extraction
prompt, reflection prompt, mood signal, entity resolution) produce
behaviorally correct output.
