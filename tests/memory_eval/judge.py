"""Judge · ask the same LLM provider a yes/no question with evidence.

Judge is deliberately the same ``LLMProvider`` the extractor runs on,
so eval cost lives on a single account. The judge prompt is
intentionally thin: "here is the scenario + produced output · answer
YES or NO to this question · one short sentence of reasoning".

We pass ``model_role="judge"`` because eval judgements are where a
tuned model earns its keep — a fast model is too prone to rubber
stamping and the main interaction model is overkill for a binary.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from echovessel.runtime.llm.base import LLMProvider

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are an evaluator for an AI memory system. For each question you are
asked, respond with strict JSON in this exact shape:

  {"verdict": "yes" | "no", "reasoning": "<one short sentence>"}

No preamble, no markdown, no code fences. Judge strictly but fairly —
if the evidence only partially supports the claim, answer "no".
"""


@dataclass(slots=True)
class JudgeVerdict:
    prompt: str
    verdict: bool
    reasoning: str
    raw: str


async def judge_prompts(
    *,
    llm: LLMProvider,
    evidence: str,
    prompts: list[str],
    model_role: str = "judge",
) -> list[JudgeVerdict]:
    """Run each question as its own LLM call so verdicts are
    independent. Returns one :class:`JudgeVerdict` per prompt, in
    order. Never raises on a malformed judge response — unparseable
    output is recorded as ``verdict=False`` with the raw text in
    ``reasoning`` so the test failure message explains what happened.
    """
    out: list[JudgeVerdict] = []
    for q in prompts:
        user = (
            f"--- EVIDENCE ---\n{evidence}\n\n"
            f"--- QUESTION ---\n{q}\n\n"
            "Answer strict JSON."
        )
        raw = ""
        try:
            raw, _usage = await llm.complete(
                _SYSTEM_PROMPT,
                user,
                model_role=model_role,
                max_tokens=200,
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001
            out.append(
                JudgeVerdict(
                    prompt=q,
                    verdict=False,
                    reasoning=f"judge call raised: {e}",
                    raw="",
                )
            )
            continue

        verdict, reason = _parse(raw)
        out.append(JudgeVerdict(prompt=q, verdict=verdict, reasoning=reason, raw=raw))
    return out


def _parse(raw: str) -> tuple[bool, str]:
    """Tolerant parser — look for the first JSON object in the blob
    and read its ``verdict`` / ``reasoning`` fields. Fall back to a
    regex yes/no if JSON fails. On pure gibberish, default to ``False``.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            verdict = str(obj.get("verdict", "")).strip().lower()
            reasoning = str(obj.get("reasoning", "") or "")
            return verdict == "yes", reasoning or raw
        except json.JSONDecodeError:
            pass

    low = raw.strip().lower()
    if low.startswith("yes"):
        return True, raw
    if low.startswith("no"):
        return False, raw
    return False, f"(unparseable judge output: {raw[:120]!r})"


__all__ = ["JudgeVerdict", "judge_prompts"]
