import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { TopBar } from '../components/TopBar'
import { PersonaFactsEditor } from '../components/PersonaFactsEditor'
import {
  postImportUploadText,
  postPersonaExtract,
  postPersonaUpdate,
} from '../api/client'
import { ApiError, EMPTY_PERSONA_FACTS } from '../api/types'
import type {
  ExtractedEvent,
  OnboardingPayload,
  PersonaExtractResponse,
  PersonaFacts,
} from '../api/types'

type Step =
  | 'welcome'
  | 'blank-write'
  | 'blank-analysing'
  | 'import-upload'
  | 'import-waiting'
  | 'review'

interface OnboardingProps {
  completeOnboarding: (payload: OnboardingPayload) => Promise<void>
  error: string | null
}

const MIN_MATERIAL_CHARS = 80

export function Onboarding({ completeOnboarding, error }: OnboardingProps) {
  const { t, i18n } = useTranslation()
  const [step, setStep] = useState<Step>('welcome')

  // Blank-write state.
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Import-upload state.
  const [material, setMaterial] = useState('')
  const [importError, setImportError] = useState<string | null>(null)
  const [extract, setExtract] =
    useState<PersonaExtractResponse | null>(null)
  const [extractSource, setExtractSource] =
    useState<'blank_write' | 'import_upload'>('blank_write')

  // Review state — the 5 blocks + 15 facts the user edits before
  // POSTing /onboarding. Populated from the extract response.
  const [draftPersona, setDraftPersona] = useState('')
  const [draftSelf, setDraftSelf] = useState('')
  const [draftUser, setDraftUser] = useState('')
  const [draftMood, setDraftMood] = useState('')
  const [draftRelationship, setDraftRelationship] = useState('')
  const [draftFacts, setDraftFacts] = useState<PersonaFacts>(
    EMPTY_PERSONA_FACTS,
  )
  const [draftEvents, setDraftEvents] = useState<ExtractedEvent[]>([])

  const charCount = text.trim().length
  const canSubmit = charCount >= 10 && !submitting

  /** Seed the review state from a successful extraction response. */
  const primeReviewFromExtract = (
    res: PersonaExtractResponse,
    source: 'blank_write' | 'import_upload',
  ) => {
    setExtract(res)
    setExtractSource(source)
    setDraftPersona(res.core_blocks.persona_block)
    setDraftSelf(res.core_blocks.self_block)
    setDraftUser(res.core_blocks.user_block)
    setDraftMood(res.core_blocks.mood_block)
    setDraftRelationship(res.core_blocks.relationship_block)
    setDraftFacts(res.facts)
    setDraftEvents(res.events)
  }

  const handleBlankAnalyse = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setStep('blank-analysing')
    try {
      const res = await postPersonaExtract({
        input_type: 'blank_write',
        user_input: text.trim(),
        persona_display_name:
          name.trim() || t('onboarding.default_display_name'),
        locale: i18n.language,
      })
      primeReviewFromExtract(res, 'blank_write')
      setStep('review')
    } catch (err) {
      let msg = t('onboarding.import_failed')
      if (err instanceof ApiError) msg = err.detail
      else if (err instanceof Error) msg = err.message
      setImportError(msg)
      setStep('blank-write')
    } finally {
      setSubmitting(false)
    }
  }

  const materialChars = material.trim().length
  const canRunImport = materialChars >= MIN_MATERIAL_CHARS && !submitting

  const handleRunImport = async () => {
    if (!canRunImport) return
    setImportError(null)
    setSubmitting(true)
    setStep('import-waiting')

    try {
      const upload = await postImportUploadText({
        text: material.trim(),
        source_label: 'onboarding_material',
      })
      const res = await postPersonaExtract({
        input_type: 'import_upload',
        upload_id: upload.upload_id,
        persona_display_name: t('onboarding.default_display_name'),
        locale: i18n.language,
      })
      primeReviewFromExtract(res, 'import_upload')
      setStep('review')
    } catch (err) {
      let msg = t('onboarding.import_failed')
      if (err instanceof ApiError) msg = err.detail
      else if (err instanceof Error) msg = err.message
      setImportError(msg)
      setStep('import-upload')
    } finally {
      setSubmitting(false)
    }
  }

  const handleCommitReviewed = async () => {
    if (submitting) return
    setSubmitting(true)
    try {
      const displayName =
        extractSource === 'blank_write' && name.trim()
          ? name.trim()
          : t('onboarding.default_display_name')
      await completeOnboarding({
        display_name: displayName,
        persona_block: draftPersona.trim(),
        self_block: draftSelf.trim(),
        user_block: draftUser.trim(),
        mood_block: draftMood.trim(),
        facts: draftFacts,
      })
      // relationship_block is written via a follow-up PATCH — the
      // onboarding contract intentionally omits it. Same behaviour as
      // before the facts initiative landed.
      const rel = draftRelationship.trim()
      if (rel.length > 0) {
        try {
          await postPersonaUpdate({ relationship_block: rel })
        } catch {
          // best-effort — persona is still usable with the four
          // primary blocks. User can retry from Admin later.
        }
      }
    } catch {
      // Error surfaces via the `error` prop.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="onboarding-wrap">
      <TopBar mood={t('onboarding.topbar_mood')} />

      {step === 'welcome' && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            {t('onboarding.welcome_title')}
            <span className="onboarding-punct">
              {t('onboarding.welcome_punct')}
            </span>
          </h1>
          <p className="onboarding-lead">
            {t('onboarding.welcome_subtitle')}
          </p>

          <div className="onboarding-paths">
            <button
              type="button"
              className="path-card"
              onClick={() => {
                setImportError(null)
                setStep('blank-write')
              }}
            >
              <div className="path-card-index">01</div>
              <div className="path-card-body">
                <div className="path-card-title">
                  {t('onboarding.path_blank_title')}
                </div>
                <p className="path-card-desc">
                  {t('onboarding.path_blank_body')}
                </p>
              </div>
              <span className="path-card-arrow">→</span>
            </button>

            <button
              type="button"
              className="path-card"
              onClick={() => {
                setImportError(null)
                setStep('import-upload')
              }}
            >
              <div className="path-card-index">02</div>
              <div className="path-card-body">
                <div className="path-card-title">
                  {t('onboarding.path_material_title')}
                </div>
                <p className="path-card-desc">
                  {t('onboarding.path_material_body')}
                </p>
              </div>
              <span className="path-card-arrow">→</span>
            </button>
          </div>

          <div className="onboarding-footnote">
            {t('onboarding.footnote')}
          </div>
        </main>
      )}

      {step === 'blank-write' && (
        <main className="onboarding">
          <button
            type="button"
            className="onboarding-back"
            onClick={() => setStep('welcome')}
            disabled={submitting}
          >
            {t('onboarding.back')}
          </button>

          <h1 className="onboarding-title">
            {t('onboarding.blank_title')}
            <span className="onboarding-punct">
              {t('onboarding.blank_title_punct')}
            </span>
          </h1>
          <p className="onboarding-lead">
            {t('onboarding.blank_lead_line1')}
            <br />
            {t('onboarding.blank_lead_line2')}
          </p>

          <label className="onboarding-name-label">
            {t('onboarding.name_label')}{' '}
            <span className="onboarding-name-hint">
              {t('onboarding.name_hint')}
            </span>
          </label>
          <input
            className="onboarding-name-input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('onboarding.name_placeholder')}
            maxLength={64}
            disabled={submitting}
          />

          <textarea
            className="onboarding-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={10}
            placeholder={t('onboarding.blank_textarea_placeholder')}
            autoFocus
            disabled={submitting}
          />

          {(importError ?? error) !== null && (
            <div
              className="onboarding-hint"
              style={{
                color: 'rgba(255, 120, 120, 0.78)',
                marginTop: 12,
              }}
            >
              ⚠ {importError ?? error}
            </div>
          )}

          <div className="onboarding-actions">
            <div className="onboarding-hint">
              {submitting
                ? t('onboarding.initialising')
                : charCount === 0
                  ? t('onboarding.need_a_few_sentences')
                  : canSubmit
                    ? t('onboarding.chars_enough', { count: charCount })
                    : t('onboarding.chars_more_needed', {
                        count: charCount,
                      })}
            </div>
            <button
              type="button"
              className="onboarding-submit"
              disabled={!canSubmit}
              onClick={() => void handleBlankAnalyse()}
            >
              {submitting ? '⋯' : t('onboarding.start_chat_cta')}
            </button>
          </div>
        </main>
      )}

      {step === 'blank-analysing' && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            {t('onboarding.waiting_title')}
          </h1>
          <p className="onboarding-lead">
            {t('onboarding.blank_analysing')}
          </p>
          <div
            className="onboarding-hint"
            style={{
              marginTop: 48,
              textAlign: 'center',
              fontSize: 14,
              color: 'rgba(255, 255, 255, 0.45)',
            }}
          >
            ⋯
          </div>
        </main>
      )}

      {step === 'import-upload' && (
        <main className="onboarding">
          <button
            type="button"
            className="onboarding-back"
            onClick={() => setStep('welcome')}
            disabled={submitting}
          >
            {t('onboarding.back')}
          </button>

          <h1 className="onboarding-title">
            {t('onboarding.upload_title')}
            <span className="onboarding-punct">
              {t('onboarding.upload_punct')}
            </span>
          </h1>
          <p className="onboarding-lead">
            {t('onboarding.upload_lead_line1')}
            <br />
            {t('onboarding.upload_lead_line2')}
          </p>

          <textarea
            className="onboarding-textarea"
            value={material}
            onChange={(e) => setMaterial(e.target.value)}
            rows={14}
            placeholder={t('onboarding.material_placeholder')}
            autoFocus
            disabled={submitting}
          />

          {importError !== null && (
            <div
              className="onboarding-hint"
              style={{
                color: 'rgba(255, 120, 120, 0.78)',
                marginTop: 12,
              }}
            >
              ⚠ {importError}
            </div>
          )}

          <div className="onboarding-actions">
            <div className="onboarding-hint">
              {materialChars === 0
                ? t('onboarding.need_min_chars', { min: MIN_MATERIAL_CHARS })
                : canRunImport
                  ? t('onboarding.material_ready', { count: materialChars })
                  : t('onboarding.material_more_needed', {
                      count: materialChars,
                      remaining: MIN_MATERIAL_CHARS - materialChars,
                    })}
            </div>
            <button
              type="button"
              className="onboarding-submit"
              disabled={!canRunImport}
              onClick={() => void handleRunImport()}
            >
              {t('onboarding.run_import_cta')}
            </button>
          </div>
        </main>
      )}

      {step === 'import-waiting' && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            {t('onboarding.waiting_title')}
            <span className="onboarding-punct"></span>
          </h1>
          <p className="onboarding-lead">{t('onboarding.waiting_lead')}</p>
          <div
            className="onboarding-hint"
            style={{
              marginTop: 48,
              textAlign: 'center',
              fontSize: 14,
              color: 'rgba(255, 255, 255, 0.45)',
            }}
          >
            ⋯
          </div>
        </main>
      )}

      {step === 'review' && extract !== null && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            {t('onboarding.review_title')}
            <span className="onboarding-punct">
              {t('onboarding.review_punct')}
            </span>
          </h1>
          <p className="onboarding-lead">
            {extractSource === 'import_upload'
              ? t('onboarding.review_lead', {
                  events: extract.events.length,
                  thoughts: extract.thoughts.length,
                })
              : t('onboarding.review_lead_blank')}
          </p>

          <section className="facts-section">
            <header className="facts-section-header">
              {t('onboarding.review_section_facts')}
            </header>
            <PersonaFactsEditor
              value={draftFacts}
              onChange={setDraftFacts}
              disabled={submitting}
            />
          </section>

          <section className="facts-section">
            <header className="facts-section-header">
              {t('onboarding.review_section_blocks')}
            </header>
            <div className="onboarding-blocks">
              <BlockField
                label={t('onboarding.review_block_persona')}
                engName="persona_block"
                value={draftPersona}
                onChange={setDraftPersona}
                rows={6}
                disabled={submitting}
              />
              <BlockField
                label={t('onboarding.review_block_self')}
                engName="self_block"
                hint={t('onboarding.review_block_self_hint')}
                value={draftSelf}
                onChange={setDraftSelf}
                rows={3}
                disabled={submitting}
              />
              <BlockField
                label={t('onboarding.review_block_user')}
                engName="user_block"
                value={draftUser}
                onChange={setDraftUser}
                rows={6}
                disabled={submitting}
              />
              <BlockField
                label={t('onboarding.review_block_relationship')}
                engName="relationship_block"
                value={draftRelationship}
                onChange={setDraftRelationship}
                rows={5}
                disabled={submitting}
              />
              <BlockField
                label={t('onboarding.review_block_mood')}
                engName="mood_block"
                hint={t('onboarding.review_block_mood_hint')}
                value={draftMood}
                onChange={setDraftMood}
                rows={2}
                disabled={submitting}
              />
            </div>
          </section>

          {extractSource === 'import_upload' && draftEvents.length > 0 && (
            <section className="facts-section">
              <header className="facts-section-header">
                {t('onboarding.review_section_events')}
              </header>
              <ul
                style={{
                  fontSize: 13,
                  color: 'rgba(255, 255, 255, 0.64)',
                  paddingLeft: 18,
                  margin: 0,
                }}
              >
                {draftEvents.map((ev, i) => (
                  <li key={i} style={{ marginBottom: 6 }}>
                    impact {ev.emotional_impact >= 0 ? '+' : ''}
                    {ev.emotional_impact} · {ev.description}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {error !== null && (
            <div
              className="onboarding-hint"
              style={{
                color: 'rgba(255, 120, 120, 0.78)',
                marginTop: 12,
              }}
            >
              ⚠ {error}
            </div>
          )}

          <div className="onboarding-actions">
            <div className="onboarding-hint">
              {submitting
                ? t('onboarding.writing_persona')
                : t('onboarding.review_ready')}
            </div>
            <button
              type="button"
              className="onboarding-submit"
              disabled={submitting}
              onClick={() => void handleCommitReviewed()}
            >
              {submitting ? '⋯' : t('onboarding.commit_cta')}
            </button>
          </div>
        </main>
      )}
    </div>
  )
}

interface BlockFieldProps {
  label: string
  engName: string
  hint?: string
  value: string
  onChange: (next: string) => void
  rows: number
  disabled: boolean
}

function BlockField({
  label,
  engName,
  hint,
  value,
  onChange,
  rows,
  disabled,
}: BlockFieldProps) {
  return (
    <section className="onboarding-block-field" style={{ marginBottom: 20 }}>
      <header style={{ marginBottom: 6 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{label}</span>
        <span
          style={{
            fontSize: 11,
            marginLeft: 8,
            color: 'rgba(255, 255, 255, 0.38)',
            letterSpacing: '0.04em',
          }}
        >
          {engName}
        </span>
        {hint && (
          <div
            style={{
              fontSize: 12,
              marginTop: 2,
              color: 'rgba(255, 255, 255, 0.48)',
            }}
          >
            {hint}
          </div>
        )}
      </header>
      <textarea
        className="onboarding-textarea"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        disabled={disabled}
        style={{ width: '100%', minHeight: 0 }}
      />
    </section>
  )
}
