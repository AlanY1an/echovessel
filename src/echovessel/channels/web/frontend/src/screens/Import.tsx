/**
 * Import screen — 3-step wizard (Source · Filter · Review) over the
 * `/api/admin/import/*` pipeline.
 *
 *   Step 0 · Source   pick paste / file / (stubbed channels)
 *   Step 1 · Filter   live char + line count + estimate readout
 *   Step 2 · Review   chip summary; confirm runs start + SSE progress
 *
 * Wiring:
 *
 *   next(0→1)   upload_text   (hook auto-chains estimate)
 *   next(1→2)   — (client-side; estimate already fetched)
 *   confirm     start import  → SSE stream drives running / done / error
 *
 * Only the `paste` + `file` source tiles are wired to real endpoints;
 * `imessage` / `whatsapp` / `discord` / `email` render a "coming soon"
 * chip and disable the next button when selected.
 */

import { Fragment, useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useImport } from '../hooks/useImport'
import type {
  ImportDoneSummary,
  ImportEstimateResponse,
  ImportProgressSnapshot,
  ImportUploadResponse,
} from '../api/types'
import type { ImportPhase } from '../hooks/useImport'

const MAX_PASTE_CHARS = 50_000

type SourceKey = 'paste' | 'file' | 'imessage' | 'whatsapp' | 'discord' | 'email'

const WIRED_SOURCES: readonly SourceKey[] = ['paste', 'file']

interface ImportProps {
  onBack: () => void
}

export function ImportScreen({ onBack }: ImportProps) {
  const { t } = useTranslation()
  const {
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
  } = useImport()

  const [step, setStep] = useState<0 | 1 | 2>(0)
  const [source, setSource] = useState<SourceKey>('paste')
  const [text, setText] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)
  const [sourceLabel, setSourceLabel] = useState('')

  const sourceWired = WIRED_SOURCES.includes(source)
  // "Running" sub-view switches in only once the pipeline has actually
  // started — `pipelineId !== null` is the authoritative signal. Errors
  // hit during upload / estimate stay on the wizard step so the user
  // sees them inline next to the field that caused them.
  const isRunning =
    pipelineId !== null &&
    (phase === 'starting' ||
      phase === 'running' ||
      phase === 'done' ||
      phase === 'error')

  const charCount = text.length
  const lineCount = useMemo(() => {
    if (text.length === 0) return 0
    return text.split(/\r?\n/).length
  }, [text])

  const computedLabel = useMemo(() => {
    if (sourceLabel.trim() !== '') return sourceLabel.trim()
    if (fileName !== null) return fileName
    return `${t('import.default_label_prefix')} ${new Date().toISOString().slice(0, 10)}`
  }, [sourceLabel, fileName, t])

  const canAdvanceFromSource = sourceWired && text.trim().length > 0 && charCount <= MAX_PASTE_CHARS
  const canAdvanceFromFilter = phase === 'estimate' && estimate !== null

  const handleFilePick = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      if (!file) return
      setFileName(file.name)
      try {
        const body = await file.text()
        setText(body)
      } catch (err) {
        console.warn('file read failed', err)
      }
    },
    [],
  )

  const handleNext = useCallback(async () => {
    if (step === 0) {
      if (!canAdvanceFromSource) return
      setStep(1)
      // Kick the upload_text → estimate chain.
      await submitText(text.trim(), computedLabel)
      return
    }
    if (step === 1) {
      if (!canAdvanceFromFilter) return
      setStep(2)
      return
    }
  }, [step, canAdvanceFromSource, canAdvanceFromFilter, submitText, text, computedLabel])

  const handleConfirm = useCallback(async () => {
    await startImport()
  }, [startImport])

  const handleBack = useCallback(() => {
    if (isRunning) {
      // Running / done / error sub-view — treat back as "return to Admin".
      reset()
      onBack()
      return
    }
    if (step === 0) {
      onBack()
      return
    }
    if (step === 1) {
      // Stepping back to Source discards the pending upload so the next
      // advance re-uploads cleanly.
      reset()
      setStep(0)
      return
    }
    setStep(1)
  }, [step, isRunning, onBack, reset])

  const handleDoneReturn = useCallback(() => {
    reset()
    onBack()
  }, [reset, onBack])

  const stepperChipLabel = isRunning
    ? t(`import.chip_phase_${phase}`, { defaultValue: phase })
    : t('import.chip_default')

  return (
    <div className="imp">
      <Stepper
        step={step}
        chipLabel={stepperChipLabel}
        running={isRunning}
        onJump={(i) => {
          if (isRunning) return
          if (i <= step) setStep(i as 0 | 1 | 2)
        }}
      />

      {isRunning ? (
        <RunningBody
          phase={phase}
          upload={upload}
          progress={progress}
          writesByTarget={writesByTarget}
          droppedCount={dropped.length}
          report={report}
          error={error}
          onCancel={() => void cancelImport()}
        />
      ) : (
        <>
          {step === 0 && (
            <SourceStep
              source={source}
              setSource={setSource}
              text={text}
              setText={setText}
              fileName={fileName}
              onFilePick={handleFilePick}
            />
          )}
          {step === 1 && (
            <FilterStep
              phase={phase}
              text={text}
              charCount={charCount}
              lineCount={lineCount}
              sourceLabel={sourceLabel}
              setSourceLabel={setSourceLabel}
              estimate={estimate}
              upload={upload}
              error={error}
            />
          )}
          {step === 2 && (
            <ReviewStep
              upload={upload}
              estimate={estimate}
              charCount={charCount}
              sourceKey={source}
              sourceLabel={computedLabel}
              error={error}
            />
          )}
        </>
      )}

      <Footer
        step={step}
        isRunning={isRunning}
        phase={phase}
        canAdvanceFromSource={canAdvanceFromSource}
        canAdvanceFromFilter={canAdvanceFromFilter}
        onBack={handleBack}
        onNext={() => void handleNext()}
        onConfirm={() => void handleConfirm()}
        onDoneReturn={handleDoneReturn}
      />
    </div>
  )
}

// ─── Stepper ─────────────────────────────────────────────────────────

function Stepper({
  step,
  chipLabel,
  running,
  onJump,
}: {
  step: 0 | 1 | 2
  chipLabel: string
  running: boolean
  onJump: (i: number) => void
}) {
  const { t } = useTranslation()
  const labels = [
    t('import.step_source'),
    t('import.step_filter'),
    t('import.step_review'),
  ]
  return (
    <div className="imp-stepper">
      {labels.map((lbl, i) => {
        const on = !running && i === step
        const done = running || i < step
        const cls = ['imp-step', on ? 'on' : '', done && i < step ? 'done' : '']
          .filter(Boolean)
          .join(' ')
        return (
          <Fragment key={i}>
            <button
              type="button"
              className={cls}
              onClick={() => onJump(i)}
              style={{ display: 'flex', alignItems: 'center', gap: 8 }}
            >
              <div className="dot">{i < step || running ? '✓' : i + 1}</div>
              <div className="lbl">{lbl}</div>
            </button>
            {i < 2 && (
              <div
                className={`bar ${running || i < step ? 'done' : ''}`}
              />
            )}
          </Fragment>
        )
      })}
      <div className="flex1" />
      <span className="chip">{chipLabel}</span>
    </div>
  )
}

// ─── Step 0 · Source ─────────────────────────────────────────────────

const SOURCE_TILES: {
  k: SourceKey
  icon: string
  titleKey: string
  subKey: string
}[] = [
  { k: 'paste', icon: '✎', titleKey: 'import.src_paste_t', subKey: 'import.src_paste_s' },
  { k: 'file', icon: '▤', titleKey: 'import.src_file_t', subKey: 'import.src_file_s' },
  { k: 'imessage', icon: '◷', titleKey: 'import.src_imessage_t', subKey: 'import.src_imessage_s' },
  { k: 'whatsapp', icon: '◐', titleKey: 'import.src_whatsapp_t', subKey: 'import.src_whatsapp_s' },
  { k: 'discord', icon: '◈', titleKey: 'import.src_discord_t', subKey: 'import.src_discord_s' },
  { k: 'email', icon: '✉', titleKey: 'import.src_email_t', subKey: 'import.src_email_s' },
]

function SourceStep({
  source,
  setSource,
  text,
  setText,
  fileName,
  onFilePick,
}: {
  source: SourceKey
  setSource: (k: SourceKey) => void
  text: string
  setText: (v: string) => void
  fileName: string | null
  onFilePick: (e: React.ChangeEvent<HTMLInputElement>) => void | Promise<void>
}) {
  const { t } = useTranslation()
  const overLimit = text.length > MAX_PASTE_CHARS
  return (
    <div className="imp-body">
      <h2 className="title">{t('import.source_title')}</h2>
      <p style={{ color: 'var(--ink-2)', maxWidth: 560, lineHeight: 1.55 }}>
        {t('import.source_lead')}
      </p>

      <div className="src-grid" style={{ marginTop: 24 }}>
        {SOURCE_TILES.map((tile) => {
          const wired = WIRED_SOURCES.includes(tile.k)
          const selected = source === tile.k
          const cls = `src-tile ${selected ? 'on' : ''}`
          return (
            <button
              key={tile.k}
              type="button"
              className={cls}
              onClick={() => setSource(tile.k)}
            >
              <div className="chk">{selected ? '✓' : ''}</div>
              <div className="icn">{tile.icon}</div>
              <div className="t">{t(tile.titleKey)}</div>
              <div className="s">{t(tile.subKey)}</div>
              {selected && !wired && (
                <span
                  className="chip warn"
                  style={{ alignSelf: 'flex-start', marginTop: 4 }}
                >
                  {t('import.coming_soon')}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {source === 'paste' && (
        <div className="stack g-2" style={{ marginTop: 24, maxWidth: 880 }}>
          <span className="label">{t('import.paste_label')}</span>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t('import.paste_placeholder')}
            className="card p-3"
            style={{
              minHeight: 180,
              resize: 'vertical',
              outline: 'none',
              fontFamily: 'var(--serif)',
              fontSize: 14,
              lineHeight: 1.55,
              background: 'var(--paper)',
            }}
          />
          <div
            style={{
              fontFamily: 'var(--mono)',
              fontSize: 10,
              color: overLimit ? 'var(--accent)' : 'var(--ink-3)',
            }}
          >
            {text.length.toLocaleString()} / {MAX_PASTE_CHARS.toLocaleString()}{' '}
            {t('import.chars_suffix')}
            {overLimit && ` · ${t('import.over_limit_hint')}`}
          </div>
        </div>
      )}

      {source === 'file' && (
        <label
          className="card p-4"
          style={{
            marginTop: 24,
            maxWidth: 880,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 8,
            cursor: 'pointer',
            borderStyle: 'dashed',
            borderColor: 'var(--rule-strong)',
            borderWidth: 2,
            padding: 32,
            textAlign: 'center',
          }}
        >
          <input
            type="file"
            accept=".txt,.md,text/plain,text/markdown"
            style={{ display: 'none' }}
            onChange={(e) => void onFilePick(e)}
          />
          <div style={{ fontSize: 24 }}>▤</div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>
            {fileName ?? t('import.file_pick_title')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--ink-3)' }}>
            {fileName
              ? `${text.length.toLocaleString()} ${t('import.chars_suffix')}`
              : t('import.file_pick_sub')}
          </div>
        </label>
      )}
    </div>
  )
}

// ─── Step 1 · Filter ─────────────────────────────────────────────────

function FilterStep({
  phase,
  text,
  charCount,
  lineCount,
  sourceLabel,
  setSourceLabel,
  estimate,
  upload,
  error,
}: {
  phase: ImportPhase
  text: string
  charCount: number
  lineCount: number
  sourceLabel: string
  setSourceLabel: (v: string) => void
  estimate: ImportEstimateResponse | null
  upload: ImportUploadResponse | null
  error: string | null
}) {
  const { t } = useTranslation()
  const sample = useMemo(() => {
    return text.split(/\r?\n/).slice(0, 6).filter((l) => l.trim().length > 0)
  }, [text])

  const estimating = phase === 'uploading' || phase === 'estimating'

  return (
    <div
      className="imp-body"
      style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 28 }}
    >
      <div className="stack g-4">
        <h2 className="title">{t('import.filter_title')}</h2>

        <div className="stack g-2">
          <span className="label">{t('import.source_label_field')}</span>
          <div className="card p-3">
            <input
              value={sourceLabel}
              onChange={(e) => setSourceLabel(e.target.value)}
              placeholder={t('import.source_label_placeholder')}
              style={{
                width: '100%',
                border: 0,
                outline: 0,
                background: 'transparent',
                fontSize: 14,
              }}
            />
          </div>
        </div>

        <div className="stack g-2">
          <span className="label">{t('import.range_label')}</span>
          <div className="card p-3">
            <div className="row g-2" style={{ alignItems: 'center' }}>
              <span className="chip">{t('import.range_start')}</span>
              <div
                style={{
                  flex: 1,
                  height: 4,
                  background: 'var(--rule-strong)',
                  borderRadius: 2,
                  position: 'relative',
                }}
              >
                <div
                  style={{
                    position: 'absolute',
                    left: '0%',
                    right: '0%',
                    top: 0,
                    bottom: 0,
                    background: 'var(--ink)',
                    borderRadius: 2,
                  }}
                />
              </div>
              <span className="chip">{t('import.range_end')}</span>
            </div>
            <div
              style={{
                fontFamily: 'var(--mono)',
                fontSize: 10,
                color: 'var(--ink-3)',
                marginTop: 8,
              }}
            >
              {t('import.range_hint')}
            </div>
          </div>
        </div>

        {estimating && (
          <div className="chip dashed">{t('import.estimating')}</div>
        )}
        {!estimating && estimate !== null && (
          <div className="row g-2" style={{ flexWrap: 'wrap' }}>
            <span className="chip">
              {t('import.tokens_in_chip', {
                count: estimate.tokens_in,
              })}
            </span>
            <span className="chip">
              {t('import.tokens_out_chip', {
                count: estimate.tokens_out_est,
              })}
            </span>
            <span className="chip accent">
              {t('import.cost_chip', {
                cost: estimate.cost_usd_est.toFixed(4),
              })}
            </span>
          </div>
        )}
        {estimate?.note && (
          <div
            className="card p-3"
            style={{ fontSize: 12, color: 'var(--ink-2)' }}
          >
            {estimate.note}
          </div>
        )}
        {error !== null && (
          <div className="chip warn">⚠ {error}</div>
        )}
      </div>

      <div className="stack g-3">
        <div className="row g-2" style={{ alignItems: 'baseline' }}>
          <div
            style={{
              fontFamily: 'var(--serif)',
              fontSize: 40,
              letterSpacing: '-0.02em',
            }}
          >
            {charCount.toLocaleString()}
          </div>
          <div style={{ color: 'var(--ink-3)', fontSize: 13 }}>
            {t('import.chars_suffix')} · {lineCount.toLocaleString()}{' '}
            {t('import.lines_suffix')}
            {upload !== null && ` · ${formatBytes(upload.size_bytes)}`}
          </div>
        </div>

        <div className="card p-3 stack g-2" style={{ flex: 1, minHeight: 200 }}>
          <span className="label">{t('import.sample_label')}</span>
          {sample.length === 0 && (
            <div style={{ fontSize: 13, color: 'var(--ink-3)' }}>
              {t('import.sample_empty')}
            </div>
          )}
          {sample.map((line, i) => (
            <div
              key={i}
              className="row g-3"
              style={{
                paddingTop: i ? 6 : 0,
                borderTop: i ? '1px dashed var(--rule)' : undefined,
              }}
            >
              <span className="label" style={{ width: 28 }}>
                {String(i + 1).padStart(2, '0')}
              </span>
              <span
                style={{
                  flex: 1,
                  color: 'var(--ink-2)',
                  fontSize: 13,
                  fontFamily: 'var(--serif)',
                }}
              >
                {line}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Step 2 · Review ─────────────────────────────────────────────────

function ReviewStep({
  upload,
  estimate,
  charCount,
  sourceKey,
  sourceLabel,
  error,
}: {
  upload: ImportUploadResponse | null
  estimate: ImportEstimateResponse | null
  charCount: number
  sourceKey: SourceKey
  sourceLabel: string
  error: string | null
}) {
  const { t } = useTranslation()
  return (
    <div className="imp-body">
      <h2 className="title">{t('import.review_title')}</h2>
      <p
        style={{
          color: 'var(--ink-2)',
          maxWidth: 560,
          lineHeight: 1.55,
          marginTop: 6,
        }}
      >
        {t('import.review_lead')}
      </p>

      <div
        className="row g-2"
        style={{ flexWrap: 'wrap', margin: '16px 0 22px' }}
      >
        <span className="chip">
          {t('import.chip_source', {
            name: t(`import.src_${sourceKey}_t`, { defaultValue: sourceKey }),
          })}
        </span>
        <span className="chip">{sourceLabel}</span>
        <span className="chip">
          {t('import.chars_chip', { count: charCount })}
        </span>
        {upload !== null && (
          <span className="chip">{formatBytes(upload.size_bytes)}</span>
        )}
        {estimate !== null && (
          <>
            <span className="chip">
              {t('import.tokens_in_chip', { count: estimate.tokens_in })}
            </span>
            <span className="chip accent">
              {t('import.cost_chip', {
                cost: estimate.cost_usd_est.toFixed(4),
              })}
            </span>
          </>
        )}
      </div>

      <div className="diff-block">
        <span className="label">{t('import.review_plan_label')}</span>
        <div className="diff-line keep">{t('import.review_plan_keep')}</div>
        <div className="diff-line add">{t('import.review_plan_add')}</div>
      </div>

      {error !== null && (
        <div className="chip warn" style={{ marginTop: 12 }}>
          ⚠ {error}
        </div>
      )}
    </div>
  )
}

// ─── Running / Done / Error body ─────────────────────────────────────

function RunningBody({
  phase,
  upload,
  progress,
  writesByTarget,
  droppedCount,
  report,
  error,
  onCancel,
}: {
  phase: ImportPhase
  upload: ImportUploadResponse | null
  progress: ImportProgressSnapshot | null
  writesByTarget: Record<string, number>
  droppedCount: number
  report: ImportDoneSummary | null
  error: string | null
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const isDone = phase === 'done' && report !== null
  const isError = phase === 'error'
  const isActive = phase === 'starting' || phase === 'running'

  const pct =
    progress && progress.total_chunks > 0
      ? Math.min(
          100,
          Math.round((progress.current_chunk / progress.total_chunks) * 100),
        )
      : 0

  const liveWrites = isDone && report ? report.writes_by_target : writesByTarget
  const totalWrites = Object.values(liveWrites).reduce((a, b) => a + b, 0)

  return (
    <div className="imp-body">
      <h2 className="title">
        {isError
          ? t('import.running_title_error')
          : report !== null
            ? report.status === 'cancelled'
              ? t('import.running_title_cancelled')
              : t('import.running_title_done')
            : t('import.running_title_running')}
      </h2>
      {upload !== null && (
        <p
          style={{
            color: 'var(--ink-2)',
            maxWidth: 560,
            lineHeight: 1.55,
            marginTop: 6,
          }}
        >
          {upload.source_label} · {formatBytes(upload.size_bytes)}
        </p>
      )}

      {!isError && (
        <div className="card p-4 stack g-3" style={{ marginTop: 16, maxWidth: 720 }}>
          <div className="row g-2" style={{ alignItems: 'baseline' }}>
            <div
              style={{
                fontFamily: 'var(--serif)',
                fontSize: 32,
                letterSpacing: '-0.02em',
              }}
            >
              {pct}%
            </div>
            <div
              className="flex1"
              style={{ color: 'var(--ink-3)', fontSize: 13 }}
            >
              {progress
                ? t('import.chunk_counter', {
                    current: progress.current_chunk,
                    total: progress.total_chunks,
                  })
                : t('import.preparing')}
            </div>
          </div>
          <div
            style={{
              height: 6,
              background: 'var(--rule-strong)',
              borderRadius: 3,
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${pct}%`,
                height: '100%',
                background: 'var(--accent)',
                transition: 'width 160ms ease',
              }}
            />
          </div>
          {droppedCount > 0 && !isDone && (
            <div
              style={{
                fontFamily: 'var(--mono)',
                fontSize: 10,
                color: 'var(--ink-3)',
              }}
            >
              {t('import.dropped_live', { count: droppedCount })}
            </div>
          )}
        </div>
      )}

      {isError && error !== null && (
        <div className="chip warn" style={{ marginTop: 16 }}>
          ⚠ {error}
        </div>
      )}

      <div
        className="card p-3 stack g-2"
        style={{ marginTop: 16, maxWidth: 720 }}
      >
        <span className="label">
          {isDone ? t('import.writes_total_label') : t('import.writes_live_label')}
        </span>
        {totalWrites === 0 ? (
          <div style={{ fontSize: 13, color: 'var(--ink-3)' }}>
            {isDone
              ? t('import.writes_empty_done')
              : t('import.writes_empty_live')}
          </div>
        ) : (
          Object.entries(liveWrites).map(([target, n]) => (
            <div
              key={target}
              className="row g-3"
              style={{ alignItems: 'center' }}
            >
              <span
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 13,
                  color: 'var(--ink)',
                  minWidth: 32,
                }}
              >
                {n}
              </span>
              <span style={{ flex: 1, fontSize: 13 }}>
                {t(`import.target.${target}`, { defaultValue: target })}
              </span>
              <span className="label">{target}</span>
            </div>
          ))
        )}
      </div>

      {isDone && report !== null && report.dropped_count > 0 && (
        <div
          style={{
            marginTop: 12,
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--ink-3)',
          }}
        >
          {t('import.dropped_done', { count: report.dropped_count })}
        </div>
      )}

      {isActive && (
        <div style={{ marginTop: 20 }}>
          <button type="button" className="btn ghost" onClick={onCancel}>
            {t('import.cancel_running')}
          </button>
        </div>
      )}
    </div>
  )
}

// ─── Footer ──────────────────────────────────────────────────────────

function Footer({
  step,
  isRunning,
  phase,
  canAdvanceFromSource,
  canAdvanceFromFilter,
  onBack,
  onNext,
  onConfirm,
  onDoneReturn,
}: {
  step: 0 | 1 | 2
  isRunning: boolean
  phase: ImportPhase
  canAdvanceFromSource: boolean
  canAdvanceFromFilter: boolean
  onBack: () => void
  onNext: () => void
  onConfirm: () => void
  onDoneReturn: () => void
}) {
  const { t } = useTranslation()

  if (isRunning) {
    const finished = phase === 'done' || phase === 'error'
    return (
      <div className="imp-foot">
        <button type="button" className="btn ghost" onClick={onBack}>
          ← {t('import.back_to_admin')}
        </button>
        <div className="flex1" />
        {finished && (
          <button type="button" className="btn accent" onClick={onDoneReturn}>
            {t('import.back_to_admin')} →
          </button>
        )}
      </div>
    )
  }

  const nextDisabled =
    (step === 0 && !canAdvanceFromSource) ||
    (step === 1 && !canAdvanceFromFilter)

  return (
    <div className="imp-foot">
      <button type="button" className="btn ghost" onClick={onBack}>
        ← {step === 0 ? t('import.cancel') : t('import.back')}
      </button>
      <div className="flex1" />
      <span
        style={{
          fontFamily: 'var(--mono)',
          fontSize: 11,
          color: 'var(--ink-3)',
        }}
      >
        {t('import.step_counter', { current: step + 1, total: 3 })}
      </span>
      {step < 2 ? (
        <button
          type="button"
          className="btn accent"
          onClick={onNext}
          disabled={nextDisabled}
        >
          {t('import.next')} →
        </button>
      ) : (
        <button
          type="button"
          className="btn accent"
          onClick={onConfirm}
          disabled={phase === 'starting'}
        >
          {phase === 'starting' ? t('import.starting') : t('import.confirm')} →
        </button>
      )}
    </div>
  )
}

// ─── Shared ──────────────────────────────────────────────────────────

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}
