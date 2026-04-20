"""OpenAICompatibleProvider role-resolution, error-classification, and usage tests."""

from __future__ import annotations

import types

import pytest

from echovessel.runtime.llm.base import MODEL_ROLES
from echovessel.runtime.llm.errors import (
    LLMBudgetError,
    LLMPermanentError,
    LLMTransientError,
)
from echovessel.runtime.llm.openai_compat import (
    OpenAICompatibleProvider,
    _classify_openai_error,
)
from echovessel.runtime.llm.usage import Usage

# ---------------------------------------------------------------------------
# Helpers for mocking the OpenAI SDK client
# ---------------------------------------------------------------------------


def _ns(**kw) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


def _chunk(content: str | None) -> types.SimpleNamespace:
    """Regular stream chunk with one choice containing a delta."""
    delta = _ns(content=content)
    choice = _ns(delta=delta)
    return _ns(choices=[choice])


def _usage_chunk(prompt: int, completion: int, cached: int = 0) -> types.SimpleNamespace:
    """Terminal stream chunk: choices=[], usage populated."""
    details = _ns(cached_tokens=cached)
    usage = _ns(prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details)
    return _ns(choices=[], usage=usage)


class _FakeCompletions:
    def __init__(self, *, resp=None, stream_chunks=(), raise_after_chunks=False):
        self._resp = resp
        self._chunks = list(stream_chunks)
        self._raise_after_chunks = raise_after_chunks

    async def create(self, **kw):
        if kw.get("stream"):
            chunks = self._chunks
            should_raise = self._raise_after_chunks

            class _AsyncIter:
                def __aiter__(self):
                    return self._gen()

                async def _gen(self):
                    for c in chunks:
                        yield c
                    if should_raise:
                        raise Exception("simulated mid-stream abort")

            return _AsyncIter()
        return self._resp


def _provider_with_fake_client(
    resp=None, stream_chunks=(), raise_after_chunks=False
) -> OpenAICompatibleProvider:
    p = OpenAICompatibleProvider(api_key="fake")
    chat = type(
        "Chat",
        (),
        {
            "completions": _FakeCompletions(
                resp=resp, stream_chunks=stream_chunks, raise_after_chunks=raise_after_chunks
            )
        },
    )()
    p._client = type("C", (), {"chat": chat})()
    return p


def test_official_defaults():
    p = OpenAICompatibleProvider(api_key="fake")
    assert p.provider_name == "openai_compat"
    assert p.model_for("fast") == "gpt-4o-mini"
    assert p.model_for("main") == "gpt-4o"
    assert p.model_for("judge") == "gpt-4o"


def test_pinned_model_overrides_all_roles():
    p = OpenAICompatibleProvider(api_key="fake", pinned_model="gpt-4o")
    for role in MODEL_ROLES:
        assert p.model_for(role) == "gpt-4o"


def test_explicit_openai_base_url_still_gets_defaults():
    p = OpenAICompatibleProvider(api_key="fake", base_url="https://api.openai.com/v1")
    assert p.model_for("fast") == "gpt-4o-mini"


def test_custom_base_url_requires_model_or_role_models():
    with pytest.raises(ValueError, match="cannot resolve model for role"):
        OpenAICompatibleProvider(
            api_key=None,
            base_url="http://localhost:11434/v1",
        )


def test_ollama_with_explicit_role_models_succeeds():
    p = OpenAICompatibleProvider(
        api_key=None,
        base_url="http://localhost:11434/v1",
        role_models={"fast": "llama3:8b", "main": "llama3:70b", "judge": "llama3:70b"},
    )
    assert p.model_for("fast") == "llama3:8b"
    assert p.model_for("main") == "llama3:70b"


def test_openrouter_with_pinned_model_succeeds():
    p = OpenAICompatibleProvider(
        api_key="fake",
        base_url="https://openrouter.ai/api/v1",
        pinned_model="anthropic/claude-sonnet-4",
    )
    assert p.model_for("fast") == "anthropic/claude-sonnet-4"
    assert p.base_url == "https://openrouter.ai/api/v1"


def test_custom_base_url_judge_falls_back_to_main():
    """On a custom endpoint, 'judge' auto-inherits 'main' when unspecified."""
    p = OpenAICompatibleProvider(
        api_key=None,
        base_url="http://localhost:11434/v1",
        role_models={"fast": "llama3:8b", "main": "llama3:70b"},
    )
    assert p.model_for("judge") == "llama3:70b"


# ---- error classification --------------------------------------------------


class _FakeError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_classify_5xx_transient():
    assert isinstance(_classify_openai_error(_FakeError("", 502)), LLMTransientError)


def test_classify_401_permanent():
    assert isinstance(_classify_openai_error(_FakeError("", 401)), LLMPermanentError)


def test_classify_402_budget():
    assert isinstance(_classify_openai_error(_FakeError("", 402)), LLMBudgetError)


def test_classify_429_transient():
    assert isinstance(_classify_openai_error(_FakeError("", 429)), LLMTransientError)


# ---------------------------------------------------------------------------
# Stage 2 — usage passthrough (mocked SDK)
# ---------------------------------------------------------------------------


async def test_complete_surfaces_usage():
    details = _ns(cached_tokens=20)
    raw_usage = _ns(prompt_tokens=100, completion_tokens=50, prompt_tokens_details=details)
    msg = _ns(content="reply text")
    choice = _ns(message=msg)
    resp = _ns(choices=[choice], usage=raw_usage)
    p = _provider_with_fake_client(resp=resp)
    text, usage = await p.complete("sys", "usr")
    assert text == "reply text"
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_input_tokens == 20


async def test_stream_with_usage_chunk_yields_trailing_usage():
    chunks = [
        _chunk("hel"),
        _chunk("lo"),
        _usage_chunk(prompt=80, completion=10, cached=5),
    ]
    p = _provider_with_fake_client(stream_chunks=chunks)
    text_pieces: list[str] = []
    trailing: Usage | None = None
    async for item in p.stream("sys", "usr"):
        if isinstance(item, str):
            text_pieces.append(item)
        else:
            trailing = item
    assert "".join(text_pieces) == "hello"
    assert isinstance(trailing, Usage)
    assert trailing.input_tokens == 80
    assert trailing.output_tokens == 10
    assert trailing.cache_read_input_tokens == 5


async def test_stream_without_usage_chunk_yields_text_only():
    chunks = [_chunk("foo"), _chunk("bar")]
    p = _provider_with_fake_client(stream_chunks=chunks)
    items: list = []
    async for item in p.stream("sys", "usr"):
        items.append(item)
    assert items == ["foo", "bar"]  # no trailing Usage


async def test_stream_abort_does_not_yield_trailing_usage():
    """If the stream raises mid-way, no partial Usage is yielded.

    Per issue #1 open question #2: partial token counts are discarded
    rather than recorded as misleadingly low.
    """
    chunks = [_chunk("hel"), _chunk("lo")]
    p = _provider_with_fake_client(stream_chunks=chunks, raise_after_chunks=True)
    items: list = []
    with pytest.raises(LLMPermanentError):
        async for item in p.stream("sys", "usr"):
            items.append(item)
    assert items == ["hel", "lo"]  # only text before abort; no trailing Usage
