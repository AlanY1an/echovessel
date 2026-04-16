/**
 * useImport — drives the 3-step import wizard (upload → estimate →
 * running) against the daemon's `/api/admin/import/*` endpoints.
 *
 * State machine:
 *
 *     idle
 *      │  submitText(content, label)
 *      ▼
 *     uploading  ─── ApiError ──▶ error
 *      │  (auto)
 *      ▼
 *     estimating ─── ApiError ──▶ error
 *      │  (auto)
 *      ▼
 *     estimate                       ← user sees cost + "开始导入"
 *      │  startImport()
 *      ▼
 *     starting   ─── ApiError ──▶ error
 *      │  (auto)
 *      ▼
 *     running                        ← SSE open; user can cancel
 *      │  pipeline.done → done / error
 *      │  cancelImport  → (pipeline settles, pipeline.done w/ status=cancelled)
 *      ▼
 *     done / error
 *
 * SSE wire format: every frame arrives under the SSE event name
 * `import.progress` with payload `{pipeline_id, type, payload}`. The
 * true event kind lives in the inner `type` field (a multiplexed
 * envelope — see `docs/import/01-import-spec-v0.1.md`). This hook
 * fans out on that inner `type` internally.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  postImportCancel,
  postImportEstimate,
  postImportStart,
  postImportUploadText,
} from '../api/client'
import { ApiError } from '../api/types'
import type {
  ImportDoneSummary,
  ImportEstimateResponse,
  ImportFrame,
  ImportPipelineStatus,
  ImportProgressSnapshot,
  ImportUploadResponse,
} from '../api/types'

export type ImportPhase =
  | 'idle'
  | 'uploading'
  | 'estimating'
  | 'estimate'
  | 'starting'
  | 'running'
  | 'done'
  | 'error'

export interface ImportDroppedRecord {
  chunk_index: number
  reason: string
  stage?: string
  fatal?: boolean
}

export interface UseImportResult {
  phase: ImportPhase
  upload: ImportUploadResponse | null
  estimate: ImportEstimateResponse | null
  progress: ImportProgressSnapshot | null
  writesByTarget: Record<string, number>
  dropped: ImportDroppedRecord[]
  report: ImportDoneSummary | null
  error: string | null
  pipelineId: string | null
  submitText(content: string, sourceLabel: string): Promise<void>
  startImport(): Promise<void>
  cancelImport(): Promise<void>
  reset(): void
}

function parseEventData(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

/** Type guard for the wire payload inside an `import.progress` SSE frame. */
function isImportFrame(x: unknown): x is ImportFrame {
  if (x === null || typeof x !== 'object') return false
  const f = x as Record<string, unknown>
  return (
    typeof f.pipeline_id === 'string' &&
    typeof f.type === 'string' &&
    typeof f.payload === 'object' &&
    f.payload !== null
  )
}

/** Narrow an arbitrary payload value to string, tolerating missing keys. */
function payloadString(p: Record<string, unknown>, key: string): string {
  const v = p[key]
  return typeof v === 'string' ? v : ''
}

function payloadNumber(p: Record<string, unknown>, key: string): number {
  const v = p[key]
  return typeof v === 'number' ? v : 0
}

function payloadWritesByTarget(
  p: Record<string, unknown>,
  key: string,
): Record<string, number> {
  const raw = p[key]
  if (raw === null || typeof raw !== 'object') return {}
  const out: Record<string, number> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (typeof v === 'number') out[k] = v
  }
  return out
}

export function useImport(): UseImportResult {
  const [phase, setPhase] = useState<ImportPhase>('idle')
  const [upload, setUpload] = useState<ImportUploadResponse | null>(null)
  const [estimate, setEstimate] = useState<ImportEstimateResponse | null>(null)
  const [pipelineId, setPipelineId] = useState<string | null>(null)
  const [progress, setProgress] = useState<ImportProgressSnapshot | null>(null)
  const [writesByTarget, setWritesByTarget] = useState<Record<string, number>>(
    {},
  )
  const [dropped, setDropped] = useState<ImportDroppedRecord[]>([])
  const [report, setReport] = useState<ImportDoneSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  const esRef = useRef<EventSource | null>(null)

  const closeStream = useCallback(() => {
    const es = esRef.current
    if (es !== null) {
      es.close()
      esRef.current = null
    }
  }, [])

  const handleFrame = useCallback(
    (frame: ImportFrame) => {
      const { type, payload } = frame
      switch (type) {
        case 'pipeline.start': {
          // Authoritative total_chunks arrives here, not at upload time.
          setProgress({
            current_chunk: payloadNumber(payload, 'resume_from'),
            total_chunks: payloadNumber(payload, 'total_chunks'),
          })
          return
        }
        case 'chunk.start': {
          setProgress((prev) => ({
            current_chunk: payloadNumber(payload, 'chunk_index'),
            total_chunks:
              prev?.total_chunks ?? payloadNumber(payload, 'total_chunks'),
          }))
          return
        }
        case 'chunk.done': {
          // chunk_index is 0-based; "N chunks processed" is N+1 after
          // the Nth chunk finishes.
          const idx = payloadNumber(payload, 'chunk_index')
          setProgress((prev) => ({
            current_chunk: idx + 1,
            total_chunks: prev?.total_chunks ?? 0,
          }))
          // The `summary` field is a {content_type: count} map that the
          // pipeline emits for live write tallies.
          const summary = payload.summary
          if (summary !== null && typeof summary === 'object') {
            setWritesByTarget((prev) => {
              const next = { ...prev }
              for (const [k, v] of Object.entries(summary)) {
                if (typeof v === 'number') {
                  next[k] = (next[k] ?? 0) + v
                }
              }
              return next
            })
          }
          return
        }
        case 'chunk.error': {
          const chunkIdx = payloadNumber(payload, 'chunk_index')
          const fatal =
            typeof payload.fatal === 'boolean' ? payload.fatal : false
          const stage = payloadString(payload, 'stage')
          const reason = payloadString(payload, 'error')
          setDropped((prev) => [
            ...prev,
            {
              chunk_index: chunkIdx,
              reason,
              stage: stage || undefined,
              fatal,
            },
          ])
          return
        }
        case 'pipeline.done': {
          const statusRaw = payloadString(payload, 'status')
          const status: ImportPipelineStatus =
            statusRaw === 'success' ||
            statusRaw === 'partial_success' ||
            statusRaw === 'failed' ||
            statusRaw === 'cancelled'
              ? statusRaw
              : 'failed'
          const summary: ImportDoneSummary = {
            status,
            processed_chunks: payloadNumber(payload, 'processed_chunks'),
            total_chunks: payloadNumber(payload, 'total_chunks'),
            writes_by_target: payloadWritesByTarget(
              payload,
              'writes_by_target',
            ),
            dropped_count: payloadNumber(payload, 'dropped_count'),
            embedded_vector_count: payloadNumber(
              payload,
              'embedded_vector_count',
            ),
            error: payloadString(payload, 'error'),
          }
          setReport(summary)
          if (status === 'failed') {
            setError((prev) => prev ?? summary.error ?? 'pipeline failed')
            setPhase('error')
          } else {
            setPhase('done')
          }
          closeStream()
          return
        }
        case 'pipeline.cancelled':
        case 'pipeline.registered':
        case 'pipeline.resumed':
        default:
          // Informational frames — no UI state to update here.
          return
      }
    },
    [closeStream],
  )

  // Clean up on unmount so a navigation away does not leak the
  // EventSource (EventSource auto-reconnects; stale connections could
  // otherwise pile up).
  useEffect(() => {
    return () => {
      closeStream()
    }
  }, [closeStream])

  const reset = useCallback(() => {
    closeStream()
    setPhase('idle')
    setUpload(null)
    setEstimate(null)
    setPipelineId(null)
    setProgress(null)
    setWritesByTarget({})
    setDropped([])
    setReport(null)
    setError(null)
  }, [closeStream])

  const submitText = useCallback(
    async (content: string, sourceLabel: string): Promise<void> => {
      setError(null)
      setPhase('uploading')
      try {
        const uploadResult = await postImportUploadText({
          text: content,
          source_label: sourceLabel,
        })
        setUpload(uploadResult)
        setPhase('estimating')

        const estimateResult = await postImportEstimate({
          upload_id: uploadResult.upload_id,
          stage: 'llm',
        })
        setEstimate(estimateResult)
        setPhase('estimate')
      } catch (err) {
        setError(errorMessage(err))
        setPhase('error')
      }
    },
    [],
  )

  const startImport = useCallback(async (): Promise<void> => {
    if (upload === null) {
      setError('no upload to start')
      setPhase('error')
      return
    }
    setError(null)
    setPhase('starting')
    try {
      const startResult = await postImportStart({
        upload_id: upload.upload_id,
      })
      setPipelineId(startResult.pipeline_id)

      // Open the SSE stream. The backend emits a single event name
      // (`import.progress`) and multiplexes the true kind in
      // `data.type`. Hook the named listener plus the plain `message`
      // channel to be resilient to either wire framing.
      const url = `/api/admin/import/events?pipeline_id=${encodeURIComponent(startResult.pipeline_id)}`
      const es = new EventSource(url)
      esRef.current = es

      const onMessage = (raw: MessageEvent) => {
        const data = parseEventData(raw.data)
        if (!isImportFrame(data)) {
          console.warn('useImport: non-frame SSE payload', raw.data)
          return
        }
        handleFrame(data)
      }

      es.addEventListener('import.progress', onMessage as EventListener)
      es.addEventListener('message', onMessage as EventListener)

      es.addEventListener('error', () => {
        // EventSource's `error` fires on both transient disconnects and
        // remote-closed states. Treat as fatal only if the stream is
        // in CLOSED state (readyState 2); otherwise let it auto-reconnect.
        if (es.readyState === 2) {
          closeStream()
          setPhase((prev) => (prev === 'running' ? 'error' : prev))
          setError((prev) => prev ?? 'SSE stream closed unexpectedly')
        }
      })

      setPhase('running')
    } catch (err) {
      setError(errorMessage(err))
      setPhase('error')
      closeStream()
    }
  }, [upload, handleFrame, closeStream])

  const cancelImport = useCallback(async (): Promise<void> => {
    if (pipelineId === null) return
    try {
      await postImportCancel({ pipeline_id: pipelineId })
      // We deliberately keep `phase === 'running'` and wait for the
      // server's final `pipeline.done` (status=cancelled) so the
      // final-tally UI renders consistently.
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [pipelineId])

  return {
    phase,
    upload,
    estimate,
    progress,
    writesByTarget,
    dropped,
    report,
    error,
    pipelineId,
    submitText,
    startImport,
    cancelImport,
    reset,
  }
}
