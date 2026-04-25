"""MessageGenerator tests — snapshot build + LLM failure modes.

The F10 guard gets its own dedicated test file (test_f10_no_channel_in_prompt.py).
This file focuses on the happy path, snapshot field population, and LLM
error translation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from echovessel.proactive.core.base import (
    ActionType,
    ProactiveDecision,
    ProactiveMessage,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.engines.generator import (
    GenerationOutcome,
    MessageGenerator,
)
from tests.proactive.fakes import (
    FakeCoreBlock,
    FakeEvent,
    FakeMessage,
    InMemoryMemoryApi,
    make_fake_proactive_fn,
)


def _decision(trigger: TriggerReason = TriggerReason.LONG_SILENCE) -> ProactiveDecision:
    return ProactiveDecision(
        decision_id="d1",
        persona_id="p",
        user_id="u",
        timestamp=datetime(2026, 4, 15, 12, 0),
        trigger=trigger.value,
        action=ActionType.SEND.value,
    )


def _run(gen: MessageGenerator, decision: ProactiveDecision) -> GenerationOutcome:
    return asyncio.run(
        gen.generate(decision=decision, now=datetime(2026, 4, 15, 12, 0))
    )


def test_generate_happy_path():
    memory = InMemoryMemoryApi(
        core_blocks=[FakeCoreBlock(label="persona", content="温暖的陪伴")],
        recent_events=[FakeEvent(id=1, description="用户提到了 Mochi")],
        recent_messages=[FakeMessage(content="你好", role="user")],
    )
    fn = make_fake_proactive_fn(text="想你了, 今天过得怎么样")
    gen = MessageGenerator(memory=memory, proactive_fn=fn)

    outcome = _run(gen, _decision())
    assert outcome.message is not None
    assert outcome.message.text == "想你了, 今天过得怎么样"
    assert outcome.snapshot.trigger == TriggerReason.LONG_SILENCE.value
    assert len(outcome.snapshot.core_blocks) == 1
    assert len(outcome.snapshot.recent_l3_events) == 1
    assert len(outcome.snapshot.recent_l2_window) == 1
    assert outcome.snapshot.snapshot_hash  # non-empty 16-char hex
    assert len(outcome.snapshot.snapshot_hash) == 16


def test_generate_snapshot_hash_is_stable():
    memory = InMemoryMemoryApi(
        core_blocks=[FakeCoreBlock(content="stable")],
        recent_events=[FakeEvent(id=1, description="stable event")],
    )
    fn = make_fake_proactive_fn()
    gen = MessageGenerator(memory=memory, proactive_fn=fn)
    out1 = _run(gen, _decision())
    out2 = _run(gen, _decision())
    assert out1.snapshot.snapshot_hash == out2.snapshot.snapshot_hash


def test_generate_llm_exception_becomes_llm_error_skip():
    memory = InMemoryMemoryApi()
    fn = make_fake_proactive_fn(raise_exc=RuntimeError)
    gen = MessageGenerator(memory=memory, proactive_fn=fn)
    outcome = _run(gen, _decision())
    assert outcome.message is None
    assert outcome.skip_reason == SkipReason.LLM_ERROR
    assert "RuntimeError" in (outcome.error or "")


def test_generate_empty_text_becomes_output_invalid():
    memory = InMemoryMemoryApi()

    async def _fn(snapshot):
        return ProactiveMessage(text="  ")

    gen = MessageGenerator(memory=memory, proactive_fn=_fn)
    outcome = _run(gen, _decision())
    assert outcome.message is None
    assert outcome.skip_reason == SkipReason.LLM_OUTPUT_INVALID


def test_generate_non_message_return_becomes_parse_error():
    memory = InMemoryMemoryApi()

    async def _fn(snapshot):
        return "not a message object"  # type: ignore[return-value]

    gen = MessageGenerator(memory=memory, proactive_fn=_fn)
    outcome = _run(gen, _decision())
    assert outcome.message is None
    assert outcome.skip_reason == SkipReason.LLM_PARSE_ERROR


def test_generator_does_not_pass_channel_id_to_memory():
    """D4 cross-check: the generator's memory calls never include
    ``channel_id=`` as a kwarg. The MemoryApi Protocol doesn't even
    accept it, but we double-check at runtime by wrapping the fake."""
    memory = InMemoryMemoryApi()
    calls: list[dict] = []

    orig_list = memory.list_recall_messages

    def _spy(*args, **kwargs):
        calls.append({"fn": "list_recall_messages", "kwargs": kwargs})
        return orig_list(*args, **kwargs)

    memory.list_recall_messages = _spy  # type: ignore[assignment]

    orig_events = memory.get_recent_events

    def _spy_events(*args, **kwargs):
        calls.append({"fn": "get_recent_events", "kwargs": kwargs})
        return orig_events(*args, **kwargs)

    memory.get_recent_events = _spy_events  # type: ignore[assignment]

    fn = make_fake_proactive_fn()
    gen = MessageGenerator(memory=memory, proactive_fn=fn)
    _run(gen, _decision())

    assert calls, "expected at least one memory read call"
    for call in calls:
        assert "channel_id" not in call["kwargs"], (
            f"D4 violation: {call['fn']} called with channel_id kwarg"
        )
