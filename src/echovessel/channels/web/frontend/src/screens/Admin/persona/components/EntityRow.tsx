import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { EntityRow as EntityRowData } from '../../../../api/types'
import { formatDate } from '../../helpers'
import { EntityDescriptionEditor } from './EntityDescriptionEditor'

/**
 * Single entity card in the Social Graph section. Shows
 * canonical_name + linked event count + last-mentioned timestamp +
 * description preview. Clicking ``[编辑]`` swaps the description block
 * for an inline ``EntityDescriptionEditor`` whose Save call writes
 * via ``PATCH /api/admin/memory/entities/{id}`` — the server stamps
 * ``owner_override=true`` automatically; the client never sends the
 * flag.
 */
export function EntityRow({
  entity,
  onSaveDescription,
}: {
  entity: EntityRowData
  /**
   * Save handler — receives the raw description text. The server
   * sets ``owner_override=true`` based on the endpoint, never the
   * client. Throws to signal failure (parent shows toast).
   */
  onSaveDescription: (entityId: number, description: string) => Promise<void>
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)

  const description = entity.description ?? ''
  const overrideChip = entity.owner_override
    ? t('admin.persona.entities.owner_override_chip')
    : null
  const lastMentioned = formatDate(entity.last_mentioned_at)
  const aliasPreview =
    entity.aliases.length > 0
      ? entity.aliases.slice(0, 3).join(' · ')
      : null

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
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 15,
            color: 'var(--ink)',
            fontWeight: 500,
          }}
        >
          {entity.canonical_name}
        </span>
        {aliasPreview !== null && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--ink-3)',
              fontFamily: 'var(--mono)',
            }}
          >
            ({aliasPreview})
          </span>
        )}
        {overrideChip !== null && <span className="chip">{overrideChip}</span>}
        <div className="flex1" />
        <span
          style={{
            fontSize: 11,
            color: 'var(--ink-3)',
            fontFamily: 'var(--mono)',
          }}
        >
          {t('admin.persona.entities.linked_events', {
            count: entity.linked_events_count,
          })}{' '}
          · {lastMentioned}
        </span>
        {!editing && (
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => setEditing(true)}
          >
            {t('admin.persona.entities.edit_description')}
          </button>
        )}
      </div>
      {editing ? (
        <EntityDescriptionEditor
          entityId={entity.id}
          initial={description}
          onSave={async (next) => {
            await onSaveDescription(entity.id, next)
            setEditing(false)
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 13,
            lineHeight: 1.6,
            color: description ? 'var(--ink-2)' : 'var(--ink-4)',
            whiteSpace: 'pre-wrap',
          }}
        >
          {description ||
            t('admin.persona.entities.description_empty_placeholder')}
        </div>
      )}
    </div>
  )
}
