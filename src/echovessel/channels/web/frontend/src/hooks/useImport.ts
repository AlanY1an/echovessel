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
 *      │  import.done  → done
 *      │  import.error → error
 *      │  cancelImport → (pipeline settles, import.done w/ status=cancelled)
 *      ▼
 *     done / error
 *
 * The hook owns exactly one EventSource (opened on transition into
 * `running`, closed on `done` / `error` / unmount). Every subsequent
 * import starts from `reset()` which clears state back to `idle`.
 *
 * Contract: the backend is expected to emit `import.done` as the FINAL
 * frame on a pipeline (whether successful or cancelled); on unrecoverable
 * failure it emits `import.error` instead. The hook closes the stream on
 * either.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  postImportCancel,
  postImportEstimate,
  postImportStart,
  postImportUpload,
} from '../api/client'
import { ApiError } from '../api/types'
import type {
  ImportDoneData,
  ImportDroppedData,
  ImportErrorData,
  ImportEstimateResponse,
  ImportEvent,
  ImportProgressData,
  ImportUploadResponse,
  ImportWriteData,
} from '../api/types'
import { KNOWN_IMPORT_EVENT_NAMES } from '../api/types'

export type ImportPhase =
  | 'idle'
  | 'uploading'
  | 'estimating'
  | 'estimate'
  | 'starting'
  | 'running'
  | 'done'
  | 'error'

export interface UseImportResult {
  phase: ImportPhase
  upload: ImportUploadResponse | null
  estimate: ImportEstimateResponse | null
  progress: ImportProgressData | null
  writesByTarget: Record<string, number>
  dropped: ImportDroppedData[]
  report: ImportDoneData | null
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

export function useImport(): UseImportResult {
  const [phase, setPhase] = useState<ImportPhase>('idle')
  const [upload, setUpload] = useState<ImportUploadResponse | null>(null)
  const [estimate, setEstimate] = useState<ImportEstimateResponse | null>(null)
  const [pipelineId, setPipelineId] = useState<string | null>(null)
  const [progress, setProgress] = useState<ImportProgressData | null>(null)
  const [writesByTarget, setWritesByTarget] = useState<Record<string, number>>(
    {},
  )
  const [dropped, setDropped] = useState<ImportDroppedData[]>([])
  const [report, setReport] = useState<ImportDoneData | null>(null)
  const [error, setError] = useState<string | null>(null)

  const esRef = useRef<EventSource | null>(null)

  const closeStream = useCallback(() => {
    const es = esRef.current
    if (es !== null) {
      es.close()
      esRef.current = null
    }
  }, [])

  const handleEvent = useCallback(
    (ev: ImportEvent) => {
      switch (ev.event) {
        case 'import.progress':
          setProgress(ev.data)
          return
        case 'import.write': {
          const data = ev.data as ImportWriteData
          setWritesByTarget((prev) => ({
            ...prev,
            [data.content_type]: (prev[data.content_type] ?? 0) + 1,
          }))
          return
        }
        case 'import.dropped':
          setDropped((prev) => [...prev, ev.data])
          return
        case 'import.done': {
          const data = ev.data as ImportDoneData
          setReport(data)
          setPhase('done')
          closeStream()
          return
        }
        case 'import.error': {
          const data = ev.data as ImportErrorData
          setError(data.error)
          setPhase('error')
          closeStream()
          return
        }
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
        const uploadResult = await postImportUpload({
          source_label: sourceLabel,
          content,
        })
        setUpload(uploadResult)
        setPhase('estimating')

        const estimateResult = await postImportEstimate({
          upload_id: uploadResult.upload_id,
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
      const startResult = await postImportStart({ upload_id: upload.upload_id })
      setPipelineId(startResult.pipeline_id)

      // Open the SSE stream. Each known import event name gets a named
      // listener; unknown frames are ignored. The backend closes the
      // stream after emitting its final `import.done` / `import.error`,
      // but we also close locally on those events to release resources
      // promptly.
      const url = `/api/admin/import/events?pipeline_id=${encodeURIComponent(startResult.pipeline_id)}`
      const es = new EventSource(url)
      esRef.current = es

      for (const name of KNOWN_IMPORT_EVENT_NAMES) {
        es.addEventListener(name, (raw) => {
          const data = parseEventData((raw as MessageEvent).data)
          if (data === null) {
            console.warn('useImport: unparseable frame for', name)
            return
          }
          handleEvent({ event: name, data } as ImportEvent)
        })
      }

      es.addEventListener('error', () => {
        // EventSource's `error` fires on both transient disconnects and
        // remote-closed states. We only treat it as fatal if the stream
        // is in CLOSED state (readyState 2); otherwise leave it to
        // auto-reconnect.
        if (es.readyState === 2) {
          closeStream()
          // If we never got a terminal frame, flag the error.
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
  }, [upload, handleEvent, closeStream])

  const cancelImport = useCallback(async (): Promise<void> => {
    if (pipelineId === null) return
    try {
      await postImportCancel({ pipeline_id: pipelineId })
      // We deliberately keep `phase === 'running'` and wait for the
      // server's final `import.done` with `status=cancelled` so the
      // final-tally UI renders consistently (whether success or cancel).
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
