import type { ReactNode } from 'react'
import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { ConfigGetResponse } from '../../../api/types'
import { formatBytes, formatUptime } from '../helpers'
import { configKeyStyle } from './ConfigTab'

export function ConfigSystemCard({ config }: { config: ConfigGetResponse }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    if (!config.system.config_path) return
    try {
      await navigator.clipboard.writeText(config.system.config_path)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard may refuse */
    }
  }
  const rows: [string, ReactNode][] = [
    ['version', config.system.version],
    ['uptime', formatUptime(config.system.uptime_seconds)],
    ['data_dir', <code key="dd">{config.system.data_dir}</code>],
    [
      'db_path',
      <>
        <code>{config.system.db_path}</code> ·{' '}
        {formatBytes(config.system.db_size_bytes)}
      </>,
    ],
    [
      'config.toml',
      <code key="cp">
        {config.system.config_path ?? t('admin.config_tab.system_no_file')}
      </code>,
    ],
  ]
  return (
    <div className="card" style={{ padding: 18 }}>
      <span className="label">
        {t('admin.config_tab.system_section_title')}
      </span>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '130px 1fr',
          gap: '8px 14px',
          marginTop: 12,
          fontFamily: 'var(--mono)',
          fontSize: 12,
          color: 'var(--ink-2)',
        }}
      >
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <div style={configKeyStyle}>{k}</div>
            <div style={{ wordBreak: 'break-all' }}>{v}</div>
          </Fragment>
        ))}
      </div>
      <div
        className="row g-2"
        style={{ marginTop: 12, justifyContent: 'flex-end' }}
      >
        {copied && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--ink-3)',
              fontFamily: 'var(--mono)',
            }}
          >
            {t('admin.config_tab.system_copied')}
          </span>
        )}
        <button
          className="btn ghost sm"
          onClick={() => void handleCopy()}
          disabled={!config.system.config_path}
        >
          {t('admin.config_tab.system_copy_cta')}
        </button>
      </div>
    </div>
  )
}
