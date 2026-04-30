# docs/ai · Agent Knowledge Base

Structured, grep-first context for agents working on EchoVessel.
Human-narrative docs live in `docs/en/` + `docs/zh/` — read those when
you need design rationale or onboarding prose. Read here when you need
to find the right code path fast.

> **Code is truth.** These files are operational summaries; when they
> disagree with the code, fix the file. Always confirm against the actual
> source before recommending an action.

> **Trees are independent views.** `docs/ai/` and `docs/en|zh/` document
> the same code from different angles for different audiences. They are
> NOT synced clones — duplication is fine and expected. Don't cross-reference
> forcing readers to bounce between trees; restate facts where they belong.
> The single source of truth is code (and guard tests for invariants), not
> another doc.

---

## Routing — start here

| If the task touches… | Open |
|---|---|
| message ingest, sessions, extraction, retrieval, embeddings, soft-delete | [`memory/`](memory/overview.md) |
| daemon startup, LLM providers, per-turn pipeline, background loops | `runtime/` *(not yet drafted)* |
| Web / Discord / iMessage / wechat transports, admin routes | `channels/` *(not yet drafted)* |
| TTS, STT, voice cloning | `voice/` *(not yet drafted)* |
| idle-trigger worker (engines + execution) | `proactive/` *(not yet drafted)* |
| chat-log import pipeline | `import_/` *(not yet drafted)* |
| LLM prompt templates (extraction, reflection, judge) | `prompts/` *(not yet drafted)* |
| asyncio rules · import contracts · commits · "no backcompat shims" | [`conventions.md`](conventions.md) |

`core/` is too small to deserve its own folder — it holds shared enums
and path helpers in `src/echovessel/core/types.py` and
`src/echovessel/core/config_paths.py`. Layer rule: every other system
imports from `core`; `core` imports from nothing internal.

---

## What lives where

Each system folder may contain up to five files. Skipped sections mean
"not enough complexity yet to be worth a separate page."

- `overview.md` — what the system does, public entry points, invariants
- `architecture.md` — module map, data flow, dependency direction
- `conventions.md` — patterns specific to this system
- `workflow.md` — guideposts for common tasks (touch points, not runbooks)
- `references.md` — code paths, tests, related systems

Workflows are **guideposts, not runbooks** — they list the files you'll
touch and the order you should think about them, not literal copy-paste
steps. Always re-confirm against the code; signatures shift faster than
docs.

---

## Layered architecture (enforced by `lint-imports`)

```
runtime  →  channels | proactive  →  memory | voice  →  core
```

Plus three sub-package contracts:

- `proactive` MUST NOT import `runtime` or `prompts`.
- `proactive` sub-packages: `execution → engines → core`. No reverse.
- `runtime.wiring` MUST NOT import `runtime.turn` or `runtime.loops`.

`uv run lint-imports` is the gate. If you cross a layer, redesign the
call path — don't add a shim.

---

## Cross-cutting invariants (every system must respect)

1. **D4 铁律 · memory retrieval NEVER filters by `channel_id`.**
   Sessions are sharded by channel for *boundary* purposes only; once
   L3/L4 nodes exist they join the unified pool. `runtime/wiring/memory.py`
   has a unit test guarding this.
2. **Single asyncio loop.** Sync-only libraries wrap in `asyncio.to_thread`.
   See `voice/fishaudio.py` as the canonical example.
3. **No backcompat shims.** Pre-1.0 — when changing a public signature,
   update every call site rather than leaving aliases or stubs.
4. **One persona per daemon.** Multi-persona is explicitly deferred.
5. **Local-first.** No telemetry. LLM + voice are the only external deps,
   both pluggable.

---

## How to read source — the canonical entry points

| Want to know how X works? | Start with |
|---|---|
| Daemon startup / shutdown | `src/echovessel/runtime/app.py` |
| CLI surface | `src/echovessel/runtime/launcher.py` |
| Memory public API | `src/echovessel/memory/__init__.py` |
| Schema / tables | `src/echovessel/memory/models.py` |
| Per-turn pipeline | `src/echovessel/runtime/turn/coordinator.py` |
| Channel contract | `src/echovessel/channels/base.py` |
| Storage abstraction | `src/echovessel/memory/backend.py` |
| Voice provider contract | `src/echovessel/voice/base.py` |
| LLM provider contract | `src/echovessel/runtime/llm/base.py` |

---

## Verification before claiming done

Three commands must be green before "this is finished":

```
uv run pytest
uv run ruff check src/ tests/
uv run lint-imports
```

If any are red, the work isn't done — investigate the root cause, don't
disable the check.
