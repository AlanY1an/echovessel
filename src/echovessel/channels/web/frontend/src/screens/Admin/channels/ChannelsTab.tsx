import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useConfig } from '../../../hooks/useConfig'
import { DiscordChannelCard } from './DiscordChannelCard'
import { IMessageChannelCard } from './IMessageChannelCard'
import { WebChannelCard } from './WebChannelCard'

type ChannelKey = 'web' | 'discord' | 'imessage'

interface ChannelPillDef {
  id: ChannelKey
  enabled: boolean
  ready: boolean
  registered: boolean
}

/**
 * Top-level shell for the Channels tab. Pulls the full config via
 * :func:`useConfig` (same hook the Config tab uses) and hands the
 * ``channels`` section to three per-channel cards. Saves go through
 * ``PATCH /api/admin/channels`` which does NOT hot-reload — callers
 * must restart the daemon to apply, which the shared banner makes
 * explicit.
 */
export function AdmChannels() {
  const { t } = useTranslation()
  const { config, loading, error, refresh } = useConfig()
  const [restartPending, setRestartPending] = useState(false)
  const [selected, setSelected] = useState<ChannelKey>('imessage')

  const onSaved = async () => {
    setRestartPending(true)
    await refresh()
  }

  if (loading && config === null) {
    return (
      <div style={{ padding: '28px 36px', color: 'var(--ink-3)' }}>
        {t('admin.channels.loading')}
      </div>
    )
  }
  if (config === null) {
    return (
      <div style={{ padding: '28px 36px', color: 'var(--accent)' }}>
        ⚠ {error ?? t('admin.channels.load_error_fallback')}
      </div>
    )
  }

  const pillDefs: ChannelPillDef[] = [
    {
      id: 'web',
      enabled: config.channels.web.enabled,
      ready: config.channels.web.ready,
      registered: config.channels.web.registered,
    },
    {
      id: 'discord',
      enabled: config.channels.discord.enabled,
      ready: config.channels.discord.ready,
      registered: config.channels.discord.registered,
    },
    {
      id: 'imessage',
      enabled: config.channels.imessage.enabled,
      ready: config.channels.imessage.ready,
      registered: config.channels.imessage.registered,
    },
  ]

  // Placeholder pills for channels we haven't shipped yet. Shown dim so
  // users see the roadmap but can tell they are not clickable.
  const stubs: { id: string; labelKey: string }[] = [
    { id: 'whatsapp', labelKey: 'admin.channels.stubs.whatsapp' },
    { id: 'wechat', labelKey: 'admin.channels.stubs.wechat' },
    { id: 'line', labelKey: 'admin.channels.stubs.line' },
  ]

  return (
    <div
      style={{
        padding: '28px 36px',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title" style={{ marginBottom: 0 }}>
          {t('admin.channels.section_title')}
        </h2>
        <div className="flex1" />
      </div>
      <p style={{ color: 'var(--ink-2)', fontSize: 13, maxWidth: 640, margin: 0 }}>
        {t('admin.channels.lede')}
      </p>

      {/* Pill selector · horizontal, scales to many channels without
          stacking cards. Active pill has the ink fill; disabled pills
          are outlined; stub pills have a dashed border and are not
          clickable. */}
      <div
        className="row g-2"
        style={{ flexWrap: 'wrap', alignItems: 'center' }}
      >
        {pillDefs.map((p) => (
          <ChannelPill
            key={p.id}
            def={p}
            selected={selected === p.id}
            onClick={() => setSelected(p.id)}
          />
        ))}
        <span
          style={{
            width: 1,
            height: 20,
            background: 'var(--rule)',
            margin: '0 4px',
          }}
        />
        {stubs.map((s) => (
          <StubPill key={s.id} label={t(s.labelKey)} />
        ))}
      </div>

      {restartPending && (
        <div
          className="card"
          style={{
            padding: 14,
            borderColor: 'var(--accent)',
            background: 'var(--accent-soft)',
          }}
        >
          <span className="label" style={{ color: 'var(--accent)' }}>
            {t('admin.channels.restart_banner_label')}
          </span>
          <div
            style={{
              marginTop: 6,
              color: 'var(--ink-2)',
              fontSize: 13,
              lineHeight: 1.5,
            }}
          >
            {t('admin.channels.restart_banner_body')}
          </div>
        </div>
      )}

      {/* Only render the selected channel's pane. Each pane is
          self-contained (loads / edits / saves its own slice). */}
      {selected === 'web' && <WebChannelCard channel={config.channels.web} />}
      {selected === 'discord' && (
        <DiscordChannelCard
          channel={config.channels.discord}
          onSaved={onSaved}
        />
      )}
      {selected === 'imessage' && (
        <IMessageChannelCard
          channel={config.channels.imessage}
          onSaved={onSaved}
        />
      )}
    </div>
  )
}

/**
 * Active / inactive channel pill in the Channels tab selector. Three
 * visual states:
 *
 * - **selected**: ink fill + paper text. The current editor target.
 * - **enabled unselected**: outlined + bold name + status dot.
 * - **disabled unselected**: outlined + dim + status dot off.
 */
function ChannelPill({
  def,
  selected,
  onClick,
}: {
  def: ChannelPillDef
  selected: boolean
  onClick: () => void
}) {
  const { t } = useTranslation()
  const dotColor = def.ready
    ? 'oklch(62% 0.15 140)'
    : def.registered
      ? 'oklch(70% 0.14 80)'
      : 'var(--paper-4)'
  const label = t(`admin.channels.${def.id}.title`)
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 12px',
        borderRadius: 999,
        border: `1px solid ${selected ? 'var(--ink)' : 'var(--rule-strong)'}`,
        background: selected ? 'var(--ink)' : 'var(--paper)',
        color: selected ? 'var(--paper)' : 'var(--ink)',
        fontSize: 13,
        fontWeight: selected ? 600 : 500,
        letterSpacing: '-0.01em',
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: dotColor,
          display: 'inline-block',
          opacity: def.enabled ? 1 : 0.4,
        }}
      />
      {label}
    </button>
  )
}

function StubPill({ label }: { label: string }) {
  return (
    <span
      aria-disabled="true"
      title="Not yet supported"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '6px 12px',
        borderRadius: 999,
        border: '1px dashed var(--rule-strong)',
        background: 'transparent',
        color: 'var(--ink-4)',
        fontSize: 12,
        fontStyle: 'italic',
        cursor: 'not-allowed',
      }}
    >
      {label}
    </span>
  )
}
