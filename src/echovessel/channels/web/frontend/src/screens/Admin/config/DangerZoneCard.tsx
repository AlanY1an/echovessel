import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { postAdminReset } from '../../../api/client'
import { ApiError } from '../../../api/types'

/**
 * Last card on the Config tab. Exposes a single nuclear action —
 * `POST /api/admin/reset` — wrapped in a typed-confirmation so it can't
 * be triggered by an accidental click. The card has two visual states:
 *
 *   armed=false  · a warning paragraph and a ghost "Reset everything…"
 *                  button that flips the card into armed mode.
 *   armed=true   · an input the user must type the confirm keyword into,
 *                  plus a red accent "Reset everything" button that stays
 *                  disabled until the input matches.
 *
 * On success the page hard-reloads so App.tsx re-bootstraps and the
 * daemon's fresh `onboarding_required=true` flips us back into the
 * setup flow.
 */
export function DangerZoneCard() {
  const { t } = useTranslation()
  const [armed, setArmed] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const keyword = t('admin.danger_zone.confirm_keyword')
  const canConfirm = confirmText.trim() === keyword && !running

  const cancel = () => {
    setArmed(false)
    setConfirmText('')
    setError(null)
  }

  const runReset = async () => {
    if (!canConfirm) return
    setRunning(true)
    setError(null)
    try {
      await postAdminReset()
      // Hard reload so the AppShell re-reads /api/state and picks up
      // the fresh onboarding_required=true — which routes us into the
      // onboarding screen automatically.
      window.location.reload()
    } catch (err) {
      setRunning(false)
      if (err instanceof ApiError) setError(err.detail)
      else if (err instanceof Error) setError(err.message)
      else setError(t('admin.danger_zone.generic_error'))
    }
  }

  return (
    <div
      className="card"
      style={{
        padding: 18,
        border: '1px solid var(--accent)',
        background: 'var(--accent-soft)',
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <span className="label" style={{ color: 'var(--accent)' }}>
          {t('admin.danger_zone.label')}
        </span>
        <div className="flex1" />
      </div>
      <div
        style={{
          marginTop: 8,
          fontSize: 13,
          color: 'var(--ink-2)',
          lineHeight: 1.5,
          maxWidth: 560,
        }}
      >
        {t('admin.danger_zone.description')}
      </div>

      {!armed ? (
        <div
          className="row g-2"
          style={{ marginTop: 14, alignItems: 'center' }}
        >
          <button
            type="button"
            className="btn ghost sm"
            style={{ borderColor: 'var(--accent)', color: 'var(--accent)' }}
            onClick={() => setArmed(true)}
          >
            {t('admin.danger_zone.arm_cta')} →
          </button>
        </div>
      ) : (
        <div
          className="stack g-2"
          style={{ marginTop: 14, maxWidth: 560 }}
        >
          <label className="label" style={{ color: 'var(--ink-2)' }}>
            {t('admin.danger_zone.type_to_confirm', { keyword })}
          </label>
          <input
            type="text"
            autoFocus
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            disabled={running}
            placeholder={keyword}
            style={{
              border: '1px solid var(--rule)',
              padding: '8px 12px',
              borderRadius: 4,
              fontSize: 14,
              fontFamily: 'var(--mono)',
              background: 'var(--paper)',
              outline: 'none',
            }}
          />
          <div className="row g-2" style={{ alignItems: 'center' }}>
            <button
              type="button"
              className="btn ghost sm"
              onClick={cancel}
              disabled={running}
            >
              {t('admin.common.cancel')}
            </button>
            <div className="flex1" />
            {error && (
              <span
                style={{
                  fontSize: 11,
                  color: 'var(--accent)',
                  fontFamily: 'var(--mono)',
                }}
              >
                ⚠ {error}
              </span>
            )}
            <button
              type="button"
              className="btn accent"
              disabled={!canConfirm}
              onClick={() => void runReset()}
            >
              {running
                ? t('admin.danger_zone.running')
                : t('admin.danger_zone.commit_cta')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
