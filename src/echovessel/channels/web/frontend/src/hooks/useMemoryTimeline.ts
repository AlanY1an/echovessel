/**
 * useMemoryTimeline — Spec 3 hook that drives the Chat page's Memory
 * Timeline sidebar.
 *
 * Two data paths merge into one local store:
 *
 *   1. Initial backfill: `GET /api/admin/memory/timeline?limit=50` on
 *      mount. Returns a mixed DESC-sorted list of events / thoughts /
 *      entities / mood / session closes.
 *
 *   2. Live SSE: subscribe to the 6 topics the backend emits on every
 *      memory write, and prepend matching items to the store. SSE
 *      payloads map 1:1 to the backfill item shapes via
 *      :func:`sseEventToTimelineItem`.
 *
 * Local UI state (panel collapsed + per-kind filter checkboxes) is
 * persisted in `localStorage` so the sidebar remembers preferences
 * across reloads.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getMemoryTimeline } from '../api/client'
import type {
  ChatEvent,
  MemoryEntityConfirmedData,
  MemoryEntityDescriptionUpdatedData,
  MemoryEventCreatedData,
  MemoryThoughtCreatedData,
  MemoryTimelineItem,
  MemoryTimelineItemKind,
} from '../api/types'
import { useSSE } from './useSSE'

const STORAGE_COLLAPSED = 'echovessel.timeline.collapsed'
const STORAGE_FILTERS = 'echovessel.timeline.filters'
const DEFAULT_LIMIT = 50
const MAX_STORE_SIZE = 200

/** Per-kind visibility flags used by the Filter menu. Every flag
 *  defaults to `true` — user opts out, never in. */
export interface TimelineFilterSet {
  event: boolean
  thought_reflection: boolean
  thought_slow_tick: boolean
  intention: boolean
  expectation: boolean
  entity: boolean
  mood: boolean
  session_close: boolean
}

const DEFAULT_FILTERS: TimelineFilterSet = {
  event: true,
  thought_reflection: true,
  thought_slow_tick: true,
  intention: true,
  expectation: true,
  entity: true,
  mood: true,
  session_close: true,
}

export interface UseMemoryTimelineResult {
  items: MemoryTimelineItem[]
  allItems: MemoryTimelineItem[]
  isCollapsed: boolean
  setCollapsed(v: boolean): void
  filters: TimelineFilterSet
  setFilter(key: keyof TimelineFilterSet, value: boolean): void
  clearLocalCache(): void
  loading: boolean
}

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(STORAGE_COLLAPSED) === '1'
  } catch {
    return false
  }
}

function writeCollapsed(v: boolean): void {
  try {
    localStorage.setItem(STORAGE_COLLAPSED, v ? '1' : '0')
  } catch {
    // non-fatal
  }
}

function readFilters(): TimelineFilterSet {
  try {
    const raw = localStorage.getItem(STORAGE_FILTERS)
    if (!raw) return { ...DEFAULT_FILTERS }
    const parsed = JSON.parse(raw) as Partial<TimelineFilterSet>
    return { ...DEFAULT_FILTERS, ...parsed }
  } catch {
    return { ...DEFAULT_FILTERS }
  }
}

function writeFilters(filters: TimelineFilterSet): void {
  try {
    localStorage.setItem(STORAGE_FILTERS, JSON.stringify(filters))
  } catch {
    // non-fatal
  }
}

/** Stable identity string for dedupe — same backfill row arriving via
 *  both the initial GET and a subsequent SSE event should collapse. */
function itemKey(item: MemoryTimelineItem): string {
  const kind = item.kind
  const data = item.data as Record<string, unknown>
  if (kind === 'event') return `event:${data.node_id ?? data.event_id}`
  if (kind === 'thought') {
    return `thought:${data.node_id ?? data.thought_id}`
  }
  if (kind === 'entity') return `entity:${data.entity_id}`
  if (kind === 'entity_description') {
    return `entity_desc:${data.entity_id}:${item.timestamp}`
  }
  if (kind === 'mood') return `mood:${item.timestamp}`
  if (kind === 'session_close') return `session_close:${data.session_id}`
  return `${kind}:${item.timestamp}`
}

function dedupe(items: MemoryTimelineItem[]): MemoryTimelineItem[] {
  const seen = new Set<string>()
  const out: MemoryTimelineItem[] = []
  for (const it of items) {
    const k = itemKey(it)
    if (seen.has(k)) continue
    seen.add(k)
    out.push(it)
  }
  return out
}

function sortDesc(items: MemoryTimelineItem[]): MemoryTimelineItem[] {
  return items
    .slice()
    .sort((a, b) => (a.timestamp < b.timestamp ? 1 : a.timestamp > b.timestamp ? -1 : 0))
}

/** Translate an SSE `memory.*` event into a Timeline item, matching
 *  the shape the backfill endpoint returns so the sidebar can render
 *  both via the same code path. */
function sseEventToTimelineItem(event: ChatEvent): MemoryTimelineItem | null {
  switch (event.event) {
    case 'memory.event.created': {
      const d = event.data as MemoryEventCreatedData
      return {
        kind: 'event',
        timestamp: d.created_at ?? new Date().toISOString(),
        data: {
          node_id: d.event_id,
          type: 'event',
          description: d.description,
          emotional_impact: d.emotional_impact,
          session_id: d.session_id,
        },
      }
    }
    case 'memory.thought.created': {
      const d = event.data as MemoryThoughtCreatedData
      return {
        kind: 'thought',
        timestamp: d.created_at ?? new Date().toISOString(),
        data: {
          node_id: d.thought_id,
          type: d.type,
          subject: d.subject,
          description: d.description,
          source: d.source,
          session_id: d.session_id,
          filling_event_ids: d.filling_event_ids,
        },
      }
    }
    case 'memory.entity.confirmed': {
      const d = event.data as MemoryEntityConfirmedData
      return {
        kind: 'entity',
        timestamp: d.created_at ?? new Date().toISOString(),
        data: {
          entity_id: d.entity_id,
          canonical_name: d.canonical_name,
          kind: d.kind,
          merge_status: d.merge_status,
        },
      }
    }
    case 'memory.entity.description_updated': {
      const d = event.data as MemoryEntityDescriptionUpdatedData
      return {
        kind: 'entity_description',
        timestamp: d.updated_at ?? new Date().toISOString(),
        data: {
          entity_id: d.entity_id,
          canonical_name: d.canonical_name,
          kind: d.kind,
          description: d.description,
          source: d.source,
        },
      }
    }
    case 'chat.mood.update': {
      return {
        kind: 'mood',
        timestamp: new Date().toISOString(),
        data: {
          mood_summary: event.data.mood_summary,
        },
      }
    }
    case 'chat.session.boundary': {
      // Only the "close" half of the boundary pair becomes a Timeline
      // row — the "new" half is a chat-layer concern (message divider)
      // that already renders in useChat.
      const d = event.data
      if (d.closed_session_id === null) return null
      return {
        kind: 'session_close',
        timestamp: d.at || new Date().toISOString(),
        data: {
          session_id: d.closed_session_id,
          events_count: (d as { events_count?: number }).events_count ?? null,
          thoughts_count:
            (d as { thoughts_count?: number }).thoughts_count ?? null,
        },
      }
    }
    default:
      return null
  }
}

function matchesFilter(
  item: MemoryTimelineItem,
  filters: TimelineFilterSet,
): boolean {
  const kind: MemoryTimelineItemKind = item.kind
  const data = item.data as Record<string, unknown>
  switch (kind) {
    case 'event':
      return filters.event
    case 'thought': {
      const type = (data.type as string) ?? 'thought'
      const source = (data.source as string) ?? 'reflection'
      if (type === 'intention') return filters.intention
      if (type === 'expectation') return filters.expectation
      if (source === 'slow_tick') return filters.thought_slow_tick
      return filters.thought_reflection
    }
    case 'entity':
    case 'entity_description':
      return filters.entity
    case 'mood':
      return filters.mood
    case 'session_close':
      return filters.session_close
    default:
      return true
  }
}

export function useMemoryTimeline(): UseMemoryTimelineResult {
  const [items, setItems] = useState<MemoryTimelineItem[]>([])
  const [isCollapsed, setCollapsedState] = useState<boolean>(readCollapsed())
  const [filters, setFiltersState] = useState<TimelineFilterSet>(readFilters())
  const [loading, setLoading] = useState(false)
  const bootstrapped = useRef(false)

  const { subscribe } = useSSE()

  // Initial backfill — once per mount. StrictMode dev double-invoke
  // is guarded with the same ref pattern as useChat.
  useEffect(() => {
    if (bootstrapped.current) return
    bootstrapped.current = true
    setLoading(true)
    void getMemoryTimeline(DEFAULT_LIMIT, null)
      .then((resp) => {
        setItems((prev) =>
          dedupe(sortDesc([...resp.items, ...prev])).slice(0, MAX_STORE_SIZE),
        )
      })
      .catch(() => {
        // Backfill is best-effort — the Timeline still gets live events
        // from SSE. Don't surface a banner for this one.
      })
      .finally(() => setLoading(false))
  }, [])

  // Live SSE — prepend any memory.* (or chat.session.boundary for the
  // session_close half) event to the store.
  useEffect(() => {
    const unsubscribe = subscribe((event: ChatEvent) => {
      const item = sseEventToTimelineItem(event)
      if (item === null) return
      setItems((prev) =>
        dedupe(sortDesc([item, ...prev])).slice(0, MAX_STORE_SIZE),
      )
    })
    return unsubscribe
  }, [subscribe])

  const setCollapsed = useCallback((v: boolean) => {
    setCollapsedState(v)
    writeCollapsed(v)
  }, [])

  const setFilter = useCallback(
    (key: keyof TimelineFilterSet, value: boolean) => {
      setFiltersState((prev) => {
        const next = { ...prev, [key]: value }
        writeFilters(next)
        return next
      })
    },
    [],
  )

  const clearLocalCache = useCallback(() => {
    try {
      localStorage.removeItem(STORAGE_COLLAPSED)
      localStorage.removeItem(STORAGE_FILTERS)
    } catch {
      // non-fatal
    }
    setItems([])
    setFiltersState({ ...DEFAULT_FILTERS })
    setCollapsedState(false)
  }, [])

  const filtered = useMemo(
    () => items.filter((it) => matchesFilter(it, filters)),
    [items, filters],
  )

  return {
    items: filtered,
    allItems: items,
    isCollapsed,
    setCollapsed,
    filters,
    setFilter,
    clearLocalCache,
    loading,
  }
}
