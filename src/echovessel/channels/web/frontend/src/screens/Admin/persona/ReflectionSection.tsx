import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { PersonaThought } from '../../../api/types'
import { FillingChainModal } from './components/FillingChainModal'
import { ThoughtRow } from './components/ThoughtRow'

/**
 * Persona-tab section 2 · REFLECTION (persona-authored, owner read-mostly).
 *
 * Renders the most recent ~10 ``L4 thought[subject='persona']`` rows
 * — the persona's notes about itself written by either the slow_tick
 * worker (background self-narrative cycle) or the in-turn ``reflect``
 * fast-loop pass.
 *
 * Day-1 skeleton — no real fetch yet. The list will fill from
 * ``GET /api/admin/memory/thoughts?subject=persona&limit=10`` once
 * Worker A's Spec 1 backend lands the subject filter. The
 * ``thoughts`` const below is empty for now; the empty-state copy
 * stands in for the live list.
 */
export function ReflectionSection() {
  const { t } = useTranslation()
  const thoughts: PersonaThought[] = []
  const [modalThoughtId, setModalThoughtId] = useState<number | null>(null)

  const handleDelete = (_id: number) => {
    /* Day 4 · DELETE /api/admin/memory/thoughts/{id} via deleteMemoryThought */
  }
  const handleSeeFilling = (id: number) => setModalThoughtId(id)

  return (
    <section className="stack g-3">
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.persona.sections.reflection')}</h2>
        <span className="chip">
          {t('admin.persona.reflection.count', { count: thoughts.length })}
        </span>
        <div className="flex1" />
        <span
          style={{
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--ink-3)',
          }}
        >
          {t('admin.persona.sections.reflection_hint')}
        </span>
      </div>

      {thoughts.length === 0 ? (
        <div
          className="card"
          style={{
            padding: 18,
            color: 'var(--ink-3)',
            fontSize: 13,
            fontFamily: 'var(--serif)',
            textAlign: 'center',
          }}
        >
          {t('admin.persona.reflection.empty')}
        </div>
      ) : (
        thoughts.map((thought) => (
          <ThoughtRow
            key={thought.id}
            thought={thought}
            onDelete={handleDelete}
            onSeeFilling={handleSeeFilling}
          />
        ))
      )}

      <FillingChainModal
        open={modalThoughtId !== null}
        thoughtId={modalThoughtId}
        onClose={() => setModalThoughtId(null)}
      />
    </section>
  )
}
