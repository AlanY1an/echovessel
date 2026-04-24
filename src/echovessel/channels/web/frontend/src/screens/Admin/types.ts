/**
 * Admin-wide types shared across tabs.
 *
 * These were module-scope types inside the old single-file
 * `Admin.tsx`. They live here now so every tab folder (persona,
 * memory, …) can import them without pulling in the full Admin shell.
 */

/** Admin tab identifier — matches the keys in the sidebar nav. */
export type AdmTab =
  | 'persona'
  | 'memory'
  | 'voice'
  | 'sources'
  | 'channels'
  | 'config'

/** react-i18next's t function, typed loosely so we don't drag in
 *  the whole TFunction generic machinery every time we pass `t` into
 *  a helper. */
export type TFn = (key: string, opts?: Record<string, unknown>) => string

/**
 * Cross-tab jump target — kept for memory list ↔ trace navigation
 * inside the Memory tab. The graph view also uses this to bring the
 * list view back with the selected row highlighted.
 */
export interface CrossNav {
  navigateTo(kind: 'event' | 'thought', id: number): void
}

/** The 5 core-block keys on a persona (v0.4 · mood → style). Ordered
 *  here intentionally — the Persona tab renders in this order. */
export type BlockKey = 'persona' | 'self' | 'user' | 'relationship' | 'style'

/** Visual metadata bound to each core-block key. Drives the left-
 *  border accent colour and the i18n key for label/hint text. */
export interface BlockMeta {
  key: BlockKey
  color: string
  labelKey: string
  hintKey: string
  warningKey?: string
}
