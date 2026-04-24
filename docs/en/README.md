# EchoVessel Documentation (English)

> **Audience**: developers encountering EchoVessel for the first time. This tree is clean, current, and focused on understanding and using the system today.

EchoVessel is a local-first Python daemon that runs a long-lived digital persona with hierarchical memory, voice, and multi-channel support. It is not a chatbot framework — it is a system for persistent digital presence.

---

## Getting started

**New here?** The **[First-Time Setup](./first-time-setup.md)** guide walks you from `git clone` + `uv sync --all-extras` to chatting with your first persona in the browser — about ten minutes end to end.

Quick summary for the impatient:

```bash
git clone https://github.com/AlanY1an/echovessel.git
cd echovessel
uv sync --all-extras
uv run echovessel init         # writes ~/.echovessel/config.toml
uv run echovessel run          # opens http://localhost:7777/ on first boot
```

`Ctrl-C` cleanly shuts the daemon down. See [`configuration.md`](./configuration.md) for every config field, and [`runtime.md`](./runtime.md) for what happens during startup.

---

## Architecture at a glance

> 🗺 **Full one-page visual tour**: [`architecture.html`](https://alanyian.com/projects/echovessel/docs/architecture.html) — open in a browser to get module layers, memory L1-L6 stack, message flow, cross-channel SSE mirror, full HTTP surface, iron rules, and release timeline on a single scrollable page.
>
> 🧠 **Memory in one diagram**: [`memory/layers.html`](https://alanyian.com/projects/echovessel/docs/memory/layers.html) — the simplest possible view of L1→L6 · six layers · how they connect · write / distill / read modes · credits the Stanford Generative Agents paper for the scoring formula.
>
> 🔄 **Runtime flows / "how layers wake up"**: [`architecture-flow.html`](https://alanyian.com/projects/echovessel/docs/architecture-flow.html) — companion page focused on per-turn activation sequence, real story trace ("I got a cat named 小黑"), retrieval ranking, policy gates, and SSE nervous-system events.

```
┌───────────────────────────────────────────────────────┐
│                    RUNTIME (daemon)                   │
│        startup · turn loop · LLM streaming · SIGHUP   │
└───────────────────────────────────────────────────────┘
              ▲                        ▲
              │                        │
    ┌─────────┴─────────┐    ┌─────────┴─────────┐
    │     CHANNELS      │    │     PROACTIVE     │
    │  debounce · turn  │    │  policy · trigger │
    └─────────┬─────────┘    └─────────┬─────────┘
              │                        │
              └────────────┬───────────┘
                           ▼
              ┌────────────┴────────────┐
              │         MEMORY          │
              │ L1·L2·L3·L4·L5·L6 │
              │   retrieve · consolidate│
              │      observer pattern   │
              └────────────┬────────────┘
                           │
                  ┌────────┴────────┐
                  │      VOICE      │
                  │  TTS · STT · clone
                  └─────────────────┘

Also: IMPORT pipeline — offline ingestion of external text into memory
```

Five core modules stacked in strict layers. **Runtime** orchestrates. **Channels** and **Proactive** sit above Memory and Voice. **Memory** and **Voice** sit above the core types. Imports flow through a separate pipeline that terminates in Memory.

---

## Module documentation

Each module gets exactly one page. Read in any order — pages cross-link where helpful.

| Module | What it is |
| --- | --- |
| 📖 [memory.md](./memory.md) | Hierarchical persona memory: L1 core blocks · L2 raw messages · L3 events · L4 thoughts / intentions / expectations · L5 entities + aliases · L6 episodic state · retrieve with rerank · observer pattern for lifecycle events · idempotent schema migration |
| 🗣️ [voice.md](./voice.md) | Text-to-speech, speech-to-text, and voice cloning. Provider abstraction over FishAudio, Whisper, and stubs. `VoiceService.generate_voice()` facade with on-disk caching |
| 📡 [channels.md](./channels.md) | Channel Protocol: how external transports (web, Discord, iMessage, WeChat) plug into the daemon. Debounce state machine for burst user input. The cross-channel unified-persona design |
| ⚡ [proactive.md](./proactive.md) | Autonomous messaging. Four policy gates (quiet hours · cold user · rate limit · no-in-flight-turn). Relationship triggers. Delivery inherits from `persona.voice_enabled` |
| ⚙️ [runtime.md](./runtime.md) | The daemon. Startup sequence, turn loop with streaming, SIGHUP config reload, atomic `voice_enabled` toggle, local-first disclosure audit |
| 📥 [import.md](./import.md) | Universal LLM importer. One pipeline for any text format (diary, chat log, novel, resume). LLM-driven content-type classification into memory's 5 target categories. Mandatory embed pass |

## Reference

| Page | For |
| --- | --- |
| 🔧 [configuration.md](./configuration.md) | Every field in `config.toml`. Defaults, valid values, when to change |
| 🛠 [contributing.md](./contributing.md) | Clone, `uv sync`, run tests, PR flow, and the two ironrules every contribution must respect |
| 🧪 [memory-testing.md](./memory-testing.md) | What the memory test suite pins · stage-by-stage from ingest to retrieve · plus the live-LLM eval layer |

---

## Design principles (the short version)

1. **Local-first**: all persona data lives on your machine. Outbound network is limited to your chosen LLM endpoint and (optionally) the voice provider. No telemetry. No phone-home.
2. **Layered architecture** (enforced by `import-linter` in CI): `runtime → channels | proactive → memory | voice → core`. A layer never imports from above.
3. **Memory is never filtered by `channel_id`.** A persona is one persona across every channel it speaks on. Retrieval, core-block loading, and recall-message queries all return the unified timeline; there is no `channel_id=` parameter anywhere in the memory read API, and there never will be.
4. **LLM prompts never leak transport identity.** System prompts, user prompts, and retrieved context blocks never contain `channel_id` or any transport-identifying token. The model has no idea whether it's on Web, Discord, or iMessage.

The deep version of each principle lives in the respective module docs.

---

## Status

These docs are under active development. Pages listed above are planned but most are not yet written. For anything that isn't covered here yet, read the source under `src/echovessel/` directly — every module has detailed docstrings.
