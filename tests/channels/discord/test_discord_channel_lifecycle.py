"""Startup / gateway lifecycle tests for :class:`DiscordChannel`.

``start()`` awaits ``login`` inline so a rejected token raises straight
out of it — the channel registry logs the failure and leaves the
channel out of the started set. Later gateway failures (e.g. missing
privileged intents) are logged at error level by the background task.
"""

from __future__ import annotations

import asyncio

import pytest

# Optional ``discord.py`` extra — skip cleanly when not installed.
discord = pytest.importorskip("discord")

from echovessel.channels.discord import channel as channel_module  # noqa: E402
from echovessel.channels.discord.channel import DiscordChannel  # noqa: E402


class _FakeBot:
    """Stand-in for DiscordBot with scriptable login/connect outcomes."""

    def __init__(
        self,
        *,
        login_exc: Exception | None = None,
        connect_exc: Exception | None = None,
        **_kwargs,
    ) -> None:
        self.login_exc = login_exc
        self.connect_exc = connect_exc
        self.closed = False
        self.connect_called = asyncio.Event()

    async def login(self, _token: str) -> None:
        if self.login_exc is not None:
            raise self.login_exc

    async def connect(self, **_kwargs) -> None:
        self.connect_called.set()
        if self.connect_exc is not None:
            raise self.connect_exc
        await asyncio.Event().wait()  # hold open until cancelled

    async def close(self) -> None:
        self.closed = True

    def is_ready(self) -> bool:
        return False


def _patch_bot(monkeypatch, **bot_kwargs) -> list[_FakeBot]:
    created: list[_FakeBot] = []

    def factory(**kwargs):
        bot = _FakeBot(**bot_kwargs, **kwargs)
        created.append(bot)
        return bot

    monkeypatch.setattr(channel_module, "DiscordBot", factory)
    return created


async def test_start_raises_and_logs_on_rejected_token(monkeypatch, caplog):
    created = _patch_bot(
        monkeypatch, login_exc=discord.LoginFailure("Improper token has been passed.")
    )
    ch = DiscordChannel(token="definitely-not-a-token")

    with pytest.raises(discord.LoginFailure):
        await ch.start()

    assert ch._bot is None
    assert ch._bot_task is None
    assert ch.is_ready() is False
    # The half-constructed client was cleaned up.
    assert created[0].closed is True
    assert any(
        "discord login failed" in rec.message for rec in caplog.records if rec.levelname == "ERROR"
    )


async def test_start_succeeds_and_runs_gateway_in_background(monkeypatch):
    created = _patch_bot(monkeypatch)
    ch = DiscordChannel(token="ok-token")

    await ch.start()
    try:
        assert ch._bot is created[0]
        assert ch._bot_task is not None
        await asyncio.wait_for(created[0].connect_called.wait(), timeout=1.0)
        assert not ch._bot_task.done()
    finally:
        await ch.stop()
    assert created[0].closed is True


async def test_gateway_failure_after_login_is_logged(monkeypatch, caplog):
    created = _patch_bot(monkeypatch, connect_exc=RuntimeError("privileged intents not enabled"))
    ch = DiscordChannel(token="ok-token")

    await ch.start()
    try:
        await asyncio.wait_for(created[0].connect_called.wait(), timeout=1.0)
        # _run_gateway logs the failure instead of letting it vanish.
        await asyncio.wait_for(ch._bot_task, timeout=1.0)
        assert any(
            "discord gateway connection failed" in rec.message
            for rec in caplog.records
            if rec.levelname == "ERROR"
        )
    finally:
        await ch.stop()


def test_discord_opts_out_of_proactive_push():
    """Proactive's delivery router probes this flag; the channel routes
    sends by the in-flight turn's author, so it cannot deliver a push
    and must not advertise the capability."""
    assert DiscordChannel.supports_outgoing_push is False
