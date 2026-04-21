/**
 * useFailedSessions — observability hook for consolidate-worker FAILED
 * sessions. The admin shell renders a banner when count > 0 so the
 * operator notices silent data loss without sqlite3-ing into the db.
 *
 * No pagination, no polling — admin sessions are rare and the count is
 * small. Caller can call refresh() after running the reset script.
 */

import { useCallback, useEffect, useState } from 'react'

import { getFailedSessions } from '../api/client'
import type { FailedSession } from '../api/types'
import { ApiError } from '../api/types'

export interface UseFailedSessionsResult {
  count: number
  items: FailedSession[]
  loading: boolean
  error: string | null
  refresh(): Promise<void>
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

export function useFailedSessions(): UseFailedSessionsResult {
  const [count, setCount] = useState(0)
  const [items, setItems] = useState<FailedSession[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      const resp = await getFailedSessions()
      setCount(resp.count)
      setItems(resp.items)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { count, items, loading, error, refresh }
}
