import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { deleteMemoryThought } from '../../../api/client'
import { ApiError } from '../../../api/types'
import { usePersonaReflections } from '../../../hooks/usePersonaReflections'
import { FillingChainModal } from './components/FillingChainModal'
import { ThoughtRow } from './components/ThoughtRow'

/**
 * Persona-tab section 2 · REFLECTION (persona-authored, owner read-mostly).
 *
 * Renders the most recent ~10 ``L4 thought[subject='persona']`` rows
 * — the persona's notes about itself written by either the slow_tick
 * worker (background self-narrative cycle) or the in-turn ``reflect``
 * fast-loop pass. The owner can delete a row (the persona will not
 * spontaneously rewrite a deleted thought; slow_tick won't resurrect
 * it) and inspect the filling chain — the L3 events the reflection
 * crystallised from.
 */
export function ReflectionSection() {
  const { t } = useTranslation()
  const { thoughts, loading, error, refresh } = usePersonaReflections(10)
  const [modalThoughtId, setModalThoughtId] = useState<number | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const handleDelete = async (id: number) => {
    if (busyId !== null) return
    const target = thoughts.find((tt) => tt.id === id)
    const preview =
      target !== undefined && target.description.length > 80
        ? `${target.description.slice(0, 80)}…`
        : (target?.description ?? '')
    if (
      !window.confirm(
        t('admin.persona.reflection.delete_confirm', { preview }),
      )
    ) {
      return
    }
    setBusyId(id)
    try {
      await deleteMemoryThought(id, 'orphan')
      await refresh()
    } catch (err) {
      if (err instanceof ApiError) {
        window.alert(err.detail)
      } else if (err instanceof Error) {
        window.alert(err.message)
      }
    } finally {
      setBusyId(null)
    }
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

      {error !== null && (
        <div
          className="card"
          style={{
            padding: 12,
            color: 'var(--accent)',
            fontSize: 12,
            fontFamily: 'var(--mono)',
          }}
        >
          ⚠ {error}
        </div>
      )}

      {loading && thoughts.length === 0 && (
        <div
          className="card"
          style={{
            padding: 18,
            color: 'var(--ink-3)',
            fontSize: 13,
            textAlign: 'center',
          }}
        >
          {t('admin.common.loading')}
        </div>
      )}

      {!loading && thoughts.length === 0 && (
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
      )}

      {thoughts.map((thought) => (
        <ThoughtRow
          key={thought.id}
          thought={thought}
          onDelete={(id) => void handleDelete(id)}
          onSeeFilling={handleSeeFilling}
          busy={busyId === thought.id}
        />
      ))}

      <FillingChainModal
        open={modalThoughtId !== null}
        thoughtId={modalThoughtId}
        onClose={() => setModalThoughtId(null)}
      />
    </section>
  )
}
