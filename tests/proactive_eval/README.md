# Proactive eval suite

End-to-end behaviour tests for EchoVessel's proactive pipeline. Each
YAML fixture under `fixtures/scripted/` runs the real `ingest → close →
consolidate → scheduler dispatch` pipeline against a real LLM and
asserts hard invariants on the extracted events, audit rows per phase,
and optional supersede event. LLM-as-judge prompts cover quality
dimensions that hard invariants cannot express (message tone,
channel-id leak, etc).

## Coverage

See [COVERAGE.md](./COVERAGE.md) for the full Tier 1 / Tier 2 / Tier 3
matrix.

## Running

The suite is gated on `~/.echovessel/config.toml` having a non-stub LLM
provider with a working API key. On CI or a fresh clone, the eval suite
is deselected automatically.

```bash
# Make sure ~/.echovessel/config.toml has a real LLM and API key env var is set
uv run pytest tests/proactive_eval/ -v -m eval_proactive

# Run a single fixture by ID
uv run pytest tests/proactive_eval/ -v -m eval_proactive -k moving_houston

# Use the keyword embedder fallback (no sentence-transformers download)
ECHOVESSEL_EVAL_EMBEDDER=keyword uv run pytest tests/proactive_eval/ -v -m eval_proactive
```

Tier 1 full suite is 28 fixtures · ~$3-8 per run with
`deepseek-v4-pro/flash`.

## Adding a new fixture

1. Pick a row from [COVERAGE.md](./COVERAGE.md) marked `➕`, or add a
   new row.
2. Drop a YAML in `fixtures/scripted/` named `<id>.yaml`. Schema in
   `schema.py:ProactiveFixture`.
3. Run only that fixture:
   `uv run pytest tests/proactive_eval/ -v -m eval_proactive -k <id>`.
4. If the assertion existing invariants cannot express, extend
   `invariants.py:check_invariants()` with a new field plus unit tests.
5. Update `COVERAGE.md`.

## Module layout

- `schema.py` — `ProactiveFixture` / `ProactiveSeed` / `ExpectEvent` /
  `ExpectPhase` / `FollowUpStage` / loader.
- `runner.py` — `run_fixture()` end-to-end pipeline: seeds DB, ingests
  turns, closes session, runs consolidate, drives scheduler with mock
  clock per phase, captures audit rows + supersede event.
- `invariants.py` — `check_invariants()` returns flat violation list.
- `harness.py` — re-export shim.
- `test_eval_fixtures.py` — `@pytest.mark.eval_proactive` parametric
  runner.

## Two eval suites — when to use which

| | `tests/memory_eval/` | `tests/proactive_eval/` (this dir) |
|---|---|---|
| Tests | LLM behaviour on memory pipeline | LLM behaviour on full proactive pipeline |
| Mock clock | not needed | required (push to phase windows) |
| Audit rows | no | yes |
| LLM call density | 1-3 / fixture | 6-12 / fixture |
| pytest mark | `eval` | `eval_proactive` |

Use memory_eval when verifying L3-L6 extraction quality. Use
proactive_eval when verifying scheduling timing, gate decisions, or
generated message content.
