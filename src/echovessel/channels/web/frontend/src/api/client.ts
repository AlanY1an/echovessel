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
  ChatSendPayload,
  DaemonState,
  DeleteChoice,
  DeleteResponse,
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
  MemoryThought,
  OnboardingPayload,
  OnboardingResponse,
  PersonaStateApi,
  PersonaUpdatePayload,
  PreviewDeleteResponse,
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

/** GET /api/admin/memory/thoughts — paginated L4 list for the admin Thoughts tab. */
export async function getMemoryThoughts(
  limit = 20,
  offset = 0,
): Promise<MemoryListResponse<MemoryThought>> {
  return fetchJson<MemoryListResponse<MemoryThought>>(
    _memoryListPath('/api/admin/memory/thoughts', limit, offset),
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

