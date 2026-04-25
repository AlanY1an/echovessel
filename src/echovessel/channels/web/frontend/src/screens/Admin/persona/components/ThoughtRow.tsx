import { useTranslation } from 'react-i18next'

import type { PersonaThought } from '../../../../api/types'
import { formatDate } from '../../helpers'

/**
 * One row in the Reflection section's persona-subject thought list.
 *
 * Shows description + a source tag (slow_tick · reflection) + a
 * timestamp + delete + "see filling chain" affordances. Stateless —
 * the parent section owns the list, refetch, and the modal trigger.
 */
export function ThoughtRow({
  thought,
  onDelete,
  onSeeFilling,
  busy = false,
}: {
  thought: PersonaThought
  onDelete: (id: number) => void
  onSeeFilling: (id: number) => void
  busy?: boolean
}) {
  const { t } = useTranslation()
  const sourceLabel =
    thought.source === 'slow_tick'
      ? t('admin.persona.reflection.source_slow_tick')
      : t('admin.persona.reflection.source_reflection')
  const fillingCount = thought.filling_event_ids.length

  return (
    <div
      className="card"
      style={{
        padding: 14,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div className="row g-2" style={{ alignItems: 'center', fontSize: 11 }}>
        <span style={{ color: 'var(--ink-3)', fontFamily: 'var(--mono)' }}>
          {formatDate(thought.created_at)}
        </span>
        <span className="chip">{sourceLabel}</span>
        <div className="flex1" />
        <button
          type="button"
          className="btn ghost sm"
          disabled={busy || fillingCount === 0}
          onClick={() => onSeeFilling(thought.id)}
        >
          {t('admin.persona.reflection.see_filling', { count: fillingCount })}
        </button>
        <button
          type="button"
          className="btn ghost sm"
          disabled={busy}
          onClick={() => onDelete(thought.id)}
          aria-label={t('admin.persona.reflection.delete_aria')}
          title={t('admin.persona.reflection.delete_aria')}
          style={{ color: 'var(--accent)' }}
        >
          🗑
        </button>
      </div>
      <div
        style={{
          fontFamily: 'var(--serif)',
          fontSize: 14,
          lineHeight: 1.6,
          color: 'var(--ink-2)',
          whiteSpace: 'pre-wrap',
        }}
      >
        {thought.description}
      </div>
    </div>
  )
}
