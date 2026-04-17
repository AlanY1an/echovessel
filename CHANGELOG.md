# Changelog

All notable changes to EchoVessel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Daemon control plane**: a dedicated HTTP listener on an independent loopback TCP port for operator-level lifecycle operations (`GET /health`, `POST /shutdown`, `POST /reload`). Orthogonal to the Web channel — separate uvicorn instance, separate FastAPI app, hardcoded `127.0.0.1` bind host, Host-header middleware that rejects off-host requests with 403. `echovessel stop` / `reload` / `status` now speak HTTP first and fall back to `SIGTERM` / `SIGHUP` only when the plane is unreachable. Pidfile upgraded to JSON v2 with `control_port` so the CLI knows where to POST; v1 integer pidfiles still read correctly and cause the signal fallback to trigger transparently. See `docs/en/runtime.md` (Control plane) and `develop-docs/initiatives/_active/2026-04-daemon-control/` for the full initiative.

### Fixed

- `consolidate.{trivial_message_count, trivial_token_count, reflection_hard_gate_24h}` were listed in `HOT_RELOADABLE_CONFIG_PATHS` — the admin `PATCH /api/admin/config` accepted them — but `Runtime.reload()` only updated `ctx.config`, not the live `ConsolidateWorker` (which was constructed once at boot with the old values). Tuning these thresholds through the admin UI now takes effect without a restart.

### Changed

- `Runtime.reload()` returns a `list[str]` of reloaded component names instead of `None`. HTTP `/reload` renders this list in its JSON response so the CLI can print `reloaded (via control plane): llm, consolidate.trivial_message_count`. Reload is now serialized via an `asyncio.Lock` so HTTP + `SIGHUP` invocations cannot race on `ctx.config` mid-swap.
- `docs/{en,zh}/configuration.md`'s reload matrix is now field-by-field (13 hot-reloadable fields tabulated) instead of section-level. Matches `HOT_RELOADABLE_CONFIG_PATHS` literal-for-literal; backed by `tests/runtime/test_reload_matrix.py` so drift flips CI red.

## [0.0.1] - 2026-04-15

First tagged release of EchoVessel — an early-alpha local-first AI persona daemon
for Python 3.11+. This release contains a working CLI-managed daemon, long-term
memory, an LLM provider abstraction, voice synthesis, a Web UI, and a Discord DM
channel. The scope is intentionally narrow; everything outside the list below is
either deferred to v0.0.2+ or is a placeholder UI surface that does not yet talk
to a backend (see **Known Limitations**).

### Added

#### CLI & runtime

- `echovessel init` writes a starter `~/.echovessel/config.toml` from the bundled sample.
- `echovessel run | stop | reload | status` manage the daemon lifecycle through a pidfile under `~/.echovessel/`.
- Auto-loads `~/.echovessel/config.toml` and `./.env` (from the daemon's working directory) on startup.
- `SIGTERM` / `SIGINT` trigger a graceful shutdown that flushes in-flight turns.
- `SIGHUP` (also reachable via `echovessel reload`) reloads `config.toml` and rebuilds the LLM provider in place; in-flight turns keep the old provider via Python reference semantics.

#### Memory

- Four-tier memory schema: L1 core blocks (persona / self / user / mood / relationship), L2 raw conversation log, L3 episodic events, L4 reflections.
- SQLite backend with FTS5 full-text search and [sqlite-vec](https://github.com/asg017/sqlite-vec) vector indexes.
- Local embeddings via `sentence-transformers` (`embeddings` extra). First run downloads the model once (~90 MB).
- Background consolidation worker promotes closed sessions into events and reflections.
- Idempotent `ensure_schema_up_to_date()` migration runs at every boot.

#### LLM providers

- `openai_compat` — works against OpenAI, OpenRouter, Ollama, LM Studio, vLLM, DeepSeek, Together, Groq, xAI, Moonshot, and any other endpoint that speaks the OpenAI chat completions schema.
- `anthropic` — official `anthropic` SDK.
- `stub` — canned replies; used by the test suite and useful for offline smoke tests.
- Provider is selected in `[llm]` of `config.toml` and can be swapped live via `SIGHUP` / `echovessel reload`.

#### Voice

- FishAudio TTS via the `fish-audio-sdk` package (`voice` extra).
- `stub` TTS provider for tests and offline development.
- Per-persona `voice_id` configured under `[persona]`.
- Synthesised MP3 clips are cached on disk under `~/.echovessel/voice_cache/` keyed by message id, avoiding re-billing identical lines.
- `[persona].voice_enabled` toggle controls whether persona replies are also delivered as TTS audio. The toggle persists atomically (write-then-swap) so a crashed write never corrupts `config.toml`.

#### Channels

- **Web channel** — FastAPI backend with SSE token streaming, served at a configurable host/port (default `127.0.0.1:7777`). Ships a React 19 + Vite + TypeScript SPA bundled into the wheel. The SPA covers:
    - first-run **onboarding** (write the persona's identity block and start the daemon)
    - **chat** with token-by-token streaming
    - **admin → persona** editing of the five L1 core blocks
    - **admin → voice** toggle backed by `POST /api/admin/persona/voice-toggle`
- **Discord channel** — DM ingestion via `discord.py` (`discord` extra), gated by an optional allowlist. A debounce window (default 2 s) coalesces fast bursts of DMs into a single turn. Voice replies post as native OGG Opus voice messages when `[persona].voice_enabled = true` and `ffmpeg` is on PATH; without `ffmpeg` the channel falls back to text.

#### Packaging

- `hatch` wheel + sdist build targets configured (not yet released to PyPI — v0.0.1 runs from a `git clone` + `uv sync --all-extras`).
- A custom `hatch_build.py` build hook rebuilds the React frontend during `uv build` so any future wheel carries pre-built static assets and contributors do not need Node.js just to run the daemon.
- The bundled `config.toml.sample` is shipped as a package resource and is what `echovessel init` writes.
- Wheel is ~224 KB (frontend bundle excluded except the built static output); sdist is ~231 KB.

#### Tests

- 916 tests pass (3 skipped), covering memory, runtime, voice, channels, proactive policy, and import pipeline modules. Coverage is unit-level and module-integration-level; see **Known Limitations** for what is and isn't tested.

### Changed

- **Cross-channel live sync.** `SSEBroadcaster` is now owned by the runtime and mirrors every channel's turn events (user message, streaming tokens, completion, voice-ready) to all Web SSE subscribers. Each event carries a `source_channel_id`; the Web chat timeline tags non-Web messages with a 📱 Discord / 💬 iMessage pill. Fulfils spec Goal G5 (cross-channel unified persona) for the live view.
- **Chat history backfill.** New `GET /api/chat/history?limit=50&before=<turn_id>` returns the most-recent `recall_messages` across every channel (per iron rule D4) with `has_more` + `oldest_turn_id` for cursor pagination. The Web chat hook fetches this on mount and prepends it to the timeline, turning the Web frontend into a true "god-view" observer of every past turn regardless of origin.
- **Admin page surfaces are now real.** Events / Thoughts / Voice / Cost / Config tabs all back onto live endpoints (list, forget, clone wizard, cost summary, safe-subset config edit). Persona tab gained a `导入历史材料 →` CTA that drives the Import wizard.
- **Import pipeline is wired end-to-end.** `/api/admin/import/*` admin routes (upload, estimate, start, cancel, events SSE) reach the `ImporterFacade` constructed in runtime startup. The Web Import wizard (`/admin/import`) walks a real 3-step flow (upload → estimate → live progress). Onboarding path 2 ("上传材料") drives the same pipeline + an LLM bootstrap step that drafts initial core blocks for user review.
- **Live mood + session boundary** now stream to the Web chat timeline: mood changes reflect in the header in real time, and session rollover renders as a timestamped horizontal marker.
- GitHub Actions CI enforces `ruff check`, `lint-imports`, and `pytest` on every PR and push to `main`, across ubuntu-latest + macos-latest × Python 3.11.

### Fixed

- **Cost ledger now persists.** `cost_logger.LLMCall` is imported before `create_all_tables()` so the `llm_calls` table actually gets created on fresh boots. Previously every LLM call emitted `cost_logger: failed to persist LLM call: no such table: llm_calls` warnings and the Cost admin tab had nothing to show.

### Known Limitations

This is an early-alpha release. The following surfaces remain deliberately out of scope for v0.0.1:
- **LLM error handling has only classification-level test coverage.** The provider error hierarchy (`LLMTransientError` / `LLMPermanentError` / `LLMBudgetError`) is exercised via helper unit tests; end-to-end retry / degradation behaviour under real network failures is not yet covered.
- **Runtime CLI tests are smoke-level only.** `echovessel init`, `run`, `status`, `stop`, and `reload` are exercised by the launcher test suite (17 cases: config file round-trip, pidfile lifecycle, signal dispatch, subprocess SIGTERM path), but longer-lived behaviours (24 h-window reflection gating, multi-day idle scanner, real provider failure recovery) are not in the matrix.
- **Two `runtime/config.py` fields remain informational-only** (`persona.initial_core_blocks_path`, `channels.web.static_dir`). The rest of the schema — including the four `[memory]` / `[consolidate]` tuning knobs — is now consumed by the runtime.
- **Platform support: macOS and Linux only.** Windows is untested and unsupported in this release.
- **Discord voice messages require `ffmpeg`** on PATH (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu). Without it the Discord channel silently falls back to text replies.
- **iMessage and WeChat channel scaffolds are not present in v0.0.1.** They are listed in the long-term roadmap but no code ships in this release.

[0.0.1]: https://github.com/AlanY1an/echovessel/releases/tag/v0.0.1
