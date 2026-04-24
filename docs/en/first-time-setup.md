# First-Time Setup

> A walkthrough from `git clone` to chatting with your first persona in the browser — in about ten minutes.

## Who this page is for

You just discovered EchoVessel, you have a terminal and a browser, and you want a persona talking back in the time it takes to drink a cup of coffee. This page walks you through install → config → first boot → onboarding → first message, plus the two optional paths most people want next (voice and Discord).

**Prerequisites:**

- Python 3.11 or newer (`python --version` to check)
- A terminal you are comfortable in
- A modern browser (Chrome, Firefox, Safari, Edge — anything ES2020+)
- Optional: an `OPENAI_API_KEY` in your environment if you want real LLM replies. Not needed for the smoke test in Step 2.

The walkthrough assumes you are installing on your own machine. EchoVessel is local-first: all persona data lives under `~/.echovessel/` by default, no telemetry, no phone-home. The only outbound network traffic is to the LLM endpoint you configure and, optionally, a voice provider.

---

## Step 1 · Install (from source)

EchoVessel is **not on PyPI yet**. Clone the repo and sync a local venv with [`uv`](https://github.com/astral-sh/uv):

```bash
git clone https://github.com/AlanY1an/echovessel.git
cd echovessel
uv sync --all-extras
```

`--all-extras` pulls every optional stack (sentence-transformers embedder · OpenAI + Anthropic SDKs · FishAudio TTS · discord.py). If you want a leaner install, pick only what you need:

```bash
uv sync --extra embeddings --extra llm --extra voice --extra discord
```

All subsequent commands run inside the repo with `uv run …`.

Verify the install:

```bash
uv run echovessel --help
```

You should see a short subcommand list including `run`, `stop`, `reload`, and `status`.

---

## Step 2 · Create a config file

EchoVessel reads a single TOML file at `~/.echovessel/config.toml`. Generate it with:

```bash
uv run echovessel init
```

This writes the bundled config sample to `~/.echovessel/config.toml` and creates the data directory if it does not already exist. You will see a line like:

```
✓ wrote config to /Users/<you>/.echovessel/config.toml
```

The default config uses the `openai_compat` provider pointing at OpenAI. If you already have `OPENAI_API_KEY` set in your environment, you can skip ahead to Step 3 and everything will work.

### Zero-key smoke test path

If you do not have an API key handy — or you just want to prove the install runs — edit `~/.echovessel/config.toml`, find the `[llm]` section, and change the provider to `stub`:

```toml
[llm]
provider = "stub"
api_key_env = ""
```

The `stub` provider returns canned replies and never touches the network. This is the fastest way to confirm the daemon boots cleanly, the browser UI loads, and the memory database gets created. You can swap back to a real provider whenever you have a key in hand — the only thing that changes is the quality of the replies.

For the full field reference, see [`configuration.md`](./configuration.md). The interesting sections to know about right now are:

- `[persona]` — a local identifier and display name for your persona
- `[memory]` — where the SQLite database lives (defaults to `memory.db` under the data directory)
- `[llm]` — provider, model, and the name of the environment variable holding your API key
- `[channels.web]` — host, port, and whether the Web channel is enabled (on by default)

---

## Step 3 · Run the daemon

```bash
uv run echovessel run
```

### What happens on the first boot

**Warm-up (30–60 seconds on the very first run only):** the daemon downloads the default sentence-transformers embedder. This is a one-time ~90 MB download that caches under the data directory; subsequent boots are nearly instant. If you installed without the `[embeddings]` extra, the daemon falls back to a lightweight zero-embedder and skips the download entirely.

**Startup log:** the daemon prints a sequence of log lines as it opens the database, runs the schema migration, builds the LLM client, wires up the channels, and finally emits a `local-first disclosure:` line that summarises every piece of network traffic you should expect to see.

**Browser opens automatically:** on a fresh install the daemon detects that there are no persona core blocks yet and opens `http://localhost:7777/` in your default browser a moment after the Web channel comes online. If you are on a headless system (SSH, CI, server), the daemon logs a note that it could not open a browser and tells you to navigate to the URL manually.

### Graceful shutdown

When you are done, press `Ctrl-C` in the terminal where `uv run echovessel run` is running. The daemon unwinds cleanly: it stops the background workers, closes the channels, flushes pending writes, and exits. Your data stays on disk untouched.

You can also shut down from another terminal:

```bash
uv run echovessel stop
```

This sends the same signal the keystroke does and works even if the `run` terminal is attached to a long-running `nohup`.

---

## Step 4 · Onboard your persona

When the browser loads the URL for the first time, it shows a two-path onboarding wizard. Pick whichever matches how much material you already have:

- **Write from scratch** — type a paragraph about who this persona is. Good when you have a specific character in mind and want total control.
- **Upload material** — paste a bio, a journal, or chat logs with a persona you already like. The import pipeline runs once, extracts events and thoughts, and drafts the initial long-term memory from them.

Both paths converge on the same **review page**. The LLM reads whatever you gave it, fills in a review form, and hands it back to you to confirm. The form has two sections.

**Biographic facts — 15 fields, all optional.**

The LLM tries to extract structured identity data (name, gender, birth date, nationality, timezone, occupation, relationship status, and so on). You can correct anything it got wrong or leave fields blank. Five of these — full name, gender, birth year, occupation, native language — get injected into the system prompt on every turn as a short "who you are" bullet list. The other ten are stored for admin queries and future features (birthday reminders, locale-aware phrasing, etc.).

**Five prose core blocks — five fields, Persona block is the one that matters most.**

| Field | What goes here |
| --- | --- |
| **Display name** | A friendly name you will see in the chat. Required. |
| **Persona block** | Who the persona is: personality traits, voice, values, anything that should stay true across every conversation. This is the one block that really matters for a good first experience. |
| **Self block** | How the persona thinks about themselves, written in first person. Optional — leave blank and it will fill in naturally as you talk. |
| **User block** | What the persona knows about you, written in third person. Optional. |
| **Relationship block** | People around you the persona should know — family, close friends, pets. Optional. |
| **Mood block** | How the persona feels right now. Optional. |

A minimal first onboarding is just a display name and two or three sentences of persona block. Everything else you leave blank, and the daemon grows it from conversation through the background consolidate pass.

Click **Finish**. You will be routed to the chat view.

### Where the onboarding data lives

The review page submit maps to a single admin API call. Block text lands in the core-block memory layer; biographic facts land as structured columns on the persona row. Both are reloaded on every conversation turn and fed into the LLM prompt, so anything you write here shows up immediately in the persona's behaviour. You can edit both from the admin panel later without restarting the daemon.

---

## Step 5 · Send your first message

Type a message in the input at the bottom of the chat view and press send. You will see:

1. Your message lands in the chat as a user bubble.
2. A persona bubble appears with an empty body.
3. Tokens stream in from the LLM, filling the bubble a few characters at a time.
4. When the reply is complete, the persona bubble finalizes and the daemon writes both the user message and the persona reply into long-term memory.

Subsequent messages build on the same conversation history. The daemon keeps short-term context in a recent-window buffer and pulls richer context from hierarchical memory via retrieval on every turn.

### How memory grows over time

As you keep talking to the persona, a background worker consolidates finished conversation sessions into higher-level memory: events ("we talked about the Tokyo trip"), thoughts ("the user seems excited about cherry blossom season"), and episodic-state mood updates. This happens on its own cadence — you do not need to do anything — and starts showing up in retrieval results within a minute or two of the session ending. See [`memory.md`](./memory.md) for the full story on how the six memory layers interact.

---

## Step 6 · Optional — enable voice

Voice is fully optional. Skip this section if you only want text for now.

The voice path uses FishAudio for text-to-speech and OpenAI Whisper for speech-to-text. You can bring your own voice (cloning) or pick any public voice ID from FishAudio's library.

**Setup:**

1. Sign up at [https://fish.audio](https://fish.audio) and get an API key.
2. Export the key as an environment variable:
    ```bash
    export FISHAUDIO_API_KEY=your_key_here
    ```
3. Edit `~/.echovessel/config.toml` and set the `[voice]` section:
    ```toml
    [voice]
    enabled = true
    tts_provider = "fishaudio"
    fishaudio_api_key_env = "FISHAUDIO_API_KEY"
    ```
4. Restart the daemon: `Ctrl-C` in the run terminal, then `uv run echovessel run` again.
5. In the browser admin panel, flip the "Voice enabled" toggle on.

From that point forward, persona replies come with an audio player and reply text is spoken in whichever voice you picked. The toggle is a runtime switch — you can turn voice back off mid-conversation without losing memory or history.

For provider options, voice cloning workflow, and the full voice configuration reference, see [`voice.md`](./voice.md). For the exact `[voice]` field list, see [`configuration.md`](./configuration.md).

---

## Step 7 · Optional — enable the Discord DM channel

The same persona can speak on multiple channels at once, and they share a single long-term memory. A conversation you start on the Web channel continues on Discord without the persona forgetting a thing. This is the most load-bearing architectural rule in the project: one persona, one memory, many mouths.

**Setup (quick version):**

1. Create a new application at [https://discord.com/developers/applications](https://discord.com/developers/applications) and add a bot to it.
2. Enable the Message Content Intent under the bot's Privileged Gateway Intents.
3. Copy the bot token.
4. Make sure you installed with the Discord extra (either `uv sync --all-extras` or `uv sync --extra discord`).
5. Export the token:
    ```bash
    export ECHOVESSEL_DISCORD_TOKEN=your_token_here
    ```
6. Edit `~/.echovessel/config.toml`:
    ```toml
    [channels.discord]
    enabled = true
    token_env = "ECHOVESSEL_DISCORD_TOKEN"
    ```
7. Restart the daemon.
8. DM the bot from any Discord client. The persona replies on the same DM thread.

The full walkthrough — including inviting the bot, allowlisting specific Discord users, and troubleshooting the gateway connection — lives in [`channels.md`](./channels.md) under *Discord DM channel setup*.

---

## Troubleshooting

**Config file not found.**
You probably skipped Step 2. Run `uv run echovessel init` to create `~/.echovessel/config.toml`, then try `uv run echovessel run` again. Alternatively, pass an explicit path: `uv run echovessel run --config /path/to/config.toml`.

**Port 7777 already in use.**
Another process is holding the default Web port. Either stop it, or edit `[channels.web].port` in your config and restart the daemon. The browser auto-open path reads the configured port, so no other changes are needed.

**LLM 401 / 403 error in the startup log.**
The API key your config refers to is missing or wrong. Confirm the environment variable named by `[llm].api_key_env` is actually set *in the shell that started the daemon* — a key exported in one terminal does not leak into another. For a quick check that everything else works, switch `[llm].provider` to `"stub"` (see Step 2).

**Browser did not open automatically.**
Some environments (SSH sessions, minimal Linux installs, CI) have no registered default browser. The daemon logs a note saying so and keeps running normally. Open `http://localhost:7777/` yourself. If you are on a remote machine, tunnel the port first: `ssh -L 7777:127.0.0.1:7777 your-host`.

**First boot is slow.**
The sentence-transformers embedder downloads ~90 MB the first time you run the daemon with the `[embeddings]` extra installed. It caches under the data directory and subsequent boots skip the download. If you did not install the `[embeddings]` extra, the daemon uses the built-in zero-embedder and the first boot is near-instant.

**The persona replies with the exact same canned text every time.**
You are on the `stub` LLM provider (Step 2 smoke test). Edit `[llm].provider` back to a real provider (`openai_compat` or `anthropic`) and restart.

**I edited `config.toml` and nothing changed.**
Some sections reload on `SIGHUP`, others require a full restart. The `[llm]` section reloads live; structural sections like `[channels.*]`, `[persona]`, and `[memory]` require `Ctrl-C` + `uv run echovessel run`. See [`configuration.md`](./configuration.md) for the exact reload matrix.

---

## Where to go next

Pick whichever direction fits your curiosity:

- [`memory.md`](./memory.md) — how the six memory layers work, how retrieval and reranking feed the prompt, and how the consolidate worker turns raw conversation into long-term knowledge.
- [`voice.md`](./voice.md) — provider options, the voice cloning workflow, and how voice delivery decisions flow from persona state through the channel layer.
- [`channels.md`](./channels.md) — how to add a new transport, the debounce state machine, the one-persona-many-mouths guarantee, and the full Discord DM walkthrough.
- [`proactive.md`](./proactive.md) — how to let the persona send the first message under carefully gated conditions.
- [`import.md`](./import.md) — bulk-import existing text (diary entries, chat logs, notebooks) into the persona's memory without running a live conversation.
- [`configuration.md`](./configuration.md) — every config field, its default, and the matrix of what reloads on `SIGHUP` vs. what needs a restart.
- [`contributing.md`](./contributing.md) — clone the repo, run the test suite, and submit a pull request.

The runtime internals — startup sequence, turn loop, streaming, and the local-first disclosure audit — live in [`runtime.md`](./runtime.md).
