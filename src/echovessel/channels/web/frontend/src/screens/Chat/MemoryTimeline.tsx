/**
 * MemoryTimeline — Spec 3 chat sidebar.
 *
 * Renders a reverse-chronological list of every memory write the
 * backend broadcasts: L3 events, L4 thoughts/intentions/expectations,
 * L5 entity confirmations, L6 mood shifts, and session-close summaries.
 *
 * Layout responsibilities live here:
 *   - Collapsible panel (default expanded; state persisted via the hook)
 *   - Header with filter menu + collapse toggle
 *   - Empty state copy when the store has no items
 *
 * Per-row rendering is delegated to the `timeline/` sub-components so
 * each kind can own its icon / i18n string / future interactivity.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { MemoryTimelineItem } from '../../api/types'
import { useMemoryTimeline } from '../../hooks/useMemoryTimeline'
import { TimelineFilters } from './timeline/TimelineFilters'
import { TimelineItemEntity } from './timeline/TimelineItemEntity'
import { TimelineItemEvent } from './timeline/TimelineItemEvent'
import { TimelineItemExpectation } from './timeline/TimelineItemExpectation'
import { TimelineItemIntention } from './timeline/TimelineItemIntention'
import { TimelineItemMood } from './timeline/TimelineItemMood'
import { TimelineItemSessionClose } from './timeline/TimelineItemSessionClose'
import { TimelineItemThought } from './timeline/TimelineItemThought'
import type { RelativeTimeT } from './timeline/relativeTime'

function itemKey(item: MemoryTimelineItem, idx: number): string {
  const d = item.data as Record<string, unknown>
  const id = (d.node_id ?? d.entity_id ?? d.session_id ?? idx) as
    | string
    | number
  return `${item.kind}-${id}-${item.timestamp}`
}

function renderItem(
  item: MemoryTimelineItem,
  idx: number,
  now: Date,
  t: RelativeTimeT,
) {
  const data = item.data as { type?: string }
  switch (item.kind) {
    case 'event':
      return <TimelineItemEvent key={itemKey(item, idx)} item={item} now={now} t={t} />
    case 'thought':
      if (data.type === 'intention') {
        return (
          <TimelineItemIntention
            key={itemKey(item, idx)}
            item={item}
            now={now}
            t={t}
          />
        )
      }
      if (data.type === 'expectation') {
        return (
          <TimelineItemExpectation
            key={itemKey(item, idx)}
            item={item}
            now={now}
            t={t}
          />
        )
      }
      return (
        <TimelineItemThought key={itemKey(item, idx)} item={item} now={now} t={t} />
      )
    case 'entity':
    case 'entity_description':
      return <TimelineItemEntity key={itemKey(item, idx)} item={item} now={now} t={t} />
    case 'mood':
      return <TimelineItemMood key={itemKey(item, idx)} item={item} now={now} t={t} />
    case 'session_close':
      return (
        <TimelineItemSessionClose
          key={itemKey(item, idx)}
          item={item}
          now={now}
          t={t}
        />
      )
    default:
      return null
  }
}

export function MemoryTimeline() {
  const { t } = useTranslation()
  const {
    items,
    isCollapsed,
    setCollapsed,
    filters,
    setFilter,
    clearLocalCache,
    loading,
  } = useMemoryTimeline()

  const [filtersOpen, setFiltersOpen] = useState(false)
  // Re-render every 60s so relative timestamps ("5 分钟前") advance
  // without the user having to refresh. Cheap tick; no deps.
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 60_000)
    return () => window.clearInterval(id)
  }, [])

  if (isCollapsed) {
    return (
      <aside className="memory-timeline-panel collapsed">
        <button
          type="button"
          className="mem-expand-tab"
          onClick={() => setCollapsed(false)}
          aria-label={t('chat.timeline.expand_aria')}
        >
          {t('chat.timeline.title')}
        </button>
      </aside>
    )
  }

  return (
    <aside className="memory-timeline-panel">
      <header className="mem-head">
        <span className="mem-title">{t('chat.timeline.title')}</span>
        <div className="flex1" />
        <button
          type="button"
          className="icbtn sm"
          onClick={() => setFiltersOpen((v) => !v)}
          aria-label={t('chat.timeline.filter.toggle_aria')}
        >
          ⋯
        </button>
        <button
          type="button"
          className="icbtn sm"
          onClick={() => setCollapsed(true)}
          aria-label={t('chat.timeline.collapse_aria')}
        >
          ⤢
        </button>
      </header>

      {filtersOpen && (
        <TimelineFilters
          filters={filters}
          onToggle={setFilter}
          onClearCache={clearLocalCache}
          onClose={() => setFiltersOpen(false)}
        />
      )}

      {items.length === 0 ? (
        <div className="mem-empty">
          {loading ? t('chat.timeline.loading') : t('chat.timeline.empty')}
        </div>
      ) : (
        <ul className="mem-list">
          {items.map((item, idx) => renderItem(item, idx, now, t))}
        </ul>
      )}
    </aside>
  )
}
