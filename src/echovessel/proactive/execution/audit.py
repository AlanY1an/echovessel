"""Audit trail (spec §7).

Every call to PolicyEngine.evaluate produces exactly one ProactiveDecision,
whether the action is 'send' or 'skip'. This module persists those rows to
``~/.echovessel/logs/proactive-YYYY-MM-DD.jsonl`` and exposes the read
queries that rate_limit + cold_user policies need.

Storage choice rationale (spec §7.5):

- JSONL is grep-friendly for MVP debugging
- One file per local date keeps rotation trivial (`stat` + rename, no
  external logrotate dependency)
- No migration burden: adding a field in ProactiveDecision just adds a
  new column in the next-day's file; older files remain parseable

Invariants:

- ``record`` is synchronous and must not raise on disk-full / permission
  errors — the scheduler tick loop cannot tolerate audit raising (§16.6).
  Errors are logged at ERROR level and swallowed.
- ``record`` appends one JSON object + ``\n`` per decision (JSONL strict).
- ``update_latest`` rewrites the most-recently-appended row by
  rewriting the tail of the file. JSONL is append-only in the happy
  path; the tail rewrite is only used for the two-phase "write skeleton
  then fill outcome" pattern (§7.3). Alternative: write a second
  "outcome" record keyed by decision_id. v1.0 memory_db sink chooses
  differently; MVP keeps it simple.
- Reads span day boundaries: ``count_sends_in_last_24h`` reads both today
  and yesterday files (spec §3.2 note).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from echovessel.proactive.core.base import (
    ActionType,
    AuditSink,
    ProactiveDecision,
)

log = logging.getLogger(__name__)


def _isoformat_or_none(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def serialize_decision(decision: ProactiveDecision) -> dict[str, Any]:
    """Convert a ProactiveDecision to a JSON-serialisable dict. Handles
    datetime, nested Mapping, and nested dataclasses in trigger_payload /
    policy_snapshot fields."""
    if not is_dataclass(decision):
        raise TypeError(f"expected ProactiveDecision dataclass, got {type(decision)}")

    raw = asdict(decision)
    return _walk_jsonify(raw)


def _walk_jsonify(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk_jsonify(v) for v in obj]
    return _isoformat_or_none(obj)


def deserialize_decision(row: dict[str, Any]) -> ProactiveDecision:
    """Rebuild a ProactiveDecision from a parsed JSONL row. Inverse of
    ``serialize_decision``. Missing fields fall back to dataclass defaults
    so older files parse forward-compatibly."""
    kwargs: dict[str, Any] = {}
    for name, default in _DECISION_DEFAULTS.items():
        if name in row:
            kwargs[name] = _coerce_field(name, row[name])
        else:
            kwargs[name] = default() if callable(default) else default
    return ProactiveDecision(**kwargs)


_DECISION_DEFAULTS: dict[str, Any] = {
    "decision_id": "",
    "persona_id": "",
    "user_id": "",
    "timestamp": datetime.fromtimestamp(0),
    "trigger": "",
    "trigger_payload": None,
    "action": ActionType.SKIP.value,
    "skip_reason": None,
    "target_channel_id": None,
    "message_text": None,
    "rationale": None,
    "delivery": None,  # v0.2
    "voice_used": False,
    "voice_error": None,
    "send_ok": None,
    "send_error": None,
    "ingest_message_id": None,
    "llm_latency_ms": None,
    "prompt_tokens": None,
    "completion_tokens": None,
    "memory_snapshot_hash": None,
    "policy_snapshot": lambda: {},
    "config_version": "v0.1",
}


def _coerce_field(name: str, value: Any) -> Any:
    if name == "timestamp" and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


# ---------------------------------------------------------------------------
# JSONLAuditSink
# ---------------------------------------------------------------------------


@dataclass
class JSONLAuditSink(AuditSink):
    """Write decisions to ``<log_dir>/proactive-YYYY-MM-DD.jsonl``.

    The sink keeps an in-memory copy of the last-written decision so
    ``update_latest`` can mutate its outcome fields without re-reading
    the file. When the process restarts mid-tick (rare), in-flight
    outcome updates may be lost; the skeleton row stays correct.
    """

    log_dir: Path
    clock: Any = field(default=datetime.now)

    _latest: ProactiveDecision | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.log_dir = Path(self.log_dir).expanduser()
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error(
                "audit log dir unreachable at %s: %s", self.log_dir, e
            )

    # ------------------------------------------------------------------
    # AuditSink contract
    # ------------------------------------------------------------------

    def record(self, decision: ProactiveDecision) -> None:
        self._latest = decision
        try:
            path = self._path_for(decision.timestamp)
            row = serialize_decision(decision)
            line = json.dumps(row, ensure_ascii=False, sort_keys=True)
            # Append-only, one decision per line.
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            # Never raise — scheduler tick loop must be able to continue.
            log.error(
                "audit record write failed for %s: %s",
                decision.decision_id,
                e,
            )

    def update_latest(
        self,
        decision_id: str,
        *,
        send_ok: bool | None = None,
        send_error: str | None = None,
        ingest_message_id: int | None = None,
        delivery: str | None = None,
        voice_used: bool | None = None,
        voice_error: str | None = None,
        llm_latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        if self._latest is None or self._latest.decision_id != decision_id:
            log.warning(
                "update_latest called for %s but current latest is %s",
                decision_id,
                self._latest.decision_id if self._latest else None,
            )
            return

        self._latest.update_outcome(
            send_ok=send_ok,
            send_error=send_error,
            ingest_message_id=ingest_message_id,
            delivery=delivery,
            voice_used=voice_used,
            voice_error=voice_error,
            llm_latency_ms=llm_latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        try:
            self._rewrite_tail(self._latest)
        except OSError as e:
            log.error(
                "audit update_latest rewrite failed for %s: %s",
                decision_id,
                e,
            )

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]:
        """Return the last N decisions with action == 'send', newest-first."""
        if last_n <= 0:
            return []
        out: list[ProactiveDecision] = []
        for decision in self._iter_recent_decisions(days=2):
            if decision.action == ActionType.SEND.value:
                out.append(decision)
                if len(out) >= last_n:
                    break
        return out

    def count_sends_in_last_24h(self, *, now: datetime) -> int:
        """How many decisions with action == 'send' occurred in
        ``[now - 24h, now)``. Reads today + yesterday files (spec §3.2
        boundary note)."""
        cutoff = now - timedelta(hours=24)
        count = 0
        for decision in self._iter_recent_decisions(days=2):
            if decision.action != ActionType.SEND.value:
                continue
            if cutoff <= decision.timestamp <= now:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Additional helpers used by the scheduler / tests
    # ------------------------------------------------------------------

    def iter_recent(self, *, days: int = 2) -> list[ProactiveDecision]:
        """Expose the reverse-chronological iterator to callers that need
        to walk more than 24h (cold_user has the same data source)."""
        return list(self._iter_recent_decisions(days=days))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for(self, when: datetime) -> Path:
        return self.log_dir / f"proactive-{when.date().isoformat()}.jsonl"

    def _iter_recent_decisions(self, *, days: int) -> list[ProactiveDecision]:
        now = self.clock() if callable(self.clock) else self.clock
        out: list[ProactiveDecision] = []
        for offset in range(days):
            day = (now - timedelta(days=offset)).date()
            path = self.log_dir / f"proactive-{day.isoformat()}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                log.error("audit read failed for %s: %s", path, e)
                continue
            # Reverse order so we return newest-first across days.
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as e:
                    log.error("audit parse failed on line in %s: %s", path, e)
                    continue
                out.append(deserialize_decision(row))
        return out

    def _rewrite_tail(self, decision: ProactiveDecision) -> None:
        """Rewrite the last row of the file for the decision's date with
        the updated outcome. This is the single non-append-only operation
        on JSONL and is reserved for update_latest's two-phase write."""
        path = self._path_for(decision.timestamp)
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return

        # Find the latest matching decision_id, walking from end.
        row = serialize_decision(decision)
        new_line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        for i in range(len(lines) - 1, -1, -1):
            try:
                existing = json.loads(lines[i])
            except json.JSONDecodeError:
                continue
            if existing.get("decision_id") == decision.decision_id:
                lines[i] = new_line
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return
        # Not found — append as a new row (shouldn't happen; defensive).
        with path.open("a", encoding="utf-8") as f:
            f.write(new_line + "\n")


__all__ = [
    "JSONLAuditSink",
    "serialize_decision",
    "deserialize_decision",
]
