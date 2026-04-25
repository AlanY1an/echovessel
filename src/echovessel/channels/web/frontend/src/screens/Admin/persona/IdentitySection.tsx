import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { postPersonaStyle } from '../../../api/client'
import type {
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
} from '../../../api/types'
import { PersonaFactsEditor } from '../../../components/PersonaFactsEditor'
import { factsEqual } from '../helpers'
import type { BlockMeta } from '../types'
import { AvatarCard } from './components/AvatarCard'
import { BlockCard } from './components/BlockCard'

/**
 * Persona-tab section 1 · IDENTITY (human-authored, full owner control).
 *
 * Avatar + display name in the header row, then the three L1 prose
 * blocks (persona / user / style) and the 15-field biographic facts
 * editor. ``persona`` writes go through ``updatePersona`` (POST
 * /api/admin/persona); ``style`` writes go through
 * ``postPersonaStyle({action:'set'})`` because the daemon enforces
 * the action-tagged write path on /api/admin/persona/style.
 */

const PERSONA_BLOCK_META: BlockMeta = {
  key: 'persona',
  color: 'oklch(72% 0.13 30)',
  labelKey: 'admin.persona.blocks.persona_label',
  hintKey: 'admin.persona.blocks.persona_hint',
}

const USER_BLOCK_META: BlockMeta = {
  key: 'user',
  color: 'oklch(68% 0.13 250)',
  labelKey: 'admin.persona.blocks.user_label',
  hintKey: 'admin.persona.blocks.user_hint',
}

const STYLE_BLOCK_META: BlockMeta = {
  key: 'style',
  color: 'oklch(68% 0.13 330)',
  labelKey: 'admin.persona.blocks.style_label',
  hintKey: 'admin.persona.blocks.style_hint',
}

export function IdentitySection({
  persona,
  updatePersona,
  updateFacts,
  onOpenVoiceTab,
}: {
  persona: PersonaStateApi
  updatePersona: (payload: PersonaUpdatePayload) => Promise<void>
  updateFacts: (facts: Partial<PersonaFacts>) => Promise<void>
  onOpenVoiceTab: () => void
}) {
  const { t } = useTranslation()

  const [nameDraft, setNameDraft] = useState(persona.display_name)
  const [nameSaving, setNameSaving] = useState(false)
  useEffect(() => setNameDraft(persona.display_name), [persona.display_name])
  const nameDirty =
    nameDraft.trim() !== persona.display_name && nameDraft.trim().length > 0

  const handleSaveName = async () => {
    if (!nameDirty || nameSaving) return
    setNameSaving(true)
    try {
      await updatePersona({ display_name: nameDraft.trim() })
    } finally {
      setNameSaving(false)
    }
  }

  const [factsDraft, setFactsDraft] = useState<PersonaFacts>(persona.facts)
  const [factsSaving, setFactsSaving] = useState(false)
  const [factsSavedAt, setFactsSavedAt] = useState<number | null>(null)
  useEffect(() => setFactsDraft(persona.facts), [persona.facts])
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

  const handleSaveStyle = async (next: string) => {
    const trimmed = next.trim()
    await postPersonaStyle({
      action: trimmed.length === 0 ? 'clear' : 'set',
      text: trimmed,
    })
  }

  // Read-only voice status chip in the section header. Source of truth
  // is the Voice tab (clone wizard, voice_id picker); this is just a
  // display + jump-link so the owner sees current state without leaving
  // the persona view.
  const voiceChipText = !persona.voice_enabled
    ? t('admin.persona.voice_chip.off')
    : persona.voice_id !== null
      ? t('admin.persona.voice_chip.cloned')
      : t('admin.persona.voice_chip.default')
  const voiceChipIcon = persona.voice_enabled ? '🔊' : '🔇'

  return (
    <section className="stack g-3">
      <div className="row g-2" style={{ alignItems: 'baseline' }}>
        <h2 className="title">{t('admin.persona.sections.identity')}</h2>
        <span
          style={{
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--ink-3)',
          }}
        >
          {t('admin.persona.sections.identity_hint')}
        </span>
        <div className="flex1" />
        <button
          type="button"
          className="chip"
          onClick={onOpenVoiceTab}
          title={t('admin.persona.voice_chip.aria')}
          aria-label={t('admin.persona.voice_chip.aria')}
          style={{ cursor: 'pointer' }}
        >
          {voiceChipIcon} {t('admin.persona.voice_chip.label')} · {voiceChipText} ↗
        </button>
      </div>

      <AvatarCard
        initialHasAvatar={persona.has_avatar}
        displayName={persona.display_name}
      />

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
          {t('admin.persona.display_name')}
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
          {nameSaving ? '⋯' : t('admin.persona.display_name_rename')}
        </button>
      </div>

      <BlockCard
        meta={PERSONA_BLOCK_META}
        value={persona.core_blocks.persona}
        onSave={(next) => updatePersona({ persona_block: next })}
      />
      <BlockCard
        meta={USER_BLOCK_META}
        value={persona.core_blocks.user}
        onSave={(next) => updatePersona({ user_block: next })}
      />
      <BlockCard
        meta={STYLE_BLOCK_META}
        value={persona.core_blocks.style}
        onSave={handleSaveStyle}
      />

      <div className="stack g-2">
        <span className="label">{t('admin.persona.facts_heading')}</span>
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
              {factsSavedAt !== null && <span>{t('admin.common.saved')}</span>}
              {factsSavedAt === null && factsDirty && (
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
    </section>
  )
}
