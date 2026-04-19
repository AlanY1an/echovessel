import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { patchChannels } from '../../../api/client'
import type {
  ChannelsPatchPayload,
  ConfigChannelDiscord,
} from '../../../api/types'
import { arraysEqual } from '../helpers'
import {
  Advanced,
  ChipListInput,
  FieldLabel,
  SaveRow,
  StatusBadge,
  ToggleSwitch,
  TokenDot,
  inputBoxStyle,
} from '../_shared'

/** Discord channel card · enabled toggle + allowlist + token status. */
export function DiscordChannelCard({
  channel,
  onSaved,
}: {
  channel: ConfigChannelDiscord
  onSaved: () => Promise<void>
}) {
  const { t } = useTranslation()
  const [enabled, setEnabled] = useState(channel.enabled)
  const [tokenEnv, setTokenEnv] = useState(channel.token_env)
  const [allowed, setAllowed] = useState<string[]>(
    channel.allowed_user_ids.map(String),
  )
  const [debounce, setDebounce] = useState(channel.debounce_ms)
  const [saving, setSaving] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    setEnabled(channel.enabled)
    setTokenEnv(channel.token_env)
    setAllowed(channel.allowed_user_ids.map(String))
    setDebounce(channel.debounce_ms)
  }, [channel])

  const dirty =
    enabled !== channel.enabled ||
    tokenEnv.trim() !== channel.token_env ||
    !arraysEqual(allowed, channel.allowed_user_ids.map(String)) ||
    debounce !== channel.debounce_ms

  const save = async () => {
    setLocalError(null)
    setSaving(true)
    try {
      const parsed: number[] = []
      for (const raw of allowed) {
        const n = Number(raw.trim())
        if (!Number.isFinite(n) || Math.floor(n) !== n) {
          setLocalError(t('admin.channels.discord.invalid_id', { value: raw }))
          return
        }
        parsed.push(n)
      }
      const patch: ChannelsPatchPayload = {
        discord: {
          enabled,
          token_env: tokenEnv.trim(),
          allowed_user_ids: parsed,
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

  return (
    <div className="card" style={{ padding: 18 }}>
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span className="label">{t('admin.channels.discord.title')}</span>
        <StatusBadge ready={channel.ready} registered={channel.registered} />
        <div className="flex1" />
        <TokenDot loaded={channel.token_loaded} />
        <ToggleSwitch value={enabled} onChange={setEnabled} />
      </div>
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
        <FieldLabel>{t('admin.channels.discord.allowed_user_ids')}</FieldLabel>
        <ChipListInput
          values={allowed}
          onChange={setAllowed}
          placeholder={t('admin.channels.discord.allowed_placeholder')}
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
          <FieldLabel>{t('admin.channels.discord.token_env')}</FieldLabel>
          <input
            className="bare"
            style={inputBoxStyle}
            value={tokenEnv}
            onChange={(e) => setTokenEnv(e.target.value)}
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
