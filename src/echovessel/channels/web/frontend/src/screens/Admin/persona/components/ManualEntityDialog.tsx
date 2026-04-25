import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../../../api/types'
import type { EntityCreatePayload, EntityRow } from '../../../../api/types'

/**
 * Modal form for owner-initiated entity creation
 * (``POST /api/admin/memory/entities``).
 *
 * Fields: canonical_name (required), kind (select), description
 * (optional). Aliases are intentionally omitted from MVP — the
 * server records the canonical name as an alias automatically and
 * additional aliases can be added via future episodic linking; the
 * owner just needs the simplest possible "I know this person, file
 * them as X" affordance here.
 */
const KINDS: EntityRow['kind'][] = ['person', 'pet', 'place', 'org', 'other']

export function ManualEntityDialog({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean
  onClose: () => void
  onSubmit: (payload: EntityCreatePayload) => Promise<EntityRow>
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<EntityRow['kind']>('person')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!open) return null

  const reset = () => {
    setName('')
    setKind('person')
    setDescription('')
    setError(null)
    setSubmitting(false)
  }
  const close = () => {
    reset()
    onClose()
  }

  const trimmed = name.trim()
  const canSubmit = trimmed.length > 0 && !submitting

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await onSubmit({
        canonical_name: trimmed,
        kind,
        description: description.trim() || null,
      })
      reset()
      onClose()
    } catch (err) {
      if (err instanceof ApiError) setError(err.detail)
      else if (err instanceof Error) setError(err.message)
      else setError('unknown error')
      setSubmitting(false)
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={close}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 50,
      }}
    >
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => void handleSubmit(e)}
        className="card stack g-3"
        style={{
          padding: 22,
          maxWidth: 480,
          width: '92%',
          background: 'var(--paper)',
        }}
      >
        <div className="row g-2" style={{ alignItems: 'baseline' }}>
          <h3 className="title" style={{ margin: 0 }}>
            {t('admin.persona.entities.manual_dialog_title')}
          </h3>
          <div className="flex1" />
          <button
            type="button"
            className="btn ghost sm"
            onClick={close}
            disabled={submitting}
          >
            {t('admin.common.cancel')}
          </button>
        </div>

        <label className="stack g-1">
          <span className="label">
            {t('admin.persona.entities.manual_field_name')}
          </span>
          <input
            type="text"
            className="bare"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={submitting}
            autoFocus
            style={{
              border: '1px solid var(--rule)',
              padding: '6px 10px',
              borderRadius: 4,
              background: 'var(--paper)',
              fontSize: 13,
            }}
          />
        </label>

        <label className="stack g-1">
          <span className="label">
            {t('admin.persona.entities.manual_field_kind')}
          </span>
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as EntityRow['kind'])}
            disabled={submitting}
            style={{
              border: '1px solid var(--rule)',
              padding: '6px 10px',
              borderRadius: 4,
              background: 'var(--paper)',
              fontSize: 13,
            }}
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`admin.persona.entities.kind.${k}`)}
              </option>
            ))}
          </select>
        </label>

        <label className="stack g-1">
          <span className="label">
            {t('admin.persona.entities.manual_field_description')}
          </span>
          <textarea
            className="bare"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={submitting}
            placeholder={t(
              'admin.persona.entities.manual_field_description_placeholder',
            )}
            style={{
              border: '1px solid var(--rule)',
              padding: 10,
              borderRadius: 6,
              background: 'var(--paper)',
              fontSize: 13,
              fontFamily: 'var(--serif)',
              lineHeight: 1.5,
            }}
          />
        </label>

        {error !== null && (
          <span
            className="chip warn"
            style={{ color: 'var(--accent)' }}
          >
            ⚠ {error}
          </span>
        )}

        <div className="row g-2" style={{ alignItems: 'center' }}>
          <div className="flex1" />
          <button
            type="submit"
            className="btn sm"
            disabled={!canSubmit}
          >
            {submitting ? '⋯' : t('admin.persona.entities.manual_submit')}
          </button>
        </div>
      </form>
    </div>
  )
}
