/**
 * API client — thin typed fetch wrapper for the EchoVessel daemon HTTP API.
 *
 * Usage:
 *
 *   import { getState, postChatSend } from '../api/client'
 *   const state = await getState()
 *   await postChatSend({ content: 'hi', user_id: 'self' })
 *
 * In dev mode `vite.config.ts` proxies `/api/*` to `http://localhost:7777`
 * (the daemon). In production the frontend is served by the daemon at the
 * same origin, so the relative `/api/*` URLs just work.
 *
 * All non-2xx responses are translated into `ApiError(status, detail)` and
 * thrown. Network failures (DNS, offline, aborted) propagate as the
 * underlying `TypeError` from `fetch`.
 */

import type {
  ChannelsPatchPayload,
  ChannelsPatchResponse,
  ChatHistoryResponse,
  ChatSendPayload,
  ConfigGetResponse,
  ConsolidateTraceResponse,
  ConfigPatchPayload,
  ConfigPatchResponse,
  CostRecentResponse,
  CostSummaryResponse,
  DaemonState,
  DeleteChoice,
  DeleteResponse,
  EventDependentsResponse,
  FailedSessionsResponse,
  ImportCancelPayload,
  ImportCancelResponse,
  ImportEstimatePayload,
  ImportEstimateResponse,
  ImportStartPayload,
  ImportStartResponse,
  ImportUploadResponse,
  ImportUploadTextPayload,
  MemoryEvent,
  MemoryListResponse,
  MemorySearchResponse,
  MemorySearchType,
  MemoryThought,
  MemoryTimelineResponse,
  OnboardingPayload,
  OnboardingResponse,
  PersonaBootstrapRequest,
  PersonaBootstrapResponse,
  PersonaExtractRequest,
  PersonaExtractResponse,
  PersonaFactsUpdatePayload,
  PersonaFactsUpdateResponse,
  PersonaStateApi,
  PersonaUpdatePayload,
  PreviewDeleteResponse,
  StyleUpdatePayload,
  StyleUpdateResponse,
  ThoughtTraceResponse,
  TurnTraceListResponse,
  TurnTraceResponse,
  UsersTimezonePayload,
  UsersTimezoneResponse,
  VoiceActivateResponse,
  VoiceCloneResponse,
  VoiceSampleListResponse,
  VoiceSampleUploadResponse,
  VoiceToggleResponse,
} from './types'
import { ApiError } from './types'

// ─── Internals ───────────────────────────────────────────────────────────

interface ServerErrorBody {
  detail?: string
}

/**
 * Extract a human-readable detail from a non-2xx response. FastAPI emits
 * `{ "detail": "..." }` by default. If the body is not JSON or has no
 * `detail` key, fall back to the HTTP status text.
 */
async function extractDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ServerErrorBody
    if (body && typeof body.detail === 'string') {
      return body.detail
    }
  } catch {
    // Body is not JSON or is empty — fall through.
  }
  return response.statusText || `HTTP ${response.status}`
}

/**
 * Bare fetch helper that parses JSON and throws `ApiError` on non-2xx.
 * Treats HTTP 202 as success (used by chat send — the daemon acknowledges
 * ingest before the turn loop completes).
 */
async function fetchJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options?.headers ?? {}),
    },
  })

  if (!response.ok) {
    const detail = await extractDetail(response)
    throw new ApiError(response.status, detail)
  }

  // 204 No Content — return undefined cast to T.
  if (response.status === 204) {
    return undefined as T
  }

  return (await response.json()) as T
}

// ─── Typed endpoint functions ────────────────────────────────────────────

/**
 * GET /api/state — boot-time daemon snapshot. Used by App.tsx to decide
 * whether to render the onboarding screen.
 */
export async function getState(): Promise<DaemonState> {
  return fetchJson<DaemonState>('/api/state')
}

/**
 * GET /api/admin/persona — full persona state for the Admin screen.
 */
export async function getPersona(): Promise<PersonaStateApi> {
  return fetchJson<PersonaStateApi>('/api/admin/persona')
}

/**
 * POST /api/admin/persona/onboarding — first-run persona creation.
 * Throws ApiError(409, detail) if the persona already exists.
 */
export async function postOnboarding(
  payload: OnboardingPayload,
): Promise<OnboardingResponse> {
  return fetchJson<OnboardingResponse>('/api/admin/persona/onboarding', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/persona/bootstrap-from-material — Worker κ.
 *
 * Wait for an import pipeline to finish (started inline via
 * `upload_id` or already running via `pipeline_id`) and ask the LLM
 * for five suggested core blocks. Blocking: can take tens of seconds
 * for a large material; the returned blocks are DRAFTS that the user
 * should edit + commit via `postOnboarding`.
 *
 * The backend imposes a 10-minute server-side timeout on the pipeline
 * wait; if you want a shorter UX timeout, add `AbortController` logic
 * client-side.
 */
export async function postPersonaBootstrapFromMaterial(
  payload: PersonaBootstrapRequest,
): Promise<PersonaBootstrapResponse> {
  return fetchJson<PersonaBootstrapResponse>(
    '/api/admin/persona/bootstrap-from-material',
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
}

/**
 * POST /api/admin/persona — partial persona update. Every field in
 * payload is optional; the server applies only the ones present.
 */
export async function postPersonaUpdate(
  payload: PersonaUpdatePayload,
): Promise<{ ok: true }> {
  return fetchJson<{ ok: true }>('/api/admin/persona', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/persona/extract-from-input — unified extraction
 * endpoint used by both onboarding paths. Dispatch on
 * ``input_type``; the response always carries both blocks and facts
 * plus a confidence self-assessment. For path B (``import_upload``)
 * the response also includes the pipeline's extracted events +
 * thoughts so the review page can render them.
 */
export async function postPersonaExtract(
  payload: PersonaExtractRequest,
): Promise<PersonaExtractResponse> {
  return fetchJson<PersonaExtractResponse>(
    '/api/admin/persona/extract-from-input',
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
}

/**
 * PATCH /api/admin/persona/facts — partial update of the fifteen
 * biographic fact columns. Only keys present in ``payload.facts`` are
 * written; explicit null clears a previously-set field.
 */
export async function patchPersonaFacts(
  payload: PersonaFactsUpdatePayload,
): Promise<PersonaFactsUpdateResponse> {
  return fetchJson<PersonaFactsUpdateResponse>(
    '/api/admin/persona/facts',
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    },
  )
}

/**
 * POST /api/admin/persona/voice-toggle — flip the persona's voice
 * output preference. Returns the new value for optimistic UI confirm.
 * Throws ApiError(400, detail) if the daemon is in config-override mode
 * (where voice_enabled is pinned by config and cannot be toggled at
 * runtime).
 */
export async function postVoiceToggle(
  enabled: boolean,
): Promise<VoiceToggleResponse> {
  return fetchJson<VoiceToggleResponse>('/api/admin/persona/voice-toggle', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })
}

/**
 * POST /api/admin/persona/style — owner-directed voice / style
 * preference write path (plan §6.6). Three actions: set (replace),
 * append (join with newline), clear (soft-delete the row).
 */
export async function postPersonaStyle(
  payload: StyleUpdatePayload,
): Promise<StyleUpdateResponse> {
  return fetchJson<StyleUpdateResponse>('/api/admin/persona/style', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/users/timezone — store the local owner's IANA
 * timezone string. Default behaviour writes only when currently null;
 * set ``override: true`` to overwrite (Admin UI manual edit path).
 * Backend 400s on non-IANA strings.
 */
export async function postUsersTimezone(
  payload: UsersTimezonePayload,
): Promise<UsersTimezoneResponse> {
  return fetchJson<UsersTimezoneResponse>('/api/admin/users/timezone', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/persona/avatar — upload a new profile picture.
 * Accepts png/jpg/webp/gif up to ~4 MiB. Previous avatars (possibly
 * with a different extension) are replaced atomically on the server.
 */
export async function postPersonaAvatar(
  file: File,
): Promise<{ ok: true; size_bytes: number; ext: string }> {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch('/api/admin/persona/avatar', {
    method: 'POST',
    body,
  })
  if (!response.ok) {
    const detail = await extractDetail(response)
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as {
    ok: true
    size_bytes: number
    ext: string
  }
}

/**
 * DELETE /api/admin/persona/avatar — remove the current avatar.
 * 404 when no avatar is set.
 */
export async function deletePersonaAvatar(): Promise<{ deleted: true }> {
  return fetchJson<{ deleted: true }>('/api/admin/persona/avatar', {
    method: 'DELETE',
  })
}

/**
 * Build a cache-busted URL for the current avatar. Callers thread a
 * monotonically increasing key (post-upload timestamp, or a React state
 * counter) so <img src> reloads after every mutation without any
 * server-side cache-control trickery.
 */
export function avatarUrl(version: string | number): string {
  return `/api/admin/persona/avatar?v=${encodeURIComponent(String(version))}`
}

/**
 * POST /api/admin/reset — nuclear wipe. Deletes every memory row,
 * clears the persona's display_name + facts + voice_id, drops voice
 * sample files, and returns the daemon to an onboarding-required
 * state. Idempotent.
 *
 * Callers typically reload the page after this resolves so that the
 * fresh `/api/state` bootstrap takes effect immediately.
 */
export async function postAdminReset(): Promise<{
  ok: true
  persona_id: string
}> {
  return fetchJson<{ ok: true; persona_id: string }>('/api/admin/reset', {
    method: 'POST',
  })
}

/**
 * POST /api/chat/send — ingest a user message into the turn loop. The
 * daemon responds with 202 Accepted as soon as the message is persisted;
 * the actual reply arrives asynchronously via the SSE stream.
 */
export async function postChatSend(
  payload: ChatSendPayload,
): Promise<{ ok: true }> {
  return fetchJson<{ ok: true }>('/api/chat/send', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * GET /api/chat/history — cross-channel backfill of L2 recall messages.
 * Called on mount by `useChat` so the browser timeline is pre-populated
 * with recent conversation (including messages sent from other
 * channels like Discord). `before` is the `oldest_turn_id` from a
 * previous response to paginate to older pages.
 */
export async function getChatHistory(
  limit = 50,
  before?: string,
): Promise<ChatHistoryResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  if (before) params.set('before', before)
  return fetchJson<ChatHistoryResponse>(`/api/chat/history?${params}`)
}

// ─── Import endpoints ──────────────────────────────────────────────────

/**
 * POST /api/admin/import/upload_text — stage text content (paste or
 * file-read-as-text) server-side and return an opaque `upload_id`.
 *
 * The sibling multipart endpoint (`/upload`) is for raw file bytes;
 * because the MVP frontend reads every file into a string before
 * submitting, we only need the JSON path here. If we later add binary
 * formats (PDF / audio) we'll add a parallel `postImportUploadFile`
 * that uses `FormData` against `/upload`.
 */
export async function postImportUploadText(
  payload: ImportUploadTextPayload,
): Promise<ImportUploadResponse> {
  return fetchJson<ImportUploadResponse>('/api/admin/import/upload_text', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/import/estimate — run the chunker + token counter and
 * return estimated token / cost numbers. Cheap dry-run; no LLM calls.
 */
export async function postImportEstimate(
  payload: ImportEstimatePayload,
): Promise<ImportEstimateResponse> {
  return fetchJson<ImportEstimateResponse>('/api/admin/import/estimate', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/import/start — kick off the pipeline and return the
 * `pipeline_id` the client uses to open the SSE stream at
 * `/api/admin/import/events?pipeline_id=...`.
 */
export async function postImportStart(
  payload: ImportStartPayload,
): Promise<ImportStartResponse> {
  return fetchJson<ImportStartResponse>('/api/admin/import/start', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * POST /api/admin/import/cancel — ask the pipeline to stop at the next
 * chunk boundary. Already-written memory is kept; the pipeline emits a
 * final `import.done` with `status=cancelled` once it has wound down.
 */
export async function postImportCancel(
  payload: ImportCancelPayload,
): Promise<ImportCancelResponse> {
  return fetchJson<ImportCancelResponse>('/api/admin/import/cancel', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

// ─── Config endpoints (Worker η) ────────────────────────────────────────

/**
 * GET /api/admin/config — safe subset of the live daemon config. No
 * secrets (`api_key_present: boolean` only) + system info (uptime,
 * db size, version) folded in so the ConfigTab only makes one call.
 */
export async function getConfig(): Promise<ConfigGetResponse> {
  return fetchJson<ConfigGetResponse>('/api/admin/config')
}

/**
 * PATCH /api/admin/config — apply a nested patch dict. Server atomic-
 * writes config.toml, validates with Pydantic, triggers an internal
 * reload, then returns the applied field paths. Rejects restart-
 * required fields with 400 and out-of-range values with 422.
 */
export async function patchConfig(
  payload: ConfigPatchPayload,
): Promise<ConfigPatchResponse> {
  return fetchJson<ConfigPatchResponse>('/api/admin/config', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

/**
 * PATCH /api/admin/channels — apply per-channel config changes. Writes
 * config.toml atomically but does NOT hot-reload — the caller should
 * display a "restart daemon to apply" banner. Secrets (Discord token,
 * future iMessage credentials) are environment-driven and never
 * accepted as input here.
 */
export async function patchChannels(
  payload: ChannelsPatchPayload,
): Promise<ChannelsPatchResponse> {
  return fetchJson<ChannelsPatchResponse>('/api/admin/channels', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

// ─── Memory list + delete endpoints (Worker α / W-β) ────────────────────

/** Build a path with `?limit=&offset=` query string. */
function _memoryListPath(
  base: string,
  limit: number,
  offset: number,
): string {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  return `${base}?${params.toString()}`
}

/**
 * GET /api/admin/memory/events — paginated L3 list for the admin
 * Events tab. ``limit`` is server-capped at 100; ``offset`` is the
 * number of newer rows to skip from the head of the DESC order.
 */
export async function getMemoryEvents(
  limit = 20,
  offset = 0,
): Promise<MemoryListResponse<MemoryEvent>> {
  return fetchJson<MemoryListResponse<MemoryEvent>>(
    _memoryListPath('/api/admin/memory/events', limit, offset),
  )
}

/**
 * GET /api/admin/memory/thoughts — paginated L4 list for the admin
 * Thoughts tab.
 *
 * ``subject`` (v0.5 hotfix) scopes the list to one subject value
 * ("persona" / "user" / "shared"); omit for the legacy "all
 * subjects" behaviour. The Admin Persona tab Reflection section
 * passes ``subject: 'persona'`` to surface only the persona-authored
 * introspection rows.
 */
export async function getMemoryThoughts(
  limit = 20,
  offset = 0,
  subject?: 'persona' | 'user' | 'shared',
): Promise<MemoryListResponse<MemoryThought>> {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  if (subject !== undefined) params.set('subject', subject)
  return fetchJson<MemoryListResponse<MemoryThought>>(
    `/api/admin/memory/thoughts?${params.toString()}`,
  )
}

/**
 * GET /api/admin/memory/timeline — Spec 3 backfill for the chat
 * Memory Timeline sidebar. Returns a mixed, DESC-sorted list of
 * L3 events / L4 thoughts / L5 entities / L6 mood / session closes.
 * Pass ``since`` (an ISO timestamp — typically the oldest row
 * currently rendered) to page older.
 */
export async function getMemoryTimeline(
  limit = 50,
  since: string | null = null,
): Promise<MemoryTimelineResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  if (since !== null) params.set('since', since)
  return fetchJson<MemoryTimelineResponse>(
    `/api/admin/memory/timeline?${params.toString()}`,
  )
}

/**
 * POST /api/admin/memory/preview-delete — peek at the cascade
 * consequences of deleting a concept node before issuing the DELETE.
 *
 * Returns the dependent thought ids + descriptions; if `has_dependents`
 * is false, the UI can skip the choice dialog and call the DELETE
 * directly with the default ``"orphan"`` choice.
 */
export async function postMemoryPreviewDelete(
  nodeId: number,
): Promise<PreviewDeleteResponse> {
  return fetchJson<PreviewDeleteResponse>('/api/admin/memory/preview-delete', {
    method: 'POST',
    body: JSON.stringify({ node_id: nodeId }),
  })
}

/**
 * DELETE /api/admin/memory/events/{node_id}?choice=… — soft-delete an
 * L3 event. ``choice`` controls how dependent thoughts are handled
 * (``orphan`` keeps them, ``cascade`` deletes them too). Default
 * ``orphan`` matches the memory module's default.
 */
export async function deleteMemoryEvent(
  nodeId: number,
  choice: DeleteChoice = 'orphan',
): Promise<DeleteResponse> {
  return fetchJson<DeleteResponse>(
    `/api/admin/memory/events/${nodeId}?choice=${choice}`,
    { method: 'DELETE' },
  )
}

/** DELETE /api/admin/memory/thoughts/{node_id}?choice=… — soft-delete an L4 thought. */
export async function deleteMemoryThought(
  nodeId: number,
  choice: DeleteChoice = 'orphan',
): Promise<DeleteResponse> {
  return fetchJson<DeleteResponse>(
    `/api/admin/memory/thoughts/${nodeId}?choice=${choice}`,
    { method: 'DELETE' },
  )
}

/**
 * GET /api/admin/memory/search — keyword search over L3 events + L4
 * thoughts via the SQLite FTS5 index. ``q`` is the user's query
 * string (server-side sanitised); ``type`` scopes to events / thoughts
 * / both; ``tag`` is an optional exact-match filter on the
 * ``emotion_tags`` or ``relational_tags`` JSON arrays.
 *
 * Returns hits in matched-relevance order plus a parallel array of
 * ``{node_id, snippet}`` objects whose ``snippet`` field is server-
 * rendered HTML containing only ``<b>…</b>`` tags around matched
 * substrings (for highlighting in the admin search UI).
 */
export async function searchMemory(
  q: string,
  opts: {
    type?: MemorySearchType
    tag?: string | null
    limit?: number
    offset?: number
  } = {},
): Promise<MemorySearchResponse> {
  const params = new URLSearchParams({ q })
  if (opts.type) params.set('type', opts.type)
  if (opts.tag) params.set('tag', opts.tag)
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  if (opts.offset !== undefined) params.set('offset', String(opts.offset))
  return fetchJson<MemorySearchResponse>(
    `/api/admin/memory/search?${params.toString()}`,
  )
}

/**
 * GET /api/admin/memory/thoughts/{id}/trace — list the L3 events that
 * produced this L4 thought plus the set of source sessions. Returns
 * empty arrays (not 404) when the thought exists but has no live
 * filling lineage.
 */
export async function getThoughtTrace(
  nodeId: number,
): Promise<ThoughtTraceResponse> {
  return fetchJson<ThoughtTraceResponse>(
    `/api/admin/memory/thoughts/${nodeId}/trace`,
  )
}

/**
 * GET /api/admin/memory/events/{id}/dependents — list the L4 thoughts
 * derived from this L3 event. Reverse direction of `getThoughtTrace`.
 */
export async function getEventDependents(
  nodeId: number,
): Promise<EventDependentsResponse> {
  return fetchJson<EventDependentsResponse>(
    `/api/admin/memory/events/${nodeId}/dependents`,
  )
}

// ─── Cost endpoints (Worker ζ) ──────────────────────────────────────────

/** GET /api/admin/cost/summary?range=today|7d|30d */
export async function getCostSummary(
  range: 'today' | '7d' | '30d' = '30d',
): Promise<CostSummaryResponse> {
  return fetchJson<CostSummaryResponse>(
    `/api/admin/cost/summary?range=${range}`,
  )
}

/** GET /api/admin/cost/recent?limit=N */
export async function getCostRecent(
  limit = 50,
): Promise<CostRecentResponse> {
  return fetchJson<CostRecentResponse>(
    `/api/admin/cost/recent?limit=${limit}`,
  )
}

// ─── Voice clone wizard (Worker λ) ──────────────────────────────────────

/**
 * POST /api/admin/voice/samples — upload one audio sample via multipart.
 * Backend stores it under <data_dir>/voice_samples/{sample_id}/ and
 * returns the generated sample_id.
 */
export async function postVoiceSampleUpload(
  file: File,
): Promise<VoiceSampleUploadResponse> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch('/api/admin/voice/samples', {
    method: 'POST',
    body: form,
  })
  if (!response.ok) {
    const detail = await extractDetail(response)
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as VoiceSampleUploadResponse
}

/** GET /api/admin/voice/samples — list every draft sample. */
export async function getVoiceSamples(): Promise<VoiceSampleListResponse> {
  return fetchJson<VoiceSampleListResponse>('/api/admin/voice/samples')
}

/** DELETE /api/admin/voice/samples/{sample_id} — drop one draft sample. */
export async function deleteVoiceSample(
  sampleId: string,
): Promise<{ deleted: true; sample_id: string }> {
  return fetchJson<{ deleted: true; sample_id: string }>(
    `/api/admin/voice/samples/${encodeURIComponent(sampleId)}`,
    { method: 'DELETE' },
  )
}

/**
 * POST /api/admin/voice/clone — train a voice from the current draft
 * samples. Requires at least `minimum_required` samples (default 3).
 */
export async function postVoiceClone(
  displayName: string,
): Promise<VoiceCloneResponse> {
  return fetchJson<VoiceCloneResponse>('/api/admin/voice/clone', {
    method: 'POST',
    body: JSON.stringify({ display_name: displayName }),
  })
}

/**
 * POST /api/admin/voice/preview — render `text` through `voice_id` and
 * return the resulting MP3 as a Blob the UI can feed into `<audio>`.
 */
export async function postVoicePreview(
  voiceId: string,
  text: string,
): Promise<Blob> {
  const response = await fetch('/api/admin/voice/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ voice_id: voiceId, text }),
  })
  if (!response.ok) {
    const detail = await extractDetail(response)
    throw new ApiError(response.status, detail)
  }
  return await response.blob()
}

/**
 * POST /api/admin/voice/activate — persist voice_id to config.toml via
 * the daemon's atomic write and mirror it in-memory so the next turn
 * uses the new voice.
 */
export async function postVoiceActivate(
  voiceId: string,
): Promise<VoiceActivateResponse> {
  return fetchJson<VoiceActivateResponse>('/api/admin/voice/activate', {
    method: 'POST',
    body: JSON.stringify({ voice_id: voiceId }),
  })
}

/**
 * GET /api/admin/sessions/failed — every session the consolidate worker
 * marked FAILED, newest first. Empty list when nothing has failed.
 */
export async function getFailedSessions(): Promise<FailedSessionsResponse> {
  return fetchJson<FailedSessionsResponse>('/api/admin/sessions/failed')
}

/**
 * GET /api/admin/turns — recent turn-trace headers (Spec 4 dev-mode).
 */
export async function getTurnTraces(
  limit = 20,
): Promise<TurnTraceListResponse> {
  return fetchJson<TurnTraceListResponse>(
    `/api/admin/turns?limit=${encodeURIComponent(String(limit))}`,
  )
}

/**
 * GET /api/admin/turns/{turn_id} — full per-turn trace (Spec 4).
 */
export async function getTurnTrace(turnId: string): Promise<TurnTraceResponse> {
  return fetchJson<TurnTraceResponse>(
    `/api/admin/turns/${encodeURIComponent(turnId)}`,
  )
}

/**
 * GET /api/admin/sessions/{session_id}/consolidate-trace — phases A–G.
 */
export async function getConsolidateTrace(
  sessionId: string,
): Promise<ConsolidateTraceResponse> {
  return fetchJson<ConsolidateTraceResponse>(
    `/api/admin/sessions/${encodeURIComponent(sessionId)}/consolidate-trace`,
  )
}

