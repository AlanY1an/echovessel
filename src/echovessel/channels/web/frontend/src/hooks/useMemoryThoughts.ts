/**
 * useMemoryThoughts — paginated L4 thought list for the admin Thoughts tab.
 *
 * Mirror of :func:`useMemoryEvents` against
 * GET /api/admin/memory/thoughts and
 * DELETE /api/admin/memory/thoughts/{id}. Kept as a separate hook
 * (instead of a generic ``useMemoryNodes(type)``) so callers can
 * narrow the item type without runtime branching, and so the eventual
 * hook-specific extras (e.g. filling lookups for L4) can land here
 * without re-typing the events branch.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  deleteMemoryThought,
  getMemoryThoughts,
  postMemoryPreviewDelete,
} from '../api/client'
import type {
  DeleteChoice,
  MemoryThought,
  PreviewDeleteResponse,
} from '../api/types'
import { ApiError } from '../api/types'

const DEFAULT_PAGE_SIZE = 20

export interface UseMemoryThoughtsResult {
  items: MemoryThought[]
  total: number
  loading: boolean
  loadingMore: boolean
  error: string | null
  hasMore: boolean
  refresh(): Promise<void>
  loadMore(): Promise<void>
  previewDelete(nodeId: number): Promise<PreviewDeleteResponse>
  deleteThought(nodeId: number, choice?: DeleteChoice): Promise<void>
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

export function useMemoryThoughts(
  pageSize: number = DEFAULT_PAGE_SIZE,
): UseMemoryThoughtsResult {
  const [items, setItems] = useState<MemoryThought[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      const page = await getMemoryThoughts(pageSize, 0)
      setItems(page.items)
      setTotal(page.total)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [pageSize])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const loadMore = useCallback(async (): Promise<void> => {
    if (loadingMore || items.length >= total) return
    setLoadingMore(true)
    setError(null)
    try {
      const page = await getMemoryThoughts(pageSize, items.length)
      setItems((prev) => [...prev, ...page.items])
      setTotal(page.total)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoadingMore(false)
    }
  }, [items.length, total, pageSize, loadingMore])

  const previewDelete = useCallback(
    async (nodeId: number): Promise<PreviewDeleteResponse> => {
      return postMemoryPreviewDelete(nodeId)
    },
    [],
  )

  const deleteThought = useCallback(
    async (
      nodeId: number,
      choice: DeleteChoice = 'orphan',
    ): Promise<void> => {
      setError(null)
      try {
        await deleteMemoryThought(nodeId, choice)
        await refresh()
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const hasMore = items.length < total

  return {
    items,
    total,
    loading,
    loadingMore,
    error,
    hasMore,
    refresh,
    loadMore,
    previewDelete,
    deleteThought,
  }
}
