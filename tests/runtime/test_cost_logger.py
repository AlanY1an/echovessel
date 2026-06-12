"""Worker ζ · cost_logger module unit tests.

Coverage:

- :class:`CostRecorder` writes a row and returns a populated
  :class:`LLMCallRecord`
- :class:`CostTrackingProvider` records on every ``complete`` call
  and on every ``stream`` consumption
- :func:`feature_context` propagates the feature label into the
  recorded row
- :func:`summarize` aggregates by feature and by day across the
  ``today`` / ``7d`` / ``30d`` ranges
- :func:`list_recent` returns newest-first up to its cap
- Stub-provider records are zero-cost (free tier short-circuit)
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session as DbSession

from echovessel.memory import create_all_tables, create_engine
from echovessel.runtime.cost_logger import (
    CostRecorder,
    CostTrackingProvider,
    feature_context,
    list_recent,
    summarize,
)
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.llm.usage import Usage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_engine_and_recorder():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    def _factory() -> DbSession:
        return DbSession(engine)

    return engine, CostRecorder(_factory)


class _FakeProvider:
    """Tiny LLMProvider stub: records every call's args and returns
    a deterministic body so tests can assert on token counts."""

    def __init__(self, *, name: str = "openai_compat", model: str = "gpt-4o") -> None:
        self.provider_name = name
        self._model = model
        self.complete_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def model_for(self, model_role: str) -> str:
        return self._model

    async def complete(
        self,
        system: str,
        user: str,
        *,
        model_role: str = "main",
        max_tokens: int = 1024,
        temperature: float = 0.7,
        thinking_enabled: bool | None = None,
        timeout: float | None = None,
    ) -> tuple[str, Usage | None]:
        self.complete_calls.append(
            {
                "system": system,
                "user": user,
                "model_role": model_role,
                "thinking_enabled": thinking_enabled,
            }
        )
        return "ok response", None

    async def stream(
        self,
        system: str,
        user: str,
        *,
        model_role: str = "main",
        max_tokens: int = 1024,
        temperature: float = 0.7,
        thinking_enabled: bool | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str | Usage]:
        self.stream_calls.append(
            {
                "system": system,
                "user": user,
                "model_role": model_role,
                "thinking_enabled": thinking_enabled,
            }
        )
        for piece in ("hel", "lo ", "wor", "ld"):
            yield piece


# ---------------------------------------------------------------------------
# CostRecorder direct
# ---------------------------------------------------------------------------


def test_recorder_persists_row_and_returns_record() -> None:
    _engine, recorder = _build_engine_and_recorder()
    record = recorder.record(
        provider="openai_compat",
        model="gpt-4o",
        feature="chat",
        model_role="main",
        input_text="hello system\nhello user",
        output_text="here is a reply",
        turn_id="turn-abc",
    )
    assert record is not None
    assert record.provider == "openai_compat"
    assert record.model == "gpt-4o"
    assert record.feature == "chat"
    assert record.tier == "main"
    assert record.tokens_in > 0
    assert record.tokens_out > 0
    assert record.cost_usd > 0  # non-stub provider has a non-zero rate
    assert record.turn_id == "turn-abc"
    assert record.timestamp  # ISO 8601 string


def test_recorder_stub_provider_yields_zero_cost() -> None:
    _engine, recorder = _build_engine_and_recorder()
    record = recorder.record(
        provider="stub",
        model="stub-model",
        feature="consolidate",
        model_role="fast",
        input_text="x" * 500,
        output_text="y" * 500,
    )
    assert record is not None
    assert record.cost_usd == 0.0
    # Tokens are still counted so the admin tab can show usage even
    # for free providers.
    assert record.tokens_in > 0
    assert record.tokens_out > 0


# ---------------------------------------------------------------------------
# CostTrackingProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracking_provider_records_complete_call() -> None:
    engine, recorder = _build_engine_and_recorder()
    inner = _FakeProvider()
    wrapped = CostTrackingProvider(inner, recorder)

    with feature_context("chat", turn_id="turn-1"):
        result, usage = await wrapped.complete("sys", "usr", model_role="main")
    assert result == "ok response"
    assert usage is None
    assert inner.complete_calls  # delegated

    with DbSession(engine) as db:
        rows = list_recent(db, limit=10)
    assert len(rows) == 1
    assert rows[0].feature == "chat"
    assert rows[0].turn_id == "turn-1"
    assert rows[0].tier == "main"


@pytest.mark.asyncio
async def test_tracking_provider_records_stream_after_full_consume() -> None:
    engine, recorder = _build_engine_and_recorder()
    inner = _FakeProvider()
    wrapped = CostTrackingProvider(inner, recorder)

    with feature_context("import"):
        chunks = []
        async for piece in wrapped.stream("sys", "usr", model_role="fast"):
            chunks.append(piece)
    assert "".join(chunks) == "hello world"

    with DbSession(engine) as db:
        rows = list_recent(db, limit=10)
    assert len(rows) == 1
    assert rows[0].feature == "import"
    assert rows[0].tier == "fast"
    # tokens_out reflects the joined stream output, not just one chunk.
    assert rows[0].tokens_out >= 1


@pytest.mark.asyncio
async def test_feature_context_default_when_unset() -> None:
    engine, recorder = _build_engine_and_recorder()
    wrapped = CostTrackingProvider(_FakeProvider(), recorder)

    # No feature_context wrapper — call still records, label is "unknown".
    await wrapped.complete("a", "b")
    with DbSession(engine) as db:
        rows = list_recent(db, limit=5)
    assert len(rows) == 1
    assert rows[0].feature == "unknown"


@pytest.mark.asyncio
async def test_tracking_provider_forwards_thinking_enabled_on_complete() -> None:
    """thinking_enabled must reach the inner provider unchanged — wiring
    (consolidate / reflect / slow-cycle / proactive) passes it on every call."""
    _engine, recorder = _build_engine_and_recorder()
    inner = _FakeProvider()
    wrapped = CostTrackingProvider(inner, recorder)

    await wrapped.complete("sys", "usr", model_role="fast", thinking_enabled=True)
    assert inner.complete_calls[0]["thinking_enabled"] is True

    await wrapped.complete("sys", "usr", model_role="fast", thinking_enabled=False)
    assert inner.complete_calls[1]["thinking_enabled"] is False

    await wrapped.complete("sys", "usr", model_role="fast")
    assert inner.complete_calls[2]["thinking_enabled"] is None


@pytest.mark.asyncio
async def test_tracking_provider_forwards_thinking_enabled_on_stream() -> None:
    _engine, recorder = _build_engine_and_recorder()
    inner = _FakeProvider()
    wrapped = CostTrackingProvider(inner, recorder)

    async for _ in wrapped.stream("sys", "usr", model_role="main", thinking_enabled=True):
        pass
    assert inner.stream_calls[0]["thinking_enabled"] is True

    async for _ in wrapped.stream("sys", "usr", model_role="main"):
        pass
    assert inner.stream_calls[1]["thinking_enabled"] is None


@pytest.mark.parametrize("method_name", ["complete", "stream"])
def test_tracking_provider_signature_matches_protocol(method_name: str) -> None:
    """Guard against signature drift: runtime_checkable Protocols only check
    method names, so a kwarg added to LLMProvider but missing here would
    surface as a TypeError in production instead of a test failure."""
    protocol_params = list(inspect.signature(getattr(LLMProvider, method_name)).parameters)
    wrapper_params = list(inspect.signature(getattr(CostTrackingProvider, method_name)).parameters)
    assert wrapper_params == protocol_params


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def _seed_call(
    recorder: CostRecorder,
    *,
    feature: str,
    when: datetime,
    cost_provider: str = "openai_compat",
) -> None:
    recorder.record(
        provider=cost_provider,
        model="gpt-4o",
        feature=feature,
        model_role="judge",
        input_text="hello world " * 5,
        output_text="reply " * 8,
        timestamp=when,
    )


def test_summarize_groups_by_feature_within_30d_window() -> None:
    engine, recorder = _build_engine_and_recorder()
    today = datetime.now()
    _seed_call(recorder, feature="chat", when=today)
    _seed_call(recorder, feature="chat", when=today - timedelta(days=2))
    _seed_call(recorder, feature="import", when=today - timedelta(days=5))
    _seed_call(recorder, feature="proactive", when=today - timedelta(days=15))

    with DbSession(engine) as db:
        summary = summarize(db, range_label="30d", now=today)
    assert summary["range"] == "30d"
    assert summary["total_tokens"] > 0
    assert summary["total_usd"] > 0
    assert set(summary["by_feature"].keys()) == {"chat", "import", "proactive"}
    assert summary["by_feature"]["chat"]["calls"] == 2
    assert summary["by_feature"]["import"]["calls"] == 1
    assert summary["by_feature"]["proactive"]["calls"] == 1
    # by_day is ordered ASC by date and has at least 3 distinct dates
    dates = [b["date"] for b in summary["by_day"]]
    assert dates == sorted(dates)
    assert len(dates) >= 3


def test_summarize_today_excludes_yesterday() -> None:
    engine, recorder = _build_engine_and_recorder()
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_call(recorder, feature="chat", when=today)
    _seed_call(recorder, feature="chat", when=today - timedelta(days=1))

    with DbSession(engine) as db:
        summary = summarize(db, range_label="today", now=today)
    assert summary["by_feature"]["chat"]["calls"] == 1


def test_summarize_unknown_range_raises() -> None:
    engine, _recorder = _build_engine_and_recorder()
    with DbSession(engine) as db, pytest.raises(ValueError):
        summarize(db, range_label="all-time")


def test_list_recent_orders_newest_first_and_caps_limit() -> None:
    engine, recorder = _build_engine_and_recorder()
    base = datetime(2026, 4, 16, 12, 0, 0)
    for i in range(5):
        _seed_call(
            recorder,
            feature="chat",
            when=base + timedelta(minutes=i),
        )
    with DbSession(engine) as db:
        rows = list_recent(db, limit=3)
    assert len(rows) == 3
    # ISO timestamps strictly decreasing.
    times = [r.timestamp for r in rows]
    assert times == sorted(times, reverse=True)


# ---------------------------------------------------------------------------
# Acceptance criteria — issue #1 Stage 3: usage passthrough to DB rows
# ---------------------------------------------------------------------------


def test_recorder_prefers_usage_over_count_tokens() -> None:
    """When `usage` is supplied, the row reflects SDK-reported token counts.

    The input_text "short" and output_text "hi" would produce ~1-2 tokens
    from _count_tokens, but the Usage carries 999/333 — the row must use
    those exact numbers, proving _count_tokens was bypassed.
    """
    _engine, recorder = _build_engine_and_recorder()
    usage = Usage(
        input_tokens=999,
        output_tokens=333,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=100,
    )
    record = recorder.record(
        provider="anthropic",
        model="claude-sonnet-4-6",
        feature="chat",
        model_role="judge",
        input_text="short",
        output_text="hi",
        usage=usage,
    )
    assert record is not None
    assert record.tokens_in == 999
    assert record.tokens_out == 333
    assert record.cache_read_input_tokens == 500
    assert record.cache_creation_input_tokens == 100


def test_recorder_fallback_to_count_tokens_when_usage_none() -> None:
    """When `usage` is None, tokens_in/out come from _count_tokens and
    cache columns are 0."""
    _engine, recorder = _build_engine_and_recorder()
    record = recorder.record(
        provider="openai_compat",
        model="gpt-4o",
        feature="chat",
        model_role="judge",
        input_text="hello world " * 20,
        output_text="reply text " * 10,
        usage=None,
    )
    assert record is not None
    assert record.tokens_in > 0
    assert record.tokens_out > 0
    assert record.cache_read_input_tokens == 0
    assert record.cache_creation_input_tokens == 0


@pytest.mark.asyncio
async def test_tracking_provider_wires_complete_usage_to_db_row() -> None:
    """CostTrackingProvider with a provider that returns real Usage → row
    reflects provider-reported token counts, not _count_tokens estimates."""

    class _UsageProvider(_FakeProvider):
        async def complete(self, system, user, *, model_role="judge", **_kw):
            self.complete_calls.append({"system": system, "user": user})
            return "x", Usage(
                input_tokens=1234,
                output_tokens=567,
                cache_read_input_tokens=800,
                cache_creation_input_tokens=50,
            )

    engine, recorder = _build_engine_and_recorder()
    wrapped = CostTrackingProvider(_UsageProvider(), recorder)

    with feature_context("chat"):
        await wrapped.complete("sys", "usr")

    with DbSession(engine) as db:
        rows = list_recent(db, limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r.tokens_in == 1234
    assert r.tokens_out == 567
    assert r.cache_read_input_tokens == 800
    assert r.cache_creation_input_tokens == 50


@pytest.mark.asyncio
async def test_tracking_provider_stream_without_trailing_usage_falls_back() -> None:
    """When the stream yields no trailing Usage, the row uses _count_tokens
    and cache columns are 0."""
    engine, recorder = _build_engine_and_recorder()
    # _FakeProvider.stream yields str chunks only, no trailing Usage
    wrapped = CostTrackingProvider(_FakeProvider(), recorder)

    with feature_context("import"):
        async for _ in wrapped.stream("sys", "usr"):
            pass

    with DbSession(engine) as db:
        rows = list_recent(db, limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r.tokens_in > 0
    assert r.tokens_out > 0
    assert r.cache_read_input_tokens == 0
    assert r.cache_creation_input_tokens == 0


@pytest.mark.asyncio
async def test_tracking_provider_stream_with_trailing_usage_uses_sdk_counts() -> None:
    """When the stream yields a trailing Usage, the row reflects those counts."""

    class _StreamWithUsageProvider(_FakeProvider):
        async def stream(self, system, user, *, model_role="judge", **_kw):
            yield "hello"
            yield " world"
            yield Usage(
                input_tokens=400,
                output_tokens=50,
                cache_read_input_tokens=200,
                cache_creation_input_tokens=0,
            )

    engine, recorder = _build_engine_and_recorder()
    wrapped = CostTrackingProvider(_StreamWithUsageProvider(), recorder)

    with feature_context("chat"):
        async for _ in wrapped.stream("sys", "usr"):
            pass

    with DbSession(engine) as db:
        rows = list_recent(db, limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r.tokens_in == 400
    assert r.tokens_out == 50
    assert r.cache_read_input_tokens == 200
    assert r.cache_creation_input_tokens == 0


# ---------------------------------------------------------------------------
# Smoke: contextvars survive asyncio.create_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_context_propagates_through_create_task() -> None:
    engine, recorder = _build_engine_and_recorder()
    wrapped = CostTrackingProvider(_FakeProvider(), recorder)

    async def _inner_call() -> None:
        await wrapped.complete("sys", "usr")

    with feature_context("proactive"):
        # asyncio.create_task copies the current context, so the
        # feature label "proactive" must reach the cost row even
        # though the call happens inside a fresh task.
        task = asyncio.create_task(_inner_call())
        await task

    with DbSession(engine) as db:
        rows = list_recent(db, limit=5)
    assert len(rows) == 1
    assert rows[0].feature == "proactive"


# ---------------------------------------------------------------------------
# Per-model pricing (Task 4 migration to runtime.llm.pricing)
# ---------------------------------------------------------------------------


def test_recorder_costs_differ_by_model_for_same_role() -> None:
    """Two calls with identical role + tokens but different model strings
    must produce different cost_usd rows after the pricing migration.
    Uses LiteLLM-confirmed dated model IDs so we hit the JSON path
    rather than the role fallback."""
    _engine, recorder = _build_engine_and_recorder()
    sonnet_usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    haiku_usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    sonnet = recorder.record(
        provider="anthropic",
        model="claude-3-7-sonnet-20250219",
        feature="chat",
        model_role="main",
        input_text="",
        output_text="",
        usage=sonnet_usage,
    )
    haiku = recorder.record(
        provider="anthropic",
        model="claude-3-haiku-20240307",
        feature="chat",
        model_role="fast",
        input_text="",
        output_text="",
        usage=haiku_usage,
    )
    assert sonnet is not None
    assert haiku is not None
    assert sonnet.cost_usd > haiku.cost_usd
    # As of the vendored snapshot: Sonnet ~$18 / Haiku ~$1.5 for 1M+1M tokens.
    assert sonnet.cost_usd > 10.0
    assert haiku.cost_usd > 1.0
    assert haiku.cost_usd < sonnet.cost_usd


def test_recorder_local_base_url_is_free() -> None:
    _engine, recorder = _build_engine_and_recorder()
    row = recorder.record(
        provider="openai_compat",
        model="llama-3.3-70b",
        feature="chat",
        model_role="main",
        input_text="x" * 100,
        output_text="y" * 100,
        usage=Usage(input_tokens=10_000, output_tokens=10_000),
        base_url="http://localhost:11434/v1",
    )
    assert row is not None
    assert row.cost_usd == 0.0
