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
  ImportCancelPayload,
  ImportCancelResponse,
  ImportEstimatePayload,
  ImportEstimateResponse,
  ImportStartPayload,
  ImportStartResponse,
  ImportUploadPayload,
  ImportUploadResponse,
  OnboardingPayload,
  OnboardingResponse,
  PersonaStateApi,
  PersonaUpdatePayload,
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
 * POST /api/admin/import/upload — stage the source text server-side and
 * return an opaque `upload_id`. Raw bytes never re-travel the wire; the
 * frontend only holds the id until /start fires the pipeline.
 */
export async function postImportUpload(
  payload: ImportUploadPayload,
): Promise<ImportUploadResponse> {
  return fetchJson<ImportUploadResponse>('/api/admin/import/upload', {
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

