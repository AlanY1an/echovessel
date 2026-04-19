import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { ConfigPatchPayload } from '../../../api/types'
import {
  configInputStyle,
  configKeyStyle,
  type ConfigCardProps,
} from './ConfigTab'

export function ConfigMemoryCard({ config, save, saving }: ConfigCardProps) {
  const { t } = useTranslation()
  const [retrieveK, setRetrieveK] = useState(config.memory.retrieve_k)
  const [bonus, setBonus] = useState(config.memory.relational_bonus_weight)
  const [recent, setRecent] = useState(config.memory.recent_window_size)
  useEffect(() => {
    setRetrieveK(config.memory.retrieve_k)
    setBonus(config.memory.relational_bonus_weight)
    setRecent(config.memory.recent_window_size)
  }, [config.memory])
  const dirty =
    retrieveK !== config.memory.retrieve_k ||
    bonus !== config.memory.relational_bonus_weight ||
    recent !== config.memory.recent_window_size
  const handle = () => {
    const patch: ConfigPatchPayload = { memory: {} }
    if (retrieveK !== config.memory.retrieve_k)
      patch.memory!.retrieve_k = retrieveK
    if (bonus !== config.memory.relational_bonus_weight)
      patch.memory!.relational_bonus_weight = bonus
    if (recent !== config.memory.recent_window_size)
      patch.memory!.recent_window_size = recent
    void save(patch).catch(() => {})
  }
  return (
    <div className="card" style={{ padding: 18 }}>
      <span className="label">
        {t('admin.config_tab.memory_section_title')}
      </span>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '180px 1fr',
          gap: '10px 14px',
          marginTop: 12,
          fontFamily: 'var(--mono)',
          fontSize: 12,
        }}
      >
        <div style={configKeyStyle}>retrieve_k</div>
        <div className="row g-2" style={{ alignItems: 'center' }}>
          <input
            type="range"
            min={1}
            max={30}
            step={1}
            value={retrieveK}
            onChange={(e) => setRetrieveK(parseInt(e.target.value, 10))}
            disabled={saving}
            style={{ flex: 1 }}
          />
          <span style={{ width: 32, textAlign: 'right', color: 'var(--ink-2)' }}>
            {retrieveK}
          </span>
        </div>
        <div style={configKeyStyle}>relational_bonus_weight</div>
        <div className="row g-2" style={{ alignItems: 'center' }}>
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={bonus}
            onChange={(e) => setBonus(parseFloat(e.target.value))}
            disabled={saving}
            style={{ flex: 1 }}
          />
          <span style={{ width: 40, textAlign: 'right', color: 'var(--ink-2)' }}>
            {bonus.toFixed(2)}
          </span>
        </div>
        <div style={configKeyStyle}>recent_window_size</div>
        <input
          type="number"
          min={1}
          max={200}
          value={recent}
          onChange={(e) => setRecent(parseInt(e.target.value, 10) || 0)}
          disabled={saving}
          style={configInputStyle}
        />
      </div>
      <div
        className="row g-2"
        style={{ marginTop: 12, justifyContent: 'flex-end' }}
      >
        <button className="btn sm" disabled={!dirty || saving} onClick={handle}>
          {saving ? '⋯' : t('admin.common.save')}
        </button>
      </div>
    </div>
  )
}
