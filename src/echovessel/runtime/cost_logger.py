"""LLM call accounting for the admin Cost tab.

Worker ζ scope. Persists one row per successful ``LLMProvider.complete()``
or ``LLMProvider.stream()`` call into a new ``llm_calls`` SQLite table,
plus query helpers for the admin summary / recent endpoints.

Design notes:

* Cost numbers are estimates — the constants below are a first
  approximation and the front-end labels them as such. Authoritative
  billing lives on the LLM provider's dashboard.

* The "feature" label (``chat`` / ``import`` / ``consolidate`` /
  ``reflection`` / ``proactive``) is set per-call by the caller via a
  :func:`feature_context` ``contextvars`` window. Wrapping the
  underlying :class:`LLMProvider` in :class:`CostTrackingProvider`
  means every call site gets recorded automatically — no caller edits
  besides setting the context.

* Failures are non-fatal. If the cost write raises (locked DB, disk
  full, schema mismatch on a partially-migrated install) we log a
  warning and continue. Lost rows are acceptable; pinning daemon
  liveness on the cost ledger would be silly.

The :class:`LLMCall` SQLModel class is registered with
``SQLModel.metadata`` at import time, so ``create_all_tables`` in
``echovessel.memory.db`` picks it up on fresh DBs. Legacy databases
get the table on the next boot via the same ``CREATE TABLE IF NOT
EXISTS`` semantics.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel, select
from sqlmodel import Session as DbSession

from echovessel.runtime.llm.base import DEFAULT_ROLE, LLMProvider
from echovessel.runtime.llm.usage import Usage

log = logging.getLogger(__name__)


# Five canonical feature labels. The Literal is exported so the admin
# router and tests can validate against the same source of truth.
Feature = Literal["chat", "import", "consolidate", "reflection", "proactive"]


# ---------------------------------------------------------------------------
# Pricing table — per-role rates in USD per 1K tokens
# ---------------------------------------------------------------------------

# Rough 2026-04 OpenAI rates. Other providers (anthropic, openrouter, …)
# use the same numbers — until the cost ledger grows a per-provider rate
# matrix, treating every provider as gpt-4o-class is the honest default
# (the front-end already labels these as estimates).
#
# Legacy tier keys (small / medium / large) are accepted too for back-compat
# with old ``llm_calls`` rows; see ``_rate_for_role``.
_ROLE_RATES_USD_PER_1K: dict[str, dict[str, float]] = {
    "fast": {"in": 0.00015, "out": 0.00060},  # gpt-4o-mini class
    "main": {"in": 0.0025, "out": 0.010},  # gpt-4o class
    "judge": {"in": 0.0025, "out": 0.010},  # gpt-4o class
}

# Read-only alias kept so ledger rows persisted under legacy tier names still
# resolve to a rate. Writes always use role keys.
_LEGACY_TIER_TO_ROLE: dict[str, str] = {
    "small": "fast",
    "medium": "judge",
    "large": "main",
}

# Stub provider has zero billable cost. The wrapper short-circuits the
# pricing math when it sees this provider name to avoid leaking fake
# numbers into the ledger.
_FREE_PROVIDERS: frozenset[str] = frozenset({"stub"})


def _count_tokens(text: str) -> int:
    """Best-effort token count.

    Tries :func:`tiktoken.encoding_for_model("cl100k_base")` first;
    falls back to a ``len(text) // 4`` heuristic when ``tiktoken`` is
    missing or its encoder raises.
    """

    if not text:
        return 0
    try:
        import tiktoken
    except ImportError:
        return max(1, len(text) // 4)
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001
        return max(1, len(text) // 4)


def _estimate_cost_usd(provider: str, model_role: str, tokens_in: int, tokens_out: int) -> float:
    """Convert ``(tokens_in, tokens_out, model_role)`` into a USD estimate.

    Accepts both new role keys (``fast`` / ``main`` / ``judge``) and
    legacy tier keys (``small`` / ``medium`` / ``large``) — legacy rows
    in old ledgers still resolve to a plausible rate. Unknown roles fall
    back to the ``fast`` rate; stub provider returns 0.0. Rounded to 6
    decimal places to keep the ledger column readable.
    """

    if provider in _FREE_PROVIDERS:
        return 0.0
    # Normalise legacy tier names to canonical roles before lookup.
    role_key = _LEGACY_TIER_TO_ROLE.get(model_role, model_role)
    rates = _ROLE_RATES_USD_PER_1K.get(role_key, _ROLE_RATES_USD_PER_1K["fast"])
    cost = (tokens_in / 1000.0) * rates["in"] + (tokens_out / 1000.0) * rates["out"]
    return round(cost, 6)


# ---------------------------------------------------------------------------
# SQLModel row + serialisation dataclass
# ---------------------------------------------------------------------------


class LLMCall(SQLModel, table=True):
    """Persistent row for one observed LLM call.

    The shape mirrors :class:`LLMCallRecord` 1:1; the SQLModel class is
    the on-disk definition that ``SQLModel.metadata.create_all`` picks
    up at startup, while :class:`LLMCallRecord` is the API-facing
    dataclass returned by query helpers (decoupled so admin handlers
    don't accidentally serialise SQLModel instances).
    """

    __tablename__ = "llm_calls"

    id: int | None = Field(default=None, primary_key=True)
    timestamp: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    provider: str = Field(index=True)
    model: str
    feature: str = Field(index=True)
    tier: str = Field(default="medium")
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """Plain dataclass for the API surface.

    Same fields as :class:`LLMCall` minus the SQLAlchemy machinery —
    safer to JSON-serialise from FastAPI handlers.
    """

    id: int
    timestamp: str  # ISO 8601
    provider: str
    model: str
    feature: str
    tier: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    turn_id: str | None


def _row_to_record(row: LLMCall) -> LLMCallRecord:
    return LLMCallRecord(
        id=int(row.id or 0),
        timestamp=row.timestamp.isoformat() if row.timestamp else "",
        provider=row.provider,
        model=row.model,
        feature=row.feature,
        tier=row.tier,
        tokens_in=int(row.tokens_in),
        tokens_out=int(row.tokens_out),
        cost_usd=float(row.cost_usd),
        cache_read_input_tokens=int(row.cache_read_input_tokens),
        cache_creation_input_tokens=int(row.cache_creation_input_tokens),
        turn_id=row.turn_id,
    )


# ---------------------------------------------------------------------------
# Per-call feature context (set by callers before the LLM call)
# ---------------------------------------------------------------------------

_current_feature: ContextVar[str | None] = ContextVar("echovessel_cost_feature", default=None)
_current_turn_id: ContextVar[str | None] = ContextVar("echovessel_cost_turn_id", default=None)


@contextmanager
def feature_context(name: str, *, turn_id: str | None = None):
    """Tag every LLM call inside the ``with`` block with ``name``.

    ``contextvars`` propagate automatically across :func:`asyncio.create_task`
    boundaries (each task copies the parent's context), so wrapping a
    coroutine that spawns helper tasks works without extra plumbing.

    Optional ``turn_id`` is stored in the same context so chat-tier
    calls can be cross-referenced with the turn they belong to in the
    admin tab's "recent calls" list.
    """

    token = _current_feature.set(name)
    turn_token = _current_turn_id.set(turn_id) if turn_id is not None else None
    try:
        yield
    finally:
        _current_feature.reset(token)
        if turn_token is not None:
            _current_turn_id.reset(turn_token)


# ---------------------------------------------------------------------------
# CostRecorder — actually writes rows
# ---------------------------------------------------------------------------


DbSessionFactory = Callable[[], DbSession]


class CostRecorder:
    """Writes one ``LLMCall`` row per observed call.

    The writer opens a fresh ``DbSession`` per call so SQLite stays
    happy across the worker pool without us managing transactions.
    Failures are caught and logged — cost tracking must not break the
    actual LLM call path.
    """

    def __init__(self, db_factory: DbSessionFactory) -> None:
        self._db_factory = db_factory

    def record(
        self,
        *,
        provider: str,
        model: str,
        feature: str,
        model_role: str | None = None,
        input_text: str,
        output_text: str,
        turn_id: str | None = None,
        timestamp: datetime | None = None,
        usage: Usage | None = None,  # Prefer SDK-reported usage over _count_tokens fallback
        tier: str | None = None,  # Deprecated alias for model_role; kept one release
    ) -> LLMCallRecord | None:
        # Backwards compatibility: callers that still pass ``tier=...`` keep
        # working for one release. Prefer the new ``model_role`` kwarg.
        if model_role is None:
            model_role = tier if tier is not None else DEFAULT_ROLE
        if usage is not None:
            tokens_in = usage.input_tokens
            tokens_out = usage.output_tokens
            cache_read = usage.cache_read_input_tokens
            cache_create = usage.cache_creation_input_tokens
        else:
            tokens_in = _count_tokens(input_text)
            tokens_out = _count_tokens(output_text)
            cache_read = cache_create = 0
        cost_usd = _estimate_cost_usd(provider, model_role, tokens_in, tokens_out)
        ts = timestamp or datetime.now()
        # DB column is still named `tier` (free-text label) — we store the
        # role value there so historical tier-named rows and new role-named
        # rows coexist in one column. UI reads and interprets both.
        row = LLMCall(
            timestamp=ts,
            provider=provider,
            model=model,
            feature=feature,
            tier=model_role,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
            turn_id=turn_id,
        )
        try:
            with self._db_factory() as db:
                db.add(row)
                db.commit()
                db.refresh(row)
        except Exception as e:  # noqa: BLE001
            log.warning("cost_logger: failed to persist LLM call: %s", e)
            return None
        return _row_to_record(row)


# ---------------------------------------------------------------------------
# CostTrackingProvider — drop-in LLMProvider wrapper
# ---------------------------------------------------------------------------


class CostTrackingProvider:
    """Wrap an :class:`LLMProvider` so every call gets logged.

    Intercepts both ``complete`` and ``stream``. The streaming path
    accumulates yielded chunks into one string before recording so the
    output token count is exact; downstream consumers see the unchanged
    chunk-by-chunk yields.
    """

    def __init__(self, inner: LLMProvider, recorder: CostRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    @property
    def provider_name(self) -> str:
        return self._inner.provider_name

    def model_for(self, model_role: str) -> str:
        return self._inner.model_for(model_role)

    async def complete(
        self,
        system: str,
        user: str,
        *,
        model_role: str = DEFAULT_ROLE,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> tuple[str, Usage | None]:
        text, usage = await self._inner.complete(
            system,
            user,
            model_role=model_role,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        self._record(model_role, system, user, text, usage=usage)
        return text, usage

    async def stream(
        self,
        system: str,
        user: str,
        *,
        model_role: str = DEFAULT_ROLE,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> AsyncIterator[str | Usage]:
        chunks: list[str] = []
        trailing_usage: Usage | None = None
        async for item in self._inner.stream(
            system,
            user,
            model_role=model_role,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        ):
            if isinstance(item, str):
                chunks.append(item)
                yield item
            else:
                trailing_usage = item
        self._record(model_role, system, user, "".join(chunks), usage=trailing_usage)

    def _record(
        self,
        model_role: str,
        system: str,
        user: str,
        output: str,
        *,
        usage: Usage | None = None,
    ) -> None:
        feature = _current_feature.get() or "unknown"
        turn_id = _current_turn_id.get()
        try:
            self._recorder.record(
                provider=self._inner.provider_name,
                model=self._inner.model_for(model_role),
                feature=feature,
                model_role=model_role,
                input_text=f"{system}\n{user}",
                output_text=output,
                turn_id=turn_id,
                usage=usage,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("cost_logger: record_call raised: %s", e)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _range_to_window(range_label: str, now: datetime | None = None) -> datetime:
    """Translate a ``range`` query string into a ``since`` cutoff."""

    now = now or datetime.now()
    if range_label == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_label == "7d":
        return now - timedelta(days=7)
    if range_label == "30d":
        return now - timedelta(days=30)
    raise ValueError(f"unknown cost range {range_label!r}; expected today | 7d | 30d")


def summarize(
    db: DbSession,
    *,
    range_label: str = "30d",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the ``GET /api/admin/cost/summary`` payload.

    Returns ``{range, since, total_usd, total_tokens, by_feature, by_day}``
    where ``by_feature`` is a per-feature dict of
    ``{calls, tokens_in, tokens_out, cost_usd}`` and ``by_day`` is a
    list of ``{date, usd, tokens, calls}`` entries ordered ASC by date.
    """

    since = _range_to_window(range_label, now=now)
    rows: list[LLMCall] = list(db.exec(select(LLMCall).where(LLMCall.timestamp >= since)).all())

    total_usd = round(sum(float(r.cost_usd) for r in rows), 6)
    total_tokens_in = sum(int(r.tokens_in) for r in rows)
    total_tokens_out = sum(int(r.tokens_out) for r in rows)
    total_cache_read_input_tokens = sum(int(r.cache_read_input_tokens) for r in rows)
    total_cache_creation_input_tokens = sum(int(r.cache_creation_input_tokens) for r in rows)

    by_feature: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_feature.setdefault(
            row.feature,
            {
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )
        bucket["calls"] += 1
        bucket["tokens_in"] += int(row.tokens_in)
        bucket["tokens_out"] += int(row.tokens_out)
        bucket["cost_usd"] = round(float(bucket["cost_usd"]) + float(row.cost_usd), 6)
        bucket["cache_read_input_tokens"] += int(row.cache_read_input_tokens)
        bucket["cache_creation_input_tokens"] += int(row.cache_creation_input_tokens)

    by_day_acc: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row.timestamp:
            continue
        date_key = row.timestamp.date().isoformat()
        bucket = by_day_acc.setdefault(
            date_key,
            {
                "date": date_key,
                "usd": 0.0,
                "tokens": 0,
                "calls": 0,
            },
        )
        bucket["usd"] = round(float(bucket["usd"]) + float(row.cost_usd), 6)
        bucket["tokens"] += int(row.tokens_in) + int(row.tokens_out)
        bucket["calls"] += 1

    by_day = sorted(by_day_acc.values(), key=lambda b: b["date"])

    return {
        "range": range_label,
        "since": since.isoformat(),
        "total_usd": total_usd,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_tokens": total_tokens_in + total_tokens_out,
        "total_cache_read_input_tokens": total_cache_read_input_tokens,
        "total_cache_creation_input_tokens": total_cache_creation_input_tokens,
        "by_feature": by_feature,
        "by_day": by_day,
    }


def list_recent(
    db: DbSession,
    *,
    limit: int = 50,
) -> list[LLMCallRecord]:
    """Return the ``limit`` most recent LLM calls, newest first."""

    limit = max(1, min(limit, 200))
    rows = list(
        db.exec(
            select(LLMCall)
            .order_by(LLMCall.timestamp.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all()
    )
    return [_row_to_record(r) for r in rows]


__all__ = [
    "CostRecorder",
    "CostTrackingProvider",
    "Feature",
    "LLMCall",
    "LLMCallRecord",
    "feature_context",
    "list_recent",
    "summarize",
]
