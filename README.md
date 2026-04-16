# EchoVessel

[![CI](https://github.com/AlanY1an/echovessel/actions/workflows/ci.yml/badge.svg)](https://github.com/AlanY1an/echovessel/actions/workflows/ci.yml)

> 🌐 **Read this in another language:** [中文](./README.zh.md)

*Some people leave behind memories.  
Some leave behind a voice.  
Some stay with us in fragments: a tone, a rhythm, a way of speaking that never fully disappears.*

**EchoVessel** is an open-source engine for building digital personas that can remember, respond, evolve, and stay present across time.

It is designed for people who want to create characters, companions, fictional personas, personal echoes, or consented digital counterparts with:

- identity and style
- long-term memory
- relationship evolution
- voice interaction
- local-first privacy

EchoVessel is not a generic chatbot.  
It is a vessel for presence.

---

## Getting Started (v0.0.1)

v0.0.1 is an early-alpha tagged release. It ships a local-first daemon built on the full 5-module stack (memory / voice / channels / proactive / runtime), with a working Web channel (chat + persona-block admin + voice toggle + first-run onboarding) and a working Discord DM channel. Several admin surfaces are intentional placeholders in this release — see **Known Limitations** in [`CHANGELOG.md`](./CHANGELOG.md) for the full list of what's deferred to v0.0.2+. Tested on macOS and Linux; Windows is not yet supported.

### Install

EchoVessel targets Python **3.11+**. Install from PyPI with [`uv`](https://github.com/astral-sh/uv) (recommended) or plain `pip`:

```bash
uv pip install echovessel
# or: pip install echovessel
```

Optional extras pull in the heavier stacks on demand:

```bash
uv pip install 'echovessel[embeddings,llm,voice,discord]'
```

- `embeddings` — local sentence-transformers embedder
- `llm` — OpenAI / Anthropic SDKs
- `voice` — FishAudio TTS SDK
- `discord` — `discord.py` for the Discord DM channel

End users do **not** need Node.js — the wheel embeds the pre-built React bundle.

### First Launch

EchoVessel reads `~/.echovessel/config.toml`. Create a starter config from the bundled sample:

```bash
echovessel init
```

Secrets live in `~/.echovessel/.env` — the daemon auto-loads it on startup. Typical keys:

```
OPENAI_API_KEY=sk-...
FISH_AUDIO_KEY=...              # optional · FishAudio TTS
ECHOVESSEL_DISCORD_TOKEN=...    # optional · Discord bot token
```

Edit `~/.echovessel/config.toml` to pick an LLM provider — zero-config works with any OpenAI-compatible endpoint (set `OPENAI_API_KEY`), or switch to `anthropic` + `ANTHROPIC_API_KEY`, or `ollama` (local, no key). See the sample for every option.

**Smoke-test without any API key**: set `[llm].provider = "stub"` in the config to boot the daemon with canned stub replies — useful for verifying the install.

### Run the Daemon

```bash
echovessel run
```

First startup downloads the sentence-transformers embedder (~90MB, one-time). Subsequent boots are instant.

Expected log on clean boot:
```
schema migration: created table core_block_appends
importer facade: built
memory observer: registered
EchoVessel runtime started | ...
local-first disclosure: outbound = only <llm endpoint>; embedder runs locally; no telemetry
```

Data lives in `~/.echovessel/memory.db` (SQLite + sqlite-vec). Logs in `~/.echovessel/logs/`.

### Web Channel

The daemon serves the React UI directly at `http://127.0.0.1:7777/` (host/port configurable under `[channels.web]` in `config.toml`). Open it in a browser — that's it. No `npm`, no separate dev server.

If you want to rebuild the frontend from source (contributors only), the sources live in `src/echovessel/channels/web/frontend/`. Run:

```bash
cd src/echovessel/channels/web/frontend
npm install
npm run build
```

The hatch build hook copies the output into `src/echovessel/channels/web/static/`, which ships inside the wheel.

### Discord Channel

EchoVessel can talk to you over Discord DMs — text replies plus native OGG Opus voice messages when voice is enabled.

1. Create an application + bot at <https://discord.com/developers/applications>. Under **Bot → Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**.
2. Copy the bot token into `~/.echovessel/.env`:
   ```
   ECHOVESSEL_DISCORD_TOKEN=...
   ```
3. In `~/.echovessel/config.toml`:
   ```toml
   [channels.discord]
   enabled = true
   token_env = "ECHOVESSEL_DISCORD_TOKEN"
   debounce_ms = 2000
   # allowed_user_ids = [123456789012345678]   # optional allowlist
   ```
4. Invite the bot to your account (OAuth2 URL generator → `bot` scope + DM permissions), then DM it. Incoming messages are debounced (2s default) and dispatched as a single turn.
5. Voice messages send as native Discord voice bubbles when `[persona].voice_enabled = true` **and** `ffmpeg` is on PATH — the channel converts FishAudio's MP3 output to OGG Opus on the fly. Install with `brew install ffmpeg` (macOS) or `apt install ffmpeg` (Debian/Ubuntu). Without ffmpeg the Discord channel falls back to text.

### Voice

EchoVessel uses [FishAudio](https://fish.audio) for TTS. Put `FISH_AUDIO_KEY` in `~/.echovessel/.env` and pick a `voice_id` under `[persona]` in `config.toml`. Set `[persona].voice_enabled = true` to emit voice alongside text. The Discord voice-message path additionally requires `ffmpeg` (MP3 → OGG Opus conversion).

### Running Tests

```bash
uv run pytest tests/ -q                # 839 tests across memory / runtime / voice / proactive / channels / import / integration
uv run ruff check src/ tests/          # lint
uv run lint-imports                    # layered architecture contracts
```

### Project Layout

```
src/echovessel/
├── core/            — shared types, enums, utilities
├── memory/          — L1-L4 memory · SQLite + sqlite-vec · observers + migrations
├── voice/           — TTS + STT + voice cloning (FishAudio + Whisper + stub)
├── proactive/       — autonomous messaging · policy gates · delivery
├── channels/        — Channel Protocol + per-channel adapters (web + discord)
│   ├── web/         — FastAPI routes + SSE + embedded React bundle
│   │   ├── frontend/ — React 19 + Vite + TS source (contributors)
│   │   └── static/  — built bundle served by the daemon
│   └── discord/     — discord.py bot · DM ingestion · OGG Opus voice
├── import_/         — universal LLM importer pipeline (text → memory)
├── prompts/         — system prompts for extraction / reflection / interaction
├── resources/       — bundled config.toml.sample
└── runtime/         — daemon · turn dispatcher · LLM providers · CLI
```

### Current Status (v0.0.1)

- ✅ **Daemon**: boots end-to-end, all startup wiring verified in log, 839 tests passing (10 skipped)
- ✅ **Memory**: L1–L4 hierarchy, idempotent schema migration, observer hooks, 4/4 MVP eval metrics passing (Over-recall FP Rate 0.08 ≤ 0.15 target)
- ✅ **Voice**: FishAudio TTS + stub TTS provider · `VoiceService.generate_voice()` facade · per-persona `voice_id` · on-disk MP3 cache
- ✅ **Proactive**: policy engine · four gates including `no_in_flight_turn` · delivery inherits `persona.voice_enabled`
- ✅ **Runtime**: streaming turn loop (IncomingTurn + text delta) · atomic persona voice toggle · `SIGHUP` hot reload · memory observer wiring
- ✅ **Web channel** (production paths): FastAPI + SSE streaming · embedded React 19 bundle · onboarding flow · chat with token streaming · admin → persona core-block editing · admin → voice toggle
- 🚧 **Web channel** (placeholders this release): admin → events / thoughts / voice cloning / config tabs render section chrome only; the Onboarding "上传材料" import path is a coming-soon screen
- ✅ **Discord channel**: DM ingestion with debounce · text replies · native OGG Opus voice messages (ffmpeg required)
- ✅ **Import pipeline** (library only): universal LLM importer · five content-type classification · `self_block` side path · mandatory embed pass — *no HTTP route is exposed yet, so neither the Web SPA nor the CLI can drive a real import in this release*
- ⚠️ **Platform**: macOS and Linux tested; Windows is not yet supported
- 🔜 **v0.0.2 targets**: wire `/api/admin/import/*` routes + Web import wizard · Admin events / thoughts list views · live mood / session-boundary SSE feed on the Web chat

---

## What It Is

EchoVessel is a local-first `Digital Persona Engine`.

It lets users define or distill a persona from structured settings and source material, then run that persona through a long-term interaction system with memory, voice, and relational behavior.

The goal is not to generate one-off replies.  
The goal is to create a persona that feels continuous.

## Supported Persona Sources

EchoVessel is intended for:

- fictional characters
- original characters
- self personas
- consented digital counterparts
- memorial, creative, or research-oriented reconstructions

EchoVessel is not intended to be an impersonation tool for pretending to be a real person in external communication.

## Core Ideas

### 1. Persona Definition

Each persona can be shaped by:

- name
- identity
- age
- background
- personality
- values
- relationship role
- speaking style

### 2. Style Distillation

A persona's interaction style can be learned from:

- chat logs
- novels
- scripts
- dialogue lines
- mixed source materials

The aim is not shallow copying.  
The aim is coherent behavioral style.

### 3. Memory System

EchoVessel treats memory as a first-class system:

- factual memory
- preference memory
- emotional patterns
- event timeline
- relationship memory

The hard problem is not just storing memory, but deciding:

- what should be remembered
- how it should be represented
- when it should influence behavior

### 4. Relationship Evolution

EchoVessel does not depend on a visible "affection meter."

Instead, personas evolve through internal relational state, expressed through:

- tone shifts
- naming changes
- different levels of initiative
- deeper contextual recall
- adaptive comfort and support patterns

### 5. Interaction Layer

Planned interaction modes include:

- text chat
- voice messages
- proactive messaging
- greetings and check-ins
- group chat presence
- multi-persona interaction
- AI-generated photo sharing

### 6. Voice Layer

Voice is a core part of the project, not an optional extra.

The system is designed to support persona voice output and voice message exchange, with local-first or self-hosted voice pipelines where possible.

## Design Principles

- local-first by default
- privacy matters
- memory is the moat
- voice is part of identity
- relationships should evolve through behavior, not exposed scores
- personas should feel persistent, not stateless

## Early MVP Direction

The first usable version of EchoVessel should likely focus on:

- one persona
- text chat
- voice messages
- long-term memory
- relationship state
- proactive messaging
- a simple web interface
- one external channel adapter

## Long-Term Direction

EchoVessel may grow toward:

- persona marketplace
- import/exportable persona packs
- plugin-based adapters and behaviors
- multi-persona social spaces
- world simulation and narrative scenarios
- self-hosted deployments across messaging channels

## Why Open Source

EchoVessel should remain open, inspectable, modifiable, and personal.

This project is built on the belief that digital presence, memory systems, and intimate computing tools should not belong only to closed commercial platforms.

## Current Status

EchoVessel **v0.0.1** is an early-alpha tagged release — the core 5-module stack is in place, the daemon boots end-to-end, and the Web + Discord channels work for chat. Several admin surfaces are placeholders this release; see **Known Limitations** in [`CHANGELOG.md`](./CHANGELOG.md) and the **Current Status (v0.0.1)** breakdown near the top of this README.

- ✅ 5-module architecture (memory / voice / channels / proactive / runtime) implemented and tested
- ✅ CLI daemon boots and runs end-to-end
- ✅ 839 tests passing (10 skipped) · layered import contracts enforced · 4/4 MVP eval metrics passing
- ✅ Web channel: daemon serves the embedded React bundle on port 7777 (chat + onboarding + persona-block admin + voice toggle)
- ✅ Discord DM channel: text + native OGG Opus voice messages (requires ffmpeg)
- 🚧 Import flow not yet wired into the daemon · admin Events / Thoughts / Voice cloning / Config tabs are placeholders · targeted for v0.0.2
- ⚠️ macOS / Linux only — Windows not yet supported
- 🔜 iMessage / WeChat channels, persona self-selecting voice delivery, and multi-persona are scheduled for later releases

## Name

**EchoVessel** means carrying an echo long enough for it to become presence.
