import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { BlockMeta } from '../types'

export function BlockCard({
  meta,
  value,
  onSave,
}: {
  meta: BlockMeta
  value: string
  onSave: (next: string) => Promise<void>
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    if (!editing) setDraft(value)
  }, [value, editing])

  const dirty = draft !== value
  const wordCount = value.trim() ? value.trim().split(/\s+/).length : 0

  const handleDone = async () => {
    if (dirty && !saving) {
      setSaving(true)
      try {
        await onSave(draft)
      } finally {
        setSaving(false)
      }
    }
    setEditing(false)
  }

  return (
    <div
      className="card"
      style={{ padding: 16, borderLeft: `3px solid ${meta.color}` }}
    >
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span className="label">{t(meta.labelKey)}</span>
        <div className="flex1" />
        <span className="chip">
          {wordCount} {wordCount === 1 ? 'word' : 'words'}
        </span>
        <button
          className="btn ghost sm"
          onClick={() => (editing ? void handleDone() : setEditing(true))}
          disabled={saving}
        >
          {saving ? '⋯' : editing ? t('admin.common.save') : '✎ edit'}
        </button>
      </div>
      {meta.warningKey && editing && (
        <div
          style={{
            fontSize: 11,
            color: 'var(--accent)',
            marginTop: 6,
          }}
        >
          ⚠ {t(meta.warningKey)}
        </div>
      )}
      {editing ? (
        <textarea
          className="bare"
          rows={4}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={saving}
          style={{
            marginTop: 10,
            fontFamily: 'var(--serif)',
            fontSize: 14,
            lineHeight: 1.6,
            width: '100%',
            border: '1px solid var(--rule)',
            padding: 10,
            borderRadius: 6,
            background: 'var(--paper)',
            color: 'var(--ink)',
          }}
        />
      ) : (
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 14,
            lineHeight: 1.6,
            color: 'var(--ink-2)',
            marginTop: 8,
            whiteSpace: 'pre-wrap',
          }}
        >
          {value || (
            <span style={{ color: 'var(--ink-4)' }}>
              {t('admin.persona_blocks.placeholder_default')}
            </span>
          )}
        </div>
      )}
      <div
        style={{
          fontSize: 10,
          color: 'var(--ink-3)',
          marginTop: 6,
          fontFamily: 'var(--mono)',
        }}
      >
        {t(meta.hintKey)}
      </div>
    </div>
  )
}
