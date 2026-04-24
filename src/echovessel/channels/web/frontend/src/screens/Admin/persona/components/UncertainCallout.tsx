import { useTranslation } from 'react-i18next'

import type { EntityRow as EntityRowData } from '../../../../api/types'

/**
 * Top-of-section callout listing every entity in ``merge_status='uncertain'``.
 *
 * Each row asks "X ↔ Y · same person?" with two buttons:
 *   是 · merge   → POST /api/admin/memory/entities/{id}/merge
 *   不是 · 分开  → flips merge_status to 'confirmed'
 *
 * Day-1 skeleton: renders the list + handlers but the parent section
 * only fans out the click events. Real arbitration endpoints land
 * once Worker A merges the contract.
 */
export function UncertainCallout({
  uncertain,
  onMerge,
  onSeparate,
}: {
  uncertain: EntityRowData[]
  onMerge: (entityId: number) => void
  onSeparate: (entityId: number) => void
}) {
  const { t } = useTranslation()
  if (uncertain.length === 0) return null
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
        <span className="chip">{uncertain.length}</span>
      </div>
      {uncertain.map((entity) => (
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
              aliases: entity.aliases.slice(0, 2).join(' / ') || '?',
            })}
          </div>
          <div className="row g-2">
            <button
              type="button"
              className="btn sm"
              onClick={() => onMerge(entity.id)}
            >
              {t('admin.persona.entities.merge_same')}
            </button>
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => onSeparate(entity.id)}
            >
              {t('admin.persona.entities.separate')}
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
