import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { patchChannels } from '../../../api/client'
import type {
  ChannelsPatchPayload,
  ConfigChannelIMessage,
} from '../../../api/types'
import { arraysEqual } from '../helpers'
import {
  Advanced,
  ChipListInput,
  FieldLabel,
  SaveRow,
  StatusBadge,
  ToggleSwitch,
  inputBoxStyle,
} from '../_shared'

/** iMessage channel card · enabled + persona_apple_id + allowed_handles + ... */
export function IMessageChannelCard({
  channel,
  onSaved,
}: {
  channel: ConfigChannelIMessage
  onSaved: () => Promise<void>
}) {
  const { t } = useTranslation()
  const [enabled, setEnabled] = useState(channel.enabled)
  const [personaAppleId, setPersonaAppleId] = useState(channel.persona_apple_id)
  const [cliPath, setCliPath] = useState(channel.cli_path)
  const [dbPath, setDbPath] = useState(channel.db_path)
  const [allowed, setAllowed] = useState<string[]>(channel.allowed_handles)
  const [defaultService, setDefaultService] = useState(channel.default_service)
  const [region, setRegion] = useState(channel.region)
  const [debounce, setDebounce] = useState(channel.debounce_ms)
  const [saving, setSaving] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    setEnabled(channel.enabled)
    setPersonaAppleId(channel.persona_apple_id)
    setCliPath(channel.cli_path)
    setDbPath(channel.db_path)
    setAllowed(channel.allowed_handles)
    setDefaultService(channel.default_service)
    setRegion(channel.region)
    setDebounce(channel.debounce_ms)
  }, [channel])

  const dirty =
    enabled !== channel.enabled ||
    personaAppleId.trim() !== channel.persona_apple_id ||
    cliPath.trim() !== channel.cli_path ||
    dbPath.trim() !== channel.db_path ||
    !arraysEqual(allowed, channel.allowed_handles) ||
    defaultService !== channel.default_service ||
    region.trim() !== channel.region ||
    debounce !== channel.debounce_ms

  const save = async () => {
    setLocalError(null)
    setSaving(true)
    try {
      const patch: ChannelsPatchPayload = {
        imessage: {
          enabled,
          persona_apple_id: personaAppleId.trim(),
          cli_path: cliPath.trim() || 'imsg',
          db_path: dbPath.trim(),
          allowed_handles: allowed,
          default_service: defaultService,
          region: region.trim() || 'US',
          debounce_ms: debounce,
        },
      }
      await patchChannels(patch)
      await onSaved()
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const mode = personaAppleId.trim()
    ? t('admin.channels.imessage.mode_dual')
    : t('admin.channels.imessage.mode_single')

  return (
    <div className="card" style={{ padding: 18 }}>
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span className="label">{t('admin.channels.imessage.title')}</span>
        <StatusBadge ready={channel.ready} registered={channel.registered} />
        <span className="chip">{mode}</span>
        <div className="flex1" />
        <ToggleSwitch value={enabled} onChange={setEnabled} />
      </div>

      {/* Essentials only — the fields users actually touch. Advanced
          tuning (region, cli path, debounce, …) is hidden below. */}
      <div
        style={{
          marginTop: 14,
          display: 'grid',
          gridTemplateColumns: '140px 1fr',
          rowGap: 12,
          columnGap: 14,
          fontSize: 13,
        }}
      >
        <FieldLabel>{t('admin.channels.imessage.persona_apple_id')}</FieldLabel>
        <div>
          <input
            className="bare"
            style={inputBoxStyle}
            value={personaAppleId}
            placeholder={t(
              'admin.channels.imessage.persona_apple_id_placeholder',
            )}
            onChange={(e) => setPersonaAppleId(e.target.value)}
          />
          <div style={{ fontSize: 11, color: 'var(--ink-3)', marginTop: 4 }}>
            {t('admin.channels.imessage.persona_apple_id_hint')}
          </div>
        </div>

        <FieldLabel>{t('admin.channels.imessage.allowed_handles')}</FieldLabel>
        <ChipListInput
          values={allowed}
          onChange={setAllowed}
          placeholder={t('admin.channels.imessage.allowed_placeholder')}
        />
      </div>

      <Advanced labelKey="admin.channels.advanced">
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '140px 1fr',
            rowGap: 10,
            columnGap: 14,
            fontSize: 13,
            marginTop: 10,
          }}
        >
          <FieldLabel>{t('admin.channels.imessage.default_service')}</FieldLabel>
          <select
            className="bare"
            style={{ ...inputBoxStyle, width: 180 }}
            value={defaultService}
            onChange={(e) =>
              setDefaultService(e.target.value as typeof defaultService)
            }
          >
            <option value="auto">auto</option>
            <option value="imessage">imessage</option>
            <option value="sms">sms</option>
          </select>

          <FieldLabel>{t('admin.channels.imessage.region')}</FieldLabel>
          <input
            className="bare"
            style={{ ...inputBoxStyle, width: 120 }}
            value={region}
            onChange={(e) => setRegion(e.target.value)}
          />

          <FieldLabel>{t('admin.channels.imessage.cli_path')}</FieldLabel>
          <input
            className="bare"
            style={inputBoxStyle}
            value={cliPath}
            placeholder="imsg"
            onChange={(e) => setCliPath(e.target.value)}
          />

          <FieldLabel>{t('admin.channels.imessage.db_path')}</FieldLabel>
          <input
            className="bare"
            style={inputBoxStyle}
            value={dbPath}
            placeholder={t('admin.channels.imessage.db_path_placeholder')}
            onChange={(e) => setDbPath(e.target.value)}
          />

          <FieldLabel>{t('admin.channels.debounce_ms')}</FieldLabel>
          <input
            type="number"
            className="bare"
            style={{ ...inputBoxStyle, width: 120 }}
            value={debounce}
            min={0}
            max={60000}
            onChange={(e) => setDebounce(Number(e.target.value))}
          />
        </div>
      </Advanced>

      <SaveRow
        dirty={dirty}
        saving={saving}
        error={localError}
        onSave={save}
      />
    </div>
  )
}
