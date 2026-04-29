"""Per-call cost estimation.

Lookup precedence:
1. `provider == "stub"` -> 0.0 (stub is always free)
2. `base_url` host is a loopback / dev address -> 0.0 (local-only inference)
3. `prices.lookup_price(provider, model)` is not None -> use those rates
4. Else -> legacy role-based rate (`fast`/`main`/`judge`), falling back
   to `fast` if the role is unrecognized

The role fallback exists for two cases: (a) a custom-endpoint model
that LiteLLM doesn't track, (b) a typo in the user's model field. Both
are rare on the happy path; the dropdown nudges users toward known
models. Users wanting exact billing for a long-tail endpoint should
add the model upstream in LiteLLM (or refresh the vendored JSON).
"""

from __future__ import annotations

from echovessel.core.llm.endpoints import is_local_base_url
from echovessel.core.llm.prices import lookup_price

_FREE_PROVIDERS: frozenset[str] = frozenset({"stub"})

_ROLE_RATES_USD_PER_1K: dict[str, dict[str, float]] = {
    "fast": {"in": 0.00015, "out": 0.00060},
    "main": {"in": 0.0025, "out": 0.010},
    "judge": {"in": 0.0025, "out": 0.010},
}


def estimate_cost(
    *,
    provider: str,
    model: str | None,
    model_role: str,
    base_url: str | None,
    tokens_in: int,
    tokens_out: int,
    cache_read: int,
    cache_creation: int,
) -> float:
    if provider in _FREE_PROVIDERS:
        return 0.0
    if is_local_base_url(base_url):
        return 0.0

    price = lookup_price(provider, model)
    if price is not None:
        return (
            (tokens_in / 1000) * price.input_per_1k_usd
            + (tokens_out / 1000) * price.output_per_1k_usd
            + (cache_read / 1000) * price.cache_read_per_1k_usd
            + (cache_creation / 1000) * price.cache_creation_per_1k_usd
        )

    rate = _ROLE_RATES_USD_PER_1K.get(model_role, _ROLE_RATES_USD_PER_1K["fast"])
    return (tokens_in / 1000) * rate["in"] + (tokens_out / 1000) * rate["out"]
