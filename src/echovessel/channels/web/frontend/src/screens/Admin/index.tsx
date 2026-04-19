/**
 * Admin shell — top bar · sidebar · body.
 *
 * Every tab lives in its own folder (persona/, memory/, voice/,
 * imports/, channels/, config/); this file is just the entry router.
 * App.tsx imports `{ Admin }` from `./screens/Admin`, which resolves
 * to this file because it is named `index.tsx`.
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { LanguageToggle } from '../../components/LanguageToggle'
import type {
  DaemonState,
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
} from '../../api/types'
import { AdmChannels } from './channels/ChannelsTab'
import { AdmConfig } from './config/ConfigTab'
import { AdmImports } from './imports/ImportsTab'
import { AdmMemory } from './memory/MemoryTab'
import { AdmPersona } from './persona/PersonaTab'
import type { AdmTab } from './types'
import { AdmVoice } from './voice/VoiceTab'

interface AdminProps {
  persona: PersonaStateApi
  daemonState: DaemonState
  updatePersona: (payload: PersonaUpdatePayload) => Promise<void>
  updateFacts: (facts: Partial<PersonaFacts>) => Promise<void>
  toggleVoice: (enabled: boolean) => Promise<void>
  onBackToChat: () => void
}

export function Admin({
  persona,
  daemonState: _daemonState,
  updatePersona,
  updateFacts,
  toggleVoice,
  onBackToChat,
}: AdminProps) {
  const { t } = useTranslation()
  const [tab, setTab] = useState<AdmTab>('persona')

  const tabDefs: { id: AdmTab; labelKey: string }[] = [
    { id: 'persona', labelKey: 'admin.tabs.persona' },
    { id: 'memory', labelKey: 'admin.tabs.memory' },
    { id: 'voice', labelKey: 'admin.tabs.voice' },
    { id: 'channels', labelKey: 'admin.tabs.channels' },
    { id: 'sources', labelKey: 'admin.tabs.imports' },
    { id: 'config', labelKey: 'admin.tabs.config' },
  ]

  return (
    <div className="adm-screen">
      <div className="adm-top">
        <button className="btn ghost sm" onClick={onBackToChat}>
          ← {persona.display_name}
        </button>
        <div className="flex1" />
        <span className="label">{t('admin.page_title')}</span>
        <span className="chip">
          {persona.display_name} · {persona.id}
        </span>
        <div className="flex1" />
        <LanguageToggle />
      </div>
      <div className="adm">
        <div className="adm-nav">
          <span className="lbl">{persona.display_name}</span>
          {tabDefs.map((d) => (
            <button
              key={d.id}
              className={tab === d.id ? 'on' : ''}
              onClick={() => setTab(d.id)}
            >
              {t(d.labelKey)}
            </button>
          ))}
        </div>
        <div className="adm-body">
          {tab === 'persona' && (
            <AdmPersona
              persona={persona}
              updatePersona={updatePersona}
              updateFacts={updateFacts}
            />
          )}
          {tab === 'memory' && <AdmMemory />}
          {tab === 'voice' && (
            <AdmVoice persona={persona} toggleVoice={toggleVoice} />
          )}
          {tab === 'sources' && <AdmImports />}
          {tab === 'channels' && <AdmChannels />}
          {tab === 'config' && <AdmConfig />}
        </div>
      </div>
    </div>
  )
}
