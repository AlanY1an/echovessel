# Repo-wide conventions

Rules that apply across every system. System-specific patterns live in
each `<system>/conventions.md`.

> Re-confirm against `pyproject.toml` and `src/echovessel/__init__.py`
> when these matter for a code change — config drifts faster than docs.

---

## Async-first daemon

The runtime owns one asyncio event loop. Sync-only third-party libraries
**must** wrap in `asyncio.to_thread`. The canonical example is
`src/echovessel/voice/fishaudio.py` — `fish-audio-sdk` is sync-only, so
every call goes through `asyncio.to_thread`.

If you find yourself writing `time.sleep`, `requests.get`, or any
blocking I/O outside an `asyncio.to_thread` wrapper, you've introduced a
loop-blocker. The fix is the wrapper, not a thread pool of your own.

---

## Layered import contracts

`uv run lint-imports` enforces four contracts (defined in
`pyproject.toml`):

```
runtime  →  channels | proactive  →  memory | voice  →  core
```

Plus three sub-package rules:

1. `proactive` MUST NOT import `runtime` or `prompts`.
2. `proactive` sub-packages: `execution → engines → core`.
3. `runtime.wiring` MUST NOT import `runtime.turn` or `runtime.loops`.

A failing contract is a redesign signal, never a "add `# noqa`" signal.
If a memory function needs runtime-side state, the boundary is wrong —
either pass the state in as a parameter / callable, or move the function
to runtime.

---

## Type hints

- **Pydantic v2** for public / persisted schemas (config, request /
  response bodies, anything that crosses a process boundary).
- **`@dataclass(slots=True)`** for internal value types (DTOs that stay
  inside one module).
- **Plain typing** otherwise — `list[X]`, `dict[K, V]`, `X | None`.
  Python 3.11+ syntax, no `Optional`, no `Union`, no `List`.

---

## Ruff configuration (from `pyproject.toml`)

- Target: `py311`
- Line length: **100** (E501 is ignored — long lines are allowed when
  natural, but don't be ridiculous)
- Selected rules: `E, F, I, W, N, UP, B, C4, SIM`
- Use `match` statements, `X | Y` unions, dict / list builtin generics

`uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/`
must both pass before commit.

---

## No backcompat shims

This is a pre-1.0 project. When changing a public signature:

- Update every call site in the same commit.
- Do **not** leave deprecated aliases, re-export stubs, or "removed in
  next version" comments.
- Do **not** add a feature flag for code you can simply replace.

If you can't update all call sites, the change is too big — split it.

---

## Comments are rare

Code + well-named identifiers should explain *what*. Comments only
explain *why* when the why is non-obvious — a hidden constraint, a
subtle invariant, a workaround for a specific bug. If removing a comment
wouldn't confuse the next reader, delete it.

Never write multi-paragraph docstrings or multi-line comment blocks for
ordinary functions. Module-level docstrings can be longer when they
carry a contract (see `memory/observers.py`, `memory/retrieve/core.py`).

---

## Error handling

Validate at system boundaries only:

- User input (web/Discord/iMessage payloads)
- External APIs (LLM responses, FishAudio output)
- Config load (Pydantic models in `runtime/config.py`)

Trust internal code. Don't defend against scenarios that can't happen.
The DB CHECK constraints (e.g. `concept_nodes.imported_from XOR
source_session_id`) exist to fail loud if a bug breaks the invariant —
not as a runtime branching mechanism.

---

## Test layout

`tests/` mirrors `src/echovessel/`. Pytest is configured with
`asyncio_mode = "auto"` — `async def test_*` runs without decorators.

Three commands gate "done":

```
uv run pytest
uv run ruff check src/ tests/
uv run lint-imports
```

If you can't get all three green, the change isn't ready.

---

## Commits

- Subject = what, body = why (skip body if subject is self-explanatory).
- Imperative mood, ≤ 72 chars.
- One logical change per commit.
- Conventional Commits prefix preferred for single-area changes:
  `feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:` / `perf:`.
- Three green (pytest / ruff / lint-imports) before commit.

---

## Docs

Bilingual canonical docs live under `docs/en/` + `docs/zh/` for humans;
agent context lives here under `docs/ai/`. Both human files (en + zh)
must change in the same commit when one moves. Agent docs are derived
from code, not from human docs — when they conflict, fix the agent doc
to match the code.
