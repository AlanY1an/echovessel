import type { ReactNode } from 'react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

/**
 * Collapsible "Advanced" section for per-channel editor cards. Hides
 * the rarely-touched tuning knobs by default so the primary editor
 * stays clean.
 */
export function Advanced({
  labelKey,
  children,
}: {
  labelKey: string
  children: ReactNode
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  return (
    <div style={{ marginTop: 14 }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          fontFamily: 'var(--mono)',
          fontSize: 10,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: 'var(--ink-3)',
          padding: 0,
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
        }}
      >
        <span style={{ fontSize: 11 }}>{open ? '▾' : '▸'}</span>
        {t(labelKey)}
      </button>
      {open && children}
    </div>
  )
}
