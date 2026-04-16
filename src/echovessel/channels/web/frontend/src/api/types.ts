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
 * current L1 core block text for each of the five labels. `voice_id` may
 * be null if no voice has been cloned/selected yet.
 */
export interface PersonaStateApi {
  id: string
  display_name: string
  voice_enabled: boolean
  voice_id: string | null
  core_blocks: {
    persona: string
    self: string
    user: string
    mood: string
    relationship: string
  }
}

// ─── HTTP · POST /api/admin/persona/onboarding ───────────────────────────

/**
 * First-run onboarding payload. Creates the persona and writes the
 * initial values for the four core blocks. Rejected with 409 if already
 * onboarded.
 */
export interface OnboardingPayload {
  display_name: string
  persona_block: string
  self_block: string
  user_block: string
  mood_block: string
}

export interface OnboardingResponse {
  ok: true
  persona_id: string
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
  mood_block?: string
  relationship_block?: string
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
}

export interface ChatMessageTokenData {
  message_id: number
  delta: string
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
 * `update_mood_block` fires its lifecycle hook (e.g. after the
 * consolidate worker reflects on a closed session). `mood_summary`
 * carries the full new mood block text — despite the name, it is not
 * pre-shrunk server-side today. Consumers should treat it as "current
 * mood block text" and render / truncate as they see fit.
 */
export interface ChatMoodUpdateData {
  persona_id: string
  user_id: string
  mood_summary: string
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
  | { event: 'chat.message.token'; data: ChatMessageTokenData }
  | { event: 'chat.message.done'; data: ChatMessageDoneData }
  | { event: 'chat.settings.updated'; data: ChatSettingsUpdatedData }
  | { event: 'chat.session.boundary'; data: ChatSessionBoundaryData }
  | { event: 'chat.mood.update'; data: ChatMoodUpdateData }
  | { event: 'chat.message.voice_ready'; data: ChatMessageVoiceReadyData }
  | { event: 'chat.message.error'; data: ChatMessageErrorData }

/**
 * List of SSE event names the useSSE hook must register listeners for.
 * Kept in sync with the ChatEvent union. If a new event is added above,
 * add it here too so EventSource.addEventListener picks it up.
 */
export const KNOWN_CHAT_EVENT_NAMES: readonly ChatEvent['event'][] = [
  'chat.connection.ready',
  'chat.connection.heartbeat',
  'chat.message.user_appended',
  'chat.message.token',
  'chat.message.done',
  'chat.settings.updated',
  'chat.session.boundary',
  'chat.mood.update',
  'chat.message.voice_ready',
  'chat.message.error',
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
