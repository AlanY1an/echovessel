"""Hard-invariant checker for proactive_eval fixtures."""

from __future__ import annotations

from typing import Any

from tests.proactive_eval.schema import (
    ExpectEvent,
    ProactiveEvalResult,
    ProactiveFixture,
)

__all__ = ["check_invariants", "render_evidence"]


def _find_event(events: list[dict[str, Any]], must_contain: list[str]) -> dict[str, Any] | None:
    for e in events:
        desc = e.get("description") or ""
        if all(tok in desc for tok in must_contain):
            return e
    return None


def _check_event(ee: ExpectEvent, events: list[dict[str, Any]]) -> list[str]:
    target = _find_event(events, ee.description_contains)
    out: list[str] = []
    if target is None:
        out.append(
            f"event_extracted: no event matches description_contains={ee.description_contains!r}"
        )
        return out

    fua = target.get("follow_up_at")
    if ee.follow_up_at_required and not fua:
        out.append(
            f"follow_up_at_required: event {target.get('description')!r} has follow_up_at=None"
        )
    if ee.follow_up_at_forbidden and fua:
        out.append(
            f"follow_up_at_forbidden: event {target.get('description')!r} unexpectedly has "
            f"follow_up_at={fua!r}"
        )

    def _check_range(fname: str, want: tuple[int, int] | None) -> None:
        if want is None:
            return
        v = target.get(fname)
        if v is None:
            out.append(
                f"{fname}: event {target.get('description')!r} has {fname}=None (want {want})"
            )
            return
        lo, hi = want
        if not (lo <= int(v) <= hi):
            out.append(f"{fname}: got {v} not in [{lo}, {hi}]")

    _check_range("advance_pre_hours", ee.advance_pre_hours_range)
    _check_range("advance_post_hours", ee.advance_post_hours_range)
    _check_range("estimated_arc_days", ee.estimated_arc_days_range)

    if ee.relational_tags_any:
        got = set(target.get("relational_tags") or [])
        want = set(ee.relational_tags_any)
        if not (got & want):
            out.append(f"relational_tags_any: wanted any of {sorted(want)}, got {sorted(got)}")

    if ee.follow_up_hint_min_chars > 0:
        hint = target.get("follow_up_hint") or ""
        if len(hint) < ee.follow_up_hint_min_chars:
            out.append(
                f"follow_up_hint_min_chars: hint {hint!r} shorter than {ee.follow_up_hint_min_chars}"
            )

    if ee.event_time_required and not target.get("event_time_start"):
        out.append(
            f"event_time_required: event {target.get('description')!r} has event_time_start=None"
        )

    return out


def check_invariants(fixture: ProactiveFixture, result: ProactiveEvalResult) -> list[str]:
    violations: list[str] = []

    if fixture.expect.event_extracted and result.consolidate_skipped:
        violations.append(
            f"event_extracted expected but consolidate skipped (reason={result.consolidate_skip_reason!r})"
        )
    else:
        for ee in fixture.expect.event_extracted:
            violations.extend(_check_event(ee, result.events))

    for phase_name, ep in fixture.expect.phases.items():
        per = result.audit_per_phase.get(phase_name)
        if per is None or not per.audit_rows:
            violations.append(f"phase {phase_name}: no audit row produced")
            continue
        row = per.audit_rows[-1]
        action = row.get("action")
        if ep.audit_action == "fire":
            if action != "send" or not row.get("send_ok"):
                violations.append(
                    f"phase {phase_name}: expected fire (send + send_ok=True), "
                    f"got action={action!r} send_ok={row.get('send_ok')!r} "
                    f"suppress_reason={row.get('suppress_reason')!r}"
                )
        elif ep.audit_action == "skip":
            if action != "skip":
                violations.append(f"phase {phase_name}: expected skip, got action={action!r}")
            if ep.suppress_reason and row.get("suppress_reason") != ep.suppress_reason:
                violations.append(
                    f"phase {phase_name}: expected suppress_reason={ep.suppress_reason!r}, "
                    f"got {row.get('suppress_reason')!r}"
                )

    if (
        fixture.follow_up_stage
        and fixture.follow_up_stage.expect_supersede_event_description_contains
    ):
        if result.supersede_event is None:
            violations.append("supersede expected but no supersede event produced")
        else:
            desc = result.supersede_event.get("description") or ""
            for tok in fixture.follow_up_stage.expect_supersede_event_description_contains:
                if tok not in desc:
                    violations.append(f"supersede event description {desc!r} missing token {tok!r}")

    return violations


def render_evidence(fixture: ProactiveFixture, result: ProactiveEvalResult) -> str:
    lines = [f"Fixture: {fixture.fixture_id}", f"Scenario: {fixture.scenario}", "", "--- Turns ---"]
    for t in fixture.turns:
        lines.append(f"  {t.role}: {t.content}")
    lines.append("")
    lines.append(f"--- Extracted events ({len(result.events)}) ---")
    for e in result.events:
        lines.append(
            f"  [{e.get('type')}] desc={e.get('description')!r} "
            f"follow_up_at={e.get('follow_up_at')} "
            f"advance_pre/post={e.get('advance_pre_hours')}/{e.get('advance_post_hours')} "
            f"est_arc={e.get('estimated_arc_days')} "
            f"hint={e.get('follow_up_hint')!r} "
            f"tags={e.get('relational_tags')}"
        )
    if result.audit_per_phase:
        lines.append("")
        lines.append("--- Audit per phase ---")
        for phase, per in result.audit_per_phase.items():
            lines.append(f"  {phase} @ {per.fire_at_clock}:")
            for row in per.audit_rows:
                lines.append(
                    f"    action={row.get('action')!r} "
                    f"send_ok={row.get('send_ok')} "
                    f"suppress_reason={row.get('suppress_reason')!r} "
                    f"phase_col={row.get('phase')!r}"
                )
            for msg in per.generated_messages:
                lines.append(f"    msg: {msg!r}")
    if result.supersede_event:
        lines.append("")
        lines.append(f"--- Supersede event ---\n  {result.supersede_event.get('description')!r}")
    return "\n".join(lines)
