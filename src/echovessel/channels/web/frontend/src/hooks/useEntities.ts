/**
 * useEntities — read + mutate loop for the Admin Persona tab's Social
 * Graph section.
 *
 * Hits ``GET /api/admin/memory/entities`` (server pre-sorts uncertain
 * rows to the top) and exposes derived views the section needs:
 *
 *   - ``entities``  — flat list, server order preserved
 *   - ``uncertain`` — only ``merge_status='uncertain'`` rows
 *   - ``byKind``    — confirmed rows bucketed by kind, sorted within
 *                     each bucket by ``last_mentioned_at`` (DESC)
 *
 * Mutation methods (``saveDescription`` / ``mergeOne`` /
 * ``separateOne`` / ``createManual``) refetch on success so callers
 * always see the canonical server state.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  confirmSeparateEntities,
  createEntity,
  getEntities,
  mergeEntities,
  patchEntityDescription,
} from '../api/client'
import type { EntityCreatePayload, EntityRow } from '../api/types'
import { ApiError } from '../api/types'

export interface UseEntitiesResult {
  entities: EntityRow[]
  uncertain: EntityRow[]
  byKind: Map<EntityRow['kind'], EntityRow[]>
  loading: boolean
  error: string | null
  refresh(): Promise<void>
  saveDescription(entityId: number, description: string): Promise<void>
  mergeOne(entityId: number, targetId: number): Promise<void>
  separateOne(entityId: number, otherId: number): Promise<void>
  createManual(payload: EntityCreatePayload): Promise<EntityRow>
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

export function useEntities(): UseEntitiesResult {
  const [entities, setEntities] = useState<EntityRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      const res = await getEntities()
      setEntities(res.entities)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const saveDescription = useCallback(
    async (entityId: number, description: string): Promise<void> => {
      setError(null)
      try {
        await patchEntityDescription(entityId, description)
        await refresh()
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const mergeOne = useCallback(
    async (entityId: number, targetId: number): Promise<void> => {
      setError(null)
      try {
        await mergeEntities(entityId, { target_id: targetId })
        await refresh()
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const separateOne = useCallback(
    async (entityId: number, otherId: number): Promise<void> => {
      setError(null)
      try {
        await confirmSeparateEntities(entityId, { other_id: otherId })
        await refresh()
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const createManual = useCallback(
    async (payload: EntityCreatePayload): Promise<EntityRow> => {
      setError(null)
      try {
        const row = await createEntity(payload)
        await refresh()
        return row
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const { uncertain, byKind } = useMemo(() => {
    const uncertainList: EntityRow[] = []
    const groups = new Map<EntityRow['kind'], EntityRow[]>()
    for (const e of entities) {
      if (e.merge_status === 'uncertain') {
        uncertainList.push(e)
        continue
      }
      if (e.merge_status !== 'confirmed') continue
      const bucket = groups.get(e.kind) ?? []
      bucket.push(e)
      groups.set(e.kind, bucket)
    }
    for (const bucket of groups.values()) {
      bucket.sort((a, b) => {
        const aT = a.last_mentioned_at ?? ''
        const bT = b.last_mentioned_at ?? ''
        return bT.localeCompare(aT)
      })
    }
    return { uncertain: uncertainList, byKind: groups }
  }, [entities])

  return {
    entities,
    uncertain,
    byKind,
    loading,
    error,
    refresh,
    saveDescription,
    mergeOne,
    separateOne,
    createManual,
  }
}
