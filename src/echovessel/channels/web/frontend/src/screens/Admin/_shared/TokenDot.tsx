import { useTranslation } from 'react-i18next'

/** Presence indicator for an environment-variable-backed secret
 *  (e.g. Discord bot token). Shows a green filled circle when the
 *  env var is set, or a red open circle when missing. We deliberately
 *  never show the secret itself. */
export function TokenDot({ loaded }: { loaded: boolean }) {
  const { t } = useTranslation()
  return (
    <span
      className="chip"
      style={{
        color: loaded ? 'oklch(58% 0.14 140)' : 'var(--accent)',
      }}
    >
      {loaded ? '●' : '○'}{' '}
      {loaded
        ? t('admin.channels.token.loaded')
        : t('admin.channels.token.missing')}
    </span>
  )
}
