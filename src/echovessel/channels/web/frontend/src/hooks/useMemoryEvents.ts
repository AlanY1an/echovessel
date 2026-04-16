/**
 * useMemoryEvents — paginated L3 event list for the admin Events tab.
 *
 * Wraps GET /api/admin/memory/events with offset / "load more" pagination
 * and a delete helper that:
 *
 *   1. Calls POST /api/admin/memory/preview-delete to learn whether
 *      the row has dependent L4 thoughts.
 *   2. Lets the caller decide what to do — the hook only surfaces the
 *      preview shape; the UI component owns the confirmation dialog.
 *   3. Calls DELETE /api/admin/memory/events/{id}?choice=… and refreshes
 *      the visible page.
 *
 * Pagination strategy: we keep an `offset` cursor and append rows when
 * `loadMore()` is called. `refresh()` resets to the head and reloads.
 * `total` is the server-side total so the UI can show "showing X of Y"
 * without fetching every row.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  deleteMemoryEvent,
  getMemoryEvents,
  postMemoryPreviewDelete,
} from '../api/client'
import type {
  DeleteChoice,
  MemoryEvent,
  PreviewDeleteResponse,
} from '../api/types'
import { ApiError } from '../api/types'

const DEFAULT_PAGE_SIZE = 20

export interface UseMemoryEventsResult {
  items: MemoryEvent[]
  total: number
  loading: boolean
  loadingMore: boolean
  error: string | null
  hasMore: boolean
  refresh(): Promise<void>
  loadMore(): Promise<void>
  previewDelete(nodeId: number): Promise<PreviewDeleteResponse>
  deleteEvent(nodeId: number, choice?: DeleteChoice): Promise<void>
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

export function useMemoryEvents(
  pageSize: number = DEFAULT_PAGE_SIZE,
): UseMemoryEventsResult {
  const [items, setItems] = useState<MemoryEvent[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      const page = await getMemoryEvents(pageSize, 0)
      setItems(page.items)
      setTotal(page.total)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [pageSize])

  // Initial fetch.
  useEffect(() => {
    void refresh()
  }, [refresh])

  const loadMore = useCallback(async (): Promise<void> => {
    if (loadingMore || items.length >= total) return
    setLoadingMore(true)
    setError(null)
    try {
      const page = await getMemoryEvents(pageSize, items.length)
      setItems((prev) => [...prev, ...page.items])
      // Use the freshest server total — rows may have been deleted
      // elsewhere between page loads.
      setTotal(page.total)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoadingMore(false)
    }
  }, [items.length, total, pageSize, loadingMore])

  const previewDelete = useCallback(
    async (nodeId: number): Promise<PreviewDeleteResponse> => {
      // We deliberately do NOT clear `error` here so an in-flight
      // preview-failure stays visible until the user picks an action.
      return postMemoryPreviewDelete(nodeId)
    },
    [],
  )

  const deleteEvent = useCallback(
    async (
      nodeId: number,
      choice: DeleteChoice = 'orphan',
    ): Promise<void> => {
      setError(null)
      try {
        await deleteMemoryEvent(nodeId, choice)
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
    deleteEvent,
  }
}
