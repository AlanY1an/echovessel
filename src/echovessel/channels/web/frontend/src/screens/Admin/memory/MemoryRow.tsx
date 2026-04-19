import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type {
  MemoryEvent,
  MemoryThought,
  TraceNode,
} from '../../../api/types'
import { EmotionBar } from '../../../components/primitives'
import { useMemoryTrace } from '../../../hooks/useMemoryTrace'
import { formatDate, sanitiseSnippet, truncate } from '../helpers'
import type { CrossNav } from '../types'

export interface MemoryRowProps {
  kind: 'event' | 'thought'
  item: MemoryEvent | MemoryThought
  highlighted: boolean
  onHighlightConsumed: () => void
  onDelete: () => void
  crossNav: CrossNav
  snippet?: string
}

export function MemoryRow({
  kind,
  item,
  highlighted,
  onHighlightConsumed,
  onDelete,
  crossNav,
  snippet,
}: MemoryRowProps) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)
  const trace = useMemoryTrace({ kind, nodeId: item.id })

  useEffect(() => {
    if (!highlighted) return
    const node = ref.current
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const timer = window.setTimeout(onHighlightConsumed, 1800)
    return () => window.clearTimeout(timer)
  }, [highlighted, onHighlightConsumed])

  const toggle = () => {
    if (!expanded && trace.data === null) void trace.load()
    setExpanded((v) => !v)
  }

  const expandedNodes: TraceNode[] = (() => {
    if (!trace.data) return []
    if (trace.data.kind === 'thought') return trace.data.response.source_events
    return trace.data.response.dependent_thoughts
  })()

  const toggleLabel =
    kind === 'thought'
      ? t('admin.memory_list.lineage_sources')
      : t('admin.memory_list.lineage_thoughts')

  return (
    <div
      ref={ref}
      className="card"
      style={{
        padding: 14,
        outline: highlighted ? '2px solid var(--accent)' : 'none',
        outlineOffset: 2,
        transition: 'outline 160ms',
      }}
    >
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <span className="label">#{item.id}</span>
        <span
          style={{
            fontSize: 11,
            color: 'var(--ink-3)',
            fontFamily: 'var(--mono)',
          }}
        >
          {formatDate(item.created_at)} · used {item.access_count}×
        </span>
        <div className="flex1" />
        <EmotionBar v={item.emotional_impact} />
        <button
          className="btn ghost sm"
          title={
            kind === 'event'
              ? t('admin.events.delete_title')
              : t('admin.thoughts.delete_title')
          }
          onClick={onDelete}
          style={{ padding: '4px 8px' }}
        >
          ✕
        </button>
      </div>
      {snippet ? (
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 14,
            color: 'var(--ink)',
            marginTop: 6,
            lineHeight: 1.5,
          }}
          dangerouslySetInnerHTML={{ __html: sanitiseSnippet(snippet) }}
        />
      ) : (
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 14,
            color: 'var(--ink)',
            marginTop: 6,
            lineHeight: 1.5,
          }}
        >
          {item.description}
        </div>
      )}
      <div className="row g-2" style={{ marginTop: 8, flexWrap: 'wrap' }}>
        {item.emotion_tags.map((tg) => (
          <span key={tg} className="chip">
            {tg}
          </span>
        ))}
        {item.relational_tags.map((tg) => (
          <span key={tg} className="chip accent">
            ↔ {tg}
          </span>
        ))}
        {item.imported_from && (
          <span className="chip dashed">imported · {item.imported_from}</span>
        )}
      </div>
      <div style={{ marginTop: 10 }}>
        <button
          onClick={toggle}
          disabled={trace.loading}
          style={{
            background: 'transparent',
            fontFamily: 'var(--mono)',
            fontSize: 10,
            letterSpacing: '0.1em',
            textTransform: 'uppercase',
            color: 'var(--ink-3)',
            padding: 0,
          }}
        >
          {trace.loading
            ? t('admin.memory_list.lineage_searching')
            : `${toggleLabel}${
                trace.data
                  ? ' ' +
                    t('admin.memory_list.lineage_count', {
                      count: expandedNodes.length,
                    })
                  : ''
              } ${expanded ? '▾' : '▸'}`}
        </button>
      </div>
      {expanded && (
        <div
          style={{
            marginTop: 10,
            paddingTop: 10,
            borderTop: '1px dashed var(--rule)',
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          {trace.error && (
            <div style={{ color: 'var(--accent)', fontSize: 12 }}>
              ⚠ {trace.error}
            </div>
          )}
          {!trace.loading && trace.data && expandedNodes.length === 0 && (
            <div
              style={{
                color: 'var(--ink-3)',
                fontSize: 12,
                fontStyle: 'italic',
              }}
            >
              {kind === 'thought'
                ? t('admin.memory_list.no_sources')
                : t('admin.memory_list.no_derivatives')}
            </div>
          )}
          {expandedNodes.map((n) => (
            <button
              key={n.id}
              onClick={() =>
                crossNav.navigateTo(
                  kind === 'thought' ? 'event' : 'thought',
                  n.id,
                )
              }
              style={{
                textAlign: 'left',
                padding: '8px 10px',
                background: 'var(--paper-2)',
                border: '1px solid var(--rule)',
                borderRadius: 4,
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
              }}
            >
              <div
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 10,
                  color: 'var(--ink-3)',
                  letterSpacing: '0.08em',
                }}
              >
                #{n.id} · {formatDate(n.created_at)}
              </div>
              <div style={{ fontSize: 13, color: 'var(--ink)' }}>
                {truncate(n.description, 120)}
              </div>
            </button>
          ))}
          {trace.data?.kind === 'thought' &&
            trace.data.response.source_sessions.length > 0 && (
              <div
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 10,
                  color: 'var(--ink-3)',
                  paddingTop: 4,
                }}
              >
                {t('admin.memory_list.from_sessions', {
                  count: trace.data.response.source_sessions.length,
                })}
              </div>
            )}
        </div>
      )}
    </div>
  )
}
