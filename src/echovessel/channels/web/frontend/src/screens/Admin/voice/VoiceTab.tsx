import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import type { PersonaStateApi } from '../../../api/types'
import { Avatar, Wave, fmtT } from '../../../components/primitives'
import { useVoiceClone } from '../../../hooks/useVoiceClone'

export function AdmVoice({
  persona,
  toggleVoice,
}: {
  persona: PersonaStateApi
  toggleVoice: (enabled: boolean) => Promise<void>
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const voice = useVoiceClone()
  const [toggling, setToggling] = useState(false)

  const handleToggle = async () => {
    setToggling(true)
    try {
      await toggleVoice(!persona.voice_enabled)
    } finally {
      setToggling(false)
    }
  }

  const total = voice.samples.reduce(
    (a, s) => a + (s.duration_seconds || 0),
    0,
  )

  return (
    <div
      style={{
        padding: '28px 36px',
        display: 'flex',
        flexDirection: 'column',
        gap: 20,
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.voice_tab.section_title')}</h2>
        <div className="flex1" />
        <div className="row g-2" style={{ alignItems: 'center' }}>
          <span style={{ fontSize: 13, color: 'var(--ink-2)' }}>
            {persona.voice_enabled
              ? t('admin.voice_tab.toggle_label_on')
              : t('admin.voice_tab.toggle_label_off')}
          </span>
          <button
            onClick={() => void handleToggle()}
            disabled={toggling}
            style={{
              width: 40,
              height: 22,
              borderRadius: 999,
              background: persona.voice_enabled ? 'var(--ink)' : 'var(--paper-4)',
              position: 'relative',
              transition: 'background 120ms',
              cursor: toggling ? 'wait' : 'pointer',
            }}
          >
            <div
              style={{
                position: 'absolute',
                top: 2,
                left: persona.voice_enabled ? 20 : 2,
                width: 18,
                height: 18,
                borderRadius: '50%',
                background: 'var(--paper)',
                transition: 'left 120ms',
              }}
            />
          </button>
        </div>
      </div>

      <div
        className="card"
        style={{
          padding: 18,
          display: 'flex',
          gap: 18,
          alignItems: 'center',
        }}
      >
        <Avatar letter={persona.display_name.charAt(0).toUpperCase()} size="lg" />
        <div className="stack g-1" style={{ flex: 1 }}>
          <div style={{ fontWeight: 600 }}>
            {persona.display_name}
            {persona.voice_id ? ' · active' : ' · default voice'}
          </div>
          <div
            style={{
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--ink-3)',
            }}
          >
            voice_id · {persona.voice_id ?? '—'}
          </div>
        </div>
        <button
          className="btn ghost sm"
          onClick={() => navigate('/admin/voice/clone')}
        >
          {persona.voice_id
            ? t('admin.voice_tab.reclone_cta')
            : t('admin.voice_tab.clone_cta')}
        </button>
      </div>

      <div
        style={{
          padding: 14,
          border: '1px dashed var(--rule-strong)',
          borderRadius: 6,
          background: 'var(--paper-2)',
          fontSize: 12,
          color: 'var(--ink-2)',
        }}
      >
        {t('admin.voice_tab.clone_body')}
      </div>

      <div className="stack g-2">
        <div className="row g-2" style={{ alignItems: 'baseline' }}>
          <span className="label">
            {t('admin.voice_tab.samples_label', {
              count: voice.samples.length,
              duration: fmtT(total),
            })}
          </span>
          <div className="flex1" />
          <button
            className="btn sm"
            onClick={() => navigate('/admin/voice/clone')}
          >
            + {t('admin.voice_tab.add_sample')}
          </button>
        </div>
        {voice.samples.length === 0 ? (
          <div
            style={{
              color: 'var(--ink-3)',
              fontSize: 12,
              padding: 16,
              textAlign: 'center',
              border: '1px dashed var(--rule)',
              borderRadius: 4,
            }}
          >
            {t('admin.voice.no_samples')}
          </div>
        ) : (
          voice.samples.map((s, i) => (
            <div key={s.sample_id} className="sample-row">
              <button className="play" type="button">
                ▶
              </button>
              <div className="name">
                {s.filename}
                <br />
                <span style={{ color: 'var(--ink-3)', fontSize: 9 }}>
                  {fmtT(s.duration_seconds)} ·{' '}
                  {(s.size_bytes / 1e6).toFixed(1)}MB
                </span>
              </div>
              <div className="wave">
                <Wave bars={60} seed={i * 7} />
              </div>
              <button
                className="btn ghost sm"
                style={{ padding: '4px 8px' }}
                onClick={() => void voice.removeSample(s.sample_id)}
              >
                ✕
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
