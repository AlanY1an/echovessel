"""Layer 1 PROFILE derivation prompt + parser.

Pure library code — no LLM client, no memory imports. Initiative
``2026-04-proactive-v2`` § Layer 1 spells out the contract this prompt
implements: read core_blocks + locale + (optional) imported corpus
sample, return a JSON object with three fields used to populate the
``persona_profile`` row.

Layering rule (enforced by import-linter):

    prompts → core only

The runtime wiring layer (:mod:`echovessel.runtime.wiring.proactive_profile`)
joins this template to ``runtime.ctx.llm`` and exposes the closure
through ``runtime.ctx.regenerate_profile_fn``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


PROFILE_DERIVATION_SYSTEM_PROMPT: str = (
    "你是一个性格学习专家，专注于描述说话方式（不是性格判断）。"
    "你只输出 JSON。"
    "你的描述必须忠于素材里实际观察到的细节；"
    "在素材里看不到的项必须明确写「不确定」，"
    "绝不编造没有根据的细节。"
)


_USER_PROMPT_TEMPLATE = """\
# 素材

## persona block（她是谁）
{persona_block}

## user block（她对用户的了解）
{user_block}

## style block（用户偏好的风格备忘）
{style_block}

## locale
{locale}

## 历史聊天样本（若有）
{corpus_sample}

# 任务

输出一个 JSON 对象，包含且只包含以下三个字段：

- `style_summary`: 100-200 字自然语言描述这个 persona 的说话方式。
  必须覆盖：
    1. 典型消息长度（短句 / 中等 / 长段落）
    2. 开场风格（关心式 / 披露式 / 直奔主题 / preamble）
    3. emoji / 表情包 / 颜文字使用习惯（高 / 中 / 低 / 无）
    4. 问问题方式（直接 / 含糊到具体 / 兜圈子）
    5. 沉默处理（不催 / 隔天提 / 不再说）
  只描述说话方式，不写性格判断（不要「她很温柔」），
  在素材里看不到的项写「不确定」。

- `quiet_hours`: [start_hour, end_hour] · 0-23 整数 · 推断她不说话的时段。
  默认 [23, 7]；素材里若有线索（夜班 / 海外）适当调整。

- `forbidden_topics`: 字符串数组 · 她绝不主动起的话题。
  例如 ["politics", "money_explicit", "ex_partner"]。
  不确定就空数组。

输出 JSON，不要别的文本，不要 markdown 包裹。
"""


@dataclass(frozen=True)
class ParsedProfileDerivation:
    """The three fields returned by the LLM call."""

    style_summary: str
    quiet_hours: list[int]
    forbidden_topics: list[str]


class ProfileDerivationParseError(ValueError):
    """Raised when the LLM response cannot be parsed into a profile."""


def format_profile_derivation_user_prompt(
    *,
    core_blocks: dict[str, str],
    locale: str,
    imported_corpus_sample: list[str] | None = None,
) -> str:
    """Render the user prompt for the LARGE-tier LLM call."""

    sample_lines: list[str]
    if imported_corpus_sample:
        sample_lines = [f"- {line.strip()}" for line in imported_corpus_sample if line.strip()]
        sample = "\n".join(sample_lines) if sample_lines else "（无样本）"
    else:
        sample = "（无样本）"

    return _USER_PROMPT_TEMPLATE.format(
        persona_block=core_blocks.get("persona") or "（空）",
        user_block=core_blocks.get("user") or "（空）",
        style_block=core_blocks.get("style") or "（空）",
        locale=locale,
        corpus_sample=sample,
    )


def parse_profile_derivation_response(text: str) -> ParsedProfileDerivation:
    """Parse the LLM response into a structured object.

    Raises :class:`ProfileDerivationParseError` for any of:

    * not valid JSON
    * not a JSON object
    * missing one of the three required keys
    * shape violations (``quiet_hours`` not exactly two integers in
      ``0..23``; ``style_summary`` empty)
    """

    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        # The LARGE provider sometimes wraps the JSON in a fenced code
        # block. Strip the fence so the raw JSON parser succeeds.
        first_newline = cleaned.find("\n")
        if first_newline > 0:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()

    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ProfileDerivationParseError(f"response is not JSON: {e}") from e

    if not isinstance(data, dict):
        raise ProfileDerivationParseError("response is not a JSON object")

    style_summary = data.get("style_summary")
    if not isinstance(style_summary, str) or not style_summary.strip():
        raise ProfileDerivationParseError("missing or empty style_summary")

    quiet_hours_raw = data.get("quiet_hours")
    if (
        not isinstance(quiet_hours_raw, list)
        or len(quiet_hours_raw) != 2
        or not all(isinstance(h, int) and 0 <= h <= 23 for h in quiet_hours_raw)
    ):
        raise ProfileDerivationParseError("quiet_hours must be [start, end] with each in 0..23")

    forbidden_topics_raw = data.get("forbidden_topics", [])
    if not isinstance(forbidden_topics_raw, list) or not all(
        isinstance(t, str) for t in forbidden_topics_raw
    ):
        raise ProfileDerivationParseError("forbidden_topics must be a list of strings")
    forbidden_topics = [t.strip() for t in forbidden_topics_raw if t.strip()]

    return ParsedProfileDerivation(
        style_summary=style_summary.strip(),
        quiet_hours=list(quiet_hours_raw),
        forbidden_topics=forbidden_topics,
    )


__all__ = [
    "PROFILE_DERIVATION_SYSTEM_PROMPT",
    "ParsedProfileDerivation",
    "ProfileDerivationParseError",
    "format_profile_derivation_user_prompt",
    "parse_profile_derivation_response",
]
