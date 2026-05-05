# Proactive eval coverage matrix

> Status legend:
> - `✅` shipped + verified against a live LLM run
> - `➕` fixture YAML written; awaiting live-LLM tuning
> - `❌` xfail (known LLM / prompt gap)
> - `🌐` requires runner extension not yet built

## Tier 1 · Detection · advance_hours table coverage (10)

| ID | Behaviour | PART F category | Status |
|---|---|---|---|
| `moving_houston` | relocation event populates follow_up_at + advance fields | 搬家 / 重要决定 24/24 | ❌ |
| `interview_pre_on_post` | interview · all 3 phases fire | 考试 / 面试 12-24 | ➕ |
| `surgery_no_post` | surgery · advance_post=0 | 手术 48-72/0 | ➕ |
| `medical_routine` | routine medical · short windows | 体检 2-6/2-4 | ➕ |
| `travel_departure` | travel · post=0 | 旅行 6-12/0 | ➕ |
| `deadline_paper` | paper deadline | 论文 24-48/6-12 | ➕ |
| `celebration_event` | birthday celebration | 庆祝 4-8/2-4 | ➕ |
| `reminder_only_on` | explicit reminder | reminder 0/0 | ➕ |
| `vague_date_anchor` | date concrete · time fuzzy | (vague) | ➕ |
| `commitment_third_party` | user → third-party promise | (打电话给妈) | ➕ |

## Tier 1 · Detection · semantics (4)

| ID | Behaviour | Status |
|---|---|---|
| `commitment_user` | user commitment · `relational_tags=[commitment]` | ➕ |
| `unresolved_short_window` | unresolved emotion · NOW + 1-2 days | ➕ |
| `ongoing_check_n` | open-ended ongoing · check_N series | ➕ |
| `fuzzy_future_check` | fuzzy future · should NOT lock date · check_N | ➕ |

## Tier 1 · Detection reverse (4)

| ID | Behaviour | Status |
|---|---|---|
| `low_value_future` | low-value mundane · `follow_up_at=null` | ➕ |
| `historical_no_followup` | pure-past · `follow_up_at=null` | ➕ |
| `trivial_no_followup` | small-talk · skipped or `null` | ➕ |
| `user_explicit_decline` | PART F explicit-suppress rule | ➕ |

## Tier 1 · Multi-event + supersede + sensitive + bypass (4)

| ID | Behaviour | Status |
|---|---|---|
| `multi_event_extraction` | one session → two ConceptNodes | ➕ |
| `supersede_blocks_post` | outcome reported · post NOT fired | ➕ |
| `vulnerability_moment` | `\|impact\| >= 7` bypass engagement | ➕ |
| `medical_sensitive_tone` | high-sensitivity · judge tone | ➕ |

## Tier 1 · Policy gates 5/5 + bypass (5+1)

| ID | Gate | Status |
|---|---|---|
| `quiet_hours_skip_then_retry` | 1 quiet_hours + retry on exit | ➕ |
| `forbidden_topic_suppress` | 2 forbidden_topics + permanent suppress | ➕ |
| `in_flight_turn_blocks` | 3 in_flight_turn predicate | ➕ |
| `rate_limit_cap_4h` | 4 rate_limit + 4h cooldown | ➕ |
| `engagement_low_blocks` | 5 engagement_score | ➕ |
| `engagement_bypass_commitment` | 5 bypass via commitment tag | ➕ |

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
