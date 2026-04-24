import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'
import { postUsersTimezone } from './api/client'
import { Chat } from './screens/Chat'
import { Onboarding } from './screens/Onboarding'
import { Admin } from './screens/Admin'
import { ImportScreen } from './screens/Import'
import { VoiceClone } from './screens/VoiceClone'
import { usePersona } from './hooks/usePersona'
import type {
  DaemonState,
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
} from './api/types'

// Best-effort browser IANA timezone detection. Plan decision 5: the
// web channel posts this on first connect so the daemon can render a
// dual-timezone ``# Right now`` section. Runs once per mount and
// silently ignores failures — daemon falls back to single-tz rendering.
function useTimezoneAutoDetect(): void {
  useEffect(() => {
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
      if (!tz) return
      void postUsersTimezone({ timezone: tz, override: false }).catch(() => {
        /* non-fatal */
      })
    } catch {
      /* non-fatal — older browsers without Intl.DateTimeFormat */
    }
  }, [])
}

export function App() {
  return (
    <BrowserRouter>
      <AppShell />
    </BrowserRouter>
  )
}

function AppShell() {
  // Single usePersona instance for the whole app — opens the SSE
  // connection once at the top level so it stays alive across route
  // transitions (see 05-stage-4-proper-tracker.md §3 Task 7).
  const {
    persona,
    daemonState,
    loading,
    error,
    updatePersona,
    updateFacts,
    toggleVoice,
    completeOnboarding,
  } = usePersona()

  useTimezoneAutoDetect()

  if (loading && daemonState === null) {
    return <BootScreen />
  }

  if (error !== null && daemonState === null) {
    return <BootError message={error} />
  }

  if (daemonState === null) {
    // Defensive — loading just flipped but state not yet populated.
    return <BootScreen />
  }

  if (daemonState.onboarding_required) {
    return (
      <Onboarding
        completeOnboarding={completeOnboarding}
        error={error}
      />
    )
  }

  return (
    <Routes>
      <Route path="/" element={<Navigate to="/chat" replace />} />

      <Route
        path="/onboarding"
        element={<Navigate to="/chat" replace />}
      />

      <Route
        path="/chat"
        element={
          persona !== null ? (
            <ChatRoute
              displayName={persona.display_name}
              voiceEnabled={persona.voice_enabled}
              voiceId={persona.voice_id}
              hasAvatar={persona.has_avatar}
            />
          ) : (
            <BootScreen />
          )
        }
      />

      <Route
        path="/admin/*"
        element={
          persona !== null ? (
            <AdminRoute
              persona={persona}
              daemonState={daemonState}
              updatePersona={updatePersona}
              updateFacts={updateFacts}
              toggleVoice={toggleVoice}
            />
          ) : (
            <BootScreen />
          )
        }
      />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

// ─── Route wrappers (inject useNavigate) ───

function ChatRoute({
  displayName,
  voiceEnabled,
  voiceId,
  hasAvatar,
}: {
  displayName: string
  voiceEnabled: boolean
  voiceId: string | null
  hasAvatar: boolean
}) {
  const navigate = useNavigate()
  return (
    <Chat
      displayName={displayName}
      voiceEnabled={voiceEnabled}
      voiceId={voiceId}
      hasAvatar={hasAvatar}
      onOpenAdmin={() => navigate('/admin')}
    />
  )
}

function AdminRoute({
  persona,
  daemonState,
  updatePersona,
  updateFacts,
  toggleVoice,
}: {
  persona: PersonaStateApi
  daemonState: DaemonState
  updatePersona: (payload: PersonaUpdatePayload) => Promise<void>
  updateFacts: (facts: Partial<PersonaFacts>) => Promise<void>
  toggleVoice: (enabled: boolean) => Promise<void>
}) {
  const navigate = useNavigate()
  // Nested routes inside the /admin/* wildcard. `index` renders the
  // main Admin tabbed shell; `import` renders the 3-step import wizard
  // — both share the same top-level /admin/* mount from AppShell.
  return (
    <Routes>
      <Route
        index
        element={
          <Admin
            persona={persona}
            daemonState={daemonState}
            updatePersona={updatePersona}
            updateFacts={updateFacts}
            toggleVoice={toggleVoice}
            onBackToChat={() => navigate('/chat')}
          />
        }
      />
      <Route
        path="import"
        element={<ImportScreen onBack={() => navigate('/admin')} />}
      />
      <Route
        path="voice/clone"
        element={<VoiceClone onBack={() => navigate('/admin')} />}
      />
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  )
}

// ─── Boot-time screens ───

function BootScreen() {
  return (
    <div className="boot">
      <div className="boot-dot" />
    </div>
  )
}

function BootError({ message }: { message: string }) {
  const { t } = useTranslation()
  return (
    <div className="boot">
      <div
        className="boot-dot"
        style={{ background: 'rgba(255, 80, 80, 0.6)' }}
      />
      <div
        style={{
          position: 'absolute',
          bottom: '24%',
          left: 0,
          right: 0,
          textAlign: 'center',
          color: 'rgba(255, 255, 255, 0.65)',
          fontSize: 13,
          letterSpacing: '0.04em',
          padding: '0 32px',
        }}
      >
        {t('boot.cannot_reach_daemon', { message })}
      </div>
    </div>
  )
}

