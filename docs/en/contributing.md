# Contributing

EchoVessel is a small, opinionated codebase. This page tells you how to set up a development environment, what the architectural rules are, and what a good pull request looks like.

## Development setup

You need Python 3.11 or newer and [`uv`](https://github.com/astral-sh/uv). Everything else is managed through `uv sync`.

```bash
git clone <repo-url> echovessel
cd echovessel
uv sync --extra dev
```

This installs the runtime dependencies plus the development toolchain: `pytest`, `pytest-asyncio`, `ruff`, and `import-linter`. Optional extras (`embeddings`, `llm`, `voice`) are only needed if you are working on those subsystems:

```bash
uv sync --extra dev --extra embeddings --extra llm --extra voice
```

Verify the install with the full check suite:

```bash
uv run pytest tests/ -q            # the full test suite — should be all green
uv run ruff check src/ tests/      # lint
uv run lint-imports                # architectural contracts — must stay green
```

If any of those three fail on a fresh clone, that is a bug; please open an issue.

## Project layout

```
EchoVessel/
├── src/echovessel/           the library and daemon
│   ├── core/                 shared types, enums, utilities
│   ├── memory/               L1-L6 persona memory
│   ├── voice/                TTS, STT, voice cloning
│   ├── channels/             Channel Protocol + concrete channels
│   │   └── web/frontend/     React 19 + Vite + TypeScript UI source (built into `web/static/`)
│   ├── proactive/            autonomous messaging engine
│   ├── prompts/              system prompts for extraction, reflection, interaction
│   ├── import_/              universal LLM importer pipeline
│   └── runtime/              the daemon: startup, turn loop, LLM providers, CLI
├── tests/                    test suite, mirrors src/ layout
│   ├── integration/          cross-module composition smoke tests
│   └── eval/                 persona-quality eval harness
├── docs/                     the documentation you're reading
├── src/echovessel/resources/  bundled resources (config.toml.sample for `echovessel init`)
├── pyproject.toml            dependencies, layering contracts, lint config
└── README.md
```

Each subsystem under `src/echovessel/` has a matching test directory under `tests/`. Cross-module composition tests live in `tests/integration/`. The eval harness under `tests/eval/` runs a set of golden questions against the memory module and reports metrics — it is the fastest way to check that a memory change did not regress retrieval quality.

## The layered architecture

EchoVessel has five core modules stacked in strict layers:

```
runtime
   │
   ▼
channels    proactive
   │             │
   └──────┬──────┘
          ▼
     memory     voice
          │       │
          └───┬───┘
              ▼
            core
```

A layer may import from the layers directly below it. It may not import from the layer directly above it or from its siblings on the same layer. In concrete terms:

- `runtime` can import from everything below it.
- `channels` and `proactive` can import from `memory`, `voice`, and `core`, but **not from each other** and **not from `runtime`**.
- `memory` and `voice` can import from `core`, but **not from each other** and **not from above**.
- `core` imports nothing from EchoVessel.

This contract is enforced at lint time by `import-linter`, configured in `pyproject.toml`. A pull request that breaks the layering fails CI regardless of whether the tests pass. Adding a new module means deciding where it fits on this ladder and declaring that placement in the `import-linter` config.

There is also a small utility module, `import_/`, that sits alongside `memory` and `voice` on layer 2 (it writes to memory during imports). It follows the same sibling rules.

## The two ironrules

Two rules are load-bearing for the whole system and are enforced by explicit guard tests.

### Memory retrieval never filters by channel

The memory module's `retrieve()` function, its core-block loader, and its recall-message query all accept persona and user, and **never** accept a transport identifier. There is no `retrieve(..., channel_id="web")` overload and there never will be. A persona is one continuous identity across every channel it speaks on; allowing retrieval to be sharded by transport would silently turn that one persona into a pile of per-channel bots.

The guard test lives in `tests/runtime/test_memory_facade.py::test_no_channel_id_kwarg_in_reads`. It AST-walks the memory facade and fails if any read path mentions `channel_id=`. If you need to add a new memory read API, the AST walk will check it automatically.

### LLM prompts never leak transport identity

Nothing in any prompt that is sent to the LLM should contain a `channel_id` string or any other transport-identity token. The persona has no idea whether it is currently speaking on the Web, Discord, or iMessage — and every design decision that could leak that information has been deliberately routed around.

The guard test lives in `tests/runtime/test_f10_no_channel_in_prompt.py`. It renders real prompts from a fixture and grep-walks them for forbidden substrings. If you add a new prompt slot, extend this test to cover it.

Both ironrules exist because violations are silent and compounding. A single retrieval that filters by channel, or a single prompt that leaks `channel_id`, does not immediately break anything — it quietly removes a guarantee the rest of the system relies on. The guard tests exist so that "silently broken" becomes "loudly broken in CI".

## Testing conventions

The test layout mirrors the source layout. Module-specific tests live under `tests/<module>/`. Integration tests that touch multiple modules live under `tests/integration/`. The persona-quality eval harness lives under `tests/eval/`.

When you add a feature:

1. **Unit tests go in the module's own test directory.** A new memory function gets a test in `tests/memory/`. A new voice provider gets one in `tests/voice/`.
2. **Cross-module wiring tests go in `tests/integration/`.** If your change affects how two modules interact, add a test that exercises both of them through a realistic entry point.
3. **Memory-retrieval changes should be validated against the eval harness.** Run `uv run python -m tests.eval.run_baseline` and check that the four quality metrics still clear their thresholds.
4. **Prefer stub providers in tests.** The codebase ships with `StubProvider` (LLM), `StubVoiceProvider` (TTS/STT), and a stub channel. Use them so your tests do not depend on network calls or API keys.

Every PR must keep `pytest tests/` green, `ruff check` clean, and both `import-linter` contracts satisfied.

## Commit messages

The repo enforces a small set of rules and recommends the rest.

**Required of every commit:**

- **Subject = what. Body = why.** The diff already says how. Skip the body when the subject is self-explanatory; write one whenever a reasonable reader would ask "why?".
- **Imperative mood.** `add X`, `fix Y` — not `added X` / `fixes Y`.
- **One logical change per commit.** No "fix bug + refactor helper + touch unrelated docs" mega-commits. Split them.
- **Three green before committing.** `uv run pytest`, `uv run ruff check src/ tests/`, and `uv run lint-imports` must all pass locally.
- **Subject ≤ 72 characters** so `git log --oneline` stays readable.

**Recommended (soft):**

Use a Conventional Commits prefix for single-area changes:

| Prefix | Use for |
| --- | --- |
| `feat:` | user-visible new capability |
| `fix:` | bug fix |
| `docs:` | documentation only |
| `refactor:` | internal change, no behavior change |
| `test:` | test-only change |
| `chore:` | build / deps / tooling |
| `perf:` | performance improvement |

Add a scope when it clarifies (module name or file): `fix(memory): ...`, `docs(README): ...`, `refactor(runtime): ...`.

For milestones or cross-module pushes that don't fit a single prefix, a free-form subject with `·` as a separator is fine — for example `Wave A · admin UI truth-layer landing`.

**Anti-patterns:**

- Vague subjects: `updates`, `misc fixes`, `WIP` left on `main`.
- `fix X and refactor Y` — two topics, two commits.
- Stating only *what* when *why* would not be obvious to a reader six months from now.

**What the repo deliberately does not require:**

No commit-msg hook, no CI enforcement, no DCO sign-off, no issue-number trailer. The reviewer (and future you) is the checker.

## Submitting a pull request

A good PR:

- **Does one thing.** Unrelated cleanups belong in a separate PR. If you have to say "while I was there I also...", split it.
- **Updates tests.** Every behavioral change has at least one test that would have caught the bug before the fix. Passing tests without a new test is a yellow flag.
- **Keeps `lint-imports` green.** If you added a new dependency between modules, the layering contract must still pass. If it does not, rethink the dependency.
- **Stays out of the ironrules' way.** Do not add memory read APIs that accept `channel_id`. Do not add prompt content that leaks transport identity. The guard tests will catch the obvious cases; code review will catch the subtle ones.
- **Has a commit message that explains the why.** The title says what, the body says why. The diff already says how.

## Running the eval harness

The eval harness measures memory quality with four metrics: Factual Recall F1, Emotional Peak Retention, Over-recall False Positive Rate, and Deletion Compliance. Each one has a threshold the project considers acceptable for release.

```bash
uv run python -m tests.eval.run_baseline
```

The harness uses a fixed stub LLM by default so results are deterministic across runs. It prints a table of metric values and pass/fail against thresholds. If a change you made drops a metric below its threshold, you have a regression.

The full metric definitions and interpretation notes live in the eval harness source under `tests/eval/`.

## Where to ask questions

Before filing an issue:

1. Read the relevant module doc in `docs/en/` or `docs/zh/`.
2. Check the module's source code — every file has detailed docstrings.
3. Search existing issues.

When opening an issue, describe the behavior you see, what you expected, and the minimum config needed to reproduce. A pasted `uv run python -m echovessel run` startup log is almost always the right thing to attach.
