import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { MemoryEvent, MemoryThought } from '../../../api/types'
import { EmotionBar } from '../../../components/primitives'
import { formatDate, truncate } from '../helpers'
import type { CrossNav } from '../types'

/** Memory graph · events ⊕ thoughts laid out by impact × recency. */
export function MemoryGraph({
  events,
  thoughts,
  crossNav,
}: {
  events: MemoryEvent[]
  thoughts: MemoryThought[]
  crossNav: CrossNav
}) {
  const { t } = useTranslation()
  const all = useMemo(
    () =>
      [
        ...events.map((e) => ({ ...e, kind: 'event' as const })),
        ...thoughts.map((e) => ({ ...e, kind: 'thought' as const })),
      ],
    [events, thoughts],
  )
  const [sel, setSel] = useState<number | null>(null)

  const layout = useMemo(() => {
    if (all.length === 0) return []
    const now = Date.now()
    const times = all.map((n) =>
      n.created_at ? new Date(n.created_at).getTime() : now,
    )
    const oldest = Math.min(...times)
    const span = now - oldest || 1
    return all.map((n, i) => {
      const t = n.created_at ? new Date(n.created_at).getTime() : now
      const x = 50 + n.emotional_impact * 42
      const age = (now - t) / span
      const y = 12 + age * 72 + ((i * 37) % 11) - 5
      const size = 10 + Math.min(28, Math.sqrt(n.access_count) * 6)
      return { n, x, y, size }
    })
  }, [all])

  const selNode = sel !== null ? all.find((n) => n.id === sel) ?? null : null
  const related = selNode
    ? all.filter(
        (n) =>
          n.id !== sel &&
          n.relational_tags.some((tg) => selNode.relational_tags.includes(tg)),
      )
    : []

  if (all.length === 0) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--ink-3)',
        }}
      >
        {t('admin.memory_tab.graph_empty')}
      </div>
    )
  }

  return (
    <div
      className="graph-layout"
      style={{
        flex: 1,
        minHeight: 0,
        border: '1px solid var(--rule)',
        borderRadius: 6,
        overflow: 'hidden',
      }}
    >
      <div className="graph-canvas">
        <div
          style={{
            position: 'absolute',
            left: '50%',
            top: 0,
            bottom: 0,
            width: 1,
            background: 'var(--rule)',
            opacity: 0.6,
          }}
        />
        <div
          style={{
            position: 'absolute',
            left: 14,
            top: 14,
            fontFamily: 'var(--mono)',
            fontSize: 10,
            color: 'var(--ink-3)',
            letterSpacing: '0.1em',
          }}
        >
          NEWER
        </div>
        <div
          style={{
            position: 'absolute',
            left: 14,
            bottom: 14,
            fontFamily: 'var(--mono)',
            fontSize: 10,
            color: 'var(--ink-3)',
            letterSpacing: '0.1em',
          }}
        >
          OLDER
        </div>
        <div
          style={{
            position: 'absolute',
            left: 14,
            top: '50%',
            fontFamily: 'var(--mono)',
            fontSize: 10,
            color: 'var(--ink-3)',
          }}
        >
          − heavy
        </div>
        <div
          style={{
            position: 'absolute',
            right: 14,
            top: '50%',
            fontFamily: 'var(--mono)',
            fontSize: 10,
            color: 'var(--ink-3)',
          }}
        >
          + warm
        </div>
        <svg
          style={{
            position: 'absolute',
            inset: 0,
            width: '100%',
            height: '100%',
            pointerEvents: 'none',
          }}
        >
          {selNode &&
            layout
              .filter((l) => related.some((r) => r.id === l.n.id))
              .map((l) => {
                const src = layout.find((x) => x.n.id === sel)
                if (!src) return null
                return (
                  <line
                    key={l.n.id}
                    x1={`${src.x}%`}
                    y1={`${src.y}%`}
                    x2={`${l.x}%`}
                    y2={`${l.y}%`}
                    stroke="var(--accent)"
                    strokeOpacity={0.35}
                    strokeWidth={1}
                  />
                )
              })}
        </svg>
        {layout.map((l) => {
          const isSel = l.n.id === sel
          const isRel = related.some((r) => r.id === l.n.id)
          const neg = l.n.emotional_impact < 0
          return (
            <div
              key={`${l.n.kind}-${l.n.id}`}
              className={`node ${neg ? '' : 'accent'} ${isSel ? 'sel' : ''}`}
              style={{
                left: `${l.x}%`,
                top: `${l.y}%`,
                opacity: selNode && !isSel && !isRel ? 0.35 : 1,
              }}
              onClick={() => setSel(l.n.id)}
            >
              <div
                className="c"
                style={{
                  width: l.size,
                  height: l.size,
                  background:
                    l.n.kind === 'thought'
                      ? 'var(--accent)'
                      : 'var(--ink)',
                }}
              />
              <div className="ll">
                <b>#{l.n.id}</b>
              </div>
            </div>
          )
        })}
      </div>
      <div className="graph-panel">
        {selNode ? (
          <>
            <span className="label">
              #{selNode.id} · {selNode.kind}
            </span>
            <div style={{ fontFamily: 'var(--serif)', fontSize: 14, lineHeight: 1.5 }}>
              {selNode.description}
            </div>
            <div
              style={{
                fontFamily: 'var(--mono)',
                fontSize: 11,
                color: 'var(--ink-3)',
              }}
            >
              {formatDate(selNode.created_at)} · used {selNode.access_count}×
            </div>
            <EmotionBar v={selNode.emotional_impact} />
            <div className="row g-2" style={{ flexWrap: 'wrap' }}>
              {selNode.emotion_tags.map((tg) => (
                <span key={tg} className="chip">
                  {tg}
                </span>
              ))}
              {selNode.relational_tags.map((tg) => (
                <span key={tg} className="chip accent">
                  ↔ {tg}
                </span>
              ))}
            </div>
            <button
              className="btn ghost sm"
              onClick={() =>
                crossNav.navigateTo(
                  selNode.kind === 'thought' ? 'thought' : 'event',
                  selNode.id,
                )
              }
              style={{ marginTop: 8, alignSelf: 'flex-start' }}
            >
              {t('admin.memory_tab.open_in_list')} →
            </button>
            {related.length > 0 && (
              <div
                className="stack g-1"
                style={{
                  marginTop: 10,
                  borderTop: '1px solid var(--rule)',
                  paddingTop: 10,
                }}
              >
                <span className="label">
                  {t('admin.memory_tab.related')} ({related.length})
                </span>
                {related.map((r) => (
                  <div
                    key={`${r.kind}-${r.id}`}
                    onClick={() => setSel(r.id)}
                    style={{
                      cursor: 'pointer',
                      fontSize: 12,
                      color: 'var(--ink-2)',
                      padding: '4px 0',
                      borderBottom: '1px dashed var(--rule)',
                    }}
                  >
                    <span
                      style={{
                        fontFamily: 'var(--mono)',
                        color: 'var(--ink-3)',
                      }}
                    >
                      #{r.id}
                    </span>{' '}
                    {truncate(r.description, 60)}
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <div style={{ color: 'var(--ink-3)', fontSize: 13 }}>
            {t('admin.memory_tab.click_a_node')}
          </div>
        )}
      </div>
    </div>
  )
}
