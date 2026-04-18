/**
 * useVoiceClone — state + actions for the 3-step voice-clone wizard.
 *
 * Wizard flow:
 *   1. upload     — user drags audio files; each file hits
 *                   `POST /api/admin/voice/samples`
 *   2. clone      — user names the voice; hook calls
 *                   `POST /api/admin/voice/clone` (backend concatenates
 *                   every draft sample's bytes and trains)
 *   3. preview +  — hook holds the returned `voice_id`; UI plays the
 *      activate    preview audio url and then calls
 *                   `POST /api/admin/voice/activate` to persist.
 *
 * The hook never mounts a timer or SSE stream — uploads and deletes
 * trigger fresh `GET /api/admin/voice/samples` calls so the sample
 * count in the UI always matches the daemon's on-disk state.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  deleteVoiceSample,
  getVoiceSamples,
  postVoiceActivate,
  postVoiceClone,
  postVoicePreview,
  postVoiceSampleUpload,
} from '../api/client'
import type {
  VoiceCloneResponse,
  VoiceSample,
} from '../api/types'

// Upper bound per sample. Must stay in sync with the backend constant
// `_VOICE_SAMPLE_MAX_BYTES` in src/echovessel/channels/web/routes/admin.py.
// Checked client-side so a misclicked movie file doesn't stream hundreds
// of megabytes to localhost before the daemon rejects it.
const MAX_VOICE_SAMPLE_BYTES = 50 * 1024 * 1024

export type WizardStep = 'upload' | 'clone' | 'preview'

export interface UseVoiceCloneResult {
  step: WizardStep
  samples: VoiceSample[]
  minimumRequired: number
  uploading: boolean
  cloning: boolean
  activating: boolean
  error: string | null

  cloneResult: VoiceCloneResponse | null
  activated: boolean

  uploadSample: (file: File) => Promise<void>
  removeSample: (sampleId: string) => Promise<void>
  startClone: (displayName: string) => Promise<void>
  previewAudio: (text: string) => Promise<Blob>
  activateVoice: () => Promise<void>
  reset: () => void
}

export function useVoiceClone(): UseVoiceCloneResult {
  const [samples, setSamples] = useState<VoiceSample[]>([])
  const [minimumRequired, setMinimumRequired] = useState<number>(3)
  const [uploading, setUploading] = useState(false)
  const [cloning, setCloning] = useState(false)
  const [activating, setActivating] = useState(false)
  const [cloneResult, setCloneResult] = useState<VoiceCloneResponse | null>(null)
  const [activated, setActivated] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const res = await getVoiceSamples()
      setSamples(res.samples)
      setMinimumRequired(res.minimum_required)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const uploadSample = useCallback(
    async (file: File) => {
      setError(null)
      if (file.size > MAX_VOICE_SAMPLE_BYTES) {
        const mb = (file.size / (1024 * 1024)).toFixed(1)
        setError(
          `File too large: ${mb}MB exceeds the 50MB per-sample limit`,
        )
        return
      }
      setUploading(true)
      try {
        await postVoiceSampleUpload(file)
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        throw e
      } finally {
        setUploading(false)
      }
    },
    [refresh],
  )

  const removeSample = useCallback(
    async (sampleId: string) => {
      setError(null)
      try {
        await deleteVoiceSample(sampleId)
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [refresh],
  )

  const startClone = useCallback(
    async (displayName: string) => {
      setError(null)
      setCloning(true)
      try {
        const result = await postVoiceClone(displayName)
        setCloneResult(result)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        throw e
      } finally {
        setCloning(false)
      }
    },
    [],
  )

  const previewAudio = useCallback(
    async (text: string) => {
      if (cloneResult === null) {
        throw new Error('no clone result yet; run startClone first')
      }
      return postVoicePreview(cloneResult.voice_id, text)
    },
    [cloneResult],
  )

  const activateVoice = useCallback(async () => {
    if (cloneResult === null) {
      throw new Error('no clone result yet; run startClone first')
    }
    setError(null)
    setActivating(true)
    try {
      await postVoiceActivate(cloneResult.voice_id)
      setActivated(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      throw e
    } finally {
      setActivating(false)
    }
  }, [cloneResult])

  const reset = useCallback(() => {
    setCloneResult(null)
    setActivated(false)
    setError(null)
  }, [])

  // Derive step from state — no explicit setStep call required.
  let step: WizardStep
  if (cloneResult !== null) {
    step = 'preview'
  } else if (samples.length >= minimumRequired) {
    step = 'clone'
  } else {
    step = 'upload'
  }

  return {
    step,
    samples,
    minimumRequired,
    uploading,
    cloning,
    activating,
    error,
    cloneResult,
    activated,
    uploadSample,
    removeSample,
    startClone,
    previewAudio,
    activateVoice,
    reset,
  }
}
