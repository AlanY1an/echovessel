/**
 * API types — HTTP contract + SSE event shapes for the EchoVessel daemon.
 *
 * Stage 4-prep only. The contract here is locked against
 * `develop-docs/web-v1/04-stage-4-prep-tracker.md` §2 and is the same
 * contract the Stage 3 backend worker implements. Stage 4 proper consumes
 * these types from <Chat.tsx>, <Admin.tsx>, <Onboarding.tsx> to replace
 * the current localStorage-backed prototype wiring.
 *
 * Naming note: fields use snake_case because the Python backend emits
 * snake_case JSON. This intentionally diverges from the camelCase shapes
 * in `src/types.ts` (which describes the UI-side view model). Stage 4
 * proper will translate between the two layers.
 */

// ─── HTTP · GET /api/state ───────────────────────────────────────────────

/**
 * One row of the Admin screen's channel status strip.
 *
 * `enabled` means the channel is registered in the daemon (config turned
 * it on AND init succeeded). `ready` means its transport is currently
 * usable — for Discord, that includes the gateway handshake completing.
 * A channel with `enabled=false` is implicitly `ready=false`.
 */
export interface ChannelStatus {
  channel_id: string
  name: string
  enabled: boolean
  ready: boolean
}

/**
 * Summary of daemon state used by boot-time routing (e.g. whether to show
 * onboarding). `onboarding_required` is true iff the persona has never
 * been initialised through POST /api/admin/persona/onboarding.
 */
export interface DaemonState {
  persona: {
    id: string
    display_name: string
    voice_enabled: boolean
    has_voice_id: boolean
    has_avatar: boolean
  }
  onboarding_required: boolean
  memory_counts: {
    core_blocks: number
    messages: number
    events: number
    thoughts: number
  }
  channels: ChannelStatus[]
}

// ─── HTTP · GET /api/admin/persona ───────────────────────────────────────

/**
 * Full persona state for the Admin screen. `core_blocks` carries the
 * current L1 core block text for each of the five labels (persona /
 * self / user / relationship / style · v0.4). ``voice_id`` may be
 * null if no voice has been cloned/selected yet.
 */
export interface PersonaStateApi {
  id: string
  display_name: string
  voice_enabled: boolean
  voice_id: string | null
  has_avatar: boolean
  core_blocks: {
    persona: string
    self: string
    user: string
    relationship: string
    style: string
  }
  facts: PersonaFacts
}

// ─── Biographic facts (2026-04 persona-facts initiative) ────────────────

/**
 * Structured biographic facts that live alongside the five prose core
 * blocks on the persona row. Every field is optional; nulls mean "not
 * set / unknown". Enum-valued fields (gender, education_level,
 * relationship_status, life_stage, health_status) use the same vocabulary
 * the backend's LLM extractor emits — see
 * ``src/echovessel/prompts/persona_facts.py``.
 *
 * birth_date is carried on the wire as an ISO YYYY-MM-DD string; the
 * UI renders it with an <input type="date">. Year-only dates use
 * YYYY-01-01.
 */
export interface PersonaFacts {
  full_name: string | null
  gender: string | null // 'female' | 'male' | 'non_binary' | null
  birth_date: string | null // ISO YYYY-MM-DD
  ethnicity: string | null
  nationality: string | null // ISO 3166-1 alpha-2
  native_language: string | null // BCP 47
  locale_region: string | null
  education_level: string | null // 'high_school' | 'bachelor' | 'master' | 'phd'
  occupation: string | null
  occupation_field: string | null
  location: string | null
  timezone: string | null // IANA
  relationship_status: string | null // 'single' | 'married' | 'widowed' | 'divorced'
  life_stage: string | null // 'student' | 'working' | 'retired' | 'new_parent' | 'between_jobs'
  health_status: string | null // 'healthy' | 'chronic_illness' | 'recovering' | 'serious'
}

/** Blank facts object with every key explicitly null. */
export const EMPTY_PERSONA_FACTS: PersonaFacts = {
  full_name: null,
  gender: null,
  birth_date: null,
  ethnicity: null,
  nationality: null,
  native_language: null,
  locale_region: null,
  education_level: null,
  occupation: null,
  occupation_field: null,
  location: null,
  timezone: null,
  relationship_status: null,
  life_stage: null,
  health_status: null,
}

// ─── HTTP · POST /api/admin/persona/onboarding ───────────────────────────

/**
 * First-run onboarding payload. Creates the persona and writes the
 * initial values for the three prose core blocks (persona / self /
 * user). Rejected with 409 if already onboarded. Relationship and
 * style blocks are owner-curated later from the Admin Persona tab.
 */
export interface OnboardingPayload {
  display_name: string
  persona_block: string
  self_block: string
  user_block: string
  /** Optional biographic facts written to the persona row alongside
   *  the core blocks. Omit or send null to leave every fact unset. */
  facts?: PersonaFacts | null
}

export interface OnboardingResponse {
  ok: true
  persona_id: string
}

// ─── HTTP · POST /api/admin/persona/extract-from-input ───────────────────

/**
 * Body for the unified extraction endpoint. ``input_type`` picks the
 * path:
 *
 * - ``blank_write`` — user typed prose into the five block editors; we
 *   feed it back to the LLM to derive structured facts.
 * - ``import_upload`` — caller supplies ``upload_id`` (fresh pipeline)
 *   or ``pipeline_id`` (already running); we wait for done, then run
 *   extraction over the produced events + thoughts.
 */
export interface PersonaExtractRequest {
  input_type: 'blank_write' | 'import_upload'
  user_input?: string
  existing_blocks?: {
    persona_block?: string
    self_block?: string
    user_block?: string
    relationship_block?: string
  }
  locale?: string
  persona_display_name?: string
  upload_id?: string
  pipeline_id?: string
}

/** One imported L3 event row shown on the review page (path B only). */
export interface ExtractedEvent {
  description: string
  emotional_impact: number
  relational_tags: string[]
}

export interface PersonaExtractResponse {
  input_type: 'blank_write' | 'import_upload'
  core_blocks: {
    persona_block: string
    self_block: string
    user_block: string
    relationship_block: string
  }
  facts: PersonaFacts
  facts_confidence: number
  events: ExtractedEvent[]
  thoughts: string[]
  pipeline_status: string | null
}

// ─── HTTP · PATCH /api/admin/persona/facts ──────────────────────────────

/** Partial update — only the supplied keys are written; omitted keys
 *  keep their existing value, explicit nulls clear. */
export interface PersonaFactsUpdatePayload {
  facts: Partial<PersonaFacts>
}

export interface PersonaFactsUpdateResponse {
  ok: true
  facts: PersonaFacts
}

// ─── HTTP · POST /api/admin/persona/bootstrap-from-material ─────────────

/**
 * Worker κ · Body for the onboarding "upload material" flow.
 *
 * At least one of ``upload_id`` / ``pipeline_id`` MUST be supplied.
 * ``persona_display_name`` is an optional hint the LLM can weave into
 * the generated blocks — the ACTUAL display name is set later via
 * ``POST /api/admin/persona/onboarding``.
 */
export interface PersonaBootstrapRequest {
  upload_id?: string
  pipeline_id?: string
  persona_display_name?: string
}

/**
 * Response from bootstrap-from-material. ``suggested_blocks`` is the
 * LLM's four-block draft (persona / self / user / relationship),
 * presented to the user for review before they click "完成" (which
 * POSTs to /api/admin/persona/onboarding).
 *
 * Empty blocks are returned as empty strings, not omitted keys.
 */
export interface PersonaBootstrapResponse {
  suggested_blocks: {
    persona_block: string
    self_block: string
    user_block: string
    relationship_block: string
  }
  source_event_count: number
  source_thought_count: number
  pipeline_status: string
}

// ─── HTTP · POST /api/admin/persona ──────────────────────────────────────

/**
 * Partial persona update. Every field is optional; server applies only
 * the ones present. Unset fields are left untouched.
 */
export interface PersonaUpdatePayload {
  display_name?: string
  persona_block?: string
  self_block?: string
  user_block?: string
  relationship_block?: string
  style_block?: string
}

// ─── HTTP · POST /api/admin/persona/style ────────────────────────────────

/**
 * Three-action write path for the L1 style block (owner-directed
 * voice / style preferences, plan §6.6). ``set`` soft-deletes any
 * prior row and writes fresh; ``append`` joins prior content with a
 * newline; ``clear`` soft-deletes the row (``text`` is ignored).
 */
export interface StyleUpdatePayload {
  action: 'set' | 'append' | 'clear'
  text: string
}

export interface StyleUpdateResponse {
  ok: true
  action: 'set' | 'append' | 'clear'
}

// ─── HTTP · POST /api/admin/users/timezone ───────────────────────────────

/**
 * IANA timezone string for the local owner. Web channel posts this on
 * first connect from ``Intl.DateTimeFormat().resolvedOptions().timeZone``
 * so the daemon can render a dual-timezone ``# Right now`` section in
 * the system prompt. ``override: true`` flips the behaviour from "only
 * write if currently null" to "always overwrite" — used when the
 * owner manually picks a different zone from the Admin UI dropdown.
 */
export interface UsersTimezonePayload {
  timezone: string
  override?: boolean
}

export interface UsersTimezoneResponse {
  ok: true
  timezone: string | null
  written: boolean
}

// ─── HTTP · POST /api/admin/persona/voice-toggle ─────────────────────────

export interface VoiceTogglePayload {
  enabled: boolean
}

export interface VoiceToggleResponse {
  ok: true
  voice_enabled: boolean
}

// ─── HTTP · POST /api/chat/send ──────────────────────────────────────────

/**
 * Message to ingest into the daemon's turn loop. `user_id` is typically
 * "self" (the primary human user). `external_ref` is an optional
 * client-supplied correlation token — the server echoes it back on the
 * `chat.message.user_appended` SSE event so the UI can match send vs
 * receive if multiple tabs are open.
 */
export interface ChatSendPayload {
  content: string
  user_id: string
  external_ref?: string
}

// ─── HTTP · GET /api/chat/history ────────────────────────────────────────

/**
 * One L2 recall-message row surfaced by the history endpoint.
 *
 * The backend joins every channel into this single list (iron rule
 * D4 — persona has ONE shared timeline). `source_channel_id` is the
 * originating channel so the UI can render a decorative badge
 * (e.g. "📱 Discord") without the server having to partition rows.
 *
 * `turn_id` is nullable because pre-v0.3 rows predate the debounce
 * turn tagging; the frontend should tolerate null by grouping each
 * message as its own turn.
 */
export interface ChatHistoryMessage {
  id: number
  turn_id: string | null
  session_id: string
  source_channel_id: string
  role: 'user' | 'persona'
  content: string
  created_at: string | null
}

/**
 * Response from `GET /api/chat/history`.
 *
 * `oldest_turn_id` is the `turn_id` of the OLDEST message in the
 * returned window (= the last element, since the list is DESC by
 * `created_at`). Pass it back as `?before=<oldest_turn_id>` to fetch
 * the next-older page. Null when the window is empty.
 *
 * `has_more` is true when the server truncated to `limit` — the
 * frontend uses it to show the "↑ 加载更早的消息" button.
 */
export interface ChatHistoryResponse {
  messages: ChatHistoryMessage[]
  has_more: boolean
  oldest_turn_id: string | null
}

// ─── SSE · payload shapes ────────────────────────────────────────────────

export interface ChatConnectionReadyData {
  channel_id: string
}

/**
 * Heartbeat payload is empty in MVP; a server-side timestamp may be
 * added later, so we accept an optional string rather than `{}` (which
 * would be overly strict under `verbatimModuleSyntax`).
 */
export interface ChatConnectionHeartbeatData {
  timestamp?: string
}

export interface ChatMessageUserAppendedData {
  user_id: string
  content: string
  received_at: string
  external_ref: string | null
  /** Worker X · channel_id where this user message originated. Web
   *  (default) events carry "web"; Discord DMs carry "discord", etc.
   *  The frontend uses this to render a channel pill so the user can
   *  see at a glance which transport the message came in through. */
  source_channel_id?: string
}

/**
 * Emitted once per turn, right before the LLM stream starts. The
 * frontend inserts a placeholder persona bubble with streaming=true
 * and an empty content string so the UI can show a '正在输入...'
 * typing indicator. When ``chat.message.done`` arrives with the same
 * ``message_id``, the placeholder is replaced with the final content.
 */
export interface ChatMessageTypingStartedData {
  message_id: number
  turn_id: string
  /** Worker X · mirrors ``source_channel_id`` on the ``user_appended``
   *  event so multi-channel views tag the typing bubble correctly. */
  source_channel_id?: string
}

/**
 * `delivery` carries the chosen output modality: text-only or the single
 * neutral voice variant. Prosody tone variants (tender / whisper) are
 * deferred to v1.0 along with persona-selected delivery — they are not
 * part of the MVP wire format.
 */
export type MessageDelivery = 'text' | 'voice_neutral'

export interface ChatMessageDoneData {
  message_id: number
  content: string
  in_reply_to_turn_id: string | null
  delivery: MessageDelivery
  /** Worker X · same ``source_channel_id`` that tagged
   *  ``user_appended`` / ``token`` for this turn. Frontend reuses it
   *  to key the pill on the persona-reply bubble. */
  source_channel_id?: string
}

export interface ChatSettingsUpdatedData {
  voice_enabled: boolean
}

/**
 * Emitted by `RuntimeMemoryObserver.on_session_closed` and
 * `on_new_session_started` — one broadcast per lifecycle hook, so a
 * closed-then-reopened session surfaces as two separate boundary
 * events (`closed_session_id` set on the first, `new_session_id` set
 * on the second). The Web chat timeline uses these to draw a thin
 * session divider with a relative timestamp.
 */
export interface ChatSessionBoundaryData {
  closed_session_id: string | null
  new_session_id: string | null
  persona_id: string
  user_id: string
  /** ISO-8601 UTC timestamp written by the runtime observer. */
  at: string
}

/**
 * Emitted by `RuntimeMemoryObserver.on_mood_updated` whenever memory's
 * `update_episodic_state` fires its lifecycle hook (e.g. after the
 * consolidate worker closes a session and the extraction LLM returned
 * a ``session_mood_signal``). ``mood_summary`` is the ``repr()`` of
 * the new L6 state dict (``{mood, energy, last_user_signal,
 * updated_at}``); consumers should treat it as opaque display text
 * and pull structured fields from ``GET /api/admin/persona`` when
 * needed.
 */
export interface ChatMoodUpdateData {
  persona_id: string
  user_id: string
  mood_summary: string
}

// ─── Spec 3 · Memory Timeline (per-write SSE) ────────────────────────────

/**
 * Emitted by ``RuntimeMemoryObserver.on_event_created`` right after a
 * new L3 event row commits (both consolidate and import paths fire).
 * Drives the Memory Timeline sidebar's "记住了…" row.
 */
export interface MemoryEventCreatedData {
  event_id: number
  persona_id: string
  user_id: string
  description: string
  emotional_impact: number
  session_id: string | null
  created_at: string | null
}

/**
 * Emitted by ``RuntimeMemoryObserver.on_thought_created`` right after a
 * new L4 thought / intention / expectation row commits. ``source`` is
 * ``'reflection'`` | ``'slow_tick'`` | ``'import'`` and lets the UI
 * pick the icon (🧠 for slow_tick parallel-existing, 💭 for
 * reflection/summary).
 */
export interface MemoryThoughtCreatedData {
  thought_id: number
  persona_id: string
  user_id: string
  type: 'thought' | 'intention' | 'expectation'
  subject: string
  description: string
  source: 'reflection' | 'slow_tick' | 'import'
  session_id: string | null
  filling_event_ids: number[]
  created_at: string | null
}

/**
 * Emitted by ``RuntimeMemoryObserver.on_entity_confirmed`` when a new
 * L5 entity is resolved or an existing one alias-merges. Uncertain
 * entities (``merge_status='uncertain'``) are filtered server-side and
 * never reach this event.
 */
export interface MemoryEntityConfirmedData {
  entity_id: number
  persona_id: string
  user_id: string
  canonical_name: string
  kind: string // 'person' | 'place' | 'org' | 'pet' | 'other'
  merge_status: 'confirmed' | 'disambiguated'
  created_at: string | null
}

/**
 * Emitted by ``RuntimeMemoryObserver.on_entity_description_updated``
 * when slow_tick synthesizes a description for an entity, or when the
 * admin owner-overrides one. ``source`` distinguishes the two paths.
 */
export interface MemoryEntityDescriptionUpdatedData {
  entity_id: number
  persona_id: string
  user_id: string
  canonical_name: string
  kind: string
  description: string
  source: 'slow_tick' | 'owner'
  updated_at: string | null
}

/**
 * Emitted when the voice TTS pipeline finishes generating audio for a
 * message. Not yet emitted by the backend in Stage 2 — the hook branch
 * is stubbed in for Stage 7.
 */
export interface ChatMessageVoiceReadyData {
  message_id: number
  url: string
  duration_seconds: number
  cached: boolean
  /** Worker X · source channel for the voice_ready event. */
  source_channel_id?: string
}

export interface ChatMessageErrorData {
  message_id: number | null
  error: string
}

// ─── SSE · discriminated union ───────────────────────────────────────────

/**
 * All known event variants from `GET /api/chat/events`.
 *
 * `event` is the SSE event name (the string after `event:` in the wire
 * format). `data` is the parsed JSON body. Consumers should switch on
 * `event` for exhaustive handling.
 *
 * Unknown events are NOT represented here — the useSSE hook logs and
 * drops them rather than forwarding to listeners.
 */
export type ChatEvent =
  | { event: 'chat.connection.ready'; data: ChatConnectionReadyData }
  | { event: 'chat.connection.heartbeat'; data: ChatConnectionHeartbeatData }
  | { event: 'chat.message.user_appended'; data: ChatMessageUserAppendedData }
  | { event: 'chat.message.typing_started'; data: ChatMessageTypingStartedData }
  | { event: 'chat.message.done'; data: ChatMessageDoneData }
  | { event: 'chat.settings.updated'; data: ChatSettingsUpdatedData }
  | { event: 'chat.session.boundary'; data: ChatSessionBoundaryData }
  | { event: 'chat.mood.update'; data: ChatMoodUpdateData }
  | { event: 'chat.message.voice_ready'; data: ChatMessageVoiceReadyData }
  | { event: 'chat.message.error'; data: ChatMessageErrorData }
  | { event: 'memory.event.created'; data: MemoryEventCreatedData }
  | { event: 'memory.thought.created'; data: MemoryThoughtCreatedData }
  | { event: 'memory.entity.confirmed'; data: MemoryEntityConfirmedData }
  | {
      event: 'memory.entity.description_updated'
      data: MemoryEntityDescriptionUpdatedData
    }

/**
 * List of SSE event names the useSSE hook must register listeners for.
 * Kept in sync with the ChatEvent union. If a new event is added above,
 * add it here too so EventSource.addEventListener picks it up.
 */
export const KNOWN_CHAT_EVENT_NAMES: readonly ChatEvent['event'][] = [
  'chat.connection.ready',
  'chat.connection.heartbeat',
  'chat.message.user_appended',
  'chat.message.typing_started',
  'chat.message.done',
  'chat.settings.updated',
  'chat.session.boundary',
  'chat.mood.update',
  'chat.message.voice_ready',
  'chat.message.error',
  'memory.event.created',
  'memory.thought.created',
  'memory.entity.confirmed',
  'memory.entity.description_updated',
] as const

// ─── HTTP · GET /api/admin/memory/{events,thoughts} ─────────────────────

/**
 * Single L3 event row as returned by the admin Events tab.
 *
 * Field names mirror the SQLModel ConceptNode columns 1:1 — the
 * backend's ``_serialize_concept_node`` helper in admin.py emits
 * exactly this shape. ``node_type`` is the discriminator and is the
 * literal string ``"event"`` for every item in this list response.
 */
export interface MemoryEvent {
  id: number
  node_type: 'event'
  description: string
  emotional_impact: number
  emotion_tags: string[]
  relational_tags: string[]
  source_session_id: string | null
  source_turn_id: string | null
  imported_from: string | null
  source_deleted: boolean
  created_at: string | null
  access_count: number
}

/**
 * Single L4 thought row as returned by the admin Thoughts tab.
 *
 * Same DB columns as :type:`MemoryEvent`; the only literal-type
 * difference is the discriminator. The split into two interfaces
 * exists so callers don't accidentally render a thought in the
 * Events tab or vice versa.
 */
export interface MemoryThought {
  id: number
  node_type: 'thought'
  description: string
  emotional_impact: number
  emotion_tags: string[]
  relational_tags: string[]
  source_session_id: string | null
  source_turn_id: string | null
  imported_from: string | null
  source_deleted: boolean
  created_at: string | null
  access_count: number
}

/**
 * Generic envelope for both list endpoints. ``T`` is one of
 * :type:`MemoryEvent` / :type:`MemoryThought`. The backend includes
 * the limit/offset it applied so the client can confirm the cap was
 * honoured before computing "load more" cursors.
 */
export interface MemoryListResponse<T> {
  node_type: 'event' | 'thought'
  limit: number
  offset: number
  total: number
  items: T[]
}

/**
 * Compact concept-node shape returned inside the provenance trace
 * responses. Narrower than :type:`MemoryEvent` / :type:`MemoryThought`
 * because the trace UI only needs id + description + date + source
 * session to render an inline-expand row.
 */
export interface TraceNode {
  id: number
  description: string
  created_at: string | null
  source_session_id: string | null
}

/** Response from ``GET /api/admin/memory/thoughts/{id}/trace``. */
export interface ThoughtTraceResponse {
  thought_id: number
  source_events: TraceNode[]
  source_sessions: string[]
}

/** Response from ``GET /api/admin/memory/events/{id}/dependents``. */
export interface EventDependentsResponse {
  event_id: number
  dependent_thoughts: TraceNode[]
}

/**
 * Response from POST /api/admin/memory/preview-delete. Used to
 * decide whether to show the "keep / cascade / cancel" dialog before
 * issuing the DELETE.
 */
export interface PreviewDeleteResponse {
  target_id: number
  dependent_thought_ids: number[]
  dependent_thought_descriptions: string[]
  has_dependents: boolean
}

/** Server-side soft-delete cascade choice for concept nodes. */
export type DeleteChoice = 'orphan' | 'cascade'

export interface DeleteResponse {
  deleted: true
  node_id: number
  choice: DeleteChoice
}

// ─── HTTP · GET /api/admin/memory/timeline (Spec 3) ─────────────────────

/**
 * One row in the backfill response. ``kind`` is the discriminator that
 * the Chat Memory Timeline sidebar switches on to pick which sub-component
 * renders (event vs. thought vs. entity vs. mood vs. session_close).
 *
 * ``timestamp`` is ISO-8601 (the canonical created_at / updated_at /
 * extracted_at for that kind, picked server-side). Items are always
 * sorted newest-first on the wire; the client re-sorts after merging
 * with live SSE events.
 */
export type MemoryTimelineItemKind =
  | 'event'
  | 'thought'
  | 'entity'
  | 'entity_description'
  | 'mood'
  | 'session_close'

export interface MemoryTimelineItem {
  kind: MemoryTimelineItemKind
  timestamp: string
  data: Record<string, unknown>
}

export interface MemoryTimelineResponse {
  items: MemoryTimelineItem[]
  oldest_timestamp: string | null
  limit: number
}

// ─── HTTP · GET /api/admin/memory/search ────────────────────────────────

/** ``type`` discriminator on the search route. */
export type MemorySearchType = 'events' | 'thoughts' | 'all'

/**
 * One ``{node_id, snippet}`` row in the search response. ``snippet``
 * is server-rendered HTML containing only ``<b>…</b>`` tags around
 * matched terms — see :func:`stripUnsafeTags` for the client-side
 * sanitiser used before injecting via ``dangerouslySetInnerHTML``.
 */
export interface MemorySearchSnippet {
  node_id: number
  snippet: string
}

/**
 * Response from ``GET /api/admin/memory/search``. ``items`` is a
 * heterogeneous list of :type:`MemoryEvent` / :type:`MemoryThought`
 * when ``type=all``; the discriminator is each item's own
 * ``node_type`` field.
 */
export interface MemorySearchResponse {
  q: string
  type: MemorySearchType
  tag: string | null
  limit: number
  offset: number
  total: number
  items: (MemoryEvent | MemoryThought)[]
  matched_snippets: MemorySearchSnippet[]
}

// ─── HTTP · GET /api/admin/cost/* ───────────────────────────────────────

/** Five canonical feature labels recorded by the cost logger.
 *  Mirror of ``echovessel.runtime.cost_logger.Feature`` Literal. */
export type CostFeature =
  | 'chat'
  | 'import'
  | 'consolidate'
  | 'reflection'
  | 'proactive'
  | 'unknown'

/** Per-feature aggregated bucket inside ``CostSummaryResponse.by_feature``. */
export interface CostFeatureBucket {
  calls: number
  tokens_in: number
  tokens_out: number
  cost_usd: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}

/** One day's roll-up inside ``CostSummaryResponse.by_day``. */
export interface CostDayBucket {
  date: string  // YYYY-MM-DD
  usd: number
  tokens: number
  calls: number
}

/** Response from ``GET /api/admin/cost/summary?range=today|7d|30d``. */
export interface CostSummaryResponse {
  range: 'today' | '7d' | '30d'
  since: string  // ISO 8601
  total_usd: number
  total_tokens: number
  total_tokens_in: number
  total_tokens_out: number
  total_cache_read_input_tokens: number
  total_cache_creation_input_tokens: number
  by_feature: Record<string, CostFeatureBucket>
  by_day: CostDayBucket[]
}

/** Single LLM call row returned by ``GET /api/admin/cost/recent``. */
export interface CostCallRecord {
  id: number
  timestamp: string  // ISO 8601
  provider: string
  model: string
  feature: string
  tier: string
  tokens_in: number
  tokens_out: number
  cost_usd: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  turn_id: string | null
}

export interface CostRecentResponse {
  limit: number
  items: CostCallRecord[]
}

// ─── HTTP · POST /api/admin/import/* ─────────────────────────────────────

/**
 * Payload for the JSON-paste upload path (POST /api/admin/import/upload_text).
 * The multipart file path (POST /api/admin/import/upload) takes a
 * `file` field and is not modelled here because the MVP frontend reads
 * every file into a string and posts through the JSON path.
 */
export interface ImportUploadTextPayload {
  text: string
  source_label?: string
}

/**
 * Response from POST /api/admin/import/upload_text (and /upload).
 * `suffix` includes the leading "." or is empty; `file_hash` is a
 * SHA-256 hex digest used later for duplicate detection.
 *
 * Note: there is intentionally no `total_chunks` field here — chunking
 * runs during `pipeline.start`, not at upload time. The first
 * `pipeline.start` SSE frame is the authoritative total.
 */
export interface ImportUploadResponse {
  upload_id: string
  file_hash: string
  suffix: string
  source_label: string
  size_bytes: number
}

export interface ImportEstimatePayload {
  upload_id: string
  /** MVP backend only knows "llm"; field is optional to match the default. */
  stage?: string
}

/**
 * Response from POST /api/admin/import/estimate. Field names match the
 * backend exactly (`tokens_in` / `tokens_out_est` / `cost_usd_est`) —
 * the earlier Worker F guess was wrong.
 *
 * `note` is populated on binary uploads where LLM extraction will be
 * skipped ("upload is binary; LLM extraction skipped until the
 * pipeline decodes it").
 */
export interface ImportEstimateResponse {
  tokens_in: number
  tokens_out_est: number
  cost_usd_est: number
  note?: string
}

export interface ImportStartPayload {
  upload_id: string
  force_duplicate?: boolean
}

export interface ImportStartResponse {
  pipeline_id: string
}

export interface ImportCancelPayload {
  pipeline_id: string
}

export interface ImportCancelResponse {
  status: string
}

// ─── SSE · GET /api/admin/import/events?pipeline_id=... ──────────────────

/**
 * Every import SSE frame carries the event name `import.progress` and
 * multiplexes the true kind in `data.type`. The union below lists the
 * `type` values the current pipeline emits; unknown types are logged
 * and dropped client-side.
 *
 * Payload shapes vary by type — we keep `payload` loosely typed and let
 * the consumer pick the fields it needs. Shapes per backend
 * (`echovessel.import_.pipeline`):
 *
 *   pipeline.registered  {upload_id}
 *   pipeline.start       {total_chunks, source_label, resume_from}
 *   pipeline.cancelled   {}
 *   pipeline.resumed     {resume_from}
 *   pipeline.done        {status, processed_chunks, total_chunks,
 *                         writes_by_target, dropped_count,
 *                         embedded_vector_count, error}
 *   chunk.start          {chunk_index, total_chunks, chars_in_chunk}
 *   chunk.done           {chunk_index, writes_count, dropped_in_chunk,
 *                         summary: {content_type: count}}
 *   chunk.error          {chunk_index?, fatal, error, stage}
 */
export type ImportFrameType =
  | 'pipeline.registered'
  | 'pipeline.start'
  | 'pipeline.cancelled'
  | 'pipeline.resumed'
  | 'pipeline.done'
  | 'chunk.start'
  | 'chunk.done'
  | 'chunk.error'

export interface ImportFrame {
  pipeline_id: string
  type: ImportFrameType | string
  payload: Record<string, unknown>
}

export type ImportPipelineStatus =
  | 'success'
  | 'partial_success'
  | 'failed'
  | 'cancelled'

/** The UI's view of a finished pipeline — extracted from the
 *  `pipeline.done` frame payload. */
export interface ImportDoneSummary {
  status: ImportPipelineStatus
  processed_chunks: number
  total_chunks: number
  writes_by_target: Record<string, number>
  dropped_count: number
  embedded_vector_count: number
  error: string
}

/** The UI's view of the running pipeline — accumulated across
 *  `pipeline.start` + `chunk.start` / `chunk.done`. */
export interface ImportProgressSnapshot {
  current_chunk: number
  total_chunks: number
}

// ─── HTTP · GET/PATCH /api/admin/config ──────────────────────────────────

/**
 * Safe subset of the daemon's live config returned by
 * GET /api/admin/config. Secrets are NEVER included — the LLM section
 * only reports `api_key_present: boolean` to let the UI render a
 * "🟢 key loaded" / "🔴 missing" status dot without ever shipping the
 * key material to the browser.
 */
export interface ConfigLlmSection {
  provider: string
  model: string | null
  api_key_env: string
  timeout_seconds: number
  temperature: number
  max_tokens: number
  api_key_present: boolean
}

export interface ConfigPersonaSection {
  display_name: string
  voice_enabled: boolean
  voice_id: string | null
}

export interface ConfigMemorySection {
  retrieve_k: number
  relational_bonus_weight: number
  recent_window_size: number
}

export interface ConfigConsolidateSection {
  trivial_message_count: number
  trivial_token_count: number
  reflection_hard_gate_24h: number
}

export interface ConfigSystemSection {
  data_dir: string
  db_path: string
  version: string
  uptime_seconds: number
  db_size_bytes: number
  config_path: string | null
}

/** Shared live-state fields every channel block carries. */
export interface ChannelLiveStatus {
  /** Registered in the runtime channel registry. Implies `enabled=true`
   *  in the loaded config and that construction succeeded. */
  registered: boolean
  /** Transport is currently usable (gateway handshake complete, etc.). */
  ready: boolean
}

export interface ConfigChannelWeb extends ChannelLiveStatus {
  enabled: boolean
  channel_id: string
  host: string
  port: number
  static_dir: string
  debounce_ms: number
}

export interface ConfigChannelDiscord extends ChannelLiveStatus {
  enabled: boolean
  channel_id: string
  token_env: string
  /** Presence-only — never carries the actual token. */
  token_loaded: boolean
  allowed_user_ids: number[]
  debounce_ms: number
}

export interface ConfigChannelIMessage extends ChannelLiveStatus {
  enabled: boolean
  channel_id: string
  persona_apple_id: string
  cli_path: string
  db_path: string
  allowed_handles: string[]
  default_service: 'imessage' | 'sms' | 'auto'
  region: string
  debounce_ms: number
}

export interface ConfigChannelsSection {
  web: ConfigChannelWeb
  discord: ConfigChannelDiscord
  imessage: ConfigChannelIMessage
}

export interface ConfigGetResponse {
  llm: ConfigLlmSection
  persona: ConfigPersonaSection
  memory: ConfigMemorySection
  consolidate: ConfigConsolidateSection
  system: ConfigSystemSection
  channels: ConfigChannelsSection
}

/**
 * PATCH payload is a nested `{section: {field: value}}` dict. All
 * fields optional — server applies only what's present. Sending a
 * restart-required key (e.g. `memory.db_path`) yields 400; invalid
 * values (e.g. `llm.temperature: 5.0`) yield 422.
 */
export interface ConfigPatchPayload {
  llm?: Partial<{
    provider: string
    model: string
    api_key_env: string
    timeout_seconds: number
    temperature: number
    max_tokens: number
  }>
  persona?: Partial<{
    display_name: string
  }>
  memory?: Partial<{
    retrieve_k: number
    relational_bonus_weight: number
    recent_window_size: number
  }>
  consolidate?: Partial<{
    trivial_message_count: number
    trivial_token_count: number
    reflection_hard_gate_24h: number
  }>
}

/**
 * Body for ``PATCH /api/admin/channels``. Unlike the main config patch
 * route, channel fields are **not** hot-reloaded — the daemon must be
 * restarted before the new values take effect. Every field in
 * ``ConfigChannel*`` EXCEPT the live-state bools is patchable here.
 */
export interface ChannelsPatchPayload {
  web?: Partial<{
    enabled: boolean
    host: string
    port: number
    static_dir: string
    debounce_ms: number
  }>
  discord?: Partial<{
    enabled: boolean
    token_env: string
    allowed_user_ids: number[]
    debounce_ms: number
  }>
  imessage?: Partial<{
    enabled: boolean
    persona_apple_id: string
    cli_path: string
    db_path: string
    allowed_handles: string[]
    default_service: 'imessage' | 'sms' | 'auto'
    region: string
    debounce_ms: number
  }>
}

export interface ChannelsPatchResponse {
  updated_fields: string[]
  reload_triggered: false
  restart_required: true
}

export interface ConfigPatchResponse {
  updated_fields: string[]
  reload_triggered: boolean
  restart_required: string[]
}

// ─── Voice clone wizard (Worker λ) ──────────────────────────────────────

/**
 * One draft voice sample uploaded during the clone wizard.
 * `duration_seconds` is nullable in MVP because the backend does not
 * probe audio duration yet — the UI renders "—" when absent.
 */
export interface VoiceSample {
  sample_id: string
  filename: string
  size_bytes: number
  duration_seconds: number | null
  created_at: string
}

export interface VoiceSampleListResponse {
  samples: VoiceSample[]
  count: number
  minimum_required: number
}

export interface VoiceSampleUploadResponse {
  sample_id: string
  duration_seconds: number | null
  size_bytes: number
  accepted: true
}

export interface VoiceCloneResponse {
  voice_id: string
  display_name: string
  preview_text: string
  /** Null when the provider succeeded at clone but preview TTS failed. */
  preview_audio_url: string | null
}

export interface VoiceActivateResponse {
  activated: true
  voice_id: string
}

// ─── Error class ─────────────────────────────────────────────────────────

/**
 * Thrown by the API client on any non-2xx response from the daemon.
 * `status` is the HTTP status code; `detail` is the `detail` field from
 * the server's JSON body (FastAPI convention), falling back to the
 * response status text if the body could not be parsed.
 */
export class ApiError extends Error {
  public readonly status: number
  public readonly detail: string

  constructor(status: number, detail: string) {
    super(`[${status}] ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

// ─── Failed sessions (observability) ─────────────────────────────────────

/**
 * One row of `GET /api/admin/sessions/failed`. Surfaces consolidate-worker
 * FAILED sessions to the admin UI so silent data loss becomes visible.
 */
export interface FailedSession {
  id: string
  channel_id: string
  user_id: string
  message_count: number
  started_at: string | null
  close_trigger: string | null
}

export interface FailedSessionsResponse {
  count: number
  items: FailedSession[]
}

// ─── Dev-mode trace (Spec 4) ─────────────────────────────────────────────

/** One row on the per-turn waterfall timeline. */
export interface TurnTraceStep {
  stage: string
  t_ms: number
  duration_ms: number
  detail: Record<string, unknown>
}

/** One ``ScoredMemory`` retrieval candidate. Keys mirror the backend
 *  ``ScoredMemory`` dataclass — the drawer's VectorRetrieveTable reads
 *  these field names directly. */
export interface TurnTraceRetrievalRow {
  node_id: number | null
  type: string | null
  desc_snippet: string
  recency: number
  relevance: number
  impact: number
  relational: number
  entity_anchor: number
  total: number
  anchored: boolean
}

export interface TurnTraceEntityHit {
  entity_id: number
  canonical_name: string
  kind: string
}

export interface TurnTracePinnedThoughts {
  user: Array<{ id: number; description: string }>
  persona: Array<{ id: number; description: string }>
}

export interface TurnTraceListItem {
  turn_id: string
  persona_id: string
  user_id: string
  channel_id: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  first_token_ms: number | null
  input_tokens: number | null
  output_tokens: number | null
  llm_model: string | null
}

export interface TurnTraceResponse extends TurnTraceListItem {
  system_prompt: string | null
  user_prompt: string | null
  retrieval: TurnTraceRetrievalRow[]
  pinned_thoughts: Partial<TurnTracePinnedThoughts>
  entity_alias_hits: TurnTraceEntityHit[]
  episodic_state: Record<string, unknown> | null
  steps: TurnTraceStep[]
}

export interface TurnTraceListResponse {
  items: TurnTraceListItem[]
  limit: number
}

export interface ConsolidateTraceResponse {
  session_id: string
  finished_at: string | null
  phase_a: Record<string, unknown> | null
  phase_b: Record<string, unknown> | null
  phase_c: Record<string, unknown> | null
  phase_d: Record<string, unknown> | null
  phase_e: Record<string, unknown> | null
  phase_f: Record<string, unknown> | null
  phase_g: Record<string, unknown> | null
}
