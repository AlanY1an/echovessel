"""LLMProvider Protocol + LLMTier basic contract tests (PR 1)."""

from __future__ import annotations

import pytest

from echovessel.runtime.llm import (
    LLMBudgetError,
    LLMError,
    LLMPermanentError,
    LLMProvider,
    LLMTier,
    LLMTransientError,
    StubProvider,
)


def test_tier_values_are_stable_strings():
    # Downstream (EVAL harness, WEB) imports these literal values.
    assert LLMTier.SMALL == "small"
    assert LLMTier.MEDIUM == "medium"
    assert LLMTier.LARGE == "large"
    assert list(LLMTier) == [LLMTier.SMALL, LLMTier.MEDIUM, LLMTier.LARGE]


def test_error_hierarchy():
    assert issubclass(LLMTransientError, LLMError)
    assert issubclass(LLMPermanentError, LLMError)
    assert issubclass(LLMBudgetError, LLMPermanentError)


def test_stub_satisfies_protocol():
    stub = StubProvider(fallback="hi")
    assert isinstance(stub, LLMProvider)
    assert stub.provider_name == "stub"


async def test_stub_complete_fallback():
    stub = StubProvider(fallback="canned-fallback")
    text, usage = await stub.complete(system="sys", user="anything")
    assert text == "canned-fallback"
    assert usage is None


async def test_stub_canned_exact_match():
    stub = StubProvider(canned_responses={("sys", "hello"): "HEY"}, fallback="default")
    text_hey, _ = await stub.complete("sys", "hello")
    assert text_hey == "HEY"
    text_def, _ = await stub.complete("sys", "other")
    assert text_def == "default"


async def test_stub_responder_callable():
    def responder(*, system, user, tier, **kw):
        return f"tier={tier} says {user}"

    stub = StubProvider(responder=responder)
    out, usage = await stub.complete("sys", "ping", tier=LLMTier.LARGE)
    assert "tier=large" in out
    assert "says ping" in out
    assert usage is None


async def test_stub_async_responder():
    async def aresponder(**kw):
        return "async-ok"

    stub = StubProvider(responder=aresponder)
    out, usage = await stub.complete("s", "u")
    assert out == "async-ok"
    assert usage is None


async def test_stub_stream_yields_once_from_complete():
    stub = StubProvider(fallback="streamed")
    pieces: list[str] = []
    async for item in stub.stream("s", "u"):
        if isinstance(item, str):
            pieces.append(item)
    assert pieces == ["streamed"]


async def test_stub_keyerror_when_no_canned_and_no_fallback():
    stub = StubProvider(canned_responses={("a", "b"): "x"}, fallback=None)
    text, _ = await stub.complete("a", "b")
    assert text == "x"
    with pytest.raises(KeyError):
        await stub.complete("zz", "nope")


def test_stub_model_for_returns_configured_or_default():
    stub = StubProvider(model_for_tier={LLMTier.LARGE: "big-model"})
    assert stub.model_for(LLMTier.LARGE) == "big-model"
    assert stub.model_for(LLMTier.SMALL) == "stub-model"
