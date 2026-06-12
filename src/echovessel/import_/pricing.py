"""Import-side LLM cost estimation.

Used by the admin import UI's pre-flight "how much will this cost?"
dialog. The numbers are estimates — authoritative billing lives on
the LLM provider's dashboard — so we deliberately keep the rates as
module-level constants rather than wiring them to real provider
pricing APIs.

The estimate mirrors what the pipeline actually sends: the text is run
through :func:`echovessel.import_.chunking.chunk_text` (so sliding-
window overlap is counted, not just the raw text), and every chunk
carries the fixed prompt overhead — the extraction system prompt plus
the user-message wrapper. Persona context is not known at estimate
time and is excluded; it is empty in the current admin flow.

The estimator tokenises via :mod:`tiktoken` when it is installed
(EchoVessel already depends on it for ingest bookkeeping). Unknown
tokenisers and fallback paths use a 4-char-per-token heuristic so the
web UI never 500s just because an obscure model was selected.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from echovessel.import_.chunking import chunk_text
from echovessel.import_.extraction import (
    IMPORT_EXTRACTION_SYSTEM_PROMPT,
    format_user_prompt,
)
from echovessel.import_.models import Chunk

log = logging.getLogger(__name__)


# Ballpark USD-per-1K-tokens rates for the small-tier model that
# drives per-chunk extraction. The numbers are 2026-04 estimates taken
# from OpenAI's public pricing page; they are intentionally coarse.
# Front-end must label derived values "estimate" / "约" (spec §4.7a
# pattern — voice carries the same disclaimer).
COST_PER_1K_TOKENS_IN_USD: float = 0.00015   # gpt-4o-mini input tier
COST_PER_1K_TOKENS_OUT_USD: float = 0.00060  # gpt-4o-mini output tier

# Extraction responses are short JSON structs. Empirically ≈ 0.3x of
# the input chunk length. Keep this conservative so the estimate is a
# ceiling rather than a floor — nobody is surprised by a smaller bill.
OUTPUT_TOKEN_MULTIPLIER: float = 0.3


def _count_tokens(text: str) -> int:
    """Return a best-effort token count for ``text``.

    Tries :func:`tiktoken.encoding_for_model` first; falls back to a
    naive len/4 heuristic when the dependency is missing or the
    encoding raises.
    """

    if not text:
        return 0
    try:
        import tiktoken
    except ImportError:
        return max(1, len(text) // 4)
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001
        return max(1, len(text) // 4)


@lru_cache(maxsize=1)
def per_chunk_overhead_tokens() -> int:
    """Fixed input tokens the pipeline sends with every chunk.

    System prompt plus the user-message wrapper around an empty chunk.
    Derived from the real templates so the constant cannot drift from
    the prompts the pipeline actually uses.
    """
    empty = Chunk(chunk_index=0, total_chunks=1, content="")
    wrapper = format_user_prompt(chunk=empty)
    return _count_tokens(IMPORT_EXTRACTION_SYSTEM_PROMPT) + _count_tokens(wrapper)


def estimate_llm_cost(text: str) -> dict:
    """Return an order-of-magnitude LLM cost estimate for ``text``.

    The return shape matches the ``POST /api/admin/import/estimate``
    response body documented in the admin-import spec:

    - ``tokens_in`` — per-chunk content tokens (chunked exactly like
      the pipeline, so sliding-window overlap is included) plus the
      fixed prompt overhead for every chunk
    - ``tokens_out_est`` — heuristic ceiling for the per-chunk
      extraction output (content tokens × ``OUTPUT_TOKEN_MULTIPLIER``;
      prompt overhead does not generate output)
    - ``cost_usd_est`` — rounded float, USD, based on the two
      ``COST_PER_1K_TOKENS_*`` constants
    """

    chunks = chunk_text(text)
    content_tokens = sum(_count_tokens(c.content) for c in chunks)
    tokens_in = content_tokens + len(chunks) * per_chunk_overhead_tokens()
    tokens_out_est = int(round(content_tokens * OUTPUT_TOKEN_MULTIPLIER))

    cost_in = (tokens_in / 1000.0) * COST_PER_1K_TOKENS_IN_USD
    cost_out = (tokens_out_est / 1000.0) * COST_PER_1K_TOKENS_OUT_USD
    cost_usd_est = round(cost_in + cost_out, 6)

    return {
        "tokens_in": tokens_in,
        "tokens_out_est": tokens_out_est,
        "cost_usd_est": cost_usd_est,
    }


__all__ = [
    "estimate_llm_cost",
    "per_chunk_overhead_tokens",
    "COST_PER_1K_TOKENS_IN_USD",
    "COST_PER_1K_TOKENS_OUT_USD",
    "OUTPUT_TOKEN_MULTIPLIER",
]
