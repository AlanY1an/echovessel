"""Hard-invariant checker for eval fixtures.

``check_invariants`` walks the dict produced by ``run_fixture`` and
returns a flat list of human-readable violation strings — empty list
means every hard invariant the fixture declared has passed. Soft
quality assertions (LLM-as-judge) live separately in
:mod:`tests.memory_eval.judge`.
"""

from __future__ import annotations

from echovessel.memory.consolidate import SHOCK_IMPACT_THRESHOLD
from tests.memory_eval.schema import EvalResult, Fixture

__all__ = ["check_invariants"]


def check_invariants(fixture: Fixture, result: EvalResult) -> list[str]:
    """Return a list of human-readable invariant violations. Empty =
    all hard invariants passed.
    """
    inv = fixture.invariants
    violations: list[str] = []

    # Event count bounds — use the subset produced by the session when
    # turns exist; for retrieval-only fixtures fall back to total events.
    n_events = len(result.events)
    if inv.get("events_min") is not None and n_events < inv["events_min"]:
        violations.append(f"events_min {inv['events_min']} > produced {n_events}")
    if inv.get("events_max") is not None and n_events > inv["events_max"]:
        violations.append(f"events_max {inv['events_max']} < produced {n_events}")

    n_thoughts = len(result.thoughts)
    if inv.get("thoughts_min") is not None and n_thoughts < inv["thoughts_min"]:
        violations.append(
            f"thoughts_min {inv['thoughts_min']} > produced {n_thoughts}"
        )
    if inv.get("thoughts_max") is not None and n_thoughts > inv["thoughts_max"]:
        violations.append(
            f"thoughts_max {inv['thoughts_max']} < produced {n_thoughts}"
        )

    if inv.get("shock_event_present"):
        shocks = [
            e for e in result.events
            if abs(e["emotional_impact"]) >= SHOCK_IMPACT_THRESHOLD
        ]
        if not shocks:
            violations.append("shock_event_present: no |impact|>=8 event produced")

    if inv.get("reflection_triggered") and not result.reflection_triggered:
        violations.append("reflection_triggered: reflect_fn was never called")

    if inv.get("must_mention_any"):
        wanted = inv["must_mention_any"]
        all_text = " ".join(e["description"] for e in result.events)
        if not any(w in all_text for w in wanted):
            violations.append(
                f"must_mention_any: no event mentions any of {wanted}"
            )

    if inv.get("must_have_relational_tag_any"):
        wanted = set(inv["must_have_relational_tag_any"])
        got = set()
        for e in result.events:
            got.update(e["relational_tags"])
        if not wanted & got:
            violations.append(
                f"must_have_relational_tag_any: wanted any of {sorted(wanted)} "
                f"· got {sorted(got)}"
            )

    if inv.get("must_have_event_time"):
        events_without_time = [
            e for e in result.events if not e.get("event_time_start")
        ]
        if events_without_time:
            violations.append(
                f"must_have_event_time: {len(events_without_time)} event(s) "
                f"lack event_time_start"
            )

    if inv.get("must_have_subject_any"):
        wanted = set(inv["must_have_subject_any"])
        got = {e.get("subject") for e in result.events if e.get("subject")}
        if not wanted & got:
            violations.append(
                f"must_have_subject_any: wanted any of {sorted(wanted)} · "
                f"got {sorted(got)}"
            )

    if inv.get("must_have_concept_type_any"):
        wanted = set(inv["must_have_concept_type_any"])
        got = {e.get("type") for e in result.events if e.get("type")}
        if not wanted & got:
            violations.append(
                f"must_have_concept_type_any: wanted any of {sorted(wanted)} · "
                f"got {sorted(got)}"
            )

    if inv.get("forbidden_descriptions_contain_none"):
        forbidden = inv["forbidden_descriptions_contain_none"]
        for e in result.events:
            for phrase in forbidden:
                if phrase in e["description"]:
                    violations.append(
                        f"forbidden_descriptions_contain_none: event {e['id']} "
                        f"contains forbidden phrase {phrase!r}"
                    )
                    break

    if (
        inv.get("entity_count_eq") is not None
        and len(result.entities) != inv["entity_count_eq"]
    ):
        violations.append(
            f"entity_count_eq {inv['entity_count_eq']} != {len(result.entities)}"
        )
    if (
        inv.get("entity_count_max") is not None
        and len(result.entities) > inv["entity_count_max"]
    ):
        violations.append(
            f"entity_count_max {inv['entity_count_max']} < {len(result.entities)}"
        )

    if inv.get("entity_merge_status_eq"):
        wanted = inv["entity_merge_status_eq"]
        by_name: dict[str, list[dict]] = {}
        for e in result.entities:
            by_name.setdefault(e["canonical_name"], []).append(e)
        for name, want in wanted.items():
            matches = by_name.get(name, [])
            if not matches:
                violations.append(
                    f"entity_merge_status_eq: entity {name!r} not found"
                )
                continue
            bad = [m for m in matches if m.get("merge_status") != want]
            if bad:
                violations.append(
                    f"entity_merge_status_eq: {name!r} has {len(bad)}/{len(matches)} "
                    f"entities with status != {want!r}"
                )

    if (
        inv.get("recall_message_count_eq") is not None
        and result.recall_count != inv["recall_message_count_eq"]
    ):
        violations.append(
            f"recall_message_count_eq {inv['recall_message_count_eq']} != "
            f"{result.recall_count}"
        )

    if inv.get("core_blocks_unchanged"):
        before = result.core_block_snapshot_before
        after = result.core_block_snapshot_after
        if before != after:
            all_keys = set(before) | set(after)
            changes = [
                f"{k}: {before.get(k, '<absent>')} -> {after.get(k, '<absent>')}"
                for k in sorted(all_keys)
                if before.get(k) != after.get(k)
            ]
            violations.append(
                "core_blocks_unchanged: " + " · ".join(changes)
            )

    if inv.get("top_k_must_contain_descriptions_all"):
        wanted = inv["top_k_must_contain_descriptions_all"]
        top = result.retrieved[: inv.get("top_k_for_check", len(result.retrieved))]
        missing = [
            w for w in wanted if not any(w in m["description"] for m in top)
        ]
        if missing:
            violations.append(
                f"top_k_must_contain_descriptions_all: missing {missing} "
                f"from top-{len(top)}"
            )

    if inv.get("top_k_must_not_contain_descriptions_any"):
        forbidden = inv["top_k_must_not_contain_descriptions_any"]
        top = result.retrieved[: inv.get("top_k_for_check", len(result.retrieved))]
        leaked = [
            phrase for phrase in forbidden if any(phrase in m["description"] for m in top)
        ]
        if leaked:
            violations.append(
                f"top_k_must_not_contain_descriptions_any: {leaked} leaked "
                f"into top-{len(top)}"
            )

    if inv.get("episodic_state_mood_changed"):
        before = (result.episodic_state_before or {}).get("mood")
        after = (result.episodic_state_after or {}).get("mood")
        if before == after:
            violations.append(
                f"episodic_state_mood_changed: mood stayed {before!r}"
            )

    if inv.get("episodic_state_mood_unchanged"):
        before = (result.episodic_state_before or {}).get("mood")
        after = (result.episodic_state_after or {}).get("mood")
        if before != after:
            violations.append(
                f"episodic_state_mood_unchanged: mood drifted "
                f"{before!r} -> {after!r}"
            )

    if inv.get("filling_min") is not None:
        # Any SINGLE thought must cite at least ``filling_min`` events.
        by_parent: dict[int, int] = {}
        for r in result.filling:
            by_parent[r["parent_id"]] = by_parent.get(r["parent_id"], 0) + 1
        top = max(by_parent.values(), default=0)
        if top < inv["filling_min"]:
            violations.append(
                f"filling_min {inv['filling_min']} > largest chain {top}"
            )

    if inv.get("top3_relevant_min") is not None:
        must = inv.get("top3_description_contains_any") or []
        top3 = result.retrieved[:3]
        n_rel = sum(1 for m in top3 if any(tok in m["description"] for tok in must))
        if n_rel < inv["top3_relevant_min"]:
            violations.append(
                f"top3_relevant_min {inv['top3_relevant_min']} > matched {n_rel} "
                f"(top3: {[m['description'] for m in top3]})"
            )

    if inv.get("output_language") == "zh":
        # A quick heuristic: at least half of all event text must be CJK.
        all_text = "".join(e["description"] for e in result.events)
        if not all_text:
            violations.append("output_language=zh: no event descriptions to check")
        else:
            cjk = sum(1 for ch in all_text if "一" <= ch <= "鿿")
            if cjk * 2 < len(all_text):
                violations.append(
                    f"output_language=zh: CJK ratio {cjk}/{len(all_text)} below 50%"
                )

    return violations
