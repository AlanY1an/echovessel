import { useState } from 'react'
import { useTranslation } from 'react-i18next'

/**
 * Inline textarea editor for an entity's description prose. Keeps the
 * draft in local state; the parent owns the actual PATCH call. The
 * server-side endpoint stamps ``owner_override=true`` automatically —
 * the client must NOT send that flag (slow_tick gates write-back on
 * the server-side flag).
 */
export function EntityDescriptionEditor({
  entityId: _entityId,
  initial,
  onSave,
  onCancel,
}: {
  entityId: number
  initial: string
  onSave: (next: string) => Promise<void>
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState(initial)
  const [saving, setSaving] = useState(false)

  const dirty = draft.trim() !== initial.trim()

  const handleSave = async () => {
    if (!dirty || saving) return
    setSaving(true)
    try {
      await onSave(draft.trim())
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack g-2">
      <textarea
        className="bare"
        rows={4}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        disabled={saving}
        placeholder={t('admin.persona.entities.description_editor_placeholder')}
        style={{
          width: '100%',
          fontFamily: 'var(--serif)',
          fontSize: 13,
          lineHeight: 1.6,
          border: '1px solid var(--rule)',
          padding: 10,
          borderRadius: 6,
          background: 'var(--paper)',
          color: 'var(--ink)',
        }}
      />
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span
          style={{
            fontSize: 11,
            color: 'var(--ink-3)',
            fontFamily: 'var(--mono)',
          }}
        >
          {t('admin.persona.entities.description_editor_hint')}
        </span>
        <div className="flex1" />
        <button
          type="button"
          className="btn ghost sm"
          onClick={onCancel}
          disabled={saving}
        >
          {t('admin.common.cancel')}
        </button>
        <button
          type="button"
          className="btn sm"
          onClick={() => void handleSave()}
          disabled={!dirty || saving}
        >
          {saving ? '⋯' : t('admin.common.save')}
        </button>
      </div>
    </div>
  )
}
