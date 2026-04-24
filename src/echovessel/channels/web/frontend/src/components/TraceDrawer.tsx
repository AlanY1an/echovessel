/**
 * TraceDrawer — dev-mode per-turn waterfall viewer.
 *
 * Mounted as a sibling of the chat surface. Open via the ▸ trace icon
 * on a persona bubble — passes the bubble's ``turn_id`` here. The
 * drawer pulls the full trace + (when the message has a session id)
 * the consolidate trace via REST. Closed by the user clicking the ✕
 * button or pressing Escape.
 *
 * Spec 4 plan §4.4 + §4.5 invariants enforced here:
 *   - Pure REST pull (no SSE).
 *   - Persona bubbles only — the user bubble has no prompt to show.
 *   - Section headers in the system / user prompt are colourised.
 *   - The retrieval table renders the exact ``ScoredMemory`` columns
 *     locked by plan §4.2.
 */

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  getConsolidateTrace,
  getTurnTrace,
} from '../api/client'
import type {
  ConsolidateTraceResponse,
  TurnTraceResponse,
  TurnTraceRetrievalRow,
  TurnTraceStep,
} from '../api/types'

interface TraceDrawerProps {
  turnId: string
  sessionId?: string | null
  onClose: () => void
}

export function TraceDrawer({
  turnId,
  sessionId,
  onClose,
}: TraceDrawerProps) {
  const { t } = useTranslation()
  const [turn, setTurn] = useState<TurnTraceResponse | null>(null)
  const [consolidate, setConsolidate] = useState<
    ConsolidateTraceResponse | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setTurn(null)
    setConsolidate(null)
    void (async () => {
      try {
        const t = await getTurnTrace(turnId)
        if (cancelled) return
        setTurn(t)
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof Error
              ? e.message
              : 'failed to load trace',
          )
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
      if (sessionId) {
        try {
          const c = await getConsolidateTrace(sessionId)
          if (!cancelled) setConsolidate(c)
        } catch {
          // 404 expected for any session that hasn't been consolidated
          // yet — keep the slot empty rather than showing an error.
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [turnId, sessionId])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="trace-drawer" role="dialog" aria-modal="true">
      <div className="trace-drawer-backdrop" onClick={onClose} />
      <div className="trace-drawer-panel">
        <div className="trace-drawer-head">
          <strong>
            {t('trace.drawer.title')} · {turnId.slice(0, 12)}
          </strong>
          <button
            type="button"
            onClick={onClose}
            aria-label="close"
            className="trace-close"
          >
            ✕
          </button>
        </div>
        {loading && <div className="trace-loading">…</div>}
        {error && <div className="trace-error">{error}</div>}
        {turn && (
          <div className="trace-body">
            <TraceHeader turn={turn} t={t} />
            <Section title={t('trace.drawer.fast_loop')}>
              <Timeline steps={turn.steps} t={t} />
            </Section>
            {turn.retrieval.length > 0 && (
              <Section title={t('trace.section.retrieval')}>
                <VectorRetrieveTable rows={turn.retrieval} />
              </Section>
            )}
            {turn.system_prompt !== null && (
              <Section title={t('trace.section.system_prompt')}>
                <PromptView text={turn.system_prompt ?? ''} />
              </Section>
            )}
            {turn.user_prompt !== null && (
              <Section title={t('trace.section.user_prompt')}>
                <PromptView text={turn.user_prompt ?? ''} />
              </Section>
            )}
            <Section title={t('trace.drawer.consolidate')}>
              {consolidate ? (
                <ConsolidateView trace={consolidate} t={t} />
              ) : (
                <div className="trace-pending">
                  {t('trace.drawer.pending')}
                </div>
              )}
            </Section>
          </div>
        )}
      </div>
    </div>
  )
}

function TraceHeader({
  turn,
  t,
}: {
  turn: TurnTraceResponse
  t: (key: string) => string
}) {
  return (
    <div className="trace-meta">
      <span>
        {t('trace.meta.duration')}: {turn.duration_ms ?? '-'}ms
      </span>
      <span>
        {t('trace.meta.first_token')}: {turn.first_token_ms ?? '-'}ms
      </span>
      <span>
        {t('trace.meta.tokens')}: {turn.input_tokens ?? '-'}→
        {turn.output_tokens ?? '-'}
      </span>
      {turn.llm_model && <span>{turn.llm_model}</span>}
    </div>
  )
}

function Section({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(true)
  return (
    <div className="trace-section">
      <div
        className="trace-section-head"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="trace-section-arrow">{open ? '▾' : '▸'}</span>
        <span className="trace-section-title">{title}</span>
      </div>
      {open && <div className="trace-section-body">{children}</div>}
    </div>
  )
}

function Timeline({
  steps,
  t,
}: {
  steps: TurnTraceStep[]
  t: (key: string) => string
}) {
  if (steps.length === 0) {
    return <div className="trace-empty">—</div>
  }
  return (
    <ul className="trace-timeline">
      {steps.map((s) => (
        <StageRow key={`${s.stage}-${s.t_ms}`} step={s} t={t} />
      ))}
    </ul>
  )
}

function StageRow({
  step,
  t,
}: {
  step: TurnTraceStep
  t: (key: string) => string
}) {
  const [open, setOpen] = useState(false)
  const hasDetail =
    step.detail && Object.keys(step.detail).length > 0
  const labelKey = `trace.stage.${step.stage}`
  const label = t(labelKey)
  const display = label === labelKey ? step.stage : label
  return (
    <li className="trace-stage">
      <div
        className="trace-stage-row"
        onClick={() => hasDetail && setOpen((v) => !v)}
      >
        <span className="trace-stage-bullet">●</span>
        <span className="trace-stage-name">{display}</span>
        <span className="trace-stage-dur">{step.duration_ms} ms</span>
        {hasDetail && (
          <span className="trace-stage-arrow">{open ? '▾' : '▸'}</span>
        )}
      </div>
      {open && hasDetail && (
        <pre className="trace-detail">
          {JSON.stringify(step.detail, null, 2)}
        </pre>
      )}
    </li>
  )
}

function VectorRetrieveTable({ rows }: { rows: TurnTraceRetrievalRow[] }) {
  return (
    <table className="trace-retrieval">
      <thead>
        <tr>
          <th>id</th>
          <th>type</th>
          <th>desc</th>
          <th>rec</th>
          <th>rel</th>
          <th>imp</th>
          <th>relat</th>
          <th>anchor</th>
          <th>total</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.node_id ?? `${r.type}-${r.desc_snippet}`}>
            <td>{r.node_id}</td>
            <td>{r.type}</td>
            <td className="trace-retrieval-desc">{r.desc_snippet}</td>
            <td>{r.recency}</td>
            <td>{r.relevance}</td>
            <td>{r.impact}</td>
            <td>{r.relational}</td>
            <td>
              {r.entity_anchor}
              {r.anchored ? ' ⚓' : ''}
            </td>
            <td>
              <strong>{r.total}</strong>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Renders a prompt block, colourising ``# Section header`` lines. */
function PromptView({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // No-op: clipboard not available in some browsers.
    }
  }
  const lines = text.split('\n')
  return (
    <div className="trace-prompt">
      <button
        type="button"
        className="trace-copy"
        onClick={() => void onCopy()}
      >
        {copied ? '✓' : 'copy'}
      </button>
      <pre>
        {lines.map((line, i) => {
          const isHeader = line.startsWith('# ')
          return (
            <div
              key={i}
              className={isHeader ? 'trace-prompt-header' : ''}
            >
              {line || ' '}
            </div>
          )
        })}
      </pre>
    </div>
  )
}

function ConsolidateView({
  trace,
  t,
}: {
  trace: ConsolidateTraceResponse
  t: (key: string) => string
}) {
  const phases: Array<[string, Record<string, unknown> | null]> = [
    ['A', trace.phase_a],
    ['B', trace.phase_b],
    ['C', trace.phase_c],
    ['D', trace.phase_d],
    ['E', trace.phase_e],
    ['F', trace.phase_f],
    ['G', trace.phase_g],
  ]
  return (
    <div className="trace-consolidate">
      {phases.map(([key, payload]) => (
        <PhaseCard key={key} phaseKey={key} payload={payload} t={t} />
      ))}
    </div>
  )
}

function PhaseCard({
  phaseKey,
  payload,
  t,
}: {
  phaseKey: string
  payload: Record<string, unknown> | null
  t: (key: string) => string
}) {
  const [open, setOpen] = useState(false)
  const labelKey = `trace.phase.${phaseKey.toLowerCase()}`
  const label = t(labelKey)
  return (
    <div className="trace-phase">
      <div
        className="trace-phase-head"
        onClick={() => payload && setOpen((v) => !v)}
      >
        <strong>{phaseKey}</strong>
        <span className="trace-phase-label">
          {label === labelKey ? '' : label}
        </span>
        {payload === null && (
          <em className="trace-phase-empty">—</em>
        )}
        {payload && (
          <span className="trace-phase-arrow">{open ? '▾' : '▸'}</span>
        )}
      </div>
      {open && payload && (
        <pre className="trace-detail">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </div>
  )
}
