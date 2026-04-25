/**
 * usePersonaReflections — narrow read-loop for the Admin Persona tab's
 * Reflection section.
 *
 * Hits ``GET /api/admin/memory/thoughts?subject=persona&limit=10`` and
 * exposes the rows as :type:`PersonaThought` (source narrowed to
 * 'slow_tick' | 'reflection' since subject='persona' is always
 * server-tagged with one of those). The hook does NOT paginate — the
 * Reflection section only ever shows the most recent batch; older
 * rows belong on the (separate) admin Memory tab.
 */

import { useCallback, useEffect, useState } from 'react'

import { getMemoryThoughts } from '../api/client'
import type { MemoryThought, PersonaThought } from '../api/types'
import { ApiError } from '../api/types'

const DEFAULT_LIMIT = 10

export interface UsePersonaReflectionsResult {
  thoughts: PersonaThought[]
  loading: boolean
  error: string | null
  refresh(): Promise<void>
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

function toPersonaThought(row: MemoryThought): PersonaThought {
  return {
    id: row.id,
    description: row.description,
    subject: 'persona',
    created_at: row.created_at ?? '',
    filling_event_ids: row.filling_event_ids,
    // Server guarantees non-null source for type='thought' rows; the
    // 'reflection' fallback is purely defensive.
    source: row.source ?? 'reflection',
  }
}

export function usePersonaReflections(
  limit: number = DEFAULT_LIMIT,
): UsePersonaReflectionsResult {
  const [thoughts, setThoughts] = useState<PersonaThought[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      const page = await getMemoryThoughts(limit, 0, 'persona')
      setThoughts(page.items.map(toPersonaThought))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { thoughts, loading, error, refresh }
}
