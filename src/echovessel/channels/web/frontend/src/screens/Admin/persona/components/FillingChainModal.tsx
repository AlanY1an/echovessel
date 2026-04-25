import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { getThoughtTrace } from '../../../../api/client'
import { ApiError } from '../../../../api/types'
import type { ThoughtTraceResponse } from '../../../../api/types'
import { formatDate } from '../../helpers'

/**
 * Modal that fans out a persona-subject reflection's source L3 events
 * via ``GET /api/admin/memory/thoughts/{id}/trace``. Mounted
 * unconditionally by ``ReflectionSection`` and short-circuits to null
 * when ``open=false`` so the parent doesn't need to gate the JSX.
 *
 * The modal fetches whenever ``thoughtId`` changes (open transition);
 * it does NOT re-fetch on close. The fetch is fire-and-forget — if it
 * errors, an inline message is shown but the section list itself
 * stays usable.
 */
export function FillingChainModal({
  open,
  thoughtId,
  onClose,
}: {
  open: boolean
  thoughtId: number | null
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [trace, setTrace] = useState<ThoughtTraceResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || thoughtId === null) return
    setLoading(true)
    setError(null)
    setTrace(null)
    let cancelled = false
    void (async () => {
      try {
        const res = await getThoughtTrace(thoughtId)
        if (!cancelled) setTrace(res)
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError) setError(err.detail)
        else if (err instanceof Error) setError(err.message)
        else setError('unknown error')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, thoughtId])

  if (!open || thoughtId === null) return null
  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 50,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="card"
        style={{
          padding: 22,
          maxWidth: 560,
          width: '92%',
          maxHeight: '80vh',
          overflowY: 'auto',
          background: 'var(--paper)',
        }}
      >
        <div className="row g-2" style={{ alignItems: 'baseline' }}>
          <h3 className="title" style={{ margin: 0 }}>
            {t('admin.persona.reflection.filling_modal_title')}
          </h3>
          <span
            style={{
              fontSize: 11,
              color: 'var(--ink-3)',
              fontFamily: 'var(--mono)',
            }}
          >
            #{thoughtId}
          </span>
          <div className="flex1" />
          <button type="button" className="btn ghost sm" onClick={onClose}>
            {t('admin.common.close')}
          </button>
        </div>

        {loading && (
          <p
            style={{
              color: 'var(--ink-3)',
              fontSize: 12,
              marginTop: 16,
              fontFamily: 'var(--mono)',
              textAlign: 'center',
            }}
          >
            {t('admin.common.loading')}
          </p>
        )}

        {error !== null && (
          <p
            style={{
              color: 'var(--accent)',
              fontSize: 12,
              marginTop: 16,
              fontFamily: 'var(--mono)',
            }}
          >
            ⚠ {error}
          </p>
        )}

        {!loading && error === null && trace !== null && (
          <>
            {trace.source_events.length === 0 ? (
              <p
                style={{
                  color: 'var(--ink-3)',
                  fontSize: 13,
                  marginTop: 16,
                  fontFamily: 'var(--serif)',
                  textAlign: 'center',
                }}
              >
                {t('admin.persona.reflection.filling_modal_empty')}
              </p>
            ) : (
              <div
                className="stack g-2"
                style={{ marginTop: 14 }}
              >
                {trace.source_events.map((evt) => (
                  <div
                    key={evt.id}
                    className="card"
                    style={{
                      padding: 10,
                      borderLeft: '2px solid var(--rule)',
                    }}
                  >
                    <div
                      style={{
                        fontSize: 11,
                        color: 'var(--ink-3)',
                        fontFamily: 'var(--mono)',
                        marginBottom: 4,
                      }}
                    >
                      #{evt.id} · {formatDate(evt.created_at)}
                    </div>
                    <div
                      style={{
                        fontFamily: 'var(--serif)',
                        fontSize: 13,
                        lineHeight: 1.5,
                        color: 'var(--ink-2)',
                      }}
                    >
                      {evt.description}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {trace.source_sessions.length > 0 && (
              <p
                style={{
                  fontSize: 11,
                  color: 'var(--ink-3)',
                  marginTop: 14,
                  fontFamily: 'var(--mono)',
                }}
              >
                {t('admin.persona.reflection.filling_modal_session_count', {
                  count: trace.source_sessions.length,
                })}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
