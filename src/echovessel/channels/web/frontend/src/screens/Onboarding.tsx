/**
 * Onboarding · two-column "material ↔ fields" layout.
 *
 * Left column carries the raw source material (pasted prose or an
 * uploaded text file). The right column carries the persona description
 * — the five prose core blocks, a display name, and the 14 biographic
 * facts. When the user clicks "Analyse", we send the material through
 * ``postPersonaExtract`` and pre-fill the right column from the
 * response; the user can then edit anything before hitting "Finish".
 *
 * The right column is also fully typeable from scratch — if the user
 * skips material entirely, pressing "Finish" just POSTs the hand-typed
 * values through ``completeOnboarding``.
 */

import { useEffect, useRef, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'

import {
  postImportUploadText,
  postPersonaAvatar,
  postPersonaExtract,
} from '../api/client'
import { ApiError, EMPTY_PERSONA_FACTS } from '../api/types'
import type {
  OnboardingPayload,
  PersonaExtractResponse,
  PersonaFacts,
} from '../api/types'
import { LanguageToggle } from '../components/LanguageToggle'
import { BrandMark } from '../components/primitives'

// ─── Props ──────────────────────────────────────────────────────────

interface OnboardingProps {
  completeOnboarding: (payload: OnboardingPayload) => Promise<void>
  error: string | null
}

// ─── Static metadata for the onboarding fields ──
//
// v0.5 onboarding asks 3 prose fields + a name (4 total): display name,
// the ``persona`` block (who they are), and the ``user`` block (what
// the persona knows about you). The ``style`` block is owner-curated
// later from Admin → Persona; ``self`` and ``relationship`` were
// dropped (now L4 thought[subject='persona'] and L5
// entities.description respectively).
//
// ``accent`` picks the `.field-row.*` colour strip on the right and
// (informally) pairs with the matching `.hl.*` highlight in the left
// column. ``kind: 'long'`` renders a multi-line textarea; ``'short'``
// renders a single-line `.bare` input.

type BlockKey = 'display_name' | 'persona_block' | 'user_block'

interface BlockMeta {
  key: BlockKey
  accent: '' | 'persona' | 'user'
  kind: 'short' | 'long'
  labelKey: string
  placeholderKey: string
}

const BLOCK_META: readonly BlockMeta[] = [
  {
    key: 'display_name',
    accent: '',
    kind: 'short',
    labelKey: 'onboarding.field_name_label',
    placeholderKey: 'onboarding.field_name_placeholder',
  },
  {
    key: 'persona_block',
    accent: 'persona',
    kind: 'long',
    labelKey: 'onboarding.review_block_persona',
    placeholderKey: 'onboarding.long_placeholder',
  },
  {
    key: 'user_block',
    accent: 'user',
    kind: 'long',
    labelKey: 'onboarding.review_block_user',
    placeholderKey: 'onboarding.long_placeholder',
  },
]

// 14 biographic facts rendered inline on the right, below the 5 blocks.
// Mirrors ``PersonaFactsEditor`` / hi/admin.jsx but collapsed into a
// compact 2-column grid so the onboarding screen stays single-pass.

type FactKind = 'text' | 'date' | 'enum'

interface FactMeta {
  key: keyof PersonaFacts
  labelKey: string
  placeholderKey?: string
  kind: FactKind
  enumKey?: string // namespace under facts.enum.*
  options?: readonly string[]
}

const FACT_FIELDS: readonly FactMeta[] = [
  { key: 'full_name', labelKey: 'facts.full_name', placeholderKey: 'facts.full_name_placeholder', kind: 'text' },
  {
    key: 'gender',
    labelKey: 'facts.gender',
    kind: 'enum',
    enumKey: 'gender',
    options: ['female', 'male', 'non_binary'],
  },
  { key: 'birth_date', labelKey: 'facts.birth_date', kind: 'date' },
  { key: 'ethnicity', labelKey: 'facts.ethnicity', placeholderKey: 'facts.ethnicity_placeholder', kind: 'text' },
  { key: 'nationality', labelKey: 'facts.nationality', placeholderKey: 'facts.nationality_placeholder', kind: 'text' },
  {
    key: 'native_language',
    labelKey: 'facts.native_language',
    placeholderKey: 'facts.native_language_placeholder',
    kind: 'text',
  },
  { key: 'locale_region', labelKey: 'facts.locale_region', placeholderKey: 'facts.locale_region_placeholder', kind: 'text' },
  {
    key: 'education_level',
    labelKey: 'facts.education_level',
    kind: 'enum',
    enumKey: 'education_level',
    options: ['high_school', 'bachelor', 'master', 'phd'],
  },
  { key: 'occupation', labelKey: 'facts.occupation', placeholderKey: 'facts.occupation_placeholder', kind: 'text' },
  {
    key: 'occupation_field',
    labelKey: 'facts.occupation_field',
    placeholderKey: 'facts.occupation_field_placeholder',
    kind: 'text',
  },
  { key: 'location', labelKey: 'facts.location', placeholderKey: 'facts.location_placeholder', kind: 'text' },
  // ``timezone`` is intentionally absent from onboarding — plan
  // decision 5: the browser auto-detects ``users.timezone`` on first
  // connect via ``Intl.DateTimeFormat``, and ``persona.timezone`` stays
  // null until the owner explicitly picks one from the Admin Persona
  // tab (where the IANA dropdown lives).
  {
    key: 'relationship_status',
    labelKey: 'facts.relationship_status',
    kind: 'enum',
    enumKey: 'relationship_status',
    options: ['single', 'married', 'widowed', 'divorced'],
  },
  {
    key: 'life_stage',
    labelKey: 'facts.life_stage',
    kind: 'enum',
    enumKey: 'life_stage',
    options: ['student', 'working', 'retired', 'new_parent', 'between_jobs'],
  },
  {
    key: 'health_status',
    labelKey: 'facts.health_status',
    kind: 'enum',
    enumKey: 'health_status',
    options: ['healthy', 'chronic_illness', 'recovering', 'serious'],
  },
]

// ─── Component ──────────────────────────────────────────────────────

export function Onboarding({ completeOnboarding, error }: OnboardingProps) {
  const { t, i18n } = useTranslation()

  // Left-column · material state.
  const [material, setMaterial] = useState('')
  const [analysing, setAnalysing] = useState(false)
  const [extract, setExtract] = useState<PersonaExtractResponse | null>(null)
  const [materialError, setMaterialError] = useState<string | null>(null)
  const [fromUpload, setFromUpload] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // Right-column · the three onboarding fields + 15 facts (v0.5 · ``self``
  // and ``relationship`` dropped; ``style`` is owner-curated later from
  // the Admin Persona tab).
  const [values, setValues] = useState<Record<BlockKey, string>>({
    display_name: '',
    persona_block: '',
    user_block: '',
  })
  const [facts, setFacts] = useState<PersonaFacts>(EMPTY_PERSONA_FACTS)
  const [submitting, setSubmitting] = useState(false)
  // Optional profile picture. We defer the upload until AFTER onboarding
  // commits so a failed commit doesn't leave a dangling avatar on disk.
  const [avatarFile, setAvatarFile] = useState<File | null>(null)

  const updateBlock = (k: BlockKey, v: string) =>
    setValues((prev) => ({ ...prev, [k]: v }))

  const updateFact = <K extends keyof PersonaFacts>(
    k: K,
    v: PersonaFacts[K],
  ) => setFacts((prev) => ({ ...prev, [k]: v }))

  // ─── File upload (text only) ───────────────────────────────────
  const handleFilePick = () => fileInputRef.current?.click()
  const handleFileChosen = async (file: File | null) => {
    if (file === null) return
    try {
      const text = await file.text()
      setMaterial(text)
      setMaterialError(null)
    } catch {
      setMaterialError(t('onboarding.file_read_failed'))
    }
  }

  // ─── Analyse material via LLM ──────────────────────────────────
  //
  // Two paths converge on the same extract endpoint:
  //   (a) text-only           → input_type: 'blank_write'
  //   (b) >= ~4kB / uploaded  → import_upload (via /import/upload_text)
  // The threshold is just a heuristic — the user can pick explicitly
  // through the "+ file" affordance, which always routes through (b).
  const runAnalyse = async () => {
    const trimmed = material.trim()
    if (trimmed.length === 0 || analysing) return
    setAnalysing(true)
    setMaterialError(null)
    try {
      let res: PersonaExtractResponse
      if (fromUpload) {
        const upload = await postImportUploadText({
          text: trimmed,
          source_label: 'onboarding_material',
        })
        res = await postPersonaExtract({
          input_type: 'import_upload',
          upload_id: upload.upload_id,
          persona_display_name:
            values.display_name.trim() || t('onboarding.default_display_name'),
          locale: i18n.language,
        })
      } else {
        res = await postPersonaExtract({
          input_type: 'blank_write',
          user_input: trimmed,
          persona_display_name:
            values.display_name.trim() || t('onboarding.default_display_name'),
          locale: i18n.language,
        })
      }
      applyExtract(res)
    } catch (err) {
      let msg = t('onboarding.import_failed')
      if (err instanceof ApiError) msg = err.detail
      else if (err instanceof Error) msg = err.message
      setMaterialError(msg)
    } finally {
      setAnalysing(false)
    }
  }

  // Seed the right column from an extract response. Only fills fields
  // the user hasn't already typed into — otherwise a late-returning
  // analyse would clobber in-progress edits.
  const applyExtract = (res: PersonaExtractResponse) => {
    setExtract(res)
    setValues((prev) => ({
      display_name: prev.display_name,
      persona_block: prev.persona_block || res.core_blocks.persona_block,
      user_block: prev.user_block || res.core_blocks.user_block,
    }))
    setFacts((prev) => mergeFacts(prev, res.facts))
  }

  // ─── Commit everything to the daemon ──────────────────────────
  const required: readonly BlockKey[] = [
    'display_name',
    'persona_block',
    'user_block',
  ]
  const missing = required.filter((k) => values[k].trim().length === 0)
  const canCommit = missing.length === 0 && !submitting

  const handleCommit = async () => {
    if (!canCommit) return
    setSubmitting(true)
    try {
      await completeOnboarding({
        display_name: values.display_name.trim(),
        persona_block: values.persona_block.trim(),
        user_block: values.user_block.trim(),
        facts,
      })
      // Optional avatar upload. Deferred until after onboarding commits
      // so a failed commit doesn't leave a dangling picture on disk.
      // Swallow errors — the user can re-upload from Admin later.
      if (avatarFile !== null) {
        try {
          await postPersonaAvatar(avatarFile)
        } catch {
          /* swallowed — user can retry from Admin */
        }
      }
    } catch {
      // surfaces via `error` prop
    } finally {
      setSubmitting(false)
    }
  }

  // ─── Render ────────────────────────────────────────────────────
  const filled = required.filter((k) => values[k].trim().length > 0).length
  const total = required.length
  const materialChars = material.trim().length

  return (
    <div className="onb">
      {/* LEFT · raw material */}
      <div className="onb-left">
        <div className="row g-2" style={{ alignItems: 'center' }}>
          <BrandMark />
          <div className="flex1" />
          <LanguageToggle />
        </div>

        <div className="row g-2" style={{ alignItems: 'center' }}>
          <span className="label">{t('onboarding.material_label')}</span>
          {materialChars > 0 && (
            <span className="chip">
              {t('onboarding.material_ready_chip', { count: materialChars })}
            </span>
          )}
          <div className="flex1" />
          <button
            type="button"
            className="chip dashed"
            onClick={() => {
              setFromUpload(false)
              setMaterial('')
            }}
          >
            {t('onboarding.material_paste_chip')}
          </button>
          <button
            type="button"
            className="chip dashed"
            onClick={handleFilePick}
          >
            {t('onboarding.material_file_chip')}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="text/plain,text/markdown,.txt,.md"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null
              setFromUpload(true)
              void handleFileChosen(f)
              e.target.value = ''
            }}
          />
        </div>

        <div className="material-doc">
          {materialChars === 0 ? (
            <>
              <h4>{t('onboarding.material_empty_heading')}</h4>
              <textarea
                className="bare"
                rows={18}
                placeholder={t('onboarding.material_empty_hint')}
                value={material}
                onChange={(e) => {
                  setFromUpload(false)
                  setMaterial(e.target.value)
                }}
                style={{
                  width: '100%',
                  fontFamily: 'var(--serif)',
                  fontSize: 14,
                  lineHeight: 1.75,
                  color: 'var(--ink-2)',
                }}
              />
            </>
          ) : (
            <>
              <h4>{t('onboarding.material_doc_heading')}</h4>
              <textarea
                className="bare"
                rows={22}
                value={material}
                onChange={(e) => setMaterial(e.target.value)}
                style={{
                  width: '100%',
                  fontFamily: 'var(--serif)',
                  fontSize: 14,
                  lineHeight: 1.75,
                  color: 'var(--ink-2)',
                }}
              />
            </>
          )}
        </div>

        {materialError !== null && (
          <span className="chip warn">⚠ {materialError}</span>
        )}

        <div className="row g-2" style={{ alignItems: 'center' }}>
          <div className="flex1" />
          <button
            type="button"
            className="btn ghost sm"
            disabled={materialChars === 0 || analysing}
            onClick={() => void runAnalyse()}
          >
            {analysing
              ? t('onboarding.material_analysing')
              : t('onboarding.material_analyse_cta')}
          </button>
        </div>
      </div>

      {/* RIGHT · the persona itself */}
      <div className="onb-right">
        <div className="row g-2" style={{ alignItems: 'baseline' }}>
          <h2 className="title">
            <Trans
              i18nKey="onboarding.title_them"
              components={{ em: <em /> }}
            />
          </h2>
          <div className="flex1" />
          <span className="label">
            {t('onboarding.filled_of_total', { filled, total })}
          </span>
        </div>

        <p style={{ color: 'var(--ink-2)', margin: 0, fontSize: 13 }}>
          {t('onboarding.lead_them')}
        </p>

        <OnboardingAvatarPicker
          file={avatarFile}
          onChange={setAvatarFile}
          disabled={submitting}
          fallbackLetter={initialFor(values.display_name)}
        />

        {BLOCK_META.map((meta) => (
          <BlockFieldRow
            key={meta.key}
            meta={meta}
            value={values[meta.key]}
            onChange={(v) => updateBlock(meta.key, v)}
            hasSource={extract !== null && meta.key !== 'display_name'}
            disabled={submitting}
          />
        ))}

        <div className="stack g-2">
          <span className="label">{t('onboarding.facts_heading')}</span>
          <div
            className="card"
            style={{
              padding: 18,
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: '12px 18px',
            }}
          >
            {FACT_FIELDS.map((f) => (
              <FactField
                key={f.key}
                meta={f}
                value={facts[f.key]}
                onChange={(v) => updateFact(f.key, v)}
                disabled={submitting}
              />
            ))}
          </div>
        </div>

        {error !== null && (
          <span className="chip warn">⚠ {error}</span>
        )}

        <div className="onb-progress">
          <span
            className="label"
            style={{ color: canCommit ? 'var(--ink)' : 'var(--accent)' }}
          >
            {t('onboarding.filled_of_total', { filled, total })}
          </span>
          <div className="bar">
            {required.map((k) => (
              <span
                key={k}
                className={values[k].trim() ? 'on' : ''}
              />
            ))}
          </div>
          <button
            type="button"
            className="btn accent"
            disabled={!canCommit}
            onClick={() => void handleCommit()}
          >
            {submitting
              ? t('onboarding.writing_persona')
              : t('onboarding.commit_cta')}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Sub-components ─────────────────────────────────────────────────

function initialFor(name: string): string {
  const trimmed = name.trim()
  if (trimmed.length === 0) return 'A'
  return trimmed[0]!.toUpperCase()
}

/**
 * Square avatar chooser used once during onboarding. Holds only the
 * File in local state via the `file` / `onChange` props — the upload
 * itself happens after `completeOnboarding` succeeds so a failed
 * onboarding commit doesn't leave an orphan image on the daemon.
 */
function OnboardingAvatarPicker({
  file,
  onChange,
  disabled,
  fallbackLetter,
}: {
  file: File | null
  onChange: (next: File | null) => void
  disabled: boolean
  fallbackLetter: string
}) {
  const { t } = useTranslation()
  const [preview, setPreview] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  // Build / tear down the blob preview URL whenever the File changes.
  // Revoking on unmount prevents the browser from leaking the blob.
  useEffect(() => {
    if (file === null) {
      setPreview(null)
      return
    }
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = e.target.files?.[0] ?? null
    e.target.value = ''
    onChange(next)
  }

  return (
    <div className="card row g-3" style={{ padding: 14, alignItems: 'center' }}>
      <div
        className="w-avatar lg"
        style={{ overflow: 'hidden' }}
        aria-label={fallbackLetter}
      >
        {preview !== null ? (
          <img src={preview} alt="" draggable={false} />
        ) : (
          <span>{fallbackLetter}</span>
        )}
      </div>
      <div className="stack g-1" style={{ flex: 1 }}>
        <span className="label">{t('onboarding.avatar_label')}</span>
        <div style={{ fontSize: 12, color: 'var(--ink-3)' }}>
          {preview !== null
            ? t('onboarding.avatar_selected_hint')
            : t('onboarding.avatar_empty_hint')}
        </div>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        style={{ display: 'none' }}
        onChange={onFile}
      />
      <button
        type="button"
        className="btn ghost sm"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
      >
        {preview !== null
          ? t('onboarding.avatar_replace_cta')
          : t('onboarding.avatar_upload_cta')}
      </button>
      {preview !== null && (
        <button
          type="button"
          className="btn ghost sm"
          onClick={() => onChange(null)}
          disabled={disabled}
        >
          ✕
        </button>
      )}
    </div>
  )
}

interface BlockFieldRowProps {
  meta: BlockMeta
  value: string
  onChange: (next: string) => void
  hasSource: boolean
  disabled: boolean
}

function BlockFieldRow({
  meta,
  value,
  onChange,
  hasSource,
  disabled,
}: BlockFieldRowProps) {
  const { t } = useTranslation()
  const empty = value.trim().length === 0
  // Every onboarding field is required since the v0.5 cut to 4 fields.
  const required = true
  const cls = `field-row ${meta.accent} ${empty && required ? 'empty' : ''}`.trim()
  const wordCount =
    value.trim().length > 0 ? value.trim().split(/\s+/).length : 0

  return (
    <div className={cls}>
      <div className="field-row-head">
        <span
          className="label"
          style={{ color: empty && required ? 'var(--accent)' : undefined }}
        >
          {t(meta.labelKey)}
          {required && ' *'}
        </span>
        {hasSource && !empty && (
          <span className="chip">{t('onboarding.field_from_source')}</span>
        )}
        <div className="flex1" />
        {!empty && meta.kind === 'long' && (
          <span className="chip">
            {t('onboarding.words_chip', { count: wordCount })}
          </span>
        )}
      </div>
      {meta.kind === 'long' ? (
        <textarea
          className="bare"
          rows={empty ? 2 : 4}
          placeholder={t(meta.placeholderKey)}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        />
      ) : (
        <input
          className="bare"
          type="text"
          placeholder={t(meta.placeholderKey)}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        />
      )}
    </div>
  )
}

interface FactFieldProps {
  meta: FactMeta
  value: string | null
  onChange: (next: string | null) => void
  disabled: boolean
}

function FactField({ meta, value, onChange, disabled }: FactFieldProps) {
  const { t } = useTranslation()
  const inputStyle: React.CSSProperties = {
    border: '1px solid var(--rule)',
    padding: '6px 8px',
    borderRadius: 4,
    background: 'var(--paper)',
    fontSize: 13,
    width: '100%',
  }

  return (
    <div className="stack g-1">
      <span
        style={{
          fontFamily: 'var(--mono)',
          fontSize: 10,
          color: 'var(--ink-3)',
          letterSpacing: '0.06em',
        }}
      >
        {t(meta.labelKey)}
      </span>
      {meta.kind === 'enum' ? (
        <select
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value || null)}
          disabled={disabled}
          style={inputStyle}
        >
          <option value="">{t('facts.unknown_option')}</option>
          {meta.options?.map((o) => (
            <option key={o} value={o}>
              {t(`facts.enum.${meta.enumKey}.${o}`)}
            </option>
          ))}
        </select>
      ) : meta.kind === 'date' ? (
        <input
          type="date"
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value || null)}
          disabled={disabled}
          style={inputStyle}
        />
      ) : (
        <input
          type="text"
          value={value ?? ''}
          placeholder={meta.placeholderKey ? t(meta.placeholderKey) : '—'}
          onChange={(e) => onChange(e.target.value || null)}
          disabled={disabled}
          style={inputStyle}
        />
      )}
    </div>
  )
}

// ─── Helpers ────────────────────────────────────────────────────────

/**
 * Merge a freshly-extracted PersonaFacts into the user's current draft.
 * The rule is "keep what the user already typed, accept what they left
 * blank" — this lets a late analyse fill gaps without overwriting a
 * birthday the user already entered by hand.
 */
function mergeFacts(current: PersonaFacts, incoming: PersonaFacts): PersonaFacts {
  const out = { ...current }
  for (const key of Object.keys(incoming) as (keyof PersonaFacts)[]) {
    if ((out[key] ?? '') === '' && incoming[key] !== null) {
      out[key] = incoming[key]
    }
  }
  return out
}
