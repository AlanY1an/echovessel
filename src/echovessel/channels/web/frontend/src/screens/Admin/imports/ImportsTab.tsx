import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

/**
 * Imports tab — historical ingestion into memory.
 *
 * Previously was the "Sources" tab, which mixed channel status rows
 * with import pipeline rows. Channel status + config now live in the
 * dedicated Channels tab (see ChannelsTab.tsx); this tab is imports
 * only.
 */
export function AdmImports() {
  const { t } = useTranslation()
  const navigate = useNavigate()

  return (
    <div
      style={{
        padding: '28px 36px',
        display: 'flex',
        flexDirection: 'column',
        gap: 24,
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.imports_tab.section_title')}</h2>
        <div className="flex1" />
        <button
          className="btn sm"
          onClick={() => navigate('/admin/import')}
        >
          + {t('admin.imports_tab.new_import')}
        </button>
      </div>
      <p style={{ color: 'var(--ink-2)', fontSize: 13, margin: 0, maxWidth: 640 }}>
        {t('admin.imports_tab.lede')}
      </p>
      <div
        className="card"
        style={{
          padding: 16,
          fontSize: 12,
          color: 'var(--ink-3)',
          lineHeight: 1.6,
        }}
      >
        {t('admin.imports_tab.history_placeholder')}
      </div>
    </div>
  )
}
