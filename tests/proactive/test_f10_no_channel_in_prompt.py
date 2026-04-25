"""F10 guard — channel_id must never leak into a MemorySnapshot.

Tests the generator's ``_assert_no_channel_leak`` helper directly with
deliberately polluted snapshots, and through the public MessageGenerator
path to confirm the guard fires before proactive_fn is called.

F10 is documented in ``docs/DISCUSSION.md#2026-04-14`` §§D3-D6 derivation
and ``docs/proactive/01-spec-v0.1.md`` §1.6 / §5.4. The rule: the LLM
prompt must see persona descriptions, event contents, and L2 raw
messages — it must NOT see which channel those messages arrived on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from echovessel.proactive.core.base import MemorySnapshot, SkipReason
from echovessel.proactive.engines.generator import (
    F10Violation,
    MessageGenerator,
    _assert_no_channel_leak,
)
from tests.proactive.fakes import (
    FakeCoreBlock,
    FakeEvent,
    InMemoryMemoryApi,
)


def _snapshot(
    *,
    trigger: str = "long_silence",
    trigger_payload=None,
    core_blocks=(),
    events=(),
    msgs=(),
) -> MemorySnapshot:
    return MemorySnapshot(
        trigger=trigger,
        trigger_payload=trigger_payload or {},
        core_blocks=tuple(core_blocks),
        recent_l3_events=tuple(events),
        recent_l2_window=tuple(msgs),
        relationship_state=None,
        snapshot_hash="test",
    )


# ---------------------------------------------------------------------------
# Direct guard tests
# ---------------------------------------------------------------------------


def test_clean_snapshot_passes():
    snap = _snapshot(
        core_blocks=[FakeCoreBlock(label="persona", content="温暖的陪伴")],
        events=[FakeEvent(id=1, description="用户提到 Mochi 今天把他吵醒")],
    )
    _assert_no_channel_leak(snap)  # should not raise


def test_core_block_content_with_channel_id_raises():
    bad_block = FakeCoreBlock(
        label="persona", content="you are on channel_id web right now"
    )
    snap = _snapshot(core_blocks=[bad_block])
    with pytest.raises(F10Violation):
        _assert_no_channel_leak(snap)


def test_event_description_with_discord_prefix_raises():
    bad_event = FakeEvent(
        id=1,
        description="用户在 discord:guild123 上提到了 Mochi",
    )
    snap = _snapshot(events=[bad_event])
    with pytest.raises(F10Violation):
        _assert_no_channel_leak(snap)


def test_trigger_payload_with_channel_key_raises():
    snap = _snapshot(
        trigger_payload={
            "channel_id": "web",
            "trigger_event_id": 7,
        }
    )
    with pytest.raises(F10Violation):
        _assert_no_channel_leak(snap)


def test_trigger_payload_with_nested_channel_string_raises():
    snap = _snapshot(
        trigger_payload={
            "trigger_event_id": 7,
            "context": "channel_id=discord:g1",
        }
    )
    with pytest.raises(F10Violation):
        _assert_no_channel_leak(snap)


def test_bare_channel_token_in_core_block_raises():
    """A core block whose label is literally ``"web"`` (tokenised channel
    id) is a leak even though it's a short word."""
    bad_block = FakeCoreBlock(label="persona", content="discord")
    snap = _snapshot(core_blocks=[bad_block])
    with pytest.raises(F10Violation):
        _assert_no_channel_leak(snap)


def test_l2_message_channel_id_is_allowed_on_object():
    """RecallMessage objects ARE allowed to carry a channel_id attribute
    (that's how memory stores them). What's forbidden is putting that
    value into a user-visible prompt field. The F10 scan specifically
    skips msg.channel_id because the prompts layer will strip it.

    This test documents the carve-out so future devs don't get confused
    when a RecallMessage flows through the snapshot with channel_id set.
    """

    class _FakeRecall:
        content = "hello user"
        role = "user"
        channel_id = "discord:g1"  # present but allowed

    snap = _snapshot(msgs=[_FakeRecall()])
    _assert_no_channel_leak(snap)  # does NOT raise


# ---------------------------------------------------------------------------
# MessageGenerator integration
# ---------------------------------------------------------------------------


def test_polluted_core_block_blocks_generator_path():
    """If load_core_blocks returns channel-contaminated content, the
    generator must not reach proactive_fn at all — the F10 guard fires
    first and the outcome is a LLM_ERROR skip."""
    memory = InMemoryMemoryApi(
        core_blocks=[
            FakeCoreBlock(label="persona", content="channel_id=web persona block")
        ],
    )
    fn_called = {"n": 0}

    async def _spy_fn(snapshot):
        fn_called["n"] += 1
        from echovessel.proactive.core.base import ProactiveMessage

        return ProactiveMessage(text="should not get here")

    gen = MessageGenerator(memory=memory, proactive_fn=_spy_fn)

    from echovessel.proactive.core.base import (
        ActionType,
        ProactiveDecision,
        TriggerReason,
    )

    decision = ProactiveDecision(
        decision_id="d1",
        persona_id="p",
        user_id="u",
        timestamp=datetime(2026, 4, 15, 12, 0),
        trigger=TriggerReason.LONG_SILENCE.value,
        action=ActionType.SEND.value,
    )

    outcome = asyncio.run(gen.generate(decision=decision, now=datetime(2026, 4, 15, 12, 0)))
    assert outcome.message is None
    assert outcome.skip_reason == SkipReason.LLM_ERROR
    assert "F10 guard" in (outcome.error or "")
    assert fn_called["n"] == 0  # proactive_fn never ran
