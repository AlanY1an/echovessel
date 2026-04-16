import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'
import { Chat } from './screens/Chat'
import { Onboarding } from './screens/Onboarding'
import { Admin } from './screens/Admin'
import { ImportScreen } from './screens/Import'
import { usePersona } from './hooks/usePersona'
import type {
  DaemonState,
  PersonaStateApi,
  PersonaUpdatePayload,
} from './api/types'

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
    toggleVoice,
    completeOnboarding,
  } = usePersona()

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
              moodBlock={persona.core_blocks.mood}
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

function ChatRoute({ moodBlock }: { moodBlock: string }) {
  const navigate = useNavigate()
  return (
    <Chat
      moodBlock={moodBlock}
      onOpenAdmin={() => navigate('/admin')}
    />
  )
}

function AdminRoute({
  persona,
  daemonState,
  updatePersona,
  toggleVoice,
}: {
  persona: PersonaStateApi
  daemonState: DaemonState
  updatePersona: (payload: PersonaUpdatePayload) => Promise<void>
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
            toggleVoice={toggleVoice}
            onBackToChat={() => navigate('/chat')}
          />
        }
      />
      <Route
        path="import"
        element={<ImportScreen onBack={() => navigate('/admin')} />}
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
        无法连接到本地 daemon · {message}
      </div>
    </div>
  )
}

