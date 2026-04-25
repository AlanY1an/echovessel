"""Tests for runtime.memory_facade — MemoryFacade + ProactiveChannelRegistry.

Includes the D4 guard test: no read method passes `channel_id=` to the
underlying memory API.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.channels.base import OutgoingMessage
from echovessel.core.types import MessageRole, NodeType
from echovessel.memory import (
    ConceptNode,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import Session as SessionRow
from echovessel.proactive.base import ChannelProtocol
from echovessel.runtime.channel_registry import ChannelRegistry
from echovessel.runtime.wiring.memory import (
    MemoryFacade,
    ProactiveChannelRegistry,
    _ProactiveChannelAdapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_with_seed():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="p1", display_name="Test"))
        db.add(User(id="self", display_name="Self"))
        db.commit()
    return engine


def _db_factory(engine):
    def _make():
        return DbSession(engine)

    return _make


# ---------------------------------------------------------------------------
# D4 guard — grep the source for channel_id= kwargs
# ---------------------------------------------------------------------------


def test_no_channel_id_kwarg_in_reads():
    """🚨 D4 铁律 🚨

    MemoryFacade's read methods (load_core_blocks, list_recall_messages,
    get_recent_events, get_session_status) MUST NOT pass `channel_id=`
    to any call they make. This test walks the facade's AST and asserts
    the kwarg never appears in a Call node inside any read method.

    Adding a `channel_id=` kwarg anywhere in a facade read is a silent
    D4 violation that collapses the unified memory guarantee. Do not
    skip or loosen this test.
    """
    import echovessel.runtime.wiring.memory as mod

    with open(mod.__file__) as fp:
        tree = ast.parse(fp.read())

    read_method_names = {
        "load_core_blocks",
        "list_recall_messages",
        "get_recent_events",
        "get_session_status",
    }

    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in read_method_names:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                for kw in sub.keywords or []:
                    if kw.arg == "channel_id":
                        violations.append(f"{node.name}: channel_id= kwarg at line {sub.lineno}")

    assert not violations, "D4 violation in MemoryFacade reads:\n  " + "\n  ".join(violations)


# ---------------------------------------------------------------------------
# load_core_blocks
# ---------------------------------------------------------------------------


def test_load_core_blocks_returns_list():
    engine = _make_engine_with_seed()
    facade = MemoryFacade(_db_factory(engine))

    blocks = facade.load_core_blocks("p1", "self")
    assert isinstance(blocks, list)
    # No core blocks seeded — empty list is fine
    assert blocks == []


# ---------------------------------------------------------------------------
# list_recall_messages
# ---------------------------------------------------------------------------


def test_list_recall_messages_empty_when_no_data():
    engine = _make_engine_with_seed()
    facade = MemoryFacade(_db_factory(engine))

    msgs = facade.list_recall_messages("p1", "self", limit=10)
    assert msgs == []


def test_list_recall_messages_returns_persisted_rows():
    engine = _make_engine_with_seed()

    with DbSession(engine) as db:
        ingest_message(
            db=db,
            persona_id="p1",
            user_id="self",
            channel_id="web",
            role=MessageRole.USER,
            content="hi from web",
        )
        ingest_message(
            db=db,
            persona_id="p1",
            user_id="self",
            channel_id="discord",
            role=MessageRole.PERSONA,
            content="hi from discord",
        )

    facade = MemoryFacade(_db_factory(engine))
    msgs = facade.list_recall_messages("p1", "self", limit=10)
    assert len(msgs) == 2
    contents = {m.content for m in msgs}
    assert contents == {"hi from web", "hi from discord"}


def test_list_recall_messages_unified_across_channels():
    """Verify the cross-channel unified timeline behavior end-to-end.

    Complements the D4 AST guard: even at runtime, the facade returns
    messages from BOTH channels in one list, never filtering by
    channel_id.
    """
    engine = _make_engine_with_seed()

    with DbSession(engine) as db:
        for ch in ("web", "discord", "imessage"):
            ingest_message(
                db=db,
                persona_id="p1",
                user_id="self",
                channel_id=ch,
                role=MessageRole.USER,
                content=f"from {ch}",
            )

    facade = MemoryFacade(_db_factory(engine))
    msgs = facade.list_recall_messages("p1", "self", limit=20)

    channels_seen = {m.channel_id for m in msgs}
    assert channels_seen == {"web", "discord", "imessage"}


def test_list_recall_messages_before_cursor():
    engine = _make_engine_with_seed()

    with DbSession(engine) as db:
        ingest_message(
            db=db,
            persona_id="p1",
            user_id="self",
            channel_id="web",
            role=MessageRole.USER,
            content="old",
            now=datetime(2026, 1, 1, 10, 0, 0),
        )
        ingest_message(
            db=db,
            persona_id="p1",
            user_id="self",
            channel_id="web",
            role=MessageRole.USER,
            content="new",
            now=datetime(2026, 4, 15, 10, 0, 0),
        )

    facade = MemoryFacade(_db_factory(engine))
    msgs = facade.list_recall_messages("p1", "self", limit=10, before=datetime(2026, 2, 1))
    assert len(msgs) == 1
    assert msgs[0].content == "old"


# ---------------------------------------------------------------------------
# get_recent_events
# ---------------------------------------------------------------------------


def test_get_recent_events_empty_when_no_events():
    engine = _make_engine_with_seed()
    facade = MemoryFacade(_db_factory(engine))

    events = facade.get_recent_events("p1", "self", since=datetime(2026, 1, 1), limit=10)
    assert events == []


def test_get_recent_events_filters_by_since():
    engine = _make_engine_with_seed()
    with DbSession(engine) as db:
        old = ConceptNode(
            persona_id="p1",
            user_id="self",
            type=NodeType.EVENT,
            description="old event",
            emotional_impact=3,
            created_at=datetime(2026, 1, 1, 10, 0, 0),
        )
        recent = ConceptNode(
            persona_id="p1",
            user_id="self",
            type=NodeType.EVENT,
            description="recent event",
            emotional_impact=5,
            created_at=datetime(2026, 4, 15, 10, 0, 0),
        )
        db.add(old)
        db.add(recent)
        db.commit()

    facade = MemoryFacade(_db_factory(engine))
    events = facade.get_recent_events("p1", "self", since=datetime(2026, 2, 1), limit=10)
    assert len(events) == 1
    assert events[0].description == "recent event"


def test_get_recent_events_only_events_not_thoughts():
    """type='thought' rows should not appear in the event-only feed."""
    engine = _make_engine_with_seed()
    with DbSession(engine) as db:
        db.add(
            ConceptNode(
                persona_id="p1",
                user_id="self",
                type=NodeType.EVENT,
                description="event",
                emotional_impact=1,
                created_at=datetime(2026, 4, 15, 12, 0, 0),
            )
        )
        db.add(
            ConceptNode(
                persona_id="p1",
                user_id="self",
                type=NodeType.THOUGHT,
                description="thought",
                emotional_impact=1,
                created_at=datetime(2026, 4, 15, 12, 0, 0),
            )
        )
        db.commit()

    facade = MemoryFacade(_db_factory(engine))
    events = facade.get_recent_events("p1", "self", since=datetime(2026, 1, 1), limit=10)
    assert len(events) == 1
    descriptions = [e.description for e in events]
    assert "event" in descriptions
    assert "thought" not in descriptions


def test_get_recent_events_respects_limit_cap():
    engine = _make_engine_with_seed()
    with DbSession(engine) as db:
        for i in range(10):
            db.add(
                ConceptNode(
                    persona_id="p1",
                    user_id="self",
                    type=NodeType.EVENT,
                    description=f"event {i}",
                    emotional_impact=1,
                    created_at=datetime(2026, 4, 15, 12, 0, 0) + timedelta(minutes=i),
                )
            )
        db.commit()

    facade = MemoryFacade(_db_factory(engine))
    events = facade.get_recent_events("p1", "self", since=datetime(2026, 1, 1), limit=3)
    assert len(events) == 3


# ---------------------------------------------------------------------------
# get_session_status
# ---------------------------------------------------------------------------


def test_get_session_status_none_for_missing():
    engine = _make_engine_with_seed()
    facade = MemoryFacade(_db_factory(engine))
    assert facade.get_session_status("nonexistent") is None


def test_get_session_status_returns_row():
    engine = _make_engine_with_seed()
    with DbSession(engine) as db:
        db.add(
            SessionRow(
                id="s_test",
                persona_id="p1",
                user_id="self",
                channel_id="web",
            )
        )
        db.commit()

    facade = MemoryFacade(_db_factory(engine))
    sess = facade.get_session_status("s_test")
    assert sess is not None
    assert sess.id == "s_test"


# ---------------------------------------------------------------------------
# ingest_message
# ---------------------------------------------------------------------------


def test_ingest_message_writes_to_l2():
    engine = _make_engine_with_seed()
    facade = MemoryFacade(_db_factory(engine))

    result = facade.ingest_message(
        persona_id="p1",
        user_id="self",
        channel_id="web",
        role=MessageRole.PERSONA,
        content="proactive nudge",
    )
    # The persisted result is an IngestResult (from memory.ingest)
    assert result.message.content == "proactive nudge"
    assert result.message.channel_id == "web"
    assert result.message.role == MessageRole.PERSONA

    # And list_recall_messages sees it
    msgs = facade.list_recall_messages("p1", "self", limit=10)
    assert len(msgs) == 1


def test_ingest_message_accepts_string_role():
    engine = _make_engine_with_seed()
    facade = MemoryFacade(_db_factory(engine))

    result = facade.ingest_message(
        persona_id="p1",
        user_id="self",
        channel_id="web",
        role="persona",  # string, not enum
        content="from string role",
    )
    assert result.message.role == MessageRole.PERSONA


# ---------------------------------------------------------------------------
# ProactiveChannelRegistry + _ProactiveChannelAdapter
# ---------------------------------------------------------------------------


class _FakeRuntimeChannel:
    """Minimal runtime Channel Protocol v0.2 stub for adapter tests."""

    name = "fake"

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        self.in_flight_turn_id: str | None = None
        self.sent: list[OutgoingMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def incoming(self):
        async def _noop():
            if False:
                yield None

        return _noop()

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append(msg)

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None


def test_adapter_satisfies_channel_protocol():
    raw = _FakeRuntimeChannel("web")
    adapter = _ProactiveChannelAdapter(raw)
    assert isinstance(adapter, ChannelProtocol)


def test_adapter_exposes_name():
    raw = _FakeRuntimeChannel("discord")
    adapter = _ProactiveChannelAdapter(raw)
    assert adapter.name == "discord"
    assert adapter.channel_id == "discord"


def test_adapter_default_capability_flags():
    raw = _FakeRuntimeChannel("web")
    adapter = _ProactiveChannelAdapter(raw)
    # supports_outgoing_push defaults True (MVP web channel)
    assert adapter.supports_outgoing_push is True
    # supports_audio defaults False (none of our MVP channels do this)
    assert adapter.supports_audio is False


def test_adapter_capability_flags_pass_through():
    raw = _FakeRuntimeChannel("discord")
    raw.supports_audio = True  # type: ignore[attr-defined]
    raw.supports_outgoing_push = False  # type: ignore[attr-defined]
    adapter = _ProactiveChannelAdapter(raw)
    assert adapter.supports_audio is True
    assert adapter.supports_outgoing_push is False


async def test_adapter_send_translates_signature():
    """Proactive's `send(text)` → runtime channel's `send(OutgoingMessage)`.

    v0.2: the adapter constructs an ``OutgoingMessage`` with
    ``kind="proactive"`` / ``delivery="text"`` and both ``in_reply_to``
    and ``in_reply_to_turn_id`` as ``None``.
    """

    raw = _FakeRuntimeChannel("web")
    adapter = _ProactiveChannelAdapter(raw)
    await adapter.send("hello from proactive")
    assert len(raw.sent) == 1
    sent = raw.sent[0]
    assert sent.content == "hello from proactive"
    assert sent.kind == "proactive"
    assert sent.delivery == "text"
    assert sent.in_reply_to is None
    assert sent.in_reply_to_turn_id is None


def test_registry_wraps_runtime_registry():
    raw1 = _FakeRuntimeChannel("web")
    raw2 = _FakeRuntimeChannel("discord")
    runtime_reg = ChannelRegistry()
    runtime_reg.register(raw1)
    runtime_reg.register(raw2)

    proactive_reg = ProactiveChannelRegistry(runtime_reg)
    # ChannelRegistryApi is NOT @runtime_checkable (spec §11.3 kept the
    # check structural), so we verify by calling list_enabled directly.
    assert callable(proactive_reg.list_enabled)

    enabled = proactive_reg.list_enabled()
    assert len(enabled) == 2
    names = {ch.name for ch in enabled}
    assert names == {"web", "discord"}


def test_registry_returns_fresh_adapters_each_call():
    raw = _FakeRuntimeChannel("web")
    runtime_reg = ChannelRegistry()
    runtime_reg.register(raw)

    proactive_reg = ProactiveChannelRegistry(runtime_reg)
    first = proactive_reg.list_enabled()
    second = proactive_reg.list_enabled()
    # Different adapter instances (fresh objects) but wrapping the same raw
    assert first[0] is not second[0]
    assert first[0]._raw is second[0]._raw is raw


def test_registry_reflects_late_registrations():
    """Register a channel after the facade exists — it should still see it."""
    runtime_reg = ChannelRegistry()
    proactive_reg = ProactiveChannelRegistry(runtime_reg)

    assert proactive_reg.list_enabled() == []

    runtime_reg.register(_FakeRuntimeChannel("web"))
    enabled = proactive_reg.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].name == "web"
