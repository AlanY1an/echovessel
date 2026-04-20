# Configuration

EchoVessel reads all of its runtime state from a single TOML file. This page is the reference for every field: what it does, what values it accepts, and when you should change it.

## File location and format

The daemon looks for its configuration at `~/.echovessel/config.toml`. The starting point is the annotated template bundled inside the installed package at `echovessel/resources/config.toml.sample`. Create a working copy with the `init` subcommand:

```bash
uv run echovessel init
```

This writes `~/.echovessel/config.toml` from the bundled sample. Pass `--force` to overwrite an existing file, or `--config-path PATH` to target a different location. The `init` command works identically for source checkouts and wheel installs — it reads the sample via `importlib.resources`, not by filesystem path.

The file uses standard TOML syntax with one convention specific to EchoVessel: fields whose name ends in `_env` hold the **name of an environment variable**, not the secret itself. The daemon reads the actual value from the environment at startup. This keeps API keys, bot tokens, and provider credentials out of any file that can be copy-pasted or committed to version control by accident. If you set `api_key_env = "OPENAI_API_KEY"`, the daemon will read `os.environ["OPENAI_API_KEY"]` when it builds the LLM provider.

The daemon loads `config.toml` exactly once at startup. Changes to most sections only take effect on the next boot. A few sections can be hot-reloaded by sending `SIGHUP` to the running daemon — the table at the end of this page shows which. Admin operations like toggling `persona.voice_enabled` do not go through the TOML file; they have dedicated APIs that atomically rewrite the relevant field and update the running process in one step.

## `[runtime]`

Process-level settings for the daemon itself.

| Field | Default | Notes |
| --- | --- | --- |
| `data_dir` | `~/.echovessel` | Where everything lives: database, logs, voice cache, cloning fingerprint cache. If you set it to an absolute path, that path must be writable by the user running the daemon. |
| `log_level` | `"info"` | One of `"debug"`, `"info"`, `"warn"`, `"error"`. `"debug"` is extremely chatty and includes every LLM prompt — useful only when chasing a bug. |
| `turn_timeout_seconds` | `120` | Per-turn wall-clock cap for the serial TurnDispatcher handler. A handler exceeding this budget — typically a hung `llm.stream` — is cancelled so later messages on any channel are not blocked behind it. Set to `0` to disable (not recommended). |

## `[persona]`

Identity fields for the single persona this daemon instance serves. The phase 1 release supports exactly one persona per daemon process.

| Field | Default | Notes |
| --- | --- | --- |
| `id` | `"default"` | A short stable identifier used as the primary key in memory tables. Change it only before first boot — once the database has rows keyed to this id, changing it orphans everything. |
| `display_name` | `"Your Companion"` | What the persona calls itself in prompts and in UI. You can change this between boots without any data migration. |
| `voice_id` | unset | The reference-model id you got back from a voice cloning run. Leave unset to disable voice for this persona. |
| `voice_provider` | unset | Usually not needed — the provider is inferred from the `[voice]` section. |
| `voice_enabled` | `false` | Whether the persona's replies are delivered as voice in addition to text. This field is **not** changed by editing the TOML; it has a dedicated admin API that atomically rewrites the file and updates the running daemon in one step. Editing the file directly and rebooting works too, but the two paths should not be mixed.

## `[memory]`

The memory module's knobs for storage and retrieval.

| Field | Default | Notes |
| --- | --- | --- |
| `db_path` | `"memory.db"` | Path to the SQLite file. Relative paths are resolved against `data_dir`. The special value `":memory:"` runs everything in a throwaway in-memory database, which is useful for tests and local experiments but loses all state on shutdown. |
| `embedder` | `"all-MiniLM-L6-v2"` | Sentence-transformers model name. The daemon downloads this on first boot (~90 MB) and caches it under `data_dir/embedder.cache/`. If you change it, delete the database too — existing embeddings were produced by the old model and are not comparable. |
| `retrieve_k` | `10` | How many memory hits the retrieve pipeline returns to the prompt assembler. Higher values give the persona more context but inflate token cost. |
| `relational_bonus_weight` | `1.0` | Multiplier on the relational-bonus term in the rerank scorer. Raise it to make the persona lean harder on memories that involve the user's named relationships. |
| `recent_window_size` | `20` | How many recent L2 messages the prompt assembler always includes unconditionally, regardless of what retrieval returns. |

## `[llm]`

Which model powers the persona and how to talk to it.

| Field | Default | Notes |
| --- | --- | --- |
| `provider` | `"openai_compat"` | One of `"openai_compat"`, `"anthropic"`, `"stub"`. `openai_compat` covers any OpenAI-compatible endpoint, which in practice means OpenAI itself, OpenRouter, Ollama, LM Studio, vLLM, DeepSeek, Groq, Together, Fireworks, xAI, Perplexity, Moonshot, and Zhipu GLM. `anthropic` uses the native Anthropic SDK. `stub` returns canned replies and makes no network calls — the easiest way to verify a fresh install. |
| `api_key_env` | `"OPENAI_API_KEY"` | Environment variable holding the API key. Set to `""` for providers that do not need authentication, such as a local Ollama instance. |
| `base_url` | unset | Override the API base URL. Required for any `openai_compat` provider that is not OpenAI itself. |
| `model` | unset | Pin a single model across every semantic tier. Takes precedence over `tier_models`. |
| `max_tokens` | `1024` | Upper bound on reply length. |
| `temperature` | `0.7` | Sampling temperature. |
| `timeout_seconds` | `60` | Request timeout. |

### `[llm.tier_models]`

EchoVessel classifies its LLM calls into three semantic tiers — `small`, `medium`, `large` — and lets you map each tier to a different concrete model. Extraction and reflection are `small` tier (they run often and tolerate weaker models), the judge pass is `medium`, and the persona's live replies plus proactive generation are `large`.

```toml
[llm.tier_models]
small  = "gpt-4o-mini"
medium = "gpt-4o"
large  = "gpt-4o"
```

If `model` is set, it wins across every tier and `tier_models` is ignored. If neither is set, the provider uses its own defaults (for example the Anthropic provider falls back to `haiku` / `sonnet` / `opus`).

### Common `[llm]` recipes

**Zero-config OpenAI** — set `OPENAI_API_KEY` in your shell and leave the section at defaults.

**Ollama running locally** — no key required:

```toml
[llm]
provider    = "openai_compat"
base_url    = "http://localhost:11434/v1"
api_key_env = ""

[llm.tier_models]
small  = "llama3:8b"
medium = "llama3:70b"
large  = "llama3:70b"
```

**OpenRouter** — one account, any model:

```toml
[llm]
provider    = "openai_compat"
base_url    = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
model       = "anthropic/claude-sonnet-4"
```

**Anthropic native** — use the first-party SDK instead of the OpenAI wire format:

```toml
[llm]
provider    = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"
```

**Offline smoke test** — stub provider, no network, canned replies. This is the safest way to verify a fresh install before committing to a real provider:

```toml
[llm]
provider    = "stub"
api_key_env = ""
```

### Admin Cost tab

The **Admin → Config → Cost** card in the Web UI shows estimated LLM spending drawn from the `llm_calls` table. Token counts are taken directly from the SDK's usage response when the provider returns one; calls with no provider-reported usage fall back to a local token estimate. All figures are labelled as estimates in the UI — authoritative billing lives on the provider's own dashboard.

**"(of which N cached)"** — this annotation appears next to the token total when the provider reports prompt-cache hits for the selected time window. Two providers populate it:

- **Anthropic** (`provider = "anthropic"`) — every streaming response includes `cache_read_input_tokens` (tokens served from Anthropic's 5-minute prompt cache, billed at roughly one-tenth of the standard input rate) and `cache_creation_input_tokens` (tokens written into the cache on this call, billed at a small premium). A high cached fraction means the actual cost is significantly lower than the raw token count suggests.
- **OpenAI** (`provider = "openai_compat"` pointing at OpenAI or a compatible endpoint that honours `stream_options.include_usage`) — reports cached tokens inside `prompt_tokens_details`; cached tokens are billed at half the standard input rate.

The annotation is only shown when at least one call in the selected window had a cache hit. It does not appear for local providers (Ollama, LM Studio), the `stub` provider, or any endpoint that returns no usage data.

## `[consolidate]`

Controls the background worker that extracts events and thoughts from closed sessions.

| Field | Default | Notes |
| --- | --- | --- |
| `trivial_message_count` | `3` | Sessions with fewer messages than this are skipped — there is not enough material to extract events from. |
| `trivial_token_count` | `200` | Sessions under this token count are also skipped, by the same logic. |
| `reflection_hard_gate_24h` | `3` | Maximum number of reflection passes (L4 thought writes) allowed in any rolling 24-hour window. Reflection is the most expensive call in the system, so the gate prevents runaway cost if the user suddenly produces a lot of sessions. |
| `worker_poll_seconds` | `5` | How often the consolidate worker wakes up to check for closed sessions. Lower values react faster but spin the database more. |
| `worker_max_retries` | `3` | Retry count per session on transient failures before it is marked failed and left for manual inspection. |

## `[idle_scanner]`

The idle scanner closes stale open sessions so that memory can consolidate them.

| Field | Default | Notes |
| --- | --- | --- |
| `interval_seconds` | `60` | Scan frequency. A session that has not received a message in 30 minutes is closed on the next scan; that 30-minute threshold is a code constant, not a config knob. |

## `[proactive]`

The autonomous messaging engine. See `proactive.md` for the full design. Field names and defaults are stable across releases; the exact set grows as new policy gates land.

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Master switch. When false, the scheduler is never built and proactive never runs. Turn it on once you trust the daemon not to spam. |
| `tick_interval_seconds` | `60` | How often the scheduler wakes to evaluate the policy queue. |
| `max_per_24h` | varies | Coarse rate-limit ceiling. See `proactive.md` for the full list of policy gate fields. |

## `[voice]`

Turns the voice module on or off. If this whole section is missing or `enabled = false`, the daemon boots without constructing a `VoiceService` and any voice-related code path in runtime or channels degrades cleanly to text.

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Master switch. |
| `tts_provider` | `"stub"` | One of `"stub"`, `"fishaudio"`. |
| `stt_provider` | `"stub"` | One of `"stub"`, `"whisper_api"`. |
| `fishaudio_api_key_env` | unset | Environment variable for the FishAudio API key. |
| `whisper_api_key_env` | unset | Environment variable for the OpenAI API key used by the Whisper provider — usually the same one as `[llm].api_key_env`. |

## `[channels.*]`

One subsection per transport. v0.0.1 ships **two** working channels: the Web UI at `127.0.0.1:7777/` and a Discord DM bot. iMessage and WeChat sections exist as placeholders so the config shape is stable, but those adapters are not implemented yet.

### `[channels.web]`

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Whether to start this channel. |
| `channel_id` | `"web"` | The stable identifier used internally as a via-tag on stored messages. Changing it is usually a mistake. |
| `host` | `"127.0.0.1"` | Bind host. Keep it on `127.0.0.1` unless you explicitly want remote access — the daemon has no auth. |
| `port` | `7777` | Bind port. |
| `static_dir` | `"embedded"` | Where the built frontend lives. `"embedded"` uses the bundled static files that ship with the wheel; an absolute path lets you serve your own build during development. |

### `[channels.discord]`

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Whether to start the Discord DM bot. |
| `channel_id` | `"discord"` | Stable identifier. |
| `token_env` | `"ECHOVESSEL_DISCORD_TOKEN"` | Environment variable that holds the Discord bot token. |
| `debounce_ms` | `2000` | How long to wait for more messages before dispatching one logical turn. |
| `allowed_user_ids` | `[]` (empty = unrestricted) | Optional allowlist of Discord user IDs. |

Enable the channel, put the bot token in `./.env`, and restart the daemon. Messages sent via Discord stream into the Web chat timeline as well (see the runtime-mirror architecture in `channels.md`).

### `[channels.imessage]`

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Whether to start the iMessage channel. macOS-only; requires the external `imsg` CLI (`brew install steipete/tap/imsg`). |
| `channel_id` | `"imessage"` | Stable identifier. |
| `persona_apple_id` | `""` (empty = single-account mode) | Destination filter. Empty → accept inbound for the Mac's iMessage account, no filter. Set to a dedicated persona Apple ID → only messages addressed to that ID reach the LLM (use when the same Mac is signed into both your main and persona accounts). |
| `cli_path` | `"imsg"` | Path to the `imsg` binary. Walks `$PATH` by default; absolute path or SSH wrapper script also accepted. |
| `db_path` | `""` | Optional override for `chat.db`. Empty → `imsg` reads `~/Library/Messages/chat.db`. |
| `allowed_handles` | `[]` (empty = unrestricted) | Peer-handle allowlist (E.164 phone or email). Strongly recommended in single-account mode. |
| `default_service` | `"auto"` | Send service: `"imessage"` / `"sms"` / `"auto"` (lets `imsg` pick). |
| `region` | `"US"` | Region used to canonicalise bare-digit phone numbers without a `+`. |
| `debounce_ms` | `2000` | Burst-grouping window before dispatching one logical turn. |

See `channels.md` § "iMessage channel setup" for the full installation walkthrough (FDA / Automation permissions, single- vs dual-account modes, voice MP3 attachment behaviour).

### `[channels.wechat]`

This section exists as a placeholder. It currently only reads `enabled` and `channel_id`; setting `enabled = true` will not start a real channel yet. Actual adapter lands in a later release.

## What reloads live vs. what requires a restart

Two paths propagate config changes without restarting the daemon:

- **`echovessel reload`** — re-reads `config.toml` from disk, validates it, and applies changes to the matching runtime attributes. Equivalent to sending `SIGHUP` for scripts that still want to use signals.
- **`PATCH /api/admin/config`** — the Web admin panel writes to `config.toml` atomically and then triggers the same reload internally. This path is the one the persona toggle / LLM tuning knobs in the UI go through.

Both paths consult the same allowlist in `src/echovessel/core/config_paths.py`. Below is the field-by-field truth as of this commit; see `tests/runtime/test_reload_matrix.py` for the authoritative per-field assertions.

### Hot-reloadable fields

| Field | What happens on reload |
| --- | --- |
| `[llm].provider` · `.model` · `.api_key_env` · `.timeout_seconds` · `.temperature` · `.max_tokens` | New provider is built and swapped into `ctx.llm`. In-flight turns keep using the old provider until they finish (reference snapshot). |
| `[memory].retrieve_k` · `.recent_window_size` · `.relational_bonus_weight` | `ctx.config.memory.*` updated. The turn handler reads these per-turn, so the next turn sees the new value. |
| `[consolidate].trivial_message_count` · `.trivial_token_count` · `.reflection_hard_gate_24h` | `ctx.config.consolidate.*` updated **and** mirrored onto the live `ConsolidateWorker` instance. The worker was constructed with the old values at boot; the reload path mutates its instance attributes directly so subsequent sessions use the new thresholds without a restart. |
| `[persona].display_name` | `ctx.config.persona.display_name` updated on any reload. `ctx.persona.display_name` (the object the turn handler reads) is mirrored **only** when the change comes in via `PATCH /api/admin/config` (which has a side-path after reload). Changing the name via `echovessel reload` alone leaves `ctx.persona` stale — use the admin API. |

### Runtime-only (not read from `config.toml` at all)

- `persona.voice_enabled` — managed through the admin API (`POST /api/admin/persona/voice-toggle`). Editing the file + reloading does not pick up a change.
- `persona.voice_id` — same: goes through `POST /api/admin/voice/activate`.

### Restart-required

Every other field. The daemon loads them once into constructors that are not rebuilt mid-process. Common examples:

| Section | Why restart |
| --- | --- |
| `[runtime].data_dir` · `[memory].db_path` | Changing these mid-flight would mean reopening the database, re-running migrations, and abandoning the in-memory embedder cache. Explicitly rejected by the admin API with a 400. |
| `[memory].embedder` | Swapping the embedder mid-process would invalidate every existing vector (different model = different embedding space). |
| `[voice]` · `[proactive]` · `[idle_scanner]` | These drive background workers / scheduler objects that are constructed once in `Runtime.start()` and not rebuilt. |
| `[channels.*]` | Registering and starting a new channel happens once at boot. The Admin → Channels UI writes through `PATCH /api/admin/channels` (which persists to TOML and surfaces a "restart required" banner) but no field in this section is hot-reloadable — toggling `enabled`, editing `allowed_handles`, or changing `persona_apple_id` only takes effect on the next `echovessel run`. |

When in doubt, restart. Reload is a convenience for the handful of tunables that change often enough to care about — LLM settings, retrieval knobs, and consolidation thresholds.
