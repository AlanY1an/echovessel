import type { CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'

import type {
  ConfigGetResponse,
  ConfigPatchPayload,
} from '../../../api/types'
import { useConfig } from '../../../hooks/useConfig'
import { ConfigConsolidateCard } from './ConfigConsolidateCard'
import { ConfigLlmCard } from './ConfigLlmCard'
import { ConfigMemoryCard } from './ConfigMemoryCard'
import { ConfigSystemCard } from './ConfigSystemCard'
import { CostCard } from './CostCard'
import { DangerZoneCard } from './DangerZoneCard'

/**
 * Prop contract shared by every Config-tab card that writes config.
 * DangerZone and CostCard don't use it — they have their own unique
 * surface (reset POST, cost summary fetch).
 */
export interface ConfigCardProps {
  config: ConfigGetResponse
  save: (patch: ConfigPatchPayload) => Promise<void>
  saving: boolean
}

/** Mono field-label styling used by every Config card's 2-column grid. */
export const configKeyStyle: CSSProperties = {
  color: 'var(--ink-3)',
  alignSelf: 'center',
}

/** Mono input styling used by every Config card. */
export const configInputStyle: CSSProperties = {
  padding: '6px 8px',
  background: 'var(--paper)',
  border: '1px solid var(--rule)',
  borderRadius: 4,
  color: 'var(--ink)',
  fontFamily: 'var(--mono)',
  fontSize: 12,
}

export function AdmConfig() {
  const { t } = useTranslation()
  const { config, loading, saving, error, save } = useConfig()

  if (loading && config === null) {
    return (
      <div style={{ padding: '28px 36px', color: 'var(--ink-3)' }}>
        {t('admin.config_tab.loading')}
      </div>
    )
  }
  if (config === null) {
    return (
      <div style={{ padding: '28px 36px', color: 'var(--accent)' }}>
        ⚠ {error ?? t('admin.config_tab.load_error_fallback')}
      </div>
    )
  }

  return (
    <div
      style={{
        padding: '28px 36px',
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title" style={{ marginBottom: 0 }}>
          {t('admin.config_tab.section_title')}
        </h2>
        {saving && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--ink-3)',
              fontFamily: 'var(--mono)',
            }}
          >
            {t('admin.common.saving')}
          </span>
        )}
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
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
          gap: 14,
        }}
      >
        <ConfigLlmCard config={config} save={save} saving={saving} />
        <ConfigMemoryCard config={config} save={save} saving={saving} />
        <ConfigConsolidateCard config={config} save={save} saving={saving} />
        <ConfigSystemCard config={config} />
      </div>
      <CostCard />
      <DangerZoneCard />
    </div>
  )
}
