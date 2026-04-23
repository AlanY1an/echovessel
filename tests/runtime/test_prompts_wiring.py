"""prompts_wiring: extract/reflect/judge closures built on StubProvider."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from echovessel.core.types import MessageRole, NodeType
from echovessel.memory.consolidate import ExtractedEvent, ExtractedThought
from echovessel.memory.models import ConceptNode, RecallMessage
from echovessel.proactive.base import MemorySnapshot, ProactiveMessage
from echovessel.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    REFLECTION_SYSTEM_PROMPT,
)
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.prompts_wiring import (
    PROACTIVE_SYSTEM_PROMPT,
    make_extract_fn,
    make_judge_fn,
    make_proactive_fn,
    make_reflect_fn,
)

_EXTRACTION_RESPONSE = json.dumps(
    {
        "events": [
            {
                "description": "用户今天状态很好",
                "emotional_impact": 4,
                "emotion_tags": ["joy"],
                "relational_tags": ["identity-bearing"],
            }
        ],
        "self_check_notes": "single positive event",
    }
)


_REFLECTION_RESPONSE_TEMPLATE = {
    "thoughts": [
        {
            "description": "Alan 正在重建对自己的信任",
            "emotional_impact": 3,
            "emotion_tags": ["hope"],
            "relational_tags": ["turning-point"],
            "filling": [],  # filled at runtime below
        }
    ]
}


_JUDGE_RESPONSE = json.dumps(
    {
        "verdict": "pass",
        "overall_score": 4.2,
        "heart_scores": {
            "human_alignment": 4,
            "empathic_responsiveness": 5,
            "attunement": 4,
            "resonance": 4,
            "task_following": 4,
        },
        "anti_patterns_hit": [],
        "anti_pattern_cap_applied": False,
        "reasoning": "Response is grounded and warm without over-performing empathy.",
    }
)


def _make_messages() -> list[RecallMessage]:
    now = datetime(2026, 4, 14, 10, 30, 0)
    return [
        RecallMessage(
            id=1,
            session_id="s_test",
            persona_id="p",
            user_id="self",
            channel_id="web",
            role=MessageRole.USER,
            content="今天好开心",
            created_at=now,
        ),
        RecallMessage(
            id=2,
            session_id="s_test",
            persona_id="p",
            user_id="self",
            channel_id="web",
            role=MessageRole.PERSONA,
            content="很高兴听到你这么说",
            created_at=now,
        ),
    ]


def _make_nodes() -> list[ConceptNode]:
    return [
        ConceptNode(
            id=10,
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="首次说出心事",
            emotional_impact=4,
            emotion_tags=["relief"],
            relational_tags=["turning-point"],
            created_at=datetime(2026, 4, 14, 9, 0, 0),
        )
    ]


async def test_make_extract_fn_happy_path():
    stub = StubProvider(fallback=_EXTRACTION_RESPONSE)
    extract = make_extract_fn(stub)
    result = await extract(_make_messages())
    events = result.events
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ExtractedEvent)
    assert ev.description == "用户今天状态很好"
    assert ev.emotional_impact == 4
    assert ev.emotion_tags == ["joy"]
    assert ev.relational_tags == ["identity-bearing"]


async def test_make_extract_fn_empty_messages_returns_empty_without_calling_llm():
    called = []

    def responder(**kwargs):
        called.append(1)
        return "{}"

    stub = StubProvider(responder=responder)
    extract = make_extract_fn(stub)
    out = await extract([])
    assert out.events == []
    assert called == []


async def test_make_extract_fn_drops_on_parse_error():
    stub = StubProvider(fallback="not json")
    extract = make_extract_fn(stub)
    out = await extract(_make_messages())
    assert out.events == []


async def test_make_reflect_fn_happy_path():
    nodes = _make_nodes()
    payload = dict(_REFLECTION_RESPONSE_TEMPLATE)
    payload["thoughts"] = [{**t, "filling": [n.id for n in nodes]} for t in payload["thoughts"]]
    stub = StubProvider(fallback=json.dumps(payload))
    reflect = make_reflect_fn(stub)
    thoughts = await reflect(nodes, "shock")
    assert len(thoughts) == 1
    th = thoughts[0]
    assert isinstance(th, ExtractedThought)
    assert th.description.startswith("Alan")
    assert th.filling == [10]


async def test_make_reflect_fn_returns_empty_on_no_nodes():
    stub = StubProvider(fallback="{}")
    reflect = make_reflect_fn(stub)
    out = await reflect([], "timer")
    assert out == []


async def test_make_judge_fn_happy_path():
    stub = StubProvider(fallback=_JUDGE_RESPONSE)
    judge = make_judge_fn(stub)
    verdict = await judge(
        user_message="hi",
        persona_response="hey",
    )
    assert verdict.verdict == "pass"
    assert verdict.overall_score == 4.2


async def test_extract_fn_passes_fast_role():
    seen_roles: list[str] = []

    def responder(*, model_role, **kwargs):
        seen_roles.append(str(model_role))
        return _EXTRACTION_RESPONSE

    stub = StubProvider(responder=responder)
    extract = make_extract_fn(stub)
    await extract(_make_messages())
    assert seen_roles == ["fast"]


async def test_reflect_fn_passes_fast_role():
    nodes = _make_nodes()
    payload = dict(_REFLECTION_RESPONSE_TEMPLATE)
    payload["thoughts"] = [{**t, "filling": [n.id for n in nodes]} for t in payload["thoughts"]]
    seen_roles: list[str] = []

    def responder(*, model_role, **kwargs):
        seen_roles.append(str(model_role))
        return json.dumps(payload)

    stub = StubProvider(responder=responder)
    reflect = make_reflect_fn(stub)
    await reflect(nodes, "timer")
    assert seen_roles == ["fast"]


async def test_judge_fn_passes_judge_role():
    seen_roles: list[str] = []

    def responder(*, model_role, **kwargs):
        seen_roles.append(str(model_role))
        return _JUDGE_RESPONSE

    stub = StubProvider(responder=responder)
    judge = make_judge_fn(stub)
    await judge(user_message="a", persona_response="b")
    assert seen_roles == ["judge"]


async def test_extract_fn_sends_extraction_system_prompt():
    seen: list[str] = []

    def responder(*, system, **kwargs):
        seen.append(system)
        return _EXTRACTION_RESPONSE

    stub = StubProvider(responder=responder)
    extract = make_extract_fn(stub)
    await extract(_make_messages())
    assert seen == [EXTRACTION_SYSTEM_PROMPT]


async def test_reflect_fn_sends_reflection_system_prompt():
    nodes = _make_nodes()
    payload = dict(_REFLECTION_RESPONSE_TEMPLATE)
    payload["thoughts"] = [{**t, "filling": [n.id for n in nodes]} for t in payload["thoughts"]]
    seen: list[str] = []

    def responder(*, system, **kwargs):
        seen.append(system)
        return json.dumps(payload)

    stub = StubProvider(responder=responder)
    reflect = make_reflect_fn(stub)
    await reflect(nodes, "timer")
    assert seen == [REFLECTION_SYSTEM_PROMPT]


async def test_judge_fn_sends_judge_system_prompt():
    seen: list[str] = []

    def responder(*, system, **kwargs):
        seen.append(system)
        return _JUDGE_RESPONSE

    stub = StubProvider(responder=responder)
    judge = make_judge_fn(stub)
    await judge(user_message="a", persona_response="b")
    assert seen == [JUDGE_SYSTEM_PROMPT]


# ---------------------------------------------------------------------------
# Round 2 · make_proactive_fn
# ---------------------------------------------------------------------------


_PROACTIVE_VALID_RESPONSE = json.dumps(
    {
        "text": "昨晚你说的那件事，我一直在想。你今天怎么样？",
        "rationale": "triggered by long silence + prior vulnerability disclosure",
    }
)

_PROACTIVE_FENCED_RESPONSE = (
    "```json\n"
    + json.dumps({"text": "想起你提过的那只小猫，它还好吗？", "rationale": "check-in"})
    + "\n```"
)


class _FakeCoreBlock:
    def __init__(self, label: str, content: str) -> None:
        self.label = label
        self.content = content


class _FakeEvent:
    def __init__(
        self,
        description: str,
        *,
        emotional_impact: int = 0,
        emotion_tags: list | None = None,
        relational_tags: list | None = None,
    ) -> None:
        self.description = description
        self.emotional_impact = emotional_impact
        self.emotion_tags = emotion_tags or []
        self.relational_tags = relational_tags or []


class _FakeRecall:
    def __init__(self, role: str, content: str, channel_id: str = "web") -> None:
        self.role = role
        self.content = content
        # channel_id attribute is present on real RecallMessages; the
        # prompt builder MUST NOT read or emit it. Having it here lets
        # our F10 guard test verify the serializer drops it.
        self.channel_id = channel_id


def _make_clean_snapshot(trigger: str = "long_silence") -> MemorySnapshot:
    return MemorySnapshot(
        trigger=trigger,
        trigger_payload={"hours_silent": 36},
        core_blocks=(
            _FakeCoreBlock("persona", "温暖、耐心、会记得小事情"),
            _FakeCoreBlock("user", "用户叫 Alan，喜欢养猫"),
        ),
        recent_l3_events=(
            _FakeEvent(
                "用户昨晚凌晨说'最近真的很累'",
                emotional_impact=-5,
                emotion_tags=["fatigue", "vulnerability"],
                relational_tags=["vulnerability"],
            ),
        ),
        recent_l2_window=(
            _FakeRecall("user", "最近真的很累"),
            _FakeRecall("persona", "嗯。我在。"),
        ),
        relationship_state=None,
        snapshot_hash="abc123",
    )


# --- happy path ---------------------------------------------------


async def test_make_proactive_fn_happy_path():
    stub = StubProvider(fallback=_PROACTIVE_VALID_RESPONSE)
    proactive = make_proactive_fn(stub)

    snapshot = _make_clean_snapshot()
    message = await proactive(snapshot)

    assert isinstance(message, ProactiveMessage)
    assert message.text.startswith("昨晚你说的那件事")
    assert message.rationale is not None
    assert "long silence" in message.rationale


async def test_make_proactive_fn_accepts_fenced_json():
    stub = StubProvider(fallback=_PROACTIVE_FENCED_RESPONSE)
    proactive = make_proactive_fn(stub)

    message = await proactive(_make_clean_snapshot())
    assert "小猫" in message.text


async def test_make_proactive_fn_uses_main_role():
    seen_roles: list = []

    def responder(*, system, user, model_role, **kwargs):
        seen_roles.append(model_role)
        return _PROACTIVE_VALID_RESPONSE

    stub = StubProvider(responder=responder)
    proactive = make_proactive_fn(stub)
    await proactive(_make_clean_snapshot())

    assert seen_roles == ["main"], f"proactive must use model_role='main' (got {seen_roles})"


async def test_make_proactive_fn_sends_proactive_system_prompt():
    seen_systems: list[str] = []

    def responder(*, system, **kwargs):
        seen_systems.append(system)
        return _PROACTIVE_VALID_RESPONSE

    stub = StubProvider(responder=responder)
    proactive = make_proactive_fn(stub)
    await proactive(_make_clean_snapshot())

    assert seen_systems == [PROACTIVE_SYSTEM_PROMPT]


# --- parse failures -----------------------------------------------


async def test_make_proactive_fn_raises_on_invalid_json():
    stub = StubProvider(fallback="not valid json at all")
    proactive = make_proactive_fn(stub)

    with pytest.raises(ValueError, match="not valid JSON"):
        await proactive(_make_clean_snapshot())


async def test_make_proactive_fn_raises_on_top_level_array():
    stub = StubProvider(fallback=json.dumps([{"text": "nope"}]))
    proactive = make_proactive_fn(stub)

    with pytest.raises(ValueError, match="must be a JSON object"):
        await proactive(_make_clean_snapshot())


async def test_make_proactive_fn_raises_on_missing_text():
    stub = StubProvider(fallback=json.dumps({"rationale": "no text field"}))
    proactive = make_proactive_fn(stub)

    with pytest.raises(ValueError, match="text"):
        await proactive(_make_clean_snapshot())


async def test_make_proactive_fn_rationale_can_be_null():
    stub = StubProvider(fallback=json.dumps({"text": "hi there", "rationale": None}))
    proactive = make_proactive_fn(stub)
    msg = await proactive(_make_clean_snapshot())
    assert msg.text == "hi there"
    assert msg.rationale is None


# --- F10 铁律 guard tests -----------------------------------------


def test_proactive_system_prompt_has_no_channel_id():
    """🚨 F10 铁律 🚨

    The system prompt text MUST NOT mention any channel identifier or
    channel-naming token. We check the forbidden-substring set used by
    proactive's own F10 guard (echovessel.proactive.generator).
    """
    forbidden = ("channel_id", "discord:", "imessage:", "wechat:")
    for token in forbidden:
        assert token not in PROACTIVE_SYSTEM_PROMPT, (
            f"F10 violation: PROACTIVE_SYSTEM_PROMPT contains {token!r}"
        )


async def test_make_proactive_fn_user_prompt_has_no_channel_id():
    """🚨 F10 铁律 🚨

    Even when the underlying RecallMessage has a `channel_id` attribute,
    the serialized user prompt sent to the LLM MUST NOT contain the
    channel id string. This test captures the user prompt via a stub
    responder and greps it.
    """
    captured: list[str] = []

    def responder(*, user, **kwargs):
        captured.append(user)
        return _PROACTIVE_VALID_RESPONSE

    stub = StubProvider(responder=responder)
    proactive = make_proactive_fn(stub)

    # Inject a snapshot with multiple channel_id strings that a leaking
    # serializer would emit.
    polluted_snapshot = MemorySnapshot(
        trigger="long_silence",
        trigger_payload={"hours_silent": 24},
        core_blocks=(_FakeCoreBlock("user", "Alan"),),
        recent_l3_events=(),
        recent_l2_window=(
            _FakeRecall("user", "hi", channel_id="discord:g42"),
            _FakeRecall("persona", "hey", channel_id="discord:g42"),
            _FakeRecall("user", "still here?", channel_id="web"),
        ),
        relationship_state=None,
        snapshot_hash="test-hash",
    )
    await proactive(polluted_snapshot)

    assert captured, "proactive_fn did not call the LLM"
    user_prompt = captured[0]

    forbidden_tokens = ("channel_id", "discord:g42", "discord:", "imessage:")
    for token in forbidden_tokens:
        assert token not in user_prompt, (
            f"F10 violation: user prompt contains {token!r}:\n{user_prompt[:500]}"
        )


async def test_make_proactive_fn_prompt_does_not_use_bare_channel_names():
    """Sanity: even in completely clean snapshots with no channel_id on
    the rows, the serializer never writes 'web' / 'discord' / 'imessage'
    as standalone tokens."""
    captured: list[str] = []

    def responder(*, user, **kwargs):
        captured.append(user)
        return _PROACTIVE_VALID_RESPONSE

    stub = StubProvider(responder=responder)
    proactive = make_proactive_fn(stub)
    await proactive(_make_clean_snapshot())

    user_prompt = captured[0]
    # Look for whole-word channel names — allow matches inside natural
    # Chinese or unrelated English but catch bare `"discord"` etc.
    import re

    for name in ("discord", "imessage", "wechat"):
        pattern = rf'[\'"]{name}[\'"]'
        assert not re.search(pattern, user_prompt, re.IGNORECASE), (
            f"F10 violation: user prompt has quoted channel name {name!r}"
        )


# --- Snapshot serializer unit tests --------------------------------


def test_format_proactive_user_prompt_includes_trigger():
    from echovessel.runtime.prompts_wiring import _format_proactive_user_prompt

    snapshot = _make_clean_snapshot(trigger="warmth_burst")
    prompt = _format_proactive_user_prompt(snapshot)

    assert "warmth_burst" in prompt
    assert "persona_identity" in prompt
    assert "recent_events" in prompt
    assert "recent_conversation" in prompt
    assert "Produce the JSON output now." in prompt


def test_format_proactive_user_prompt_truncates_oversize_content():
    from echovessel.runtime.prompts_wiring import _format_proactive_user_prompt

    long_content = "x" * 2000
    snapshot = MemorySnapshot(
        trigger="test",
        trigger_payload={},
        core_blocks=(_FakeCoreBlock("persona", long_content),),
        recent_l3_events=(),
        recent_l2_window=(),
        relationship_state=None,
        snapshot_hash="h",
    )
    prompt = _format_proactive_user_prompt(snapshot)
    # The big content should have been truncated with an ellipsis
    assert "…" in prompt
    # The entire 2000-char blob should NOT be in the prompt
    assert long_content not in prompt
