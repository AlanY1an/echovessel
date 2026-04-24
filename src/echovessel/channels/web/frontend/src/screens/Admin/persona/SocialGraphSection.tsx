import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'

import type { EntityRow as EntityRowData } from '../../../api/types'
import { EntityRow } from './components/EntityRow'
import { UncertainCallout } from './components/UncertainCallout'

/**
 * Persona-tab section 3 · SOCIAL GRAPH (extraction writes name,
 * slow_tick writes description, owner can override).
 *
 * Renders the L5 entities the daemon has linked through conversation
 * (people / pets / places / orgs), grouped by ``kind``. Uncertain
 * merge candidates float to the top in an ``UncertainCallout``;
 * confirmed rows can have their description edited inline (the
 * server stamps ``owner_override=true`` on PATCH).
 *
 * Day-1 skeleton — no real fetch yet. The lists will fill from
 * ``GET /api/admin/memory/entities`` once Worker A merges. The
 * ``entities`` const stays empty for now; the empty-state placeholder
 * stands in for the live grid.
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
  const entities: EntityRowData[] = []

  const { uncertain, byKind } = useMemo(() => {
    const uncertainList: EntityRowData[] = []
    const groups = new Map<EntityRowData['kind'], EntityRowData[]>()
    for (const e of entities) {
      if (e.merge_status === 'uncertain') {
        uncertainList.push(e)
        continue
      }
      const arr = groups.get(e.kind) ?? []
      arr.push(e)
      groups.set(e.kind, arr)
    }
    for (const arr of groups.values()) {
      arr.sort((a, b) => {
        const aT = a.last_mentioned_at ?? ''
        const bT = b.last_mentioned_at ?? ''
        return bT.localeCompare(aT)
      })
    }
    return { uncertain: uncertainList, byKind: groups }
  }, [entities])

  const handleMerge = (_id: number) => {
    /* Day 4 · POST /api/admin/memory/entities/{id}/merge */
  }
  const handleSeparate = (_id: number) => {
    /* Day 4 · flip merge_status='confirmed' */
  }
  const handleSaveDescription = async (
    _entityId: number,
    _description: string,
  ): Promise<void> => {
    /* Day 4 · PATCH /api/admin/memory/entities/{id} (server sets owner_override) */
  }
  const handleAddManual = () => {
    /* Day 4 · POST /api/admin/memory/entities  (Worker A endpoint) */
  }

  return (
    <section className="stack g-3">
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.persona.sections.social_graph')}</h2>
        <span className="chip">
          {t('admin.persona.entities.count', { count: entities.length })}
        </span>
        <div className="flex1" />
        <button type="button" className="btn ghost sm" onClick={handleAddManual}>
          {t('admin.persona.entities.add_manual')}
        </button>
      </div>

      <UncertainCallout
        uncertain={uncertain}
        onMerge={handleMerge}
        onSeparate={handleSeparate}
      />

      {entities.length === 0 && (
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
              onSaveDescription={handleSaveDescription}
            />
          ))}
        </div>
      ))}
    </section>
  )
}
