import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { getCostSummary } from '../../../api/client'
import { ApiError } from '../../../api/types'
import type {
  CostFeatureBucket,
  CostSummaryResponse,
} from '../../../api/types'
import { labelForFeature } from '../helpers'

/** One full-width card bound to `getCostSummary('30d')`. */
export function CostCard() {
  const { t } = useTranslation()
  const [data, setData] = useState<CostSummaryResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getCostSummary('30d')
      .then((r) => {
        if (!cancelled) setData(r)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError) setError(err.detail)
        else if (err instanceof Error) setError(err.message)
        else setError('unknown error')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (loading && data === null) {
    return (
      <div className="card" style={{ padding: 20, color: 'var(--ink-3)' }}>
        {t('admin.common.loading')}
      </div>
    )
  }
  if (data === null) {
    return (
      <div className="card" style={{ padding: 20, color: 'var(--accent)' }}>
        ⚠ {error ?? 'cost unavailable'}
      </div>
    )
  }

  const buckets = Object.entries(data.by_feature) as [
    string,
    CostFeatureBucket,
  ][]
  buckets.sort(([, a], [, b]) => b.cost_usd - a.cost_usd)
  const totalUsd = data.total_usd || 0.0001

  return (
    <div className="card" style={{ padding: 20 }}>
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <span className="label">{t('admin.cost.last_30d')}</span>
        <div className="flex1" />
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 32,
            letterSpacing: '-0.02em',
          }}
        >
          ${data.total_usd.toFixed(2)}
        </div>
        <div style={{ fontSize: 12, color: 'var(--ink-3)' }}>
          {(data.total_tokens / 1000).toFixed(0)}k tokens
        </div>
      </div>
      {buckets.length === 0 ? (
        <div
          style={{
            marginTop: 14,
            color: 'var(--ink-3)',
            fontSize: 12,
          }}
        >
          {t('admin.cost.empty_body')}
        </div>
      ) : (
        <div
          style={{
            marginTop: 14,
            display: 'grid',
            gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
            gap: '6px 28px',
          }}
        >
          {buckets.map(([f, b]) => (
            <div
              key={f}
              className="row g-2"
              style={{ alignItems: 'center' }}
            >
              <span
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 11,
                  color: 'var(--ink-2)',
                  width: 110,
                }}
              >
                {labelForFeature(f, t)}
              </span>
              <div
                style={{
                  flex: 1,
                  height: 4,
                  background: 'var(--paper-3)',
                  borderRadius: 2,
                }}
              >
                <div
                  style={{
                    width: (b.cost_usd / totalUsd) * 100 + '%',
                    height: '100%',
                    background: 'var(--ink)',
                  }}
                />
              </div>
              <span
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 11,
                  color: 'var(--ink-2)',
                  width: 60,
                  textAlign: 'right',
                }}
              >
                ${b.cost_usd.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
