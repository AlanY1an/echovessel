"""LLM prompt templates and parsers.

Pure templates — no LLM client, no memory/runtime imports. Runtime is the
layer that wires these into `extract_fn` / `reflect_fn` / `judge_fn`
callables by combining the templates with an LLM provider.

Layering rule (enforced by import-linter):

    prompts → core only

prompts MUST NOT import from memory, voice, channels, or runtime. See
`PROJECT_TRACKER.md` §3.1.
"""

from echovessel.prompts.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    MAX_EMOTION_TAGS,
    RELATIONAL_TAG_VOCABULARY,
    ExtractionParseError,
    ExtractionParseResult,
    RawExtractedEvent,
    format_extraction_user_prompt,
    parse_extraction_response,
)
from echovessel.prompts.judge import (
    ANTI_PATTERNS,
    HEART_DIMENSIONS,
    JUDGE_SYSTEM_PROMPT,
    REASONING_SOFT_CAP_CHARS,
    VALID_VERDICTS,
    JudgeParseError,
    JudgeVerdict,
    format_judge_user_prompt,
    parse_judge_response,
)
from echovessel.prompts.persona_bootstrap import (
    MAX_MOOD_BLOCK_CHARS,
    MAX_PERSONA_BLOCK_CHARS,
    MAX_RELATIONSHIP_BLOCK_CHARS,
    MAX_SELF_BLOCK_CHARS,
    MAX_USER_BLOCK_CHARS,
    PERSONA_BOOTSTRAP_SYSTEM_PROMPT,
    BootstrappedBlocks,
    PersonaBootstrapParseError,
    format_persona_bootstrap_user_prompt,
    parse_persona_bootstrap_response,
)
from echovessel.prompts.persona_facts import (
    ENUM_EDUCATION_LEVEL,
    ENUM_GENDER,
    ENUM_HEALTH_STATUS,
    ENUM_LIFE_STAGE,
    ENUM_RELATIONSHIP_STATUS,
    FACT_FIELDS,
    PERSONA_FACTS_SYSTEM_PROMPT,
    ExtractedFacts,
    ExtractedPersona,
    PersonaFactsParseError,
    format_persona_facts_user_prompt,
    parse_persona_facts_response,
)
from echovessel.prompts.reflection import (
    MAX_THOUGHTS,
    RECOMMENDED_IMPACT_BOUND,
    REFLECTION_SYSTEM_PROMPT,
    RawExtractedThought,
    ReflectionParseError,
    ReflectionParseResult,
    format_reflection_user_prompt,
    parse_reflection_response,
)

__all__ = [
    # extraction
    "EXTRACTION_SYSTEM_PROMPT",
    "MAX_EMOTION_TAGS",
    "RELATIONAL_TAG_VOCABULARY",
    "ExtractionParseError",
    "ExtractionParseResult",
    "RawExtractedEvent",
    "format_extraction_user_prompt",
    "parse_extraction_response",
    # reflection
    "MAX_THOUGHTS",
    "RECOMMENDED_IMPACT_BOUND",
    "REFLECTION_SYSTEM_PROMPT",
    "RawExtractedThought",
    "ReflectionParseError",
    "ReflectionParseResult",
    "format_reflection_user_prompt",
    "parse_reflection_response",
    # judge
    "ANTI_PATTERNS",
    "HEART_DIMENSIONS",
    "JUDGE_SYSTEM_PROMPT",
    "REASONING_SOFT_CAP_CHARS",
    "VALID_VERDICTS",
    "JudgeParseError",
    "JudgeVerdict",
    "format_judge_user_prompt",
    "parse_judge_response",
    # persona bootstrap
    "PERSONA_BOOTSTRAP_SYSTEM_PROMPT",
    "MAX_PERSONA_BLOCK_CHARS",
    "MAX_SELF_BLOCK_CHARS",
    "MAX_USER_BLOCK_CHARS",
    "MAX_MOOD_BLOCK_CHARS",
    "MAX_RELATIONSHIP_BLOCK_CHARS",
    "BootstrappedBlocks",
    "PersonaBootstrapParseError",
    "format_persona_bootstrap_user_prompt",
    "parse_persona_bootstrap_response",
    # persona facts
    "PERSONA_FACTS_SYSTEM_PROMPT",
    "FACT_FIELDS",
    "ENUM_GENDER",
    "ENUM_EDUCATION_LEVEL",
    "ENUM_RELATIONSHIP_STATUS",
    "ENUM_LIFE_STAGE",
    "ENUM_HEALTH_STATUS",
    "ExtractedFacts",
    "ExtractedPersona",
    "PersonaFactsParseError",
    "format_persona_facts_user_prompt",
    "parse_persona_facts_response",
]
