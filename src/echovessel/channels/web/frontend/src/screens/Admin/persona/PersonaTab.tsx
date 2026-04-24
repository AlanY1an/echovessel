import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import type {
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
} from '../../../api/types'
import { PersonaFactsEditor } from '../../../components/PersonaFactsEditor'
import { factsEqual } from '../helpers'
import type { BlockMeta } from '../types'
import { AvatarCard } from './AvatarCard'
import { BlockCard } from './BlockCard'

// Core-block visual metadata. Colour hues match the hand-drawn
// prototypes; i18n keys reference `admin.persona_blocks.*`.
const BLOCK_META: BlockMeta[] = [
  {
    key: 'persona',
    color: 'oklch(72% 0.13 30)',
    labelKey: 'admin.persona_blocks.persona_label',
    hintKey: 'admin.persona_blocks.persona_hint',
  },
  {
    key: 'self',
    color: 'oklch(68% 0.13 130)',
    labelKey: 'admin.persona_blocks.self_label',
    hintKey: 'admin.persona_blocks.self_hint',
  },
  {
    key: 'user',
    color: 'oklch(68% 0.13 250)',
    labelKey: 'admin.persona_blocks.user_label',
    hintKey: 'admin.persona_blocks.user_hint',
  },
  {
    key: 'relationship',
    color: 'oklch(62% 0.12 80)',
    labelKey: 'admin.persona_blocks.relationship_label',
    hintKey: 'admin.persona_blocks.relationship_hint',
  },
  {
    key: 'style',
    color: 'oklch(68% 0.13 330)',
    labelKey: 'admin.persona_blocks.style_label',
    hintKey: 'admin.persona_blocks.style_hint',
  },
]

export function AdmPersona({
  persona,
  updatePersona,
  updateFacts,
}: {
  persona: PersonaStateApi
  updatePersona: (payload: PersonaUpdatePayload) => Promise<void>
  updateFacts: (facts: Partial<PersonaFacts>) => Promise<void>
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()

  // Display name rename
  const [nameDraft, setNameDraft] = useState(persona.display_name)
  const [nameSaving, setNameSaving] = useState(false)
  useEffect(() => setNameDraft(persona.display_name), [persona.display_name])
  const nameDirty = nameDraft.trim() !== persona.display_name && nameDraft.trim().length > 0

  const handleSaveName = async () => {
    if (!nameDirty || nameSaving) return
    setNameSaving(true)
    try {
      await updatePersona({ display_name: nameDraft.trim() })
    } finally {
      setNameSaving(false)
    }
  }

  // Facts draft (uses PersonaFactsEditor)
  const [factsDraft, setFactsDraft] = useState<PersonaFacts>(persona.facts)
  const [factsSaving, setFactsSaving] = useState(false)
  const [factsSavedAt, setFactsSavedAt] = useState<number | null>(null)
  useEffect(() => {
    setFactsDraft(persona.facts)
  }, [persona.facts])
  const factsDirty = useMemo(
    () => !factsEqual(factsDraft, persona.facts),
    [factsDraft, persona.facts],
  )

  const handleSaveFacts = async () => {
    if (!factsDirty || factsSaving) return
    setFactsSaving(true)
    try {
      await updateFacts(factsDraft)
      setFactsSavedAt(Date.now())
      window.setTimeout(() => setFactsSavedAt(null), 2000)
    } finally {
      setFactsSaving(false)
    }
  }

  return (
    <div style={{ padding: '28px 36px', display: 'flex', flexDirection: 'column', gap: 18 }}>
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.persona_blocks.section_title')}</h2>
        <div style={{ color: 'var(--ink-3)', fontSize: 12, fontFamily: 'var(--mono)' }}>
          id: {persona.id} · 5 core blocks · 14 facts
        </div>
        <div className="flex1" />
        <button
          className="btn ghost sm"
          onClick={() => navigate('/admin/import')}
          title={t('admin.persona_blocks.import_prompt')}
        >
          {t('admin.persona_blocks.import_cta')}
        </button>
      </div>

      {/* Profile picture — self-contained: its upload/remove POST to
          the daemon directly and bump a local version counter so the
          <img> refreshes without a full usePersona refresh cycle. */}
      <AvatarCard initialHasAvatar={persona.has_avatar} displayName={persona.display_name} />

      {/* Name row (persists via updatePersona) */}
      <div className="card row g-3" style={{ padding: 14, alignItems: 'center' }}>
        <span
          style={{
            fontFamily: 'var(--mono)',
            fontSize: 10,
            color: 'var(--ink-3)',
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
            width: 120,
          }}
        >
          {t('admin.persona_blocks.display_name')}
        </span>
        <input
          className="bare"
          value={nameDraft}
          onChange={(e) => setNameDraft(e.target.value)}
          disabled={nameSaving}
          style={{
            flex: 1,
            border: '1px solid var(--rule)',
            padding: '6px 10px',
            borderRadius: 4,
            background: 'var(--paper)',
            fontSize: 13,
          }}
        />
        <button
          className="btn sm"
          disabled={!nameDirty || nameSaving}
          onClick={() => void handleSaveName()}
        >
          {nameSaving ? '⋯' : t('admin.persona_blocks.display_name_rename')}
        </button>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1.15fr 1fr',
          gap: 18,
          alignItems: 'start',
        }}
      >
        <div className="stack g-3">
          <span className="label">core blocks · L1</span>
          {BLOCK_META.map((meta) => (
            <BlockCard
              key={meta.key}
              meta={meta}
              value={persona.core_blocks[meta.key]}
              onSave={(next) =>
                updatePersona({
                  [`${meta.key}_block`]: next,
                } as PersonaUpdatePayload)
              }
            />
          ))}
        </div>
        <div
          className="stack g-3"
          style={{ position: 'sticky', top: 0 }}
        >
          <span className="label">{t('facts.section_title')}</span>
          <div className="card" style={{ padding: 18 }}>
            <PersonaFactsEditor
              value={factsDraft}
              onChange={setFactsDraft}
              disabled={factsSaving}
            />
            <div
              className="row g-2"
              style={{
                marginTop: 14,
                alignItems: 'center',
                borderTop: '1px solid var(--rule)',
                paddingTop: 12,
              }}
            >
              <div className="flex1" style={{ fontSize: 11, color: 'var(--ink-3)' }}>
                {factsSavedAt && <span>{t('admin.common.saved')}</span>}
                {!factsSavedAt && factsDirty && (
                  <span>{t('admin.common.unsaved_warning')}</span>
                )}
              </div>
              <button
                className="btn sm"
                disabled={!factsDirty || factsSaving}
                onClick={() => void handleSaveFacts()}
              >
                {factsSaving ? '⋯' : t('admin.common.save')}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
