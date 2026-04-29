import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../../api/types'
import type { ConfigPatchPayload } from '../../../api/types'
import { ModelPicker } from './ModelPicker'
import {
  configInputStyle,
  configKeyStyle,
  type ConfigCardProps,
} from './ConfigTab'

const fieldErrorStyle = {
  color: 'var(--accent)',
  fontFamily: 'var(--mono)',
  fontSize: 11,
  margin: '4px 0 0',
}

export function ConfigLlmCard({ config, save, saving }: ConfigCardProps) {
  const { t } = useTranslation()
  const [provider, setProvider] = useState(config.llm.provider)
  const [model, setModel] = useState(config.llm.model ?? '')
  const [baseUrl, setBaseUrl] = useState(config.llm.base_url)
  const [apiKeyEnv, setApiKeyEnv] = useState(config.llm.api_key_env)
  const [temperature, setTemperature] = useState(config.llm.temperature)
  const [maxTokens, setMaxTokens] = useState(config.llm.max_tokens)
  const [timeout, setTimeoutS] = useState(config.llm.timeout_seconds)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  useEffect(() => {
    setProvider(config.llm.provider)
    setModel(config.llm.model ?? '')
    setBaseUrl(config.llm.base_url)
    setApiKeyEnv(config.llm.api_key_env)
    setTemperature(config.llm.temperature)
    setMaxTokens(config.llm.max_tokens)
    setTimeoutS(config.llm.timeout_seconds)
  }, [config.llm])
  const dirty =
    provider !== config.llm.provider ||
    model !== (config.llm.model ?? '') ||
    baseUrl !== config.llm.base_url ||
    apiKeyEnv !== config.llm.api_key_env ||
    temperature !== config.llm.temperature ||
    maxTokens !== config.llm.max_tokens ||
    timeout !== config.llm.timeout_seconds

  const handle = () => {
    const patch: ConfigPatchPayload = { llm: {} }
    if (provider !== config.llm.provider) patch.llm!.provider = provider
    if (model !== (config.llm.model ?? '')) patch.llm!.model = model
    if (baseUrl !== config.llm.base_url) patch.llm!.base_url = baseUrl
    if (apiKeyEnv !== config.llm.api_key_env) patch.llm!.api_key_env = apiKeyEnv
    if (temperature !== config.llm.temperature)
      patch.llm!.temperature = temperature
    if (maxTokens !== config.llm.max_tokens) patch.llm!.max_tokens = maxTokens
    if (timeout !== config.llm.timeout_seconds)
      patch.llm!.timeout_seconds = timeout
    setFieldErrors({})
    void save(patch).catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 422 && e.fieldErrors) {
        const errs: Record<string, string> = {}
        for (const item of e.fieldErrors) {
          errs[item.field] = item.msg
        }
        setFieldErrors(errs)
      }
    })
  }

  return (
    <div className="card" style={{ padding: 18 }}>
      <div className="row g-2" style={{ alignItems: 'center' }}>
        <span className="label">{t('admin.config_tab.llm_section_title')}</span>
        <div className="flex1" />
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: config.llm.api_key_present
              ? 'oklch(62% 0.15 140)'
              : 'var(--accent)',
          }}
        />
        <span
          style={{
            fontFamily: 'var(--mono)',
            fontSize: 10,
            color: 'var(--ink-3)',
          }}
        >
          ${config.llm.api_key_env}
        </span>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '130px 1fr',
          gap: '10px 14px',
          marginTop: 12,
          fontFamily: 'var(--mono)',
          fontSize: 12,
        }}
      >
        <div style={configKeyStyle}>provider</div>
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          disabled={saving}
          style={configInputStyle}
        >
          <option value="openai_compat">openai_compat</option>
          <option value="anthropic">anthropic</option>
          <option value="stub">stub</option>
        </select>
        <div style={configKeyStyle}>model</div>
        <ModelPicker
          provider={provider}
          baseUrl={baseUrl}
          value={model}
          onChange={(next) => setModel(next)}
        />
        <div style={configKeyStyle}>{t('admin.config_tab.llm_label_base_url')}</div>
        <input
          type="text"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          disabled={saving}
          placeholder={t('admin.config_tab.llm_placeholder_base_url')}
          style={{ ...configInputStyle, width: '100%' }}
        />
        <div style={configKeyStyle}>api_key_env</div>
        <div>
          <input
            value={apiKeyEnv}
            onChange={(e) => setApiKeyEnv(e.target.value)}
            disabled={saving}
            style={{ ...configInputStyle, width: '100%' }}
          />
          {fieldErrors['llm.api_key_env'] && (
            <p style={fieldErrorStyle}>{fieldErrors['llm.api_key_env']}</p>
          )}
        </div>
        <div style={configKeyStyle}>temperature</div>
        <div className="row g-2" style={{ alignItems: 'center' }}>
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={temperature}
            onChange={(e) => setTemperature(parseFloat(e.target.value))}
            disabled={saving}
            style={{ flex: 1 }}
          />
          <span style={{ width: 44, textAlign: 'right', color: 'var(--ink-2)' }}>
            {temperature.toFixed(2)}
          </span>
        </div>
        <div style={configKeyStyle}>max_tokens</div>
        <input
          type="number"
          min={64}
          max={32000}
          step={32}
          value={maxTokens}
          onChange={(e) => setMaxTokens(parseInt(e.target.value, 10) || 0)}
          disabled={saving}
          style={configInputStyle}
        />
        <div style={configKeyStyle}>timeout_s</div>
        <input
          type="number"
          min={1}
          max={600}
          value={timeout}
          onChange={(e) => setTimeoutS(parseInt(e.target.value, 10) || 0)}
          disabled={saving}
          style={configInputStyle}
        />
      </div>
      <div
        className="row g-2"
        style={{ marginTop: 12, justifyContent: 'flex-end' }}
      >
        <button
          className="btn sm"
          disabled={!dirty || saving}
          onClick={handle}
        >
          {saving ? '⋯' : t('admin.common.save')}
        </button>
      </div>
    </div>
  )
}
