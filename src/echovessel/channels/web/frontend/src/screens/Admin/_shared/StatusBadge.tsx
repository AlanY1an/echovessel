import { useTranslation } from 'react-i18next'

/** Green-yellow-grey dot + localised label describing a channel's
 *  current runtime state. Shared by the Channels tab's card headers. */
export function StatusBadge({
  ready,
  registered,
}: {
  ready: boolean
  registered: boolean
}) {
  const { t } = useTranslation()
  const label = ready
    ? t('admin.channels.status.ready')
    : registered
      ? t('admin.channels.status.starting')
      : t('admin.channels.status.disabled')
  const color = ready
    ? 'oklch(62% 0.15 140)'
    : registered
      ? 'oklch(70% 0.14 80)'
      : 'var(--ink-4)'
  return (
    <span className="chip" style={{ gap: 6 }}>
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: color,
          display: 'inline-block',
        }}
      />
      {label}
    </span>
  )
}
