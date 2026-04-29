import { useEffect, useMemo, useState } from 'react'

import { getModelCatalog } from '../../../api/client'
import type { ModelCatalogEntry } from '../../../api/types'
import { configInputStyle } from './ConfigTab'

interface Props {
  provider: string
  baseUrl: string
  value: string
  onChange: (next: string) => void
}

const CUSTOM_SENTINEL = '__custom__'

const PRESETTABLE_PROVIDERS = new Set(['anthropic', 'openai_compat'])

function looksOfficial(provider: string, baseUrl: string): boolean {
  const trimmed = baseUrl.trim()
  if (!PRESETTABLE_PROVIDERS.has(provider)) return false
  if (trimmed === '') return true
  if (provider === 'anthropic' && trimmed.includes('api.anthropic.com'))
    return true
  if (provider === 'openai_compat' && trimmed.includes('api.openai.com'))
    return true
  return false
}

const hintStyle = {
  fontFamily: 'var(--mono)',
  fontSize: 11,
  color: 'var(--ink-3)',
  margin: '4px 0 0',
}

const errorStyle = {
  fontFamily: 'var(--mono)',
  fontSize: 11,
  color: 'var(--accent)',
  margin: '4px 0 0',
}

const wrapperStyle = {
  display: 'flex',
  flexDirection: 'column' as const,
  gap: 6,
}

export function ModelPicker({ provider, baseUrl, value, onChange }: Props) {
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [customMode, setCustomMode] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    getModelCatalog(provider)
      .then((rows) => {
        if (!cancelled) setCatalog(rows)
      })
      .catch((err) => {
        if (!cancelled) setLoadError(String(err))
      })
    return () => {
      cancelled = true
    }
  }, [provider])

  const showPresets = looksOfficial(provider, baseUrl)
  const presetModels = useMemo(() => catalog.map((e) => e.model), [catalog])
  const valueIsPreset = presetModels.includes(value)

  useEffect(() => {
    if (presetModels.length > 0 && value !== '' && !presetModels.includes(value)) {
      setCustomMode(true)
    }
  }, [presetModels, value])

  if (!showPresets) {
    return (
      <div style={wrapperStyle}>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="model id (e.g. openrouter/anthropic/claude-3.5-sonnet)"
          aria-label="model"
          style={configInputStyle}
        />
        <p style={hintStyle}>
          Custom base_url detected — pricing falls back to the role-based estimate.
        </p>
      </div>
    )
  }

  const showCustomInput =
    customMode || (value !== '' && !valueIsPreset && presetModels.length > 0)
  const selectValue = showCustomInput ? CUSTOM_SENTINEL : valueIsPreset ? value : ''

  return (
    <div style={wrapperStyle}>
      <select
        value={selectValue}
        onChange={(e) => {
          const next = e.target.value
          if (next === CUSTOM_SENTINEL) {
            setCustomMode(true)
            if (valueIsPreset) {
              onChange('')
            }
          } else {
            setCustomMode(false)
            onChange(next)
          }
        }}
        aria-label="model"
        style={configInputStyle}
      >
        <option value="" disabled>
          Select a model…
        </option>
        {catalog.map((entry) => {
          const hasPrice =
            entry.input_per_1k_usd !== null && entry.output_per_1k_usd !== null
          const label = hasPrice
            ? `${entry.display_name} (in $${entry.input_per_1k_usd!.toFixed(5)} / out $${entry.output_per_1k_usd!.toFixed(5)} per 1K)`
            : `${entry.display_name} (price unknown)`
          return (
            <option key={entry.model} value={entry.model}>
              {label}
            </option>
          )
        })}
        <option value={CUSTOM_SENTINEL}>Custom…</option>
      </select>
      {showCustomInput && (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="custom model id"
          aria-label="custom model"
          style={configInputStyle}
        />
      )}
      {loadError && <p style={errorStyle}>could not load catalog: {loadError}</p>}
    </div>
  )
}
