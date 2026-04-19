import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { ConfigPatchPayload } from '../../../api/types'
import {
  configInputStyle,
  configKeyStyle,
  type ConfigCardProps,
} from './ConfigTab'

export function ConfigConsolidateCard({
  config,
  save,
  saving,
}: ConfigCardProps) {
  const { t } = useTranslation()
  const [trivMsg, setTrivMsg] = useState(
    config.consolidate.trivial_message_count,
  )
  const [trivTok, setTrivTok] = useState(
    config.consolidate.trivial_token_count,
  )
  const [reflGate, setReflGate] = useState(
    config.consolidate.reflection_hard_gate_24h,
  )
  useEffect(() => {
    setTrivMsg(config.consolidate.trivial_message_count)
    setTrivTok(config.consolidate.trivial_token_count)
    setReflGate(config.consolidate.reflection_hard_gate_24h)
  }, [config.consolidate])
  const dirty =
    trivMsg !== config.consolidate.trivial_message_count ||
    trivTok !== config.consolidate.trivial_token_count ||
    reflGate !== config.consolidate.reflection_hard_gate_24h
  const handle = () => {
    const patch: ConfigPatchPayload = { consolidate: {} }
    if (trivMsg !== config.consolidate.trivial_message_count)
      patch.consolidate!.trivial_message_count = trivMsg
    if (trivTok !== config.consolidate.trivial_token_count)
      patch.consolidate!.trivial_token_count = trivTok
    if (reflGate !== config.consolidate.reflection_hard_gate_24h)
      patch.consolidate!.reflection_hard_gate_24h = reflGate
    void save(patch).catch(() => {})
  }
  return (
    <div className="card" style={{ padding: 18 }}>
      <span className="label">
        {t('admin.config_tab.consolidate_section_title')}
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
        <div style={configKeyStyle}>trivial_message_count</div>
        <input
          type="number"
          min={0}
          max={50}
          value={trivMsg}
          onChange={(e) => setTrivMsg(parseInt(e.target.value, 10) || 0)}
          disabled={saving}
          style={configInputStyle}
        />
        <div style={configKeyStyle}>trivial_token_count</div>
        <input
          type="number"
          min={0}
          max={5000}
          step={10}
          value={trivTok}
          onChange={(e) => setTrivTok(parseInt(e.target.value, 10) || 0)}
          disabled={saving}
          style={configInputStyle}
        />
        <div style={configKeyStyle}>reflection_hard_gate_24h</div>
        <input
          type="number"
          min={0}
          max={100}
          value={reflGate}
          onChange={(e) => setReflGate(parseInt(e.target.value, 10) || 0)}
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
