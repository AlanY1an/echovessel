import { useTranslation } from 'react-i18next'

import type { ConfigChannelWeb } from '../../../api/types'
import { StatusBadge } from '../_shared'

/** Read-only Web channel card · toggling web off would kill the admin UI.
 *  Just a status note with the bind endpoint; everything else is
 *  advanced tuning that is never touched from the UI. */
export function WebChannelCard({ channel }: { channel: ConfigChannelWeb }) {
  const { t } = useTranslation()
  return (
    <div className="card" style={{ padding: 18 }}>
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span className="label">{t('admin.channels.web.title')}</span>
        <StatusBadge ready={channel.ready} registered={channel.registered} />
        <div className="flex1" />
        <span className="chip dashed">
          {t('admin.channels.web.readonly_hint')}
        </span>
      </div>
      <div
        style={{
          marginTop: 10,
          fontSize: 13,
          color: 'var(--ink-2)',
          lineHeight: 1.6,
        }}
      >
        {t('admin.channels.web.description', {
          host: channel.host,
          port: channel.port,
        })}
      </div>
    </div>
  )
}
