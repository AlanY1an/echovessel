import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useMemoryEvents } from '../../../hooks/useMemoryEvents'
import { useMemoryThoughts } from '../../../hooks/useMemoryThoughts'
import type { CrossNav } from '../types'
import { MemoryGraph } from './MemoryGraph'
import { MemoryListView } from './MemoryListView'

type MemoryView = 'list' | 'graph'
type MemoryKind = 'events' | 'thoughts'

export function AdmMemory() {
  const { t } = useTranslation()
  const events = useMemoryEvents()
  const thoughts = useMemoryThoughts()
  const [view, setView] = useState<MemoryView>('list')
  const [kind, setKind] = useState<MemoryKind>('events')
  const [highlight, setHighlight] = useState<{
    kind: 'event' | 'thought'
    id: number
  } | null>(null)

  const crossNav: CrossNav = {
    navigateTo: (k, id) => {
      setView('list')
      setKind(k === 'event' ? 'events' : 'thoughts')
      setHighlight({ kind: k, id })
    },
  }

  return (
    <div
      style={{
        padding: '24px 40px',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        height: '100%',
        overflow: 'hidden',
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.memory.section_title')}</h2>
        <div className="flex1" />
        <span className="chip">
          {events.total} {t('admin.memory_tab.events_suffix')}
        </span>
        <span className="chip">
          {thoughts.total} {t('admin.memory_tab.thoughts_suffix')}
        </span>
        <div className="seg" style={{ marginLeft: 10 }}>
          <button
            className={view === 'list' ? 'on' : ''}
            onClick={() => setView('list')}
          >
            {t('admin.memory_tab.view_list')}
          </button>
          <button
            className={view === 'graph' ? 'on' : ''}
            onClick={() => setView('graph')}
          >
            {t('admin.memory_tab.view_graph')}
          </button>
        </div>
      </div>

      {view === 'list' ? (
        <MemoryListView
          kind={kind}
          setKind={setKind}
          events={events}
          thoughts={thoughts}
          highlight={highlight}
          clearHighlight={() => setHighlight(null)}
          crossNav={crossNav}
        />
      ) : (
        <MemoryGraph
          events={events.items}
          thoughts={thoughts.items}
          crossNav={crossNav}
        />
      )}
    </div>
  )
}
