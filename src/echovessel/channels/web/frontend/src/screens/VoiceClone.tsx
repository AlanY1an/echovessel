/**
 * VoiceClone — paper/ink/rust design: three tabs (Upload · Record · Preview).
 *
 * Every real API call still flows through `useVoiceClone`:
 *   - uploadSample      -> POST /api/admin/voice/samples
 *   - removeSample      -> DELETE /api/admin/voice/samples/{id}
 *   - startClone        -> POST /api/admin/voice/clone
 *   - previewAudio      -> POST /api/admin/voice/preview
 *   - activateVoice     -> POST /api/admin/voice/activate
 *
 * The Record tab uses the browser's MediaRecorder to capture a webm/ogg
 * blob and streams it straight through `uploadSample` as a synthetic File
 * — the backend already takes arbitrary audio bytes via the samples
 * endpoint, so recorded takes show up alongside uploaded ones.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { LanguageToggle } from '../components/LanguageToggle'
import { Wave, fmtT } from '../components/primitives'
import { useVoiceClone } from '../hooks/useVoiceClone'
import type { VoiceSample } from '../api/types'

interface VoiceCloneProps {
  onBack: () => void
}

type Tab = 'upload' | 'record' | 'preview'

export function VoiceClone({ onBack }: VoiceCloneProps) {
  const { t } = useTranslation()
  const wiz = useVoiceClone()
  const [tab, setTab] = useState<Tab>('upload')

  // Auto-advance to preview once the daemon returns a voice_id.
  useEffect(() => {
    if (wiz.cloneResult !== null) setTab('preview')
  }, [wiz.cloneResult])

  return (
    <div className="vc">
      <div
        className="vc-tabs"
        style={{ alignItems: 'center', justifyContent: 'space-between' }}
      >
        <div style={{ display: 'flex' }}>
          <button
            type="button"
            className={tab === 'upload' ? 'on' : ''}
            onClick={() => setTab('upload')}
          >
            {t('voice.tab_upload')}
          </button>
          <button
            type="button"
            className={tab === 'record' ? 'on' : ''}
            onClick={() => setTab('record')}
          >
            {t('voice.tab_record')}
          </button>
          <button
            type="button"
            className={tab === 'preview' ? 'on' : ''}
            onClick={() => setTab('preview')}
            disabled={wiz.cloneResult === null}
            style={wiz.cloneResult === null ? { opacity: 0.4 } : undefined}
          >
            {t('voice.tab_preview')}
          </button>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            paddingRight: 12,
          }}
        >
          <button type="button" className="btn ghost sm" onClick={onBack}>
            {t('voice.back')}
          </button>
          <LanguageToggle />
        </div>
      </div>

      {wiz.error !== null && (
        <div
          className="label"
          style={{
            color: 'var(--accent)',
            padding: '8px 40px',
            background: 'var(--accent-soft)',
          }}
        >
          ⚠ {wiz.error}
        </div>
      )}

      {tab === 'upload' && (
        <VCUpload
          samples={wiz.samples}
          minimumRequired={wiz.minimumRequired}
          uploading={wiz.uploading}
          cloning={wiz.cloning}
          onUpload={wiz.uploadSample}
          onRemove={wiz.removeSample}
          onClone={async () => {
            // MVP: samples list carries no user-supplied display name yet,
            // so we synthesise one from the local date — it can be renamed
            // later from the Admin voice section.
            const name = `voice-${new Date().toISOString().slice(0, 10)}`
            await wiz.startClone(name)
          }}
        />
      )}

      {tab === 'record' && (
        <VCRecord onUpload={wiz.uploadSample} uploading={wiz.uploading} />
      )}

      {tab === 'preview' && wiz.cloneResult !== null && (
        <VCPreview
          displayName={wiz.cloneResult.display_name}
          previewText={wiz.cloneResult.preview_text}
          previewAudioUrl={wiz.cloneResult.preview_audio_url}
          activating={wiz.activating}
          activated={wiz.activated}
          onPreview={wiz.previewAudio}
          onActivate={wiz.activateVoice}
          onBack={onBack}
        />
      )}
      {tab === 'preview' && wiz.cloneResult === null && (
        <div className="vc-body">
          <div className="record-stage">
            <span className="label">{t('voice.preview_empty')}</span>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Upload tab ───────────────────────────────────────────────

function VCUpload({
  samples,
  minimumRequired,
  uploading,
  cloning,
  onUpload,
  onRemove,
  onClone,
}: {
  samples: VoiceSample[]
  minimumRequired: number
  uploading: boolean
  cloning: boolean
  onUpload: (file: File) => Promise<void>
  onRemove: (sampleId: string) => Promise<void>
  onClone: () => Promise<void>
}) {
  const { t } = useTranslation()
  const [hot, setHot] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleFiles = useCallback(
    async (files: FileList | File[]) => {
      for (const f of Array.from(files)) {
        try {
          await onUpload(f)
        } catch {
          return
        }
      }
    },
    [onUpload],
  )

  const ready = samples.length >= minimumRequired

  return (
    <div
      className="vc-body"
      style={{ display: 'flex', flexDirection: 'column', gap: 20 }}
    >
      <div>
        <h2 className="title">{t('voice.upload_title')}</h2>
        <p
          style={{
            color: 'var(--ink-2)',
            maxWidth: 560,
            lineHeight: 1.55,
          }}
        >
          {t('voice.upload_subtitle')}
        </p>
      </div>

      <div
        className={`dropzone ${hot ? 'hot' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setHot(true)
        }}
        onDragLeave={() => setHot(false)}
        onDrop={(e) => {
          e.preventDefault()
          setHot(false)
          if (e.dataTransfer.files.length > 0) {
            void handleFiles(e.dataTransfer.files)
          }
        }}
        onClick={() => fileInputRef.current?.click()}
      >
        <div className="kicker">
          {uploading ? t('voice.uploading') : t('voice.drop_here')}
        </div>
        <div style={{ color: 'var(--ink-3)', fontSize: 13 }}>
          {t('voice.drop_hint')}
        </div>
        <button
          type="button"
          className="btn ghost"
          onClick={(e) => {
            e.stopPropagation()
            fileInputRef.current?.click()
          }}
        >
          {t('voice.choose_files')}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="audio/*"
          multiple
          style={{ display: 'none' }}
          onChange={(e) => {
            if (e.target.files && e.target.files.length > 0) {
              void handleFiles(e.target.files)
              e.target.value = ''
            }
          }}
        />
      </div>

      <div className="stack g-2">
        <span className="label">
          {t('voice.samples_count', {
            count: samples.length,
            minimum: minimumRequired,
          })}
        </span>
        {samples.length === 0 && (
          <div style={{ color: 'var(--ink-3)', fontSize: 13, padding: '8px 0' }}>
            {t('voice.samples_empty')}
          </div>
        )}
        {samples.map((s, i) => (
          <SampleRow
            key={s.sample_id}
            sample={s}
            seed={i + 1}
            onRemove={onRemove}
          />
        ))}
      </div>

      <div className="row" style={{ alignItems: 'center' }}>
        <div className="flex1" />
        <button
          type="button"
          className="btn accent"
          disabled={!ready || cloning}
          onClick={() => void onClone()}
        >
          {cloning ? t('voice.cloning') : t('voice.clone_cta')}
        </button>
      </div>
    </div>
  )
}

function SampleRow({
  sample,
  seed,
  onRemove,
}: {
  sample: VoiceSample
  seed: number
  onRemove: (sampleId: string) => Promise<void>
}) {
  const { t } = useTranslation()
  return (
    <div className="sample-row">
      <button type="button" className="play" aria-label={t('voice.play_aria')}>
        ▶
      </button>
      <div className="name">
        {sample.filename}
        <br />
        <span style={{ color: 'var(--ink-3)', fontSize: 9 }}>
          {fmtT(sample.duration_seconds)}
        </span>
      </div>
      <div className="wave">
        <Wave bars={60} seed={seed * 7} />
      </div>
      <span className="chip">{qualityChip(sample)}</span>
      <button
        type="button"
        className="btn ghost sm"
        onClick={() => void onRemove(sample.sample_id)}
        aria-label={t('voice.delete_aria')}
      >
        ✕ {t('voice.delete')}
      </button>
    </div>
  )
}

function qualityChip(sample: VoiceSample): string {
  // Duration is nullable in MVP — the backend doesn't probe yet. We only
  // classify when present so the chip actually reflects something real.
  const d = sample.duration_seconds
  if (d === null || !Number.isFinite(d)) return 'ok'
  if (d < 10) return 'short'
  if (d >= 20) return 'good'
  return 'ok'
}

// ─── Record tab ───────────────────────────────────────────────

function VCRecord({
  onUpload,
  uploading,
}: {
  onUpload: (file: File) => Promise<void>
  uploading: boolean
}) {
  const { t } = useTranslation()
  const prompts = t('voice.record_prompts', {
    returnObjects: true,
  }) as string[]
  const [promptIdx, setPromptIdx] = useState(0)
  const [rec, setRec] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [waveH, setWaveH] = useState<number[]>(
    Array.from({ length: 60 }, () => 8),
  )
  const [recError, setRecError] = useState<string | null>(null)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)

  // Timer ticks once per second while the recorder is active.
  useEffect(() => {
    if (!rec) return
    const id = setInterval(() => setElapsed((e) => e + 1), 1000)
    return () => clearInterval(id)
  }, [rec])

  // Animated wave while recording — decorative, not real FFT.
  useEffect(() => {
    if (!rec) return
    const id = setInterval(() => {
      setWaveH(
        Array.from(
          { length: 60 },
          (_, i) =>
            6 + Math.abs(Math.sin(i * 0.4 + Date.now() * 0.005) * 40),
        ),
      )
    }, 80)
    return () => clearInterval(id)
  }, [rec])

  const stopStream = () => {
    if (streamRef.current !== null) {
      streamRef.current.getTracks().forEach((tr) => tr.stop())
      streamRef.current = null
    }
  }

  const startRecording = useCallback(async () => {
    setRecError(null)
    if (
      typeof navigator === 'undefined' ||
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === 'undefined'
    ) {
      setRecError(t('voice.record_unsupported'))
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      const recorder = new MediaRecorder(stream)
      chunksRef.current = []
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data)
      }
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || 'audio/webm',
        })
        const ext = (recorder.mimeType || 'audio/webm').includes('ogg')
          ? 'ogg'
          : 'webm'
        const fname = `recording-${Date.now()}.${ext}`
        const file = new File([blob], fname, { type: blob.type })
        stopStream()
        void onUpload(file)
      }
      recorder.start()
      recorderRef.current = recorder
      setElapsed(0)
      setRec(true)
    } catch (e) {
      setRecError(e instanceof Error ? e.message : String(e))
      stopStream()
    }
  }, [onUpload, t])

  const stopRecording = useCallback(() => {
    const r = recorderRef.current
    if (r !== null && r.state !== 'inactive') r.stop()
    recorderRef.current = null
    setRec(false)
  }, [])

  // Cleanup on unmount — never leave the mic light on.
  useEffect(() => {
    return () => {
      const r = recorderRef.current
      if (r !== null && r.state !== 'inactive') r.stop()
      stopStream()
    }
  }, [])

  const toggle = () => {
    if (rec) stopRecording()
    else void startRecording()
  }

  return (
    <div className="vc-body">
      <div className="record-stage">
        <span className="label">
          {t('voice.record_prompt_count', {
            current: promptIdx + 1,
            total: prompts.length,
          })}
        </span>
        <div className="record-prompt">{prompts[promptIdx]}</div>
        <div style={{ color: 'var(--ink-3)', fontSize: 12 }}>
          {t('voice.record_hint')}
        </div>
        <div className="record-wave" style={{ opacity: rec ? 1 : 0.35 }}>
          {waveH.map((h, i) => (
            <i key={i} style={{ height: h }} />
          ))}
        </div>
        <div className="row g-3" style={{ alignItems: 'center' }}>
          <button
            type="button"
            className={`rec-btn ${rec ? 'rec' : ''}`}
            onClick={toggle}
            aria-label={rec ? t('voice.record_stop_aria') : t('voice.record_start_aria')}
          >
            {rec ? <div className="sq" /> : <div className="circ" />}
          </button>
          <div className="stack">
            <div style={{ fontFamily: 'var(--mono)', fontSize: 18 }}>
              {fmtT(elapsed)}
            </div>
            <span className="label">
              {rec
                ? t('voice.record_state_rec')
                : uploading
                  ? t('voice.uploading')
                  : t('voice.record_state_ready')}
            </span>
          </div>
        </div>
        <div className="row g-2">
          <button
            type="button"
            className="btn ghost sm"
            disabled={rec}
            onClick={() => setPromptIdx((i) => Math.max(0, i - 1))}
          >
            {t('voice.prev')}
          </button>
          <button
            type="button"
            className="btn sm"
            disabled={rec}
            onClick={() => {
              setPromptIdx((i) => Math.min(prompts.length - 1, i + 1))
              setElapsed(0)
            }}
          >
            {t('voice.next')}
          </button>
        </div>
        {recError !== null && (
          <div
            className="label"
            style={{ color: 'var(--accent)', maxWidth: 460 }}
          >
            ⚠ {recError}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Preview tab ──────────────────────────────────────────────

function VCPreview({
  displayName,
  previewText,
  previewAudioUrl,
  activating,
  activated,
  onPreview,
  onActivate,
  onBack,
}: {
  displayName: string
  previewText: string
  previewAudioUrl: string | null
  activating: boolean
  activated: boolean
  onPreview: (text: string) => Promise<Blob>
  onActivate: () => Promise<void>
  onBack: () => void
}) {
  const { t } = useTranslation()
  const [text, setText] = useState(previewText)
  const [customUrl, setCustomUrl] = useState<string | null>(null)
  const [fetching, setFetching] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    return () => {
      if (customUrl !== null) URL.revokeObjectURL(customUrl)
    }
  }, [customUrl])

  const playDefault = () => {
    if (previewAudioUrl === null) return
    if (audioRef.current === null) {
      audioRef.current = new Audio(previewAudioUrl)
    }
    void audioRef.current.play()
  }

  const speakIt = useCallback(async () => {
    if (!text.trim()) return
    setFetching(true)
    try {
      const blob = await onPreview(text)
      const url = URL.createObjectURL(blob)
      setCustomUrl((prev) => {
        if (prev !== null) URL.revokeObjectURL(prev)
        return url
      })
      const audio = new Audio(url)
      void audio.play()
    } finally {
      setFetching(false)
    }
  }, [onPreview, text])

  const activateAndBack = useCallback(async () => {
    if (!activated) await onActivate()
    onBack()
  }, [activated, onActivate, onBack])

  return (
    <div
      className="vc-body"
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexDirection: 'column',
        gap: 20,
      }}
    >
      <div className="kicker" style={{ fontSize: 26 }}>
        {t('voice.preview_ready', { name: displayName })}
      </div>
      <div
        className="card"
        style={{
          width: 460,
          padding: 22,
          display: 'flex',
          flexDirection: 'column',
          gap: 16,
        }}
      >
        <div className="row g-3" style={{ alignItems: 'center' }}>
          <button
            type="button"
            className="play"
            onClick={playDefault}
            disabled={previewAudioUrl === null}
            style={{
              width: 34,
              height: 34,
              borderRadius: '50%',
              background: 'var(--ink)',
              color: 'var(--paper)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
            aria-label={t('voice.play_aria')}
          >
            ▶
          </button>
          <div
            style={{
              flex: 1,
              display: 'flex',
              gap: 2,
              alignItems: 'center',
              height: 28,
            }}
          >
            <Wave bars={80} seed={9} />
          </div>
          <span
            style={{
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--ink-3)',
            }}
          >
            0:04
          </span>
        </div>
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 15,
            lineHeight: 1.55,
          }}
        >
          “{previewText}”
        </div>
        <div className="rule" />
        <span className="label">{t('voice.preview_try')}</span>
        <input
          className="bare"
          value={text}
          onChange={(e) => setText(e.target.value)}
          style={{
            border: '1px solid var(--rule)',
            padding: '10px 12px',
            borderRadius: 8,
          }}
        />
        <div className="row" style={{ alignItems: 'center' }}>
          <div className="flex1" />
          <button
            type="button"
            className="btn"
            disabled={!text.trim() || fetching}
            onClick={() => void speakIt()}
          >
            {fetching ? t('voice.speak_generating') : t('voice.speak_cta')}
          </button>
        </div>
      </div>
      <button
        type="button"
        className="btn accent"
        disabled={activating}
        onClick={() => void activateAndBack()}
      >
        {activated
          ? t('voice.back_to_chat')
          : activating
            ? t('voice.activating')
            : t('voice.activate_and_back')}
      </button>
    </div>
  )
}
