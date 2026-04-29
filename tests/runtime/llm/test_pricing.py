import pytest

from echovessel.runtime.llm.pricing import estimate_cost


def test_known_model_uses_litellm_rates() -> None:
    """A model present in the vendored JSON computes from those rates."""
    cost = estimate_cost(
        provider="anthropic",
        model="claude-3-7-sonnet-20250219",
        model_role="main",
        base_url=None,
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        cache_read=0,
        cache_creation=0,
    )
    # Sonnet-class is ~$3 / 1M in, ~$15 / 1M out per the vendored JSON.
    # Allow a window so a refresh of LiteLLM that nudges the rates by a
    # few percent doesn't break the test.
    assert 15.0 < cost < 25.0


def test_unknown_model_falls_back_to_role_rate() -> None:
    cost = estimate_cost(
        provider="openai_compat",
        model="some-truly-imaginary-model-zzz",
        model_role="main",
        base_url="https://openrouter.ai/api/v1",
        tokens_in=1000,
        tokens_out=1000,
        cache_read=0,
        cache_creation=0,
    )
    assert cost == pytest.approx(0.0025 + 0.010)


def test_unknown_role_falls_back_to_fast_rate() -> None:
    cost = estimate_cost(
        provider="openai_compat",
        model="some-truly-imaginary-model-zzz",
        model_role="unrecognized",
        base_url=None,
        tokens_in=1000,
        tokens_out=1000,
        cache_read=0,
        cache_creation=0,
    )
    assert cost == pytest.approx(0.00015 + 0.00060)


def test_stub_provider_is_free() -> None:
    cost = estimate_cost(
        provider="stub",
        model="anything",
        model_role="main",
        base_url=None,
        tokens_in=10_000,
        tokens_out=10_000,
        cache_read=0,
        cache_creation=0,
    )
    assert cost == 0.0


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080/v1",
        "http://host.docker.internal:11434/v1",
    ],
)
def test_local_base_url_is_free(base_url: str) -> None:
    cost = estimate_cost(
        provider="openai_compat",
        model="llama-3.3-70b",
        model_role="main",
        base_url=base_url,
        tokens_in=1000,
        tokens_out=1000,
        cache_read=0,
        cache_creation=0,
    )
    assert cost == 0.0


def test_distinct_models_produce_distinct_costs() -> None:
    sonnet = estimate_cost(
        provider="anthropic",
        model="claude-3-7-sonnet-20250219",
        model_role="main",
        base_url=None,
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        cache_read=0,
        cache_creation=0,
    )
    haiku = estimate_cost(
        provider="anthropic",
        model="claude-3-5-haiku-20241022",
        model_role="fast",
        base_url=None,
        tokens_in=1_000_000,
        tokens_out=1_000_000,
        cache_read=0,
        cache_creation=0,
    )
    assert sonnet > haiku
