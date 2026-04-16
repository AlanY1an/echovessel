# Changelog

All notable changes to EchoVessel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- **Cross-channel live sync.** `SSEBroadcaster` is now owned by the runtime and mirrors every channel's turn events (user message, streaming tokens, completion, voice-ready) to all Web SSE subscribers. Each event carries a `source_channel_id`; the Web chat timeline tags non-Web messages with a 📱 Discord / 💬 iMessage pill.
- **Chat history backfill.** New `GET /api/chat/history?limit=50&before=<turn_id>` returns the most-recent `recall_messages` across every channel (per iron rule D4) with `has_more` + `oldest_turn_id` for cursor pagination. The Web chat hook fetches this on mount and prepends it to the timeline, turning the Web frontend into a true "god-view" observer of every past turn regardless of origin.
- GitHub Actions CI enforces `ruff check`, `lint-imports`, and `pytest` on every PR and push to `main`, across ubuntu-latest + macos-latest × Python 3.11.

### Known Limitations

This is an early-alpha release. The following surfaces are intentionally not finished
in v0.0.1; each is tracked for **v0.0.2** or later.

- **Import flow is not wired into the daemon.** The Onboarding "上传材料让它自动生成" path lands on a placeholder screen. The `import_/` pipeline module is implemented and unit-tested, and an `ImporterFacade` exists on the runtime, but no `/api/admin/import/*` HTTP routes are exposed yet, so neither the Web SPA nor the CLI can drive a real import. **Targeted for v0.0.2.**
- **Admin → Events / Thoughts / Config tabs are placeholders.** They render the section chrome and (for Events / Thoughts) a server-side row count from `/api/state`, but there is no list view, no per-row delete, and no live cost / model display. The underlying `memory/forget.py` deletion API is implemented in code but has no production caller yet.
- **Live mood updates and session-rollover markers are not surfaced in the Web chat timeline.** The underlying signals exist inside the runtime but are not broadcast to the frontend; Web chat shows a snapshot mood at render time only. **Targeted for v0.0.2.**
- **LLM error handling has only classification-level test coverage.** The provider error hierarchy (`LLMTransientError` / `LLMPermanentError` / `LLMBudgetError`) is exercised via helper unit tests; end-to-end retry / degradation behaviour under real network failures is not yet covered.
- **Runtime CLI tests are smoke-level only.** `echovessel init`, `run`, `status`, `stop`, and `reload` are exercised by the launcher test suite (17 cases: config file round-trip, pidfile lifecycle, signal dispatch, subprocess SIGTERM path), but longer-lived behaviours (24 h-window reflection gating, multi-day idle scanner, real provider failure recovery) are not in the matrix.
- **Two `runtime/config.py` fields remain informational-only** (`persona.initial_core_blocks_path`, `channels.web.static_dir`). The rest of the schema — including the four `[memory]` / `[consolidate]` tuning knobs — is now consumed by the runtime.
- **Platform support: macOS and Linux only.** Windows is untested and unsupported in this release.
- **Discord voice messages require `ffmpeg`** on PATH (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu). Without it the Discord channel silently falls back to text replies.
- **iMessage and WeChat channel scaffolds are not present in v0.0.1.** They are listed in the long-term roadmap but no code ships in this release.

[0.0.1]: https://github.com/AlanY1an/echovessel/releases/tag/v0.0.1
