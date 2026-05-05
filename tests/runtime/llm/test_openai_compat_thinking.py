"""thinking_enabled translation in OpenAICompatibleProvider.

DeepSeek expects ``extra_body={"thinking": {"type": "enabled"|"disabled"}}``.
For non-DeepSeek base URLs (official OpenAI / Ollama / unknown), the flag is
a no-op so we don't 400 with an unknown kwarg.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from echovessel.runtime.llm.openai_compat import OpenAICompatibleProvider


def _make_provider(base_url: str | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="test",
        base_url=base_url,
        pinned_model="test-model",
    )


def _stub_client_response() -> MagicMock:
    msg = MagicMock(content="ok")
    choice = MagicMock(message=msg)
    resp = MagicMock(
        choices=[choice],
        usage=MagicMock(
            prompt_tokens=1,
            completion_tokens=1,
            prompt_tokens_details=None,
        ),
    )
    return resp


@pytest.fixture
def fake_client() -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_stub_client_response())
    return client


async def test_thinking_none_does_not_set_extra_body(
    fake_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _make_provider(base_url="https://api.deepseek.com")
    monkeypatch.setattr(p, "_get_client", lambda: fake_client)
    await p.complete(system="s", user="u", thinking_enabled=None)
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs or "thinking" not in (kwargs.get("extra_body") or {})


async def test_thinking_false_sends_disabled_to_deepseek(
    fake_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _make_provider(base_url="https://api.deepseek.com")
    monkeypatch.setattr(p, "_get_client", lambda: fake_client)
    await p.complete(system="s", user="u", thinking_enabled=False)
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"


async def test_thinking_true_sends_enabled_to_deepseek(
    fake_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _make_provider(base_url="https://api.deepseek.com")
    monkeypatch.setattr(p, "_get_client", lambda: fake_client)
    await p.complete(system="s", user="u", thinking_enabled=True)
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"]["type"] == "enabled"


async def test_thinking_false_no_op_on_official_openai(
    fake_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain OpenAI base_url shouldn't get the DeepSeek-specific extra_body."""
    p = _make_provider(base_url=None)  # default = api.openai.com
    monkeypatch.setattr(p, "_get_client", lambda: fake_client)
    await p.complete(system="s", user="u", thinking_enabled=False)
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    # No extra_body sent → plain OpenAI gpt-4o ignores thinking concept gracefully.
    assert "extra_body" not in kwargs or kwargs.get("extra_body") in (None, {})
