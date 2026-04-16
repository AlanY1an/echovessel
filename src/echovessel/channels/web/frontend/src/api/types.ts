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
  'chat.message.voice_ready',
  'chat.message.error',
] as const

// ─── HTTP · POST /api/admin/import/* ─────────────────────────────────────

/**
 * Client-side file/paste submission to kick off an import. `source_label`
 * is a human-readable name shown back in the UI and persisted on the
 * pipeline report (e.g. file name, or "粘贴 2026-04-16"). `content` is
 * raw UTF-8 text — MVP only handles text; audio transcription + binary
 * file parsing is deferred.
 */
export interface ImportUploadPayload {
  source_label: string
  content: string
}

/**
 * Response from POST /api/admin/import/upload. The `upload_id` is the
 * opaque handle the frontend passes to /estimate and /start — the raw
 * bytes stay server-side. `total_chunks` is an up-front estimate based
 * on byte size; the authoritative count comes back from /estimate.
 */
export interface ImportUploadResponse {
  upload_id: string
  source_label: string
  size_bytes: number
  total_chunks: number
}

export interface ImportEstimatePayload {
  upload_id: string
}

/**
 * Response from POST /api/admin/import/estimate. Shown to the user as a
 * "这次导入会用 ≈X tokens · $Y" confirmation card before /start fires.
 */
export interface ImportEstimateResponse {
  upload_id: string
  total_chunks: number
  estimated_input_tokens: number
  estimated_output_tokens: number
  estimated_cost_usd: number
  model: string
}

export interface ImportStartPayload {
  upload_id: string
}

export interface ImportStartResponse {
  pipeline_id: string
}

export interface ImportCancelPayload {
  pipeline_id: string
}

export interface ImportCancelResponse {
  ok: true
  pipeline_id: string
}

// ─── SSE · /api/admin/import/events ──────────────────────────────────────

/**
 * Lifecycle shape for every SSE frame on the import stream. A single
 * pipeline run emits:
 *
 *   progress × N chunks (possibly with write / dropped interleaved)
 *   → done (on success / partial_success / cancelled)
 *   OR
 *   → error (on unrecoverable failure)
 */
export type ImportPipelineState =
  | 'running'
  | 'paused'
  | 'cancelled'
  | 'done'
  | 'failed'

export interface ImportProgressData {
  pipeline_id: string
  current_chunk: number
  total_chunks: number
  state: ImportPipelineState
}

/**
 * Emitted when a single LLM-output write lands in memory. Frontend uses
 * these to accumulate a live "写入" tally per content_type without
 * waiting for the final done payload.
 */
export interface ImportWriteData {
  pipeline_id: string
  chunk_index: number
  content_type: string
}

export interface ImportDroppedData {
  pipeline_id: string
  chunk_index: number
  reason: string
  raw_target: string
  payload_excerpt: string
}

/**
 * Final summary payload. Shape mirrors
 * `echovessel.import_.models.PipelineReport` but collapses the long
 * concept-node-id lists down to counts, which is all the UI renders.
 */
export type ImportFinalStatus =
  | 'success'
  | 'partial_success'
  | 'failed'
  | 'cancelled'

export interface ImportDoneData {
  pipeline_id: string
  source_label: string
  status: ImportFinalStatus
  total_chunks: number
  processed_chunks: number
  writes_by_target: Record<string, number>
  dropped_count: number
  error_message: string
}

export interface ImportErrorData {
  pipeline_id: string
  error: string
}

export type ImportEvent =
  | { event: 'import.progress'; data: ImportProgressData }
  | { event: 'import.write'; data: ImportWriteData }
  | { event: 'import.dropped'; data: ImportDroppedData }
  | { event: 'import.done'; data: ImportDoneData }
  | { event: 'import.error'; data: ImportErrorData }

export const KNOWN_IMPORT_EVENT_NAMES: readonly ImportEvent['event'][] = [
  'import.progress',
  'import.write',
  'import.dropped',
  'import.done',
  'import.error',
] as const

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
