import { useTranslation } from 'react-i18next'

import type { MemoryEvent, MemoryThought } from '../../../api/types'
import { useMemoryEvents } from '../../../hooks/useMemoryEvents'
import { useMemorySearch } from '../../../hooks/useMemorySearch'
import { useMemoryThoughts } from '../../../hooks/useMemoryThoughts'
import { confirmDelete } from '../helpers'
import type { CrossNav } from '../types'
import { MemoryRow } from './MemoryRow'

type MemoryKind = 'events' | 'thoughts'

export function MemoryListView({
  kind,
  setKind,
  events,
  thoughts,
  highlight,
  clearHighlight,
  crossNav,
}: {
  kind: MemoryKind
  setKind: (k: MemoryKind) => void
  events: ReturnType<typeof useMemoryEvents>
  thoughts: ReturnType<typeof useMemoryThoughts>
  highlight: { kind: 'event' | 'thought'; id: number } | null
  clearHighlight: () => void
  crossNav: CrossNav
}) {
  const { t } = useTranslation()
  const search = useMemorySearch(kind)
  const active = search.active

  const handleDeleteEvent = async (item: MemoryEvent) => {
    try {
      const preview = await events.previewDelete(item.id)
      const choice = await confirmDelete(item.description, preview, t)
      if (choice === null) return
      await events.deleteEvent(item.id, choice)
    } catch (err) {
      console.error('delete event failed', err)
    }
  }
  const handleDeleteThought = async (item: MemoryThought) => {
    try {
      const preview = await thoughts.previewDelete(item.id)
      const choice = await confirmDelete(item.description, preview, t)
      if (choice === null) return
      await thoughts.deleteThought(item.id, choice)
    } catch (err) {
      console.error('delete thought failed', err)
    }
  }

  const hook = kind === 'events' ? events : thoughts
  const defaultList = (
    kind === 'events' ? events.items : thoughts.items
  ) as (MemoryEvent | MemoryThought)[]
  const visible = active ? search.results : defaultList
  const visibleTotal = active ? search.total : hook.total
  const error = hook.error || search.error

  return (
    <>
      <div className="row g-3" style={{ alignItems: 'center' }}>
        <div className="seg">
          <button
            className={kind === 'events' ? 'on' : ''}
            onClick={() => setKind('events')}
          >
            {t('admin.memory_tab.events_tab')}
          </button>
          <button
            className={kind === 'thoughts' ? 'on' : ''}
            onClick={() => setKind('thoughts')}
          >
            {t('admin.memory_tab.thoughts_tab')}
          </button>
        </div>
        <input
          placeholder={t('admin.memory_list.search_placeholder')}
          value={search.query}
          onChange={(e) => search.setQuery(e.target.value)}
          style={{
            flex: 1,
            border: '1px solid var(--rule)',
            padding: '8px 12px',
            borderRadius: 4,
            fontSize: 13,
            background: 'var(--paper)',
            color: 'var(--ink)',
          }}
        />
        {active && (
          <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>
            {search.loading
              ? t('admin.events.searching')
              : t('admin.memory_list.count', { count: search.total })}
          </span>
        )}
        {search.query && (
          <button className="btn ghost sm" onClick={search.clear}>
            {t('admin.memory_list.clear')}
          </button>
        )}
      </div>

      {error && (
        <div
          style={{
            padding: 10,
            borderRadius: 4,
            background: 'var(--accent-soft)',
            color: 'var(--accent)',
            fontSize: 12,
            fontFamily: 'var(--mono)',
          }}
        >
          ⚠ {error}
        </div>
      )}

      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        {hook.loading && defaultList.length === 0 && !active ? (
          <div style={{ padding: 40, textAlign: 'center', color: 'var(--ink-3)' }}>
            {t('admin.events.loading')}
          </div>
        ) : visibleTotal === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: 'var(--ink-3)' }}>
            {active
              ? kind === 'events'
                ? t('admin.events.no_match_title')
                : t('admin.thoughts.no_match_title')
              : kind === 'events'
                ? t('admin.events.empty_title')
                : t('admin.thoughts.empty_title')}
          </div>
        ) : (
          visible.map((it) => (
            <MemoryRow
              key={it.id}
              kind={it.node_type === 'thought' ? 'thought' : 'event'}
              item={it}
              highlighted={
                highlight !== null &&
                highlight.id === it.id &&
                ((highlight.kind === 'event' && it.node_type === 'event') ||
                  (highlight.kind === 'thought' && it.node_type === 'thought'))
              }
              onHighlightConsumed={clearHighlight}
              crossNav={crossNav}
              snippet={search.snippets.get(it.id)}
              onDelete={() =>
                it.node_type === 'thought'
                  ? void handleDeleteThought(it as MemoryThought)
                  : void handleDeleteEvent(it as MemoryEvent)
              }
            />
          ))
        )}
        {!active && kind === 'events' && events.hasMore && (
          <div style={{ textAlign: 'center', padding: 12 }}>
            <button
              className="btn ghost sm"
              disabled={events.loadingMore}
              onClick={() => void events.loadMore()}
            >
              {events.loadingMore
                ? t('admin.events.loading_more')
                : t('admin.events.load_more', {
                    remaining: events.total - events.items.length,
                  })}
            </button>
          </div>
        )}
        {!active && kind === 'thoughts' && thoughts.hasMore && (
          <div style={{ textAlign: 'center', padding: 12 }}>
            <button
              className="btn ghost sm"
              disabled={thoughts.loadingMore}
              onClick={() => void thoughts.loadMore()}
            >
              {thoughts.loadingMore
                ? t('admin.events.loading_more')
                : t('admin.events.load_more', {
                    remaining: thoughts.total - thoughts.items.length,
                  })}
            </button>
          </div>
        )}
      </div>
    </>
  )
}
