# Proactive eval coverage matrix

> Status legend:
> - `✅` shipped + verified against a live LLM run
> - `❌` xfail (LLM / prompt / scheduling gap with concrete reason)
> - `🌐` requires runner extension not yet built

## Tier 1 · Detection · advance_hours table coverage (10)

| ID | Behaviour | PART F category | Status |
|---|---|---|---|
| `moving_houston` | relocation event populates follow_up_at + advance fields | 搬家 / 重要决定 24/24 | ❌ |
| `interview_pre_on_post` | interview · all 3 phases fire | 考试 / 面试 12-24 | ❌ |
| `surgery_no_post` | surgery · advance_post=0 | 手术 48-72/0 | ❌ |
| `medical_routine` | routine medical · short windows | 体检 2-6/2-4 | ✅ |
| `travel_departure` | travel · post=0 | 旅行 6-12/0 | ❌ |
| `deadline_paper` | paper deadline | 论文 24-48/6-12 | ✅ |
| `celebration_event` | birthday celebration | 庆祝 4-8/2-4 | ✅ |
| `reminder_only_on` | explicit reminder | reminder 0/0 | ✅ |
| `vague_date_anchor` | date concrete · time fuzzy | (vague) | ❌ |
| `commitment_third_party` | user → third-party promise | (打电话给妈) | ✅ |

## Tier 1 · Detection · semantics (4)

| ID | Behaviour | Status |
|---|---|---|
| `commitment_user` | user commitment · `relational_tags=[commitment]` | ✅ |
| `unresolved_short_window` | unresolved emotion · NOW + 1-2 days | ✅ |
| `ongoing_check_n` | open-ended ongoing · check_N series | ✅ |
| `fuzzy_future_check` | fuzzy future · should NOT lock date · check_N | ✅ |

## Tier 1 · Detection reverse (4)

| ID | Behaviour | Status |
|---|---|---|
| `low_value_future` | low-value mundane · `follow_up_at=null` | ❌ |
| `historical_no_followup` | pure-past · `follow_up_at=null` | ✅ |
| `trivial_no_followup` | small-talk · skipped or `null` | ✅ |
| `user_explicit_decline` | PART F explicit-suppress rule | ✅ |

## Tier 1 · Multi-event + supersede + sensitive + bypass (4)

| ID | Behaviour | Status |
|---|---|---|
| `multi_event_extraction` | one session → two ConceptNodes | ✅ |
| `supersede_blocks_post` | outcome reported · post NOT fired | ❌ |
| `vulnerability_moment` | `\|impact\| >= 7` bypass engagement | ✅ |
| `medical_sensitive_tone` | high-sensitivity · judge tone | ❌ |

## Tier 1 · Policy gates 5/5 + bypass (5+1)

| ID | Gate | Status |
|---|---|---|
| `quiet_hours_skip_then_retry` | 1 quiet_hours + retry on exit | ❌ |
| `forbidden_topic_suppress` | 2 forbidden_topics + permanent suppress | ✅ |
| `in_flight_turn_blocks` | 3 in_flight_turn predicate | ✅ |
| `rate_limit_cap_4h` | 4 rate_limit + 4h cooldown | ✅ |
| `engagement_low_blocks` | 5 engagement_score | ✅ |
| `engagement_bypass_commitment` | 5 bypass via commitment tag | ✅ |

## Tier 1 totals · run 8 (2026-05-05)

- **✅ 18 passing** · framework validated end-to-end
- **❌ 10 xfailed** with concrete reasons (PART F prompt judgment gaps · multi-phase scheduling · LLM advance_hours non-determinism)

## Bugs surfaced by this suite

End-to-end runs uncovered four production fixes shipped on this branch:

1. **Phase B parser bug** — `RawExtractedEvent` schema and `_parse_event` did not read PART F output (`follow_up_at` / `advance_pre/post_hours` / `estimated_arc_days` / `follow_up_hint`). Caused the original Houston dogfood scenario where consolidate ran but `concept_nodes.follow_up_at` was always NULL.
2. **DeepSeek V4 reasoning budget** — `make_extract_fn` / `make_judge_fn` wasted V4 thinking tokens on mechanical reformat tasks. Added per-call `thinking_enabled` knob (Protocol level, translates to provider-native API) and bumped `max_tokens` on creative call sites.
3. **`FollowUpScheduler._shutdown` not reset on `start()`** — multi-cycle schedulers (daemon restart, eval per-phase) silently no-op'd on second start.
4. **Phase A trivial gate too aggressive in tests** — short fixture conversations hit `TRIVIAL_MESSAGE_COUNT=3` ceiling, skipped consolidate. Runner now passes `trivial_message_count=0` to bypass the gate in eval.

Plus one runner observation:

- `SQLiteAuditSink.update_latest` does NOT re-persist `decision.message_text` after generation. Audit rows have empty `message_text` even when send_ok=True. Runner reads from `FakeChannel.sent` instead. Worth a follow-up production fix.

## Tier 2 · Boundaries (deferred · separate PR)

`engagement_bypass_high_impact` · `engagement_bypass_critical_event` ·
`engagement_silence_decay_7d` · `engagement_user_reply_credit` ·
`attempt_cap_after_5` · `stale_aging_3x` · `supersede_blocks_pre` ·
`supersede_blocks_on` · `cross_channel_pick` · `voice_fallback_transient` ·
`f10_judge_no_leak` · `low_presence_no_profile`

## Tier 3 · System-level (deferred · separate PR)

`resume_after_restart_rearm` · `resume_skip_already_fired` ·
`multi_event_rate_limit_interaction` · `supersede_chain_three_links` ·
`cross_session_supersede` · `send_failure_audit_orphan`
