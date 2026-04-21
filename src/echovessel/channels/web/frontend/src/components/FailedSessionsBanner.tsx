/**
 * FailedSessionsBanner — admin-shell banner that surfaces consolidate-worker
 * FAILED sessions. Hidden when there are zero failures. Click the chip to
 * expand a compact list with id / channel / message_count / reason so the
 * operator can decide whether to run `scripts/reset_failed_sessions.py`.
 */

import { useState } from 'react'

import { useFailedSessions } from '../hooks/useFailedSessions'

function formatStartedAt(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function shortenTrigger(trigger: string | null): string {
  if (!trigger) return ''
  const idx = trigger.indexOf('|failed:')
  return idx >= 0 ? trigger.slice(idx + '|failed:'.length, idx + 200) : trigger
}

export function FailedSessionsBanner() {
  const { count, items, loading, error, refresh } = useFailedSessions()
  const [expanded, setExpanded] = useState(false)

  if (loading || error || count === 0) return null

  return (
    <div className="adm-failed-banner">
      <button
        type="button"
        className="chip warn"
        onClick={() => setExpanded((v) => !v)}
        title="Sessions the consolidate worker marked FAILED"
      >
        ⚠ {count} session{count === 1 ? '' : 's'} failed consolidation
        {expanded ? ' ▴' : ' ▾'}
      </button>
      <button
        type="button"
        className="btn ghost sm"
        onClick={() => void refresh()}
      >
        refresh
      </button>
      {expanded && (
        <ul className="adm-failed-list">
          {items.map((s) => (
            <li key={s.id}>
              <code>{s.id}</code>
              <span className="chip">{s.channel_id}</span>
              <span className="chip dashed">{s.message_count} msgs</span>
              <span className="t-meta">{formatStartedAt(s.started_at)}</span>
              <div className="adm-failed-reason">
                {shortenTrigger(s.close_trigger)}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
