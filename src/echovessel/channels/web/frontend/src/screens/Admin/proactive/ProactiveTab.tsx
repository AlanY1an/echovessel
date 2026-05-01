import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  getProactiveDecisions,
  getProactiveEvents,
  suppressProactiveEvent,
} from '../../../api/client'
import type {
  ProactiveDecisionFilter,
  ProactiveDecisionItem,
  ProactiveEventItem,
  ProactiveEventState,
} from '../../../api/types'

type SubSection = 'history' | 'events'

function _formatDateTime(value: string | null | undefined): string {
  if (value === null || value === undefined) return '—'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return value
  }
}

function _formatTimeOnly(value: string): string {
  try {
    return new Date(value).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return value
  }
}

export function AdmProactive() {
  const [section, setSection] = useState<SubSection>('history')
  const { t } = useTranslation()

  return (
    <div style={{ padding: '28px 36px' }}>
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title">
          {section === 'history'
            ? t('admin.proactive.history_tab_title')
            : t('admin.proactive.events_tab_title')}
        </h2>
        <div className="flex1" />
        <div className="row g-2">
          <button
            className={`btn ghost sm ${section === 'history' ? 'on' : ''}`}
            onClick={() => setSection('history')}
          >
            {t('admin.proactive.subsection_history')}
          </button>
          <button
            className={`btn ghost sm ${section === 'events' ? 'on' : ''}`}
            onClick={() => setSection('events')}
          >
            {t('admin.proactive.subsection_events')}
          </button>
        </div>
      </div>

      {section === 'history' && <HistorySection />}
      {section === 'events' && <EventsSection />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// History sub-section
// ---------------------------------------------------------------------------

function HistorySection() {
  const { t } = useTranslation()
  const [items, setItems] = useState<ProactiveDecisionItem[]>([])
  const [filter, setFilter] = useState<ProactiveDecisionFilter>('all')
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const load = async (
    nextFilter: ProactiveDecisionFilter,
    appendCursor: string | null,
  ) => {
    setLoading(true)
    setLoadError(null)
    try {
      const resp = await getProactiveDecisions({
        action: nextFilter,
        cursor: appendCursor ?? undefined,
        limit: 50,
      })
      setItems((prev) =>
        appendCursor === null ? resp.items : [...prev, ...resp.items],
      )
      setCursor(resp.cursor)
      setHasMore(resp.has_more)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(filter, null)
    // Re-run only when the filter changes; load itself is stable enough
    // for our purposes here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter])

  const onRefresh = () => {
    void load(filter, null)
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        marginTop: 16,
      }}
    >
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as ProactiveDecisionFilter)}
          style={{ padding: '4px 8px' }}
        >
          <option value="all">{t('admin.proactive.filter_all')}</option>
          <option value="fire">{t('admin.proactive.filter_fire')}</option>
          <option value="suppress">
            {t('admin.proactive.filter_suppress')}
          </option>
        </select>
        <button className="btn ghost sm" onClick={onRefresh} disabled={loading}>
          {t('admin.proactive.refresh')}
        </button>
      </div>

      {loadError !== null && (
        <p style={{ color: 'var(--danger, #c44)', fontSize: 13 }}>
          {t('admin.proactive.load_failed', { message: loadError })}
        </p>
      )}

      {!loading && items.length === 0 && loadError === null && (
        <p style={{ color: 'var(--ink-2)', fontSize: 13 }}>
          {t('admin.proactive.empty_state_history')}
        </p>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {items.map((item) => (
          <DecisionRow key={item.id} item={item} />
        ))}
      </div>

      {hasMore && (
        <button
          className="btn ghost sm"
          onClick={() => void load(filter, cursor)}
          disabled={loading}
          style={{ alignSelf: 'flex-start' }}
        >
          {t('admin.proactive.load_more')}
        </button>
      )}
    </div>
  )
}

interface DecisionRowProps {
  item: ProactiveDecisionItem
}

function DecisionRow({ item }: DecisionRowProps) {
  const { t } = useTranslation()
  const isFire = item.action === 'fire'

  const dot = (
    <span
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: isFire ? '#3a8a3a' : '#c44',
        marginRight: 8,
      }}
      aria-hidden
    />
  )

  let replyLine: string | null = null
  if (isFire) {
    if (item.user_replied_at !== null) {
      replyLine = t('admin.proactive.user_replied', {
        time: _formatTimeOnly(item.user_replied_at),
      })
    } else if (item.settled_no_reply) {
      replyLine = t('admin.proactive.settled_no_reply')
    } else {
      replyLine = t('admin.proactive.no_reply_yet')
    }
  }

  const triggerLabel =
    item.trigger_type === 'thread_due'
      ? t('admin.proactive.trigger_thread_due')
      : item.trigger_type === 'high_emotional_event'
        ? t('admin.proactive.trigger_high_emotional_event')
        : item.trigger_type

  const phaseLabel =
    item.phase !== null
      ? t(`admin.proactive.phase.${item.phase}`, {
          defaultValue: item.phase,
        })
      : null

  const suppressLabel = item.suppress_reason
    ? t(`admin.proactive.suppress_reason.${item.suppress_reason}`, {
        defaultValue: t('admin.proactive.suppress_reason.unknown'),
      })
    : null

  return (
    <div
      className="card"
      style={{
        padding: 14,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}
    >
      <div style={{ fontSize: 13, color: 'var(--ink-2)' }}>
        {dot}
        <strong>
          {isFire
            ? t('admin.proactive.fired_label')
            : t('admin.proactive.suppressed_label')}
        </strong>
        {' · '}
        {_formatDateTime(item.timestamp)}
        {' · '}
        {triggerLabel}
        {phaseLabel !== null && (
          <>
            {' · '}
            {t('admin.proactive.phase_label')}: {phaseLabel}
          </>
        )}
        {suppressLabel !== null && (
          <>
            {' · '}
            {suppressLabel}
          </>
        )}
      </div>

      {isFire && item.message_text !== null && (
        <div style={{ fontSize: 14 }}>{item.message_text}</div>
      )}

      {item.source_event_description !== null && (
        <div style={{ fontSize: 13, color: 'var(--ink-2)' }}>
          {t('admin.proactive.source_event_label')}:{' '}
          {item.source_event_description}
        </div>
      )}

      {replyLine !== null && (
        <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>{replyLine}</div>
      )}

      {!isFire && item.gate_state_snapshot !== null && (
        <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>
          <code>{JSON.stringify(item.gate_state_snapshot)}</code>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Events sub-section
// ---------------------------------------------------------------------------

function EventsSection() {
  const { t } = useTranslation()
  const [items, setItems] = useState<ProactiveEventItem[]>([])
  const [state, setState] = useState<ProactiveEventState>('active')
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [suppressError, setSuppressError] = useState<string | null>(null)

  const load = async (nextState: ProactiveEventState) => {
    setLoading(true)
    setLoadError(null)
    try {
      const resp = await getProactiveEvents(nextState, 50)
      setItems(resp.items)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(state)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state])

  const onSuppress = async (eventId: number) => {
    if (!window.confirm(t('admin.proactive.suppress_event_confirm'))) return
    setSuppressError(null)
    try {
      await suppressProactiveEvent(eventId)
      setItems((prev) => prev.filter((it) => it.id !== eventId))
    } catch (e) {
      setSuppressError(e instanceof Error ? e.message : String(e))
    }
  }

  const emptyKey = (() => {
    switch (state) {
      case 'fired':
        return 'admin.proactive.empty_state_events_fired'
      case 'closed':
        return 'admin.proactive.empty_state_events_closed'
      default:
        return 'admin.proactive.empty_state_events_active'
    }
  })()

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        marginTop: 16,
      }}
    >
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <select
          value={state}
          onChange={(e) => setState(e.target.value as ProactiveEventState)}
          style={{ padding: '4px 8px' }}
        >
          <option value="active">{t('admin.proactive.state_active')}</option>
          <option value="fired">{t('admin.proactive.state_fired')}</option>
          <option value="closed">{t('admin.proactive.state_closed')}</option>
          <option value="all">{t('admin.proactive.state_all')}</option>
        </select>
        <button
          className="btn ghost sm"
          onClick={() => void load(state)}
          disabled={loading}
        >
          {t('admin.proactive.refresh')}
        </button>
      </div>

      {loadError !== null && (
        <p style={{ color: 'var(--danger, #c44)', fontSize: 13 }}>
          {t('admin.proactive.load_failed', { message: loadError })}
        </p>
      )}
      {suppressError !== null && (
        <p style={{ color: 'var(--danger, #c44)', fontSize: 13 }}>
          {t('admin.proactive.suppress_event_failed', {
            message: suppressError,
          })}
        </p>
      )}

      {!loading && items.length === 0 && loadError === null && (
        <p style={{ color: 'var(--ink-2)', fontSize: 13 }}>{t(emptyKey)}</p>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {items.map((event) => (
          <EventRow
            key={event.id}
            event={event}
            state={state}
            onSuppress={onSuppress}
          />
        ))}
      </div>
    </div>
  )
}

interface EventRowProps {
  event: ProactiveEventItem
  state: ProactiveEventState
  onSuppress: (eventId: number) => Promise<void> | void
}

/**
 * Map the (advance_pre_hours, advance_post_hours, estimated_arc_days)
 * tuple onto a human-readable kind label. ``estimated_arc_days`` > 0
 * marks an ongoing arc; otherwise the pair of advance_*_hours
 * disambiguates pre-event reminder vs same-instant ping vs post-event
 * follow-up.
 */
function _deriveKindKey(event: ProactiveEventItem): string {
  if (event.estimated_arc_days !== null && event.estimated_arc_days > 0) {
    return 'admin.proactive.kind.ongoing'
  }
  const pre = event.advance_pre_hours ?? 0
  const post = event.advance_post_hours ?? 0
  if (pre > 0 && post === 0) return 'admin.proactive.kind.pre_event'
  if (pre === 0 && post > 0) return 'admin.proactive.kind.post_event'
  return 'admin.proactive.kind.point_event'
}

function EventRow({ event, state, onSuppress }: EventRowProps) {
  const { t } = useTranslation()
  const kindLabel = t(_deriveKindKey(event))
  const isClosed = event.closed
  const showSuppressed =
    state !== 'active' && event.proactive_suppressed_at !== null
  const showSuperseded =
    state !== 'active' && event.superseded_by_id !== null

  return (
    <div
      className="card"
      style={{
        padding: 14,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}
    >
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <strong style={{ fontSize: 14 }}>#{event.id}</strong>
        <span style={{ fontSize: 12, color: 'var(--ink-2)' }}>{kindLabel}</span>
        <div className="flex1" />
        {state === 'active' && !isClosed && (
          <button
            className="btn ghost sm"
            onClick={() => void onSuppress(event.id)}
          >
            {t('admin.proactive.suppress_event_button')}
          </button>
        )}
      </div>

      <div style={{ fontSize: 14 }}>{event.description}</div>

      {event.follow_up_hint !== null && event.follow_up_hint !== '' && (
        <div style={{ fontSize: 13, color: 'var(--ink-2)' }}>
          {t('admin.proactive.column.follow_up_hint')}: {event.follow_up_hint}
        </div>
      )}

      <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>
        {event.follow_up_at !== null && (
          <>
            {t('admin.proactive.follow_up_at_label')}:{' '}
            {_formatDateTime(event.follow_up_at)}
            {' · '}
          </>
        )}
        {event.fired_at !== null && (
          <>
            {t('admin.proactive.fired_at_label')}:{' '}
            {_formatDateTime(event.fired_at)}
            {' · '}
          </>
        )}
        {t('admin.proactive.created_at_label')}:{' '}
        {_formatDateTime(event.created_at)}
      </div>

      {showSuppressed && event.proactive_suppressed_at !== null && (
        <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>
          {t('admin.proactive.suppressed_at_label')}:{' '}
          {_formatDateTime(event.proactive_suppressed_at)}
        </div>
      )}

      {showSuperseded && event.superseded_by_id !== null && (
        <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>
          {t('admin.proactive.superseded_by_label')}: #{event.superseded_by_id}
        </div>
      )}
    </div>
  )
}
