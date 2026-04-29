from echovessel.runtime.llm.prices import ModelPrice, lookup_price


def test_lookup_price_known_anthropic_model_returns_rates() -> None:
    # Use the dated SDK model id, which LiteLLM tracks authoritatively.
    price = lookup_price("anthropic", "claude-3-7-sonnet-20250219")
    assert isinstance(price, ModelPrice)
    assert price.input_per_1k_usd > 0
    assert price.output_per_1k_usd > price.input_per_1k_usd


def test_lookup_price_known_openai_model_returns_rates() -> None:
    price = lookup_price("openai_compat", "gpt-4o")
    assert isinstance(price, ModelPrice)
    assert price.input_per_1k_usd > 0
    assert price.output_per_1k_usd > 0


def test_lookup_price_handles_provider_prefixed_key() -> None:
    """LiteLLM sometimes only keys models under `<provider>/<model>`.
    Our loader must try both forms."""
    price = lookup_price("anthropic", "claude-3-opus-20240229")
    assert isinstance(price, ModelPrice)


def test_lookup_price_unknown_model_returns_none() -> None:
    assert lookup_price("anthropic", "claude-imaginary-9-9") is None


def test_lookup_price_none_or_empty_model_returns_none() -> None:
    assert lookup_price("anthropic", None) is None
    assert lookup_price("anthropic", "") is None


def test_lookup_price_stub_provider_returns_none() -> None:
    """Stub never has a price entry; pricing module short-circuits stub
    separately. The loader returns None to avoid masking that branch."""
    assert lookup_price("stub", "anything") is None


def test_lookup_price_wrong_provider_for_known_model_returns_none() -> None:
    """gpt-4o is an OpenAI model. Asking for it under provider=anthropic
    must not return OpenAI's price."""
    assert lookup_price("anthropic", "gpt-4o") is None


def test_lookup_price_per_1k_conversion_is_consistent() -> None:
    """LiteLLM stores per-token rates. We multiply by 1000. Spot check
    that a Sonnet input rate lands within an order of magnitude of
    the published $3 / 1M (== $0.003 / 1K)."""
    price = lookup_price("anthropic", "claude-3-7-sonnet-20250219")
    assert price is not None
    assert 0.001 < price.input_per_1k_usd < 0.01
