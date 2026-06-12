"""Cost estimate must cover per-chunk prompt overhead, not just raw text."""

from __future__ import annotations

from echovessel.import_.chunking import chunk_text
from echovessel.import_.pricing import (
    COST_PER_1K_TOKENS_IN_USD,
    COST_PER_1K_TOKENS_OUT_USD,
    OUTPUT_TOKEN_MULTIPLIER,
    _count_tokens,
    estimate_llm_cost,
    per_chunk_overhead_tokens,
)

MULTI_PARAGRAPH = "\n\n".join(
    f"Paragraph {i}: a short diary entry about day {i}." for i in range(40)
)


def test_estimate_is_at_least_naive_content_cost():
    naive_tokens_in = _count_tokens(MULTI_PARAGRAPH)
    naive_cost = (naive_tokens_in / 1000.0) * COST_PER_1K_TOKENS_IN_USD + (
        naive_tokens_in * OUTPUT_TOKEN_MULTIPLIER / 1000.0
    ) * COST_PER_1K_TOKENS_OUT_USD

    result = estimate_llm_cost(MULTI_PARAGRAPH)
    assert result["tokens_in"] >= naive_tokens_in
    assert result["cost_usd_est"] >= naive_cost


def test_estimate_accounts_for_chunk_count():
    n_chunks = len(chunk_text(MULTI_PARAGRAPH))
    assert n_chunks > 1
    overhead = per_chunk_overhead_tokens()
    # The system prompt alone is a few hundred tokens.
    assert overhead > 100

    result = estimate_llm_cost(MULTI_PARAGRAPH)
    content_tokens = sum(_count_tokens(c.content) for c in chunk_text(MULTI_PARAGRAPH))
    assert result["tokens_in"] == content_tokens + n_chunks * overhead


def test_estimate_includes_sliding_window_overlap():
    # One paragraph far above MAX_CHUNK_CHARS forces overlapping windows,
    # so the chunked token total exceeds the raw-text token count.
    long_text = "word " * 2000
    result = estimate_llm_cost(long_text)
    raw_tokens = _count_tokens(long_text.strip())
    n_chunks = len(chunk_text(long_text))
    assert n_chunks > 1
    assert result["tokens_in"] - n_chunks * per_chunk_overhead_tokens() > raw_tokens


def test_empty_text_estimates_zero():
    result = estimate_llm_cost("   \n  ")
    assert result == {"tokens_in": 0, "tokens_out_est": 0, "cost_usd_est": 0.0}
