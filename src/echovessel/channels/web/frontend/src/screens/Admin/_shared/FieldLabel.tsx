import type { CSSProperties, ReactNode } from 'react'

/** Uppercase mono caption for form-field rows in per-channel editor
 *  grids. Shared across Discord/iMessage cards and the Channels tab. */
export function FieldLabel({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        fontFamily: 'var(--mono)',
        fontSize: 10,
        color: 'var(--ink-3)',
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        paddingTop: 8,
      }}
    >
      {children}
    </div>
  )
}

/** Shared input box styling for text fields in the Channels tab
 *  editor grids. Co-located here because it's always rendered
 *  alongside FieldLabel. */
export const inputBoxStyle: CSSProperties = {
  border: '1px solid var(--rule)',
  padding: '6px 10px',
  borderRadius: 4,
  background: 'var(--paper)',
  fontSize: 13,
  width: '100%',
  minWidth: 0,
}
