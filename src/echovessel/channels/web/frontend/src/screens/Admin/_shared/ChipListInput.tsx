import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { inputBoxStyle } from './FieldLabel'

/** Multi-value text input rendered as chips. Used by the Channels tab
 *  for allowlist editing (Discord user IDs, iMessage handles).
 *  Enter or comma confirms the draft value as a new chip. */
export function ChipListInput({
  values,
  onChange,
  placeholder,
}: {
  values: string[]
  onChange: (next: string[]) => void
  placeholder: string
}) {
  const [draft, setDraft] = useState('')
  const { t } = useTranslation()

  const add = () => {
    const trimmed = draft.trim()
    if (!trimmed) return
    if (values.includes(trimmed)) {
      setDraft('')
      return
    }
    onChange([...values, trimmed])
    setDraft('')
  }

  const remove = (idx: number) => {
    onChange(values.filter((_, i) => i !== idx))
  }

  return (
    <div className="stack g-2">
      <div className="row g-2" style={{ flexWrap: 'wrap' }}>
        {values.length === 0 && (
          <span
            style={{
              fontSize: 12,
              color: 'var(--ink-3)',
              fontStyle: 'italic',
            }}
          >
            {t('admin.channels.chip_list.empty')}
          </span>
        )}
        {values.map((v, i) => (
          <span key={`${v}-${i}`} className="chip">
            {v}
            <button
              type="button"
              onClick={() => remove(i)}
              aria-label={t('admin.channels.chip_list.remove_aria', {
                value: v,
              })}
              style={{
                marginLeft: 4,
                color: 'var(--ink-3)',
                fontSize: 11,
              }}
            >
              ✕
            </button>
          </span>
        ))}
      </div>
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <input
          className="bare"
          style={inputBoxStyle}
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault()
              add()
            }
          }}
        />
        <button
          type="button"
          className="btn ghost sm"
          disabled={!draft.trim()}
          onClick={add}
        >
          {t('admin.channels.chip_list.add')}
        </button>
      </div>
    </div>
  )
}
