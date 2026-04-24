# Import Pipeline

> One universal pipeline that accepts any human-written text — diaries, chat logs, novel excerpts, resumes, essays — classifies each fragment with an LLM, and routes it into the right memory tables. No format-specific parsers. No content-specific branches.

The import pipeline is how external material enters a persona's memory. It is a deliberately thin, deliberately generic module: the only thing it "understands" about content is what the extraction LLM tells it, and the only thing it "understands" about memory is the five-bucket whitelist that `memory.import_content` exposes. Everything else — the meaning of a diary entry, the difference between a user fact and a persona fact, whether a sentence is a reflection or an event — is decided inside a single prompt.

---

## Overview

A naive importer would ship one parser per format and one handler per content type: "if this is a diary, extract dated entries; if this is a chat log, group turns; if this is a resume, pull job titles". That shape fails the moment a user drops in anything the authors did not anticipate. It also buries the interesting work — deciding what a paragraph *means* — in a growing pile of brittle if-branches.

EchoVessel takes the opposite bet. The insight is that any human-written text can be classified by a capable LLM into "what this content is about", and once you have that classification, routing it into the right memory table is a short lookup. So the import pipeline collapses to five mechanical stages — read bytes, split into chunks, ask the LLM what each chunk contains, write to memory, embed — and every decision about meaning is pushed into the extraction prompt.

This trade has three concrete payoffs:

1. **One pipeline, any source.** The same code path handles diaries, exported Discord logs, a chapter of a biography, a job application, or a plain paragraph pasted into a text box. Adding support for a new kind of personal material requires zero new Python.
2. **No preprocessing burden on the user.** Users do not need to convert, tag, or sort their content before importing. They drop in the file they already have; the LLM figures out whether each paragraph is a durable fact, an episode, or a reflection.
3. **New content categories are a prompt change, not a parser rewrite.** When the project one day decides that "favourite places" deserve its own memory bucket, the work is an edit to the extraction prompt and a new branch in `routing.py` — not a new file format handler, not a new pipeline.

The counterweight is that the quality of the import is bounded by the quality of the extraction prompt and the LLM that runs it. The pipeline takes that seriously in four concrete ways:

- Every extracted write carries a verbatim `evidence_quote` that must appear as a substring of the originating chunk. Fabricated quotes are dropped before they reach memory.
- The extraction prompt enumerates six legal targets (`L1.persona_block`, `L1.self_block`, `L1.user_block`, `L1.relationship_block`, `L3.event`, `L4.thought`); any other write target is rejected by the JSON validator.
- L1 writes below a 0.5 confidence threshold are dropped silently, so low-certainty guesses cannot pollute the persona's core identity blocks.
- Per-chunk failures are recorded as `DroppedItem`s with a reason string and a payload excerpt, surfaced through the `chunk.error` event, and included in the final `PipelineReport` — nothing is swallowed.

---

## Core Concepts

**Normalization.** Turning whatever bytes the user uploaded into plain UTF-8 text. This is the only place in the pipeline that is allowed to care about file formats. `.txt` and `.md` are decoded as-is; `.md` front-matter is flattened into `"key: value"` lines so the LLM sees the metadata; `.json` is parsed and flattened into human-readable lines (lists of dicts get blank-line separators so chunking can later break between elements); `.csv` is passed through (chunking handles row batching). Non-UTF-8 or otherwise undecodable bytes raise `NormalizationError` and the pipeline aborts. Implemented in `src/echovessel/import_/normalization.py`. Binary formats (PDF, DOCX, audio, image) are explicitly out of scope for the MVP — the pipeline refuses them at the normalization step rather than pretending it can guess their contents.

**Chunking.** Splitting the normalized text into pieces small enough for the LLM to handle in a single prompt. The strategy lives in `src/echovessel/import_/chunking.py`: paragraphs are split on blank lines; paragraphs that exceed 2000 characters are further sliced with a 1500-character sliding window at 500-character overlap; CSV-shaped text is batched at eight rows per chunk. The output is a list of `Chunk` dataclasses, each carrying its index, content, offset, and the original source label. The chunker is format-ignorant after normalization — its only concession to format is a lightweight heuristic that detects CSV-shaped text by checking that every non-empty line contains at least one comma and that the median line length is under 400 characters.

**Extraction.** The LLM read-and-classify step. `src/echovessel/import_/extraction.py` calls the injected LLM with a system prompt that enumerates the six legal write targets and a user prompt that carries one chunk. The LLM returns a JSON object with a list of writes and a one-sentence `chunk_summary`. The extractor validates the JSON shape, runs each write through `routing.translate_llm_write`, and returns a list of typed `ContentItem`s plus a list of `DroppedItem`s for writes that failed validation. Extraction runs on the `SMALL` LLM tier by default: the per-chunk call is short, structured, and repeated many times per upload, so using a cheap tier keeps the end-to-end cost predictable without noticeably hurting quality.

**`ContentItem`.** The dataclass that represents one memory write decision after extraction. It carries a `content_type` (one of the five whitelist strings), a `payload` dict shaped for the memory import API, the origin `chunk_index`, and the verbatim `evidence_quote` from the chunk. Constructing a `ContentItem` with an unknown `content_type` raises `ValueError` at construction time — the whitelist is enforced in the dataclass itself, not just in downstream code.

**Content type.** One of exactly five strings: `persona_traits`, `user_identity_facts`, `user_events`, `user_reflections`, `relationship_facts`. This is the whitelist `memory.import_content` accepts; any value outside it raises `ValueError`. The import pipeline mirrors the same whitelist in `ALLOWED_CONTENT_TYPES` inside `models.py`, so a violation is caught before it reaches the memory layer.

**Routing.** Dispatching each `ContentItem` to the correct memory writer function. `src/echovessel/import_/routing.py` inspects the `content_type`, unpacks the payload, and calls either `memory.append_to_core_block` (for L1 blocks) or `memory.import_content` (which routes to `bulk_create_events` / `bulk_create_thoughts` under the hood). Routing is also where the `L1.self_block` side path is handled — see the Architecture section. The dispatcher returns a tuple of `(ImportResult, new_concept_node_ids)` so the orchestrator can accumulate the ids it needs for the embed pass without re-querying the database.

**Embed pass.** The mandatory post-write step that computes vector embeddings for every new L3 event and L4 thought row and inserts them into `concept_nodes_vec`. Without this step, imported events and thoughts exist in SQLite but are invisible to `memory.retrieve`'s vector search — they can never surface during a conversation. The embed pass is implemented in `src/echovessel/import_/embed.py` and is non-optional: if the pipeline produced concept-node rows but the caller passed `embed_fn=None`, the pipeline raises `EmbedError` rather than silently skipping.

**Pipeline progress.** An in-memory snapshot of where a running pipeline is: `current_chunk`, `total_chunks`, `written_concept_node_ids`, and a `state` string. Lives inside the `ImporterFacade` so that a transient LLM failure can pause the pipeline and a later `resume_pipeline` call can pick up from the next chunk without re-processing anything that was already written. Not persisted to disk — a daemon restart loses in-flight pipelines and the user is expected to re-upload. The duplicate-detection path at start time (`memory.count_events_by_imported_from(file_hash)`) keeps a re-upload from silently doubling every row, so resume-via-reupload is safe as long as the user confirms.

**`PipelineReport`.** The aggregate result returned by `run_pipeline` after every stage has finished. Carries the final `status` (`"success"`, `"partial_success"`, `"failed"`, `"cancelled"`), per-content-type write counts, the list of new `concept_nodes` ids, the list of `core_block_appends` ids, the list of `DroppedItem`s with reasons, and an `embedded_vector_count`. Runtime callers translate it into a "Complete" summary for the UI; tests assert against it to confirm the pipeline did what they expected.

---

## Architecture

The pipeline is five stages in sequence:

```
upload (bytes + suffix)
       |
       v
+----------------+
| normalization  |   bytes  →  plain UTF-8 text
+----------------+
       |
       v
+----------------+
|   chunking     |   text   →  list[Chunk]
+----------------+
       |
       v          (one LLM call per chunk, SMALL tier)
+----------------+
|   extraction   |   chunk  →  list[ContentItem] + list[DroppedItem]
+----------------+
       |
       v
+----------------+
|    routing     |   ContentItem → memory writer call
+----------------+      |
       |                |                |
       v                v                v
  persona_traits   user_identity     user_events
  relationship     user_reflections
       |
       v
+----------------+
|   embed pass   |   concept_node_ids → vectors → concept_nodes_vec
+----------------+
       |
       v
  PipelineReport
```

Each stage has one clean input and one clean output, and each stage lives in its own module under `src/echovessel/import_/`. The orchestrator in `pipeline.py` is mostly glue: it emits lifecycle events (`pipeline.start`, `chunk.start`, `chunk.done`, `chunk.error`, `pipeline.done`) through an injected `event_sink` callable so the runtime facade can translate them into SSE events for a web UI.

The sequence is deliberate. Normalization happens once per pipeline so the rest of the code can assume it is reading plain UTF-8 text. Chunking is deterministic and depends only on the text, so it can be re-run across a resume without costing anything. Extraction is the only stage that talks to the LLM and is therefore the only stage that can fail transiently; placing it per-chunk means a single bad chunk does not taint the others. Routing is synchronous and local to one `ContentItem` at a time, so a bad write can be dropped without disturbing its neighbours. The embed pass is pushed to the very end because it reads back everything that was written — running it per-chunk would mean opening and closing a vector-index transaction for every LLM call, which is strictly more work for no benefit.

### The five content types and where they route

| Content type          | Memory writer                                           | Memory target                        |
| --------------------- | ------------------------------------------------------- | ------------------------------------ |
| `persona_traits`      | `append_to_core_block(label="persona")`                 | L1 persona block                     |
| `user_identity_facts` | `append_to_core_block(label="user")`                    | L1 user block                        |
| `user_events`         | `import_content` → `bulk_create_events`                 | L3 concept nodes with `type='event'` |
| `user_reflections`    | `import_content` → `bulk_create_thoughts`               | L4 concept nodes with `type='thought'` |
| `relationship_facts`  | `append_to_core_block(label="relationship_block:<key>")` | L1 relationship block keyed by person |

The L1 append writers record an audit row in `core_block_appends` as part of the same transaction that updates `core_blocks.content`, so the provenance of every imported L1 fact is reconstructible. The L3 / L4 bulk inserts tag each new row with `imported_from = <file_hash>`, which is how duplicate-import detection works on a later upload of the same file. The schema CHECK constraint on `concept_nodes` enforces that `imported_from` and `source_session_id` are mutually exclusive: a row is either an import or a consolidation output, never both, which keeps the provenance story honest even if a future code path tries to mix them.

### The `L1.self_block` side path

The extraction prompt actually enumerates **six** legal targets, not five. The sixth is `L1.self_block`: the persona's first-person self-concept, distinct from `persona_traits`, which is the third-person description of the persona. "She is curious and patient" is a `persona_trait`. "I tend to over-explain when I'm anxious" is a `self_block` statement. Collapsing the two would erase a distinction the prompt deliberately maintains.

Memory's import dispatcher, however, only accepts the five-bucket whitelist. The side path lives in `routing.py`: when the extractor sees `target: "L1.self_block"` it still produces a `ContentItem` (with `content_type="persona_traits"` so the whitelist check passes and a `_self_block=True` marker on the payload), and `dispatch_item` notices the marker and takes a direct `append_to_core_block(label="self", user_id=None)` call, bypassing `import_content` entirely. The resulting row is counted under a synthetic `persona_self_traits` key in the pipeline report so audit tooling can tell self-block appends apart from persona-block appends. The whitelist invariant is preserved: `memory.import_content` never sees `persona_self_traits`, and a unit test asserts that passing it in raises `ValueError`.

### The embed pass is mandatory

`memory.bulk_create_events` and `bulk_create_thoughts` deliberately do not compute embeddings — the memory module has no dependency on `sentence-transformers` and never will, because memory needs to run in environments without the ML stack. The cost of that discipline is that the import pipeline is responsible for the vectors.

After every chunk finishes dispatching, the orchestrator collects the new `concept_nodes.id`s into `all_new_concept_ids`. Once the per-chunk loop is done, `run_embed_pass` opens a fresh DB session, reads the `(id, description)` pairs back, calls the injected `embed_fn` once for the whole batch, and writes each resulting vector with the injected `vector_writer`. If the pipeline produced any concept-node rows but `embed_fn` or `vector_writer` is `None`, `run_embed_pass` raises `EmbedError`. Silent skipping is explicitly forbidden: a successful import with missing vectors looks healthy on paper but leaves imported content invisible to `retrieve.vector_search` forever, which is worse than an obvious failure.

### Failure mode classification

The pipeline distinguishes three kinds of failure, each with its own handling:

- **Transient.** LLM timeout, network blip, provider budget exhausted. The offending chunk raises `ExtractionError(fatal=False)`, the pipeline emits a `chunk.error` event, records the chunk as dropped, advances `progress.current_chunk` past it, and moves on to the next chunk. Dispatch-layer failures (DB contention, downstream exceptions) follow the same path. A failed chunk is marked "attempted, done" in the progress cursor, so a subsequent `resume_pipeline` call picks up strictly after it — the chunk is not re-run. The operator's recovery lever for a chunk they care about is to re-import the whole file.
- **Permanent.** A file that cannot be decoded as UTF-8, a content type outside the whitelist, a schema violation that will keep failing no matter how many times it retries. These raise `NormalizationError`, `ExtractionError(fatal=True)`, or `ValueError`. The pipeline emits a `chunk.error` with `fatal=True`, stops processing further chunks, and emits a `pipeline.done` with `status="failed"`.
- **Partial success.** Some chunks succeeded, others failed with non-fatal errors. Already-written memory rows stay on disk — the pipeline never rolls back memory writes for earlier chunks when a later chunk fails. The pipeline ends with `status="partial_success"` so the caller can decide whether to surface a warning.

The distinction matters because transient failures are recoverable and permanent ones are not. Rolling back partial writes on every failure would either require a multi-chunk distributed transaction (overkill) or force the user to re-pay for every LLM call on retry (wasteful). Keeping partial writes and letting the duplicate-detection path handle re-runs is the cheaper, more honest default.

Cancellation is a separate concern: when the facade calls `task.cancel()`, the pipeline's `asyncio.CancelledError` handler records the current chunk index into the progress snapshot, marks the state as `"cancelled"`, and re-raises so the facade's task-level handler can emit the final `pipeline.done` event with `status="cancelled"`. Already-written rows are kept, matching the partial-success rule. The user-visible effect is "cancel finishes the current chunk, then stops" — close to what a user expects from a stop button but without wasting the LLM call that was already in flight.

### Event flow

Every stage of the pipeline reports back through the injected `event_sink` callable. The events are small dicts with a `type` string and a `payload` dict; the runtime facade translates them into `PipelineEvent` instances and fan-outs them to every subscriber queue. The lifecycle looks like:

1. `pipeline.registered` — emitted by the facade the moment `start_pipeline` returns an id, before any normalization has happened. Lets a subscribed UI render a pending state immediately.
2. `pipeline.start` — emitted after normalization and chunking, carrying `total_chunks` and the resume offset. This is the first event whose payload reflects actual work to do.
3. `chunk.start` / `chunk.done` — one pair per chunk. `chunk.done` carries `writes_count`, `dropped_in_chunk`, and the one-sentence `summary` that the LLM produced so a UI can stream a live log.
4. `chunk.error` — emitted on any per-chunk failure, with `fatal` and `stage` keys so subscribers can tell transient from permanent errors apart.
5. `pipeline.done` — the terminal event. Always emitted, always last, always carries the final `status` and the per-target write counts. Subscribers use its arrival to close their async-for loops.

Because the facade fan-outs every event to every subscriber queue, a late subscriber that called `subscribe_events` after `pipeline.start` will miss any events that already happened. For replay semantics, callers keep the `PipelineReport` the facade hands back instead of reconstructing state from the event stream.

### Runtime integration

The import pipeline and the runtime are sibling layers in the five-module architecture, and the layering contract forbids sibling-to-sibling imports: `channels.web` cannot import from `import_.pipeline` directly. The mediator is `src/echovessel/runtime/importer_facade.py`, which exposes the four methods any caller needs:

- `start_pipeline(upload_id, *, raw_bytes, suffix, persona_id, user_id, ...) -> pipeline_id` — allocates a pipeline id, builds a `ProgressSnapshot`, and spawns `asyncio.create_task(run_pipeline(...))`. Emits a `pipeline.registered` event immediately so subscribers know the id is live.
- `cancel_pipeline(pipeline_id)` — marks the pipeline cancelled and calls `task.cancel()`. The pipeline's `asyncio.CancelledError` handler writes the progress snapshot so a later resume is possible.
- `resume_pipeline(pipeline_id)` — re-spawns the pipeline task with the same kwargs; `run_pipeline` reads `progress.current_chunk` and skips already-processed chunks.
- `subscribe_events(pipeline_id) -> AsyncIterator[PipelineEvent]` — returns a fresh async iterator backed by its own `asyncio.Queue`. Multiple subscribers can read the same pipeline independently, so a web UI and a log collector can listen in parallel. Each iterator terminates when the facade pushes a `None` sentinel into the queue on pipeline completion or cancellation, so consumers can use a plain `async for` without polling.

The facade also owns the dependency injection: `llm_provider`, `voice_service`, and `memory_api` are passed to the constructor at runtime startup, and every `start_pipeline` call wires them into the pipeline kwargs. The pipeline itself never imports from `runtime`, `channels`, or `proactive`; it only depends on `memory`, `core`, and its own submodules. The same facade will back the future `echovessel import <file>` CLI command — the CLI entry point will build a minimal event sink that prints each event to stdout and call the same `start_pipeline` method the web channel uses, so both drivers share one code path.

---

## How to Extend

### Add a new normalization format

Suppose you want to import a custom markdown variant that uses `:::note` fenced blocks, or a project-specific JSON export with a known shape. The entry point is `normalize_bytes` in `src/echovessel/import_/normalization.py`. Dispatch on the `suffix` and add a private helper that produces clean UTF-8 text; do not try to extract meaning from the content — leave that to the LLM.

```python
# src/echovessel/import_/normalization.py

def normalize_bytes(raw: bytes, *, suffix: str = "") -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NormalizationError(...) from exc

    suffix = suffix.lower()
    if suffix == ".json":
        return _flatten_json_text(text)
    if suffix == ".md":
        return _merge_frontmatter(text)
    if suffix == ".mynote":                # new format
        return _unwrap_note_blocks(text)
    return text


def _unwrap_note_blocks(text: str) -> str:
    """Turn `:::note ... :::` blocks into plain paragraphs."""
    out = []
    for line in text.splitlines():
        if line.strip() in (":::note", ":::"):
            out.append("")                 # blank line → chunker break
        else:
            out.append(line)
    return "\n".join(out)
```

That is the entire change. Chunking, extraction, routing, and the embed pass all remain untouched. The LLM will read the resulting plain text through the same prompt and classify its content into the same five buckets. You are adding a decoder, not a parser for the content's meaning.

Three implementation notes. Keep the helper a pure `(str) -> str` function, because that is the only contract `normalize_bytes` needs from it. Preserve blank lines between logical segments — the paragraph-split chunker relies on `\n\n` to know where one unit ends and the next begins, and your format's natural boundaries should surface as blank lines in the output. And avoid decoding twice: if your format embeds JSON inside markdown, do not recurse into `_flatten_json_text` unless the resulting text would still round-trip through the chunker cleanly; it is usually simpler to leave the embedded JSON as literal characters and let the LLM read it.

### Tune the extraction prompt

The extraction prompt is a constant inside `src/echovessel/import_/extraction.py` (`IMPORT_EXTRACTION_SYSTEM_PROMPT`). Editing it changes how the LLM classifies content without touching any other module — that is the whole point of the "prompt as routing table" design.

The prompt enumerates **six legal targets**: the five memory content types plus the `L1.self_block` side path. Adding, removing, or tightening a target requires two coordinated changes:

1. Edit the system prompt so the LLM knows the new target exists, what it means, what fields it requires, and when to prefer it over the neighbours.
2. Add a matching branch in `routing.translate_llm_write` that validates the LLM's output for that target and produces a `ContentItem` with the correct `content_type`. If the new target maps onto an existing `content_type`, the routing change is a pattern match and a payload assembly. If it needs a brand-new memory bucket, the work also extends into `memory.import_content` and the `ALLOWED_CONTENT_TYPES` whitelist — that is a larger change that crosses the import/memory boundary and must be done as one atomic commit.

For a pure prompt tweak — adjusting the wording of one target's admission criteria, tightening the closed relational-tag vocabulary, or adding a new negative example — the rule is simpler: edit the constant, update the unit tests in `tests/import_/test_extraction_stub_llm_roundtrip.py` if the expected parsed output shifts, and confirm the pipeline still produces valid `ContentItem`s under `pytest tests/import_/`.

A few rules are load-bearing and should survive every prompt revision. The `evidence_quote` requirement is not negotiable — the substring check in `routing.translate_llm_write` is how the pipeline protects against hallucinated extractions. The closed `relational_tags` vocabulary (`identity-bearing`, `unresolved`, `vulnerability`, `turning-point`, `correction`, `commitment`) lives in `extraction.RELATIONAL_TAG_VOCAB` and is silently filtered: if you want a new tag, add it to the set in code and to the prompt in the same commit. The `emotional_impact` integer range of `-10` to `+10` is validated in routing; do not widen it without a matching schema migration in memory.

### Custom post-extraction hook

Sometimes you want to inspect each extracted `ContentItem` before it hits memory — to filter low-quality writes, enrich them with extra metadata, or implement a dry-run mode. The clean place to do this is a callback sitting between `extract_chunk` and `_dispatch_chunk_items`. The pipeline does not ship a built-in hook slot, but adding one is a few lines because all the collaborators are already injected through keyword arguments.

```python
# Your caller code — e.g. a custom runtime wiring layer.

from echovessel.import_.models import ContentItem
from echovessel.import_.pipeline import run_pipeline

def skip_low_confidence(items: list[ContentItem]) -> list[ContentItem]:
    """Drop imported events with emotional_impact of exactly 0 — the
    LLM tends to use that as a 'not sure' fallback.
    """
    kept = []
    for item in items:
        if item.content_type == "user_events":
            events = item.payload.get("events", [])
            if events and events[0].get("emotional_impact") == 0:
                continue
        kept.append(item)
    return kept


async def run_with_hook(**kwargs):
    original_dispatch = kwargs.pop("_dispatch_hook", None)
    # Wrap the pipeline by patching the extractor output before
    # dispatch. The simplest integration today is a subclass-free
    # wrapper that calls run_pipeline with a pre-filtered LLM stub.
    ...
```

A more production-ready integration adds an explicit `item_filter: Callable[[list[ContentItem]], list[ContentItem]]` kwarg to `run_pipeline` and applies it between the `extract_chunk` return and the `_dispatch_chunk_items` call. The change is local to `pipeline.py`, touches no other module, and keeps the filter under test control because the filter itself is an injected callable. Make sure any filter you write is pure: the embed pass still runs over whatever actually landed on disk, so dropping an item after dispatch would desync the concept-node ids and confuse the report.

Two common mistakes to avoid when writing such a hook. First, do not mutate the `ContentItem.payload` dict in place — `ContentItem` is a frozen dataclass precisely because later stages assume its fields are stable. If you need to enrich the payload, construct a new `ContentItem` with the merged dict. Second, do not call into the memory module from inside the filter. The filter runs per chunk inside the orchestrator loop, and the orchestrator is the module that owns DB sessions; side-channel memory writes from a filter will race with `_dispatch_chunk_items` and break the embed pass's assumption that `all_new_concept_ids` is the complete set of rows that need vectors.

---

## Where to read next

- `docs/en/memory.md` explains how the L1 / L3 / L4 tables the pipeline writes to are actually stored, and how retrieval later scores and surfaces what the pipeline imported.
- `docs/en/runtime.md` covers the five-module architecture and shows where the `ImporterFacade` lives relative to the rest of the runtime surface.
- `docs/en/voice.md` describes the voice stack — relevant if you plan to re-ingest audio memos once binary-format normalization ships.
- `docs/en/configuration.md` lists the config keys that govern LLM tier selection and embedding backends.

---

The authoritative sources for everything above are the files under `src/echovessel/import_/` and `src/echovessel/runtime/importer_facade.py`; the code carries the same invariants as this document and, when the two disagree, the code wins. The test suite under `tests/import_/` is the executable specification and is the fastest way to verify any extension you make.
