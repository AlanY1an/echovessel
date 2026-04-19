/**
 * Admin-wide pure helpers shared across tabs.
 *
 * All of these were module-scope functions in the old monolithic
 * `Admin.tsx`. They live here so every tab can `import {…} from
 * '../helpers'` without a circular dependency on the tab files.
 */

import type { PersonaFacts, PreviewDeleteResponse } from '../../api/types'
import type { TFn } from './types'

/** Deep-equal check for the 15 biographic-fact columns. Used by the
 *  Persona tab's dirty-tracking — compare draft vs. baseline to decide
 *  whether to enable the Save button. */
export function factsEqual(a: PersonaFacts, b: PersonaFacts): boolean {
  const keys: (keyof PersonaFacts)[] = [
    'full_name',
    'gender',
    'birth_date',
    'ethnicity',
    'nationality',
    'native_language',
    'locale_region',
    'education_level',
    'occupation',
    'occupation_field',
    'location',
    'timezone',
    'relationship_status',
    'life_stage',
    'health_status',
  ]
  for (const k of keys) {
    if ((a[k] ?? null) !== (b[k] ?? null)) return false
  }
  return true
}

/** Shallow equality for arrays of the same length (element-by-element
 *  ===). Used by Channels-tab dirty checks for allowlist arrays. */
export function arraysEqual<T>(a: T[], b: T[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false
  }
  return true
}

/**
 * Two-step deletion confirmation for concept nodes. When the node has
 * no L4 thought dependents, surfaces a simple yes/no confirm and
 * returns `'orphan'` for yes (soft delete, nothing cascades). When
 * dependents exist, asks the user whether to cascade into those
 * thoughts too; returning `'cascade'` on yes and `'orphan'` on no.
 * Returns `null` when the user cancels the first dialog.
 */
export function confirmDelete(
  description: string,
  preview: PreviewDeleteResponse,
  t: TFn,
): Promise<'orphan' | 'cascade' | null> {
  const truncated =
    description.length > 80 ? `${description.slice(0, 80)}…` : description
  if (!preview.has_dependents) {
    const ok = window.confirm(
      t('admin.memory_list.confirm_delete_simple', { preview: truncated }),
    )
    return Promise.resolve(ok ? 'orphan' : null)
  }
  const depCount = preview.dependent_thought_ids.length
  const depsList = preview.dependent_thought_descriptions
    .slice(0, 3)
    .map((d, i) => `${i + 1}. ${d.length > 60 ? `${d.slice(0, 60)}…` : d}`)
    .join('\n')
  const cascadeMsg = t('admin.memory_list.confirm_delete_cascade', {
    preview: truncated,
    depCount,
    depsList,
  })
  const cascade = window.confirm(cascadeMsg)
  return Promise.resolve(cascade ? 'cascade' : 'orphan')
}

export function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString()
}

export function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text
  return text.slice(0, max - 1) + '…'
}

/** Strip every HTML tag except `<b>`/`</b>` from a server-rendered
 *  search snippet. Used before `dangerouslySetInnerHTML` inside
 *  MemoryRow so search highlights render as bold without letting
 *  the backend accidentally inject script. */
export function sanitiseSnippet(raw: string): string {
  return raw.replace(/<(?!\/?b\b)[^>]*>/gi, '')
}

/** Map a cost-feature tag ("chat", "import", etc.) to its localised
 *  display label. Used by the Cost card inside Admin → Config. */
export function labelForFeature(feature: string, t: TFn): string {
  switch (feature) {
    case 'chat':
      return t('admin.cost.feature_chat')
    case 'import':
      return t('admin.cost.feature_import')
    case 'consolidate':
      return t('admin.cost.feature_consolidate')
    case 'reflection':
      return t('admin.cost.feature_reflection')
    case 'proactive':
      return t('admin.cost.feature_proactive')
    default:
      return feature
  }
}
