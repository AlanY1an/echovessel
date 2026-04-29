# Memory eval coverage matrix

> Each row maps to one YAML fixture under `fixtures/scripted/` (single-session)
> or `fixtures/multi_session/` (Phase 4). Status legend:
> - `✅` shipped
> - `➕` planned in `develop-docs/initiatives/_active/2026-04-memory-eval-suite/plan.md`
> - `🌐` requires multi-session runner
> - `🔧` needs new invariant field (added in Task 2 of plan)

## L1 · Core blocks
| ID | Behavior | Status |
|---|---|---|
| `l1_persona_block_renders` | persona block reaches system prompt | ➕ |
| `l1_user_block_third_person` | user block written third-person (post-Q3) | ➕ |
| `l1_style_block_renders` | STYLE block injected via admin API path | ➕ |
| `l1_never_auto_update` | consolidate does NOT touch core_blocks | ➕ 🔧 |
| `l1_facts_view_has_location_tz` | PersonaFactsView exposes location/timezone/nationality (R2) | ➕ 🔧 |

## L2 · Recall messages
| ID | Behavior | Status |
|---|---|---|
| `l2_ingest_writes_recall` | user/persona ingest creates recall_messages rows | ➕ 🔧 |
| `l2_fts_finds_literal_phrase` | FTS5 fallback retrieves a literal phrase | ➕ 🔧 |

## L3 · Events (extraction)
| ID | Behavior | Status |
|---|---|---|
| `e1_user_self_disclosure` | identity-bearing user disclosure | ✅ |
| `e2_user_only_asks` | persona-only utterance not extracted | ✅ |
| `e3_buried_shock` | SHOCK gate triggers (C phase) | ✅ |
| `e4_correction` | supersede on correction | ✅ |
| `l3_vocative_recognition` | "我赢了，欧阳老师！" → not "user defeated persona" (Q8) | ➕ |
| `l3_persona_led_uncontested_blocked` | persona asks leading Q, user silent → no event (Issue 5) | ➕ |
| `l3_event_time_anchor_present` | extracted event has event_time_start/end (R4) | ➕ 🔧 |
| `l3_persona_commitment_subject` | "我答应你 9 点提醒" → event subject=persona, type=intention (R3 PART C) | ➕ 🔧 |
| `l3_trivial_session_skipped` | trivial gate (A phase) skips short low-emotion session | ➕ 🔧 |

## L4 · Thoughts (reflection)
| ID | Behavior | Status |
|---|---|---|
| `e5_reflection_abstraction` | TIMER (D phase) → reflection abstracts | ✅ |
| `l4_shock_reflection_via_c_gate` | SHOCK reflection (C → E) on impact≥8 | ➕ |
| `l4_hard_limit_3_per_24h` | 4th reflection in 24h is suppressed (REFLECTION_HARD_LIMIT_24H) | ➕ 🔧 |
| `l4_filling_chain_min_2` | thought parent_id chain references ≥2 events | ✅ via e5 `filling_min` |
| `l4_slow_cycle_persona_thought` | G phase produces subject=persona thought (R6) | 🌐 |

## L5 · Entities
| ID | Behavior | Status |
|---|---|---|
| `l5_alias_exact_match_dedup` | "黄逸扬" + "黄逸扬" in two events → 1 entity | ➕ 🔧 |
| `l5_embedding_dedup` | "Mochi" + "the cat Mochi" → 1 entity via L2 cosine | ➕ 🔧 |
| `l5_ambiguous_surface_keeps_separate` | "Alex" appears twice w/ no other signal → 2 entities, merge_status=uncertain | ➕ 🔧 |
| `l5_entity_anchored_retrieve_bonus` | query "Mochi" boosts entity-tagged events | ➕ 🔧 |

## L6 · Episodic state (mood)
| ID | Behavior | Status |
|---|---|---|
| `l6_mood_changes_after_shock` | session w/ heavy emotional content → episodic_state.mood updated (R1, Q1) | ➕ 🔧 |
| `l6_mood_survives_close` | mood persists across two consecutive sessions | 🌐 |
| `l6_neutral_session_no_mood_change` | chit-chat session does not perturb mood | ➕ 🔧 |

## Consolidation phases (whole-pipeline)
| ID | Behavior | Status |
|---|---|---|
| `phase_a_trivial_skip` | covered by `l3_trivial_session_skipped` | (alias) |
| `phase_c_shock_gate` | covered by `e3_buried_shock` | ✅ |
| `phase_d_timer_gate` | covered by `e5_reflection_abstraction` | ✅ |
| `phase_e_hard_limit` | covered by `l4_hard_limit_3_per_24h` | (alias) |
| `phase_g_slow_cycle` | covered by `l4_slow_cycle_persona_thought` | 🌐 |

## Retrieval scoring
| ID | Behavior | Status |
|---|---|---|
| `e6_retrieval_relevance` | top-3 over 10 events | ✅ |
| `retrieve_recency_decay_14d` | event 14 days ago drops below recent event | 🌐 |
| `retrieve_impact_boost` | high-impact event ranks above neutral when relevance ties | ➕ 🔧 |
| `retrieve_relational_bonus` | identity-bearing event ranks above neutral on tied query | ➕ 🔧 |
| `retrieve_fts_fallback` | empty vector hits → FTS surfaces literal-phrase match | ➕ 🔧 |
| `retrieve_pinned_thought_force_load` | force_load_user_thoughts >0 always loads pinned | ➕ 🔧 |
| `retrieve_cross_channel_no_filter` | event written from web, retrieved from discord shard | ➕ 🔧 |

## Forget
| ID | Behavior | Status |
|---|---|---|
| `forget_orphan_keeps_thoughts` | delete event with ORPHAN → thoughts survive marked orphaned | ➕ 🔧 |
| `forget_cascade_removes_thoughts` | delete event with CASCADE → dependent thoughts also deleted | ➕ 🔧 |
| `forget_supersede_chain` | newer event supersedes older; old hidden from retrieve | ✅ via e4 |
