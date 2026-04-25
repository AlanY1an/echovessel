import { useTranslation } from 'react-i18next'

import type { EntityRow as EntityRowData } from '../../../../api/types'

/**
 * Top-of-section callout listing every entity in ``merge_status='uncertain'``.
 *
 * Each row asks "X ↔ Y · same person?" with two buttons:
 *   是 · merge   → POST /api/admin/memory/entities/{id}/merge { target_id }
 *   不是 · 分开  → POST /api/admin/memory/entities/{id}/confirm-separate { other_id }
 *
 * Both ids come from the row itself: the daemon writes
 * ``merge_target_id`` on the uncertain row pointing at the proposed
 * partner. We look the partner's display name up via ``findEntityById``
 * (provided by the parent so we don't double-fetch). Rows whose
 * partner can't be resolved (deleted, race) fall back to "id #N" so
 * the owner can still arbitrate.
 */
export function UncertainCallout({
  uncertain,
  findEntityById,
  onMerge,
  onSeparate,
}: {
  uncertain: EntityRowData[]
  findEntityById: (id: number) => EntityRowData | undefined
  onMerge: (entityId: number, targetId: number) => void
  onSeparate: (entityId: number, otherId: number) => void
}) {
  const { t } = useTranslation()
  // Drop rows without a partner pointer — they can't be arbitrated
  // without a second id.
  const arbitratable = uncertain.filter(
    (e): e is EntityRowData & { merge_target_id: number } =>
      e.merge_target_id !== null,
  )
  if (arbitratable.length === 0) return null
  return (
    <div
      className="card"
      style={{
        padding: 14,
        borderLeft: '3px solid var(--accent)',
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
      }}
    >
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <span className="label" style={{ color: 'var(--accent)' }}>
          {t('admin.persona.entities.uncertain_title')}
        </span>
        <span className="chip">{arbitratable.length}</span>
      </div>
      {arbitratable.map((entity) => {
        const target = findEntityById(entity.merge_target_id)
        const targetLabel =
          target?.canonical_name ?? `#${entity.merge_target_id}`
        return (
          <div
            key={entity.id}
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
              paddingLeft: 4,
            }}
          >
            <div
              style={{
                fontFamily: 'var(--serif)',
                fontSize: 14,
                color: 'var(--ink-2)',
              }}
            >
              {t('admin.persona.entities.uncertain_question', {
                name: entity.canonical_name,
                other: targetLabel,
              })}
            </div>
            <div className="row g-2">
              <button
                type="button"
                className="btn sm"
                onClick={() => onMerge(entity.id, entity.merge_target_id)}
              >
                {t('admin.persona.entities.merge_same')}
              </button>
              <button
                type="button"
                className="btn ghost sm"
                onClick={() => onSeparate(entity.id, entity.merge_target_id)}
              >
                {t('admin.persona.entities.separate')}
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
