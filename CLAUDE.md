# EchoVessel — Project Guide

Local-first Python daemon that runs a digital persona with long-term memory, voice, and pluggable channels. `echovessel run` is the single entry point — it owns startup, the memory store, voice synthesis, and every bound channel (Web, Discord DM, etc).

---

## Tech stack

**Backend — Python 3.11+ (locked; do not suggest TS / Go / Rust replacements)**

- **Data:** SQLModel + SQLAlchemy 2.x, SQLite + `sqlite-vec` for vector search, Alembic for migrations
- **Config / schemas:** Pydantic v2
- **Web channel:** FastAPI + uvicorn + `sse-starlette`, `python-multipart`
- **Embeddings (extra):** `sentence-transformers`
- **LLM providers (extra):** `anthropic`, `openai`
- **Voice (extra):** `fish-audio-sdk` (sync-only — always wrap in `asyncio.to_thread`)
- **Discord DM (extra):** `discord.py` (lazy-imported; absence is non-fatal)
- **CLI:** click + rich
- **Tokens:** tiktoken

**Frontend — React 19 + Vite + TypeScript (locked; do not suggest vanilla / HTMX / Svelte)**

- Source: `src/echovessel/channels/web/frontend/`
- Build output: `src/echovessel/channels/web/static/` (served by FastAPI `StaticFiles`)
- Packaged into the wheel via the `hatch_build.py` custom build hook (runs `npm run build` pre-packaging)

---

## Project structure

```
src/echovessel/
├── core/            shared primitives, no upward deps
├── memory/          L1–L4 memory: ingest / retrieve/ / consolidate/ / forget
│   ├── retrieve/    core + scoring + search (D1)
│   └── consolidate/ core + phase_a + phase_bce + tracer (D2)
├── voice/           TTS / STT abstractions; types.py for value objects
├── channels/        web / discord / imessage / wechat adapters
│   └── web/routes/admin/   models + helpers split out (E1a)
├── proactive/       idle-trigger worker
│   ├── core/        Protocols, value types, config, errors
│   ├── engines/     decision logic (policy + generator)
│   └── execution/   delivery (scheduler / queue / delivery / audit)
├── prompts/         prompt templates + reflection/judge prompts
├── import_/         external conversation import pipeline
├── runtime/         daemon, launcher, CLI, app.py orchestration
│   ├── llm/         provider abstractions
│   ├── turn/        per-turn pipeline (coordinator, prompt_assembly, dispatcher, tracer)
│   ├── loops/       background asyncio tick loops (consolidate_worker, idle_scanner)
│   └── wiring/      DI composition root (memory / importer / prompts / observers)
└── resources/       bundled config.toml.sample + env.sample
```

**Layered architecture — enforced by `import-linter` (4 contracts):**

```
runtime  →  channels | proactive  →  memory | voice  →  core
```

Plus three forbidden / sub-package contracts:

- **`proactive` MUST NOT import `runtime` or `prompts`.**
- **`proactive` sub-packages layer execution → engines → core.** A reverse import is a regression — core is foundational, engines decide before execution delivers.
- **`runtime.wiring` MUST NOT import `runtime.turn` or `runtime.loops`.** Wiring is the composition root (build once, hand out everywhere); coupling it to per-turn or background-loop code breaks that model.

Any PR that crosses these layers fails `uv run lint-imports`. Don't add a shim to bypass — redesign the call path.

---

## Docs convention (single source of truth)

Per-module reference lives in **bilingual** pairs under `docs/`. Both files are canonical and must stay in sync:

- `docs/en/<module>.md` — English
- `docs/zh/<module>.md` — 中文

Modules covered: `channels`, `memory`, `runtime`, `voice`, `proactive`, `import`, `configuration`, `contributing`, `first-time-setup`.

**Rules:**

1. **Update in place.** No `architecture-v0.3.md` / `schema-v0.4.md` filenames — git log is the version history. A version suffix in a doc filename is a smell.
2. **Bilingual sync.** Any change to `docs/en/<module>.md` must land with the matching change to `docs/zh/<module>.md` in the same commit. If you can't do both, split the change — don't merge half.
3. **`docs/README.md` is the nav root** with the EN / 中文 split. Keep the TOC current.
4. **HTML visualizations** (`docs/architecture.html`, `docs/architecture-flow.html`, `docs/memory/layers.html`) link via the published URL `https://alanyian.com/projects/echovessel/docs/<file>.html`, never repo-relative paths. GitHub serves `.html` as raw source and drops readers into source view if linked relatively. HTML-to-HTML internal links stay relative (they resolve both on site and in local clone).
5. **Design rationale belongs in `docs/`**, not hidden. If a decision is worth preserving ("we tried X, it broke Y, so now we do Z"), write it into the module's canonical doc. Public docs carry the why, not just the what.

---

## Commands

| Task | Command |
|------|---------|
| Install dev + all extras | `uv sync --all-extras` |
| Run tests | `uv run pytest` |
| Ruff lint | `uv run ruff check src/ tests/` |
| Ruff format check | `uv run ruff format --check src/ tests/` |
| Import contracts | `uv run lint-imports` |
| Build wheel + sdist | `uv build` |
| Run daemon | `uv run python -m echovessel run` |
| Frontend dev server | `cd src/echovessel/channels/web/frontend && npm run dev` |
| Frontend prod build | `cd src/echovessel/channels/web/frontend && npm run build` |

**Test config:** pytest with `asyncio_mode = "auto"` — `async def test_*` runs without decorators. Tests live under `tests/` mirroring `src/echovessel/` structure.

Before claiming work is done: run `pytest`, `ruff check`, and `lint-imports` — all three must be green.

---

## Coding conventions

- **Ruff rules:** `E, F, I, W, N, UP, B, C4, SIM` · line-length 100 · `E501` ignored
- **Python target:** 3.11 — use modern syntax (`match`, `X | Y` unions, `list[...]`, `dict[...]`)
- **Type hints:** Pydantic v2 models for public / persisted schemas; `@dataclass` or plain typing for internal structs
- **Async-first:** the daemon runs a single asyncio event loop. Sync-only libraries wrap in `asyncio.to_thread` — the only legal blocking-I/O pattern. `voice/fishaudio.py` is the canonical example.
- **No backcompat shims.** Pre-1.0 project. When changing a public signature, update all call sites rather than leaving deprecated aliases or re-export stubs.
- **Comments are rare.** Code + well-named identifiers explain *what*; comments only explain non-obvious *why* (hidden constraint, subtle invariant, specific bug workaround).
- **Error handling:** validate at system boundaries only (user input, external APIs, config load). Trust internal code — don't defend against scenarios that can't happen.

---

## Commits

- **Subject = what, body = why.** Diff says how. Skip body if subject is self-explanatory.
- **Imperative mood** (`add X`, not `added X`); subject ≤ 72 chars.
- **One logical change per commit.** No mixed-topic commits.
- **Three green before committing:** `pytest`, `ruff check`, `lint-imports`.
- **Prefer Conventional Commits prefix** for single-area changes: `feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:` / `perf:`, optionally with scope — `fix(memory): ...`. Milestones / cross-module pushes use free-form `Name · description`.
- No CI enforcement; the reviewer is the checker. See `docs/en/contributing.md` § Commit messages for full rules.

---

## Product philosophy (locked)

- **Ship-bias.** Clever or elegant designs must pass three questions: (1) does it solve a real *current* problem? (2) is the added complexity justified by the win? (3) can we ship without it? Any "no" → take the simpler path. MVP preference is shippable over clever.
- **Local-first.** The daemon runs on the user's machine. No telemetry. No mandatory network for core memory / persona loops — LLM + voice are the only external deps, and they're pluggable.
- **One persona per daemon.** Multi-persona is explicitly deferred.
- **Cross-channel unified persona.** Memory is the shared substrate; channels are pure transport. A message sent via Discord is visible in Web history (and vice versa). The memory layer does NOT filter by channel.
