"""AnthropicProvider tier-resolution, error-classification, and usage tests.

These tests never talk to a real Anthropic endpoint. The SDK client is
constructed lazily, so we only exercise the resolution logic, the
`_classify_anthropic_error` helper, and (in Stage 2) the usage passthrough.
"""

from __future__ import annotations

import types

import pytest

from echovessel.runtime.llm.anthropic import (
    AnthropicProvider,
    _classify_anthropic_error,
)
from echovessel.runtime.llm.base import LLMTier
from echovessel.runtime.llm.errors import (
    LLMBudgetError,
    LLMPermanentError,
    LLMTransientError,
)
from echovessel.runtime.llm.usage import Usage

# ---------------------------------------------------------------------------
# Helpers for mocking the Anthropic SDK client
# ---------------------------------------------------------------------------


def _ns(**kw) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


class _FakeMessages:
    """Fake client.messages with configurable create() and stream()."""

    def __init__(self, *, resp=None, stream_events=(), raise_after_events=False):
        self._resp = resp
        self._stream_events = list(stream_events)
        self._raise_after_events = raise_after_events

    async def create(self, **_kw):
        return self._resp

    def stream(self, **_kw):
        events = self._stream_events
        should_raise = self._raise_after_events

        class _Ctx:
            async def __aenter__(self):
                async def _gen():
                    for e in events:
                        yield e
                    if should_raise:
                        raise Exception("simulated mid-stream abort")

                return _gen()

            async def __aexit__(self, *_a):
                pass

        return _Ctx()


def _provider_with_fake_client(
    resp=None, stream_events=(), raise_after_events=False
) -> AnthropicProvider:
    p = AnthropicProvider(api_key="fake")
    p._client = type(
        "C",
        (),
        {"messages": _FakeMessages(resp=resp, stream_events=stream_events, raise_after_events=raise_after_events)},
    )()
    return p


def test_default_tier_mapping_when_no_overrides():
    p = AnthropicProvider(api_key="fake")
    assert p.provider_name == "anthropic"
    assert p.model_for(LLMTier.SMALL) == "claude-haiku-4-5"
    assert p.model_for(LLMTier.MEDIUM) == "claude-sonnet-4-6"
    assert p.model_for(LLMTier.LARGE) == "claude-opus-4-6"


def test_pinned_model_overrides_all_tiers():
    p = AnthropicProvider(api_key="fake", pinned_model="claude-opus-4-6")
    for tier in LLMTier:
        assert p.model_for(tier) == "claude-opus-4-6"


def test_tier_models_override_defaults():
    p = AnthropicProvider(
        api_key="fake",
        tier_models={"small": "claude-haiku-4-5", "medium": "claude-sonnet-4-6", "large": "claude-opus-4-6"},
    )
    assert p.model_for(LLMTier.SMALL) == "claude-haiku-4-5"


def test_tier_models_partial_with_official_fills_defaults():
    p = AnthropicProvider(api_key="fake", tier_models={"large": "claude-opus-4-6"})
    assert p.model_for(LLMTier.LARGE) == "claude-opus-4-6"
    # other tiers still get defaults because base_url is official
    assert p.model_for(LLMTier.SMALL) == "claude-haiku-4-5"


def test_custom_base_url_requires_model_or_tier_models():
    with pytest.raises(ValueError, match="cannot resolve model for tier"):
        AnthropicProvider(
            api_key="fake",
            base_url="https://some-proxy.example.com/v1",
        )


def test_custom_base_url_with_pinned_model_succeeds():
    p = AnthropicProvider(
        api_key="fake",
        base_url="https://some-proxy.example.com/v1",
        pinned_model="my-custom-model",
    )
    assert p.model_for(LLMTier.SMALL) == "my-custom-model"
    assert p.base_url == "https://some-proxy.example.com/v1"


def test_unknown_tier_key_raises():
    with pytest.raises(ValueError, match="Unknown tier"):
        AnthropicProvider(api_key="fake", tier_models={"huge": "x"})


# ---- error classification --------------------------------------------------


class _FakeError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_classify_5xx_as_transient():
    out = _classify_anthropic_error(_FakeError("boom", 503))
    assert isinstance(out, LLMTransientError)


def test_classify_429_as_transient():
    out = _classify_anthropic_error(_FakeError("rate", 429))
    assert isinstance(out, LLMTransientError)


def test_classify_401_as_permanent():
    out = _classify_anthropic_error(_FakeError("unauth", 401))
    assert isinstance(out, LLMPermanentError)
    assert not isinstance(out, LLMTransientError)


def test_classify_402_as_budget():
    out = _classify_anthropic_error(_FakeError("paid", 402))
    assert isinstance(out, LLMBudgetError)


class _ConnectionTimeoutError(Exception):
    pass


def test_classify_unknown_connection_as_transient():
    out = _classify_anthropic_error(_ConnectionTimeoutError("oops"))
    assert isinstance(out, LLMTransientError)


# ---------------------------------------------------------------------------
# Stage 2 — usage passthrough (mocked SDK)
# ---------------------------------------------------------------------------


async def test_complete_surfaces_usage_with_cache_tokens():
    fake_resp = _ns(
        content=[_ns(text="hello")],
        usage=_ns(
            input_tokens=200,
            output_tokens=40,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=100,
        ),
    )
    p = _provider_with_fake_client(resp=fake_resp)
    text, usage = await p.complete("sys", "usr")
    assert text == "hello"
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 200
    assert usage.output_tokens == 40
    assert usage.cache_read_input_tokens == 500
    assert usage.cache_creation_input_tokens == 100


async def test_complete_usage_none_when_sdk_omits_usage():
    fake_resp = _ns(content=[_ns(text="hi")], usage=None)
    p = _provider_with_fake_client(resp=fake_resp)
    text, usage = await p.complete("sys", "usr")
    assert text == "hi"
    assert usage is None


async def test_stream_surfaces_trailing_usage():
    events = [
        _ns(type="message_start", message=_ns(
            usage=_ns(input_tokens=150, cache_read_input_tokens=50, cache_creation_input_tokens=10)
        )),
        _ns(type="content_block_delta", delta=_ns(type="text_delta", text="foo")),
        _ns(type="content_block_delta", delta=_ns(type="text_delta", text="bar")),
        _ns(type="message_delta", usage=_ns(output_tokens=30)),
    ]
    p = _provider_with_fake_client(stream_events=events)
    chunks: list[str] = []
    trailing: Usage | None = None
    async for item in p.stream("sys", "usr"):
        if isinstance(item, str):
            chunks.append(item)
        else:
            trailing = item
    assert "".join(chunks) == "foobar"
    assert isinstance(trailing, Usage)
    assert trailing.input_tokens == 150
    assert trailing.output_tokens == 30
    assert trailing.cache_read_input_tokens == 50
    assert trailing.cache_creation_input_tokens == 10


async def test_stream_abort_does_not_yield_trailing_usage():
    """If the stream raises mid-way, no partial Usage is yielded.

    Per issue #1 open question #2: partial token counts are discarded
    rather than recorded as misleadingly low.
    """
    events = [
        _ns(type="message_start", message=_ns(
            usage=_ns(input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        )),
        _ns(type="content_block_delta", delta=_ns(type="text_delta", text="par")),
    ]
    p = _provider_with_fake_client(stream_events=events, raise_after_events=True)
    items: list = []
    with pytest.raises(LLMPermanentError):
        async for item in p.stream("sys", "usr"):
            items.append(item)
    assert items == ["par"]  # only text before abort; no trailing Usage
