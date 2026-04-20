"""Synthesize LLM-authored companion fixtures.

For each ``scripted`` YAML this script asks the main LLM (LARGE tier)
to write a realistic user-side conversation that HITS THE SAME
SCENARIO with messier phrasing. The scripted invariants and
judge_prompts are carried over verbatim into the synthesized
fixture so the same harness can consume both.

Run locally once per round, review the output, commit the results
into ``fixtures/synthesized/``:

    uv run python -m tests.memory_eval.synthesize
    # writes tests/memory_eval/fixtures/synthesized/e*.yaml

    # skim each yaml, tweak if the LLM wandered, then:
    git add tests/memory_eval/fixtures/synthesized && git commit

The generator pins the LLM's output shape with a tight JSON schema +
explicit examples. If a run produces malformed JSON we log the raw
response and skip that scenario so partial batches still land cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import UTC, datetime

import yaml

from echovessel.runtime.llm.base import LLMProvider
from tests.memory_eval.harness import (
    FIXTURE_ROOT,
    Fixture,
    build_live_llm,
    load_fixture,
)

log = logging.getLogger(__name__)


SCRIPTED_DIR = FIXTURE_ROOT / "scripted"
SYNTHESIZED_DIR = FIXTURE_ROOT / "synthesized"


# Per-scenario cues · how long the synthesized conversation should be
# and any special tone / pacing hints the LLM should honour.
_LENGTH_HINTS: dict[str, str] = {
    "e1_user_self_disclosure": "8-10 turns · user casually drops biographic facts mixed with small-talk",
    "e2_user_only_asks": "6-8 turns · user asks the persona questions · never discloses about themselves",
    "e3_buried_shock": "6-8 turns · user buries one SHOCK disclosure mid-chatter then changes subject",
    "e4_correction": "5-7 turns · user corrects a fact they stated earlier",
    "e5_reflection_abstraction": "3-5 turns · short · the seeded events do the heavy lifting",
    "e6_retrieval_relevance": "0 turns · retrieve-only · leave ``turns`` empty",
    "e7_mood_evolution": "20 turns · user opens heavy · persona listens · conversation slowly finds footing",
    "e8_bilingual": "6-8 turns · user mixes Chinese + English · Chinese is majority",
}


_SYSTEM_PROMPT = """\
You are writing realistic user-side chat conversations for a digital
companion's memory-system eval harness. Given a scenario, produce a
conversation whose user messages feel like a real person typing on
their phone — short sentences, casual punctuation, occasional
redirects, rarely saying more than two things in one message.

Output strict JSON (no preamble, no markdown, no code fences) with
this exact shape:

  {
    "scenario": "<one-line description>",
    "turns": [
      {"role": "user", "content": "..."},
      {"role": "persona", "content": "..."},
      ...
    ]
  }

Rules:
- Alternate user / persona turns. Start with a user turn.
- Keep persona replies short and neutral — this test is about what
  the USER discloses, not about persona quality.
- Keep the scenario's emotional weight. Do NOT invent facts the
  scripted version rules out (e.g. don't add a SHOCK to e2, don't
  add a correction to e3).
- If the scenario says "retrieve-only · leave turns empty", output
  an empty ``turns`` list.
- The language of the user messages must match the scripted version's
  language (Chinese scripted → Chinese synthesized).
"""


def _user_prompt(scripted: Fixture, length_hint: str) -> str:
    return (
        f"Scripted scenario: {scripted.scenario}\n\n"
        f"Length / style hint: {length_hint}\n\n"
        f"Scripted persona_block seed:\n  {scripted.seed.persona_block}\n\n"
        "Now write a realistic user-side conversation that covers the "
        "same scenario but with messier, more natural phrasing. Emit "
        "strict JSON in the schema above."
    )


async def _synthesize_one(
    llm: LLMProvider, scripted: Fixture, length_hint: str
) -> dict | None:
    user = _user_prompt(scripted, length_hint)
    raw, _usage = await llm.complete(
        _SYSTEM_PROMPT,
        user,
        model_role="main",
        max_tokens=1500,
        temperature=0.7,
    )
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        log.warning("%s · no JSON in LLM output: %r", scripted.fixture_id, raw[:200])
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log.warning("%s · JSON decode failed: %s · raw=%r", scripted.fixture_id, e, raw[:200])
        return None

    turns = obj.get("turns")
    if not isinstance(turns, list):
        log.warning("%s · turns is not a list", scripted.fixture_id)
        return None
    for t in turns:
        if not isinstance(t, dict) or t.get("role") not in {"user", "persona"}:
            log.warning("%s · bad turn: %r", scripted.fixture_id, t)
            return None

    return {"scenario": obj.get("scenario", scripted.scenario), "turns": turns}


def _build_synthesized_yaml(
    scripted: Fixture, synth: dict, *, model_name: str
) -> str:
    """Compose the final synthesized YAML · reuses scripted invariants
    and judge prompts so the same harness can run both."""
    doc: dict = {
        "fixture_id": scripted.fixture_id + "_synthesized",
        "version": "synthesized",
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "model": model_name,
        "scenario": synth["scenario"],
        "seed": {
            "persona_block": scripted.seed.persona_block,
        },
        "turns": synth["turns"],
    }
    if scripted.seed.self_block:
        doc["seed"]["self_block"] = scripted.seed.self_block
    if scripted.seed.user_block:
        doc["seed"]["user_block"] = scripted.seed.user_block
    if scripted.seed.mood_block:
        doc["seed"]["mood_block"] = scripted.seed.mood_block
    if scripted.seed.relationship_block:
        doc["seed"]["relationship_block"] = scripted.seed.relationship_block
    if scripted.seed.seed_events:
        doc["seed"]["seed_events"] = [
            {
                "description": e.description,
                "emotional_impact": e.emotional_impact,
                "emotion_tags": e.emotion_tags,
                "relational_tags": e.relational_tags,
                "created_at_offset_hours": e.created_at_offset_hours,
            }
            for e in scripted.seed.seed_events
        ]
    if scripted.retrieve is not None:
        doc["retrieve"] = {
            "query": scripted.retrieve.query,
            "top_k": scripted.retrieve.top_k,
        }
    doc["invariants"] = scripted.invariants
    doc["judge_prompts"] = scripted.judge_prompts

    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


async def synthesize_all(*, only: list[str] | None = None) -> None:
    SYNTHESIZED_DIR.mkdir(parents=True, exist_ok=True)
    llm = build_live_llm()
    model_name = getattr(
        llm, "pinned_model", None
    ) or llm.model_for("main")

    scripted_paths = sorted(SCRIPTED_DIR.glob("*.yaml"))
    if only:
        scripted_paths = [p for p in scripted_paths if p.stem in only]

    for path in scripted_paths:
        scripted = load_fixture(path)
        length_hint = _LENGTH_HINTS.get(scripted.fixture_id, "")
        log.info(
            "synthesizing %s · length_hint=%s", scripted.fixture_id, length_hint or "-"
        )
        synth = await _synthesize_one(llm, scripted, length_hint)
        if synth is None:
            log.warning("%s · skipped (LLM output unparseable)", scripted.fixture_id)
            continue
        text = _build_synthesized_yaml(scripted, synth, model_name=model_name)
        out_path = SYNTHESIZED_DIR / f"{scripted.fixture_id}.yaml"
        out_path.write_text(text, encoding="utf-8")
        log.info("wrote %s (%d turns)", out_path, len(synth["turns"]))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        nargs="+",
        help="Scripted fixture stems to (re-)synthesize. Default: all.",
    )
    args = parser.parse_args()
    asyncio.run(synthesize_all(only=args.only))


if __name__ == "__main__":
    main()
