import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import type {
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
} from '../../../api/types'
import { IdentitySection } from './IdentitySection'
import { ReflectionSection } from './ReflectionSection'
import { SocialGraphSection } from './SocialGraphSection'

/**
 * Admin → Persona tab orchestrator. Three sections, one above the
 * other:
 *
 *   1 · Identity     — human writes (avatar, display_name, persona /
 *                      user / style blocks, biographic facts)
 *   2 · Reflection   — persona writes (L4 thought[subject='persona']
 *                      list with delete + filling-chain affordances)
 *   3 · Social Graph — hybrid (L5 entities grouped by kind, with
 *                      uncertain-merge arbitration on top + inline
 *                      description editor that stamps owner_override
 *                      server-side)
 *
 * Day 1: section 1 is fully wired against existing endpoints; sections
 * 2 + 3 render skeleton placeholders until Worker A merges Spec 1's
 * thoughts-by-subject filter and the entities list/patch endpoints.
 */
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

  return (
    <div
      style={{
        padding: '28px 36px',
        display: 'flex',
        flexDirection: 'column',
        gap: 28,
      }}
    >
      <div className="row g-3" style={{ alignItems: 'baseline' }}>
        <h1 className="title" style={{ margin: 0 }}>
          {t('admin.persona.page_title')}
        </h1>
        <div
          style={{
            color: 'var(--ink-3)',
            fontSize: 12,
            fontFamily: 'var(--mono)',
          }}
        >
          id: {persona.id} · 3 core blocks · 15 facts
        </div>
        <div className="flex1" />
        <button
          className="btn ghost sm"
          onClick={() => navigate('/admin/import')}
          title={t('admin.persona.import_prompt')}
        >
          {t('admin.persona.import_cta')}
        </button>
      </div>

      <IdentitySection
        persona={persona}
        updatePersona={updatePersona}
        updateFacts={updateFacts}
      />

      <hr style={{ border: 0, borderTop: '1px solid var(--rule)' }} />

      <ReflectionSection />

      <hr style={{ border: 0, borderTop: '1px solid var(--rule)' }} />

      <SocialGraphSection />
    </div>
  )
}
