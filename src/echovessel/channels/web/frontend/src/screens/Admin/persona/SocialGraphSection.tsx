import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { EntityRow as EntityRowData } from '../../../api/types'
import { useEntities } from '../../../hooks/useEntities'
import { EntityRow } from './components/EntityRow'
import { ManualEntityDialog } from './components/ManualEntityDialog'
import { UncertainCallout } from './components/UncertainCallout'

/**
 * Persona-tab section 3 · SOCIAL GRAPH (extraction writes name,
 * slow_tick writes description, owner can override).
 *
 * Hits ``GET /api/admin/memory/entities`` once on mount and refetches
 * after every mutation. Uncertain rows float to a top callout for
 * arbitration; confirmed rows group by kind and expose inline
 * description editing (server stamps ``owner_override=true`` so the
 * synthesizer leaves the prose alone after that).
 */
const KIND_ORDER: EntityRowData['kind'][] = [
  'person',
  'pet',
  'place',
  'org',
  'other',
]

export function SocialGraphSection() {
  const { t } = useTranslation()
  const {
    entities,
    uncertain,
    byKind,
    loading,
    error,
    saveDescription,
    mergeOne,
    separateOne,
    createManual,
  } = useEntities()
  const [manualOpen, setManualOpen] = useState(false)

  const findEntityById = useCallback(
    (id: number): EntityRowData | undefined =>
      entities.find((e) => e.id === id),
    [entities],
  )

  const confirmedCount = Array.from(byKind.values()).reduce(
    (sum, list) => sum + list.length,
    0,
  )
  const totalCount = confirmedCount + uncertain.length

  return (
    <section className="stack g-3">
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.persona.sections.social_graph')}</h2>
        <span className="chip">
          {t('admin.persona.entities.count', { count: totalCount })}
        </span>
        <div className="flex1" />
        <button
          type="button"
          className="btn ghost sm"
          onClick={() => setManualOpen(true)}
        >
          {t('admin.persona.entities.add_manual')}
        </button>
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

      <UncertainCallout
        uncertain={uncertain}
        findEntityById={findEntityById}
        onMerge={(id, target) => void mergeOne(id, target)}
        onSeparate={(id, other) => void separateOne(id, other)}
      />

      {loading && totalCount === 0 && (
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

      {!loading && totalCount === 0 && (
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
          {t('admin.persona.entities.empty')}
        </div>
      )}

      {KIND_ORDER.filter((k) => (byKind.get(k)?.length ?? 0) > 0).map((kind) => (
        <div key={kind} className="stack g-2">
          <span className="label">
            {t(`admin.persona.entities.kind.${kind}`)} ·{' '}
            {byKind.get(kind)!.length}
          </span>
          {byKind.get(kind)!.map((entity) => (
            <EntityRow
              key={entity.id}
              entity={entity}
              onSaveDescription={saveDescription}
            />
          ))}
        </div>
      ))}

      <ManualEntityDialog
        open={manualOpen}
        onClose={() => setManualOpen(false)}
        onSubmit={createManual}
      />
    </section>
  )
}
