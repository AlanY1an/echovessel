/**
 * usePersona — hook that fetches and manages persona state.
 *
 * Stage 4 proper will call this from <App.tsx> (for boot-time routing
 * based on `daemonState.onboarding_required`), from <Admin.tsx> (to read
 * `persona` and call `updatePersona` / `toggleVoice`), and from
 * <Onboarding.tsx> (to call `completeOnboarding`). Component flow:
 *
 *   const {
 *     persona, daemonState, loading, error,
 *     refresh, updatePersona, toggleVoice, completeOnboarding,
 *   } = usePersona()
 *
 * State flow:
 *   - On mount, fetches `/api/state` and `/api/admin/persona` in
 *     parallel and populates both slots.
 *   - `refresh()` re-fetches both.
 *   - `updatePersona(payload)` POSTs, then refreshes so the admin
 *     screen always shows the canonical server state.
 *   - `toggleVoice(enabled)` POSTs, then optimistically updates local
 *     state — the SSE `chat.settings.updated` broadcast will also
 *     arrive and confirm (or correct) the value.
 *   - `completeOnboarding(payload)` POSTs, then refreshes so the boot
 *     router transitions away from the onboarding screen.
 *   - Subscribes to the SSE `chat.settings.updated` event via
 *     `useSSE().subscribe(...)` so toggling voice in one tab updates
 *     the others.
 *
 * This file is Stage 4-prep only — components are not yet wired.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  getPersona,
  getState,
  patchPersonaFacts,
  postOnboarding,
  postPersonaUpdate,
  postVoiceToggle,
} from '../api/client'
import type {
  ChatEvent,
  DaemonState,
  OnboardingPayload,
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
} from '../api/types'
import { ApiError } from '../api/types'
import { useSSE } from './useSSE'

export interface UsePersonaResult {
  persona: PersonaStateApi | null
  daemonState: DaemonState | null
  loading: boolean
  error: string | null
  refresh(): Promise<void>
  updatePersona(payload: PersonaUpdatePayload): Promise<void>
  updateFacts(facts: Partial<PersonaFacts>): Promise<void>
  toggleVoice(enabled: boolean): Promise<void>
  completeOnboarding(payload: OnboardingPayload): Promise<void>
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return 'unknown error'
}

export function usePersona(): UsePersonaResult {
  const [persona, setPersona] = useState<PersonaStateApi | null>(null)
  const [daemonState, setDaemonState] = useState<DaemonState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const { subscribe } = useSSE()

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError(null)
    try {
      const [stateValue, personaValue] = await Promise.all([
        getState(),
        getPersona(),
      ])
      setDaemonState(stateValue)
      setPersona(personaValue)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch on mount.
  useEffect(() => {
    void refresh()
  }, [refresh])

  // Cross-tab + runtime-driven sync. Three events funnel into persona
  // state here:
  //
  //   • chat.settings.updated  — voice_enabled toggled from another tab
  //   • chat.mood.update       — memory's update_mood_block hook fired
  //                              (consolidate worker, explicit edit, ...)
  //
  // Each handler is a no-op if the persona hasn't loaded yet; the next
  // refresh() picks up the correct value.
  useEffect(() => {
    const unsubscribe = subscribe((event: ChatEvent) => {
      if (event.event === 'chat.settings.updated') {
        const next = event.data.voice_enabled
        setPersona((prev) =>
          prev === null ? prev : { ...prev, voice_enabled: next },
        )
        setDaemonState((prev) =>
          prev === null
            ? prev
            : { ...prev, persona: { ...prev.persona, voice_enabled: next } },
        )
        return
      }

      if (event.event === 'chat.mood.update') {
        // The server sends the full new mood block text as `mood_summary`.
        // Splice it into core_blocks.mood so any consumer reading persona
        // (Chat.tsx's top-bar moodBlock prop, Admin's mood editor) picks
        // the new value up on the next render without a manual refresh.
        const nextMood = event.data.mood_summary
        setPersona((prev) =>
          prev === null
            ? prev
            : {
                ...prev,
                core_blocks: { ...prev.core_blocks, mood: nextMood },
              },
        )
        return
      }
    })
    return unsubscribe
  }, [subscribe])

  const updatePersona = useCallback(
    async (payload: PersonaUpdatePayload): Promise<void> => {
      setError(null)
      try {
        await postPersonaUpdate(payload)
        await refresh()
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const toggleVoice = useCallback(
    async (enabled: boolean): Promise<void> => {
      setError(null)
      try {
        const result = await postVoiceToggle(enabled)
        // Optimistic local update using the server's confirmed value.
        setPersona((prev) =>
          prev === null
            ? prev
            : { ...prev, voice_enabled: result.voice_enabled },
        )
        setDaemonState((prev) =>
          prev === null
            ? prev
            : {
                ...prev,
                persona: {
                  ...prev.persona,
                  voice_enabled: result.voice_enabled,
                },
              },
        )
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [],
  )

  const completeOnboarding = useCallback(
    async (payload: OnboardingPayload): Promise<void> => {
      setError(null)
      try {
        await postOnboarding(payload)
        await refresh()
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [refresh],
  )

  const updateFacts = useCallback(
    async (facts: Partial<PersonaFacts>): Promise<void> => {
      setError(null)
      try {
        const result = await patchPersonaFacts({ facts })
        // Optimistic local update: server echo is authoritative.
        setPersona((prev) =>
          prev === null ? prev : { ...prev, facts: result.facts },
        )
      } catch (err) {
        setError(errorMessage(err))
        throw err
      }
    },
    [],
  )

  return {
    persona,
    daemonState,
    loading,
    error,
    refresh,
    updatePersona,
    updateFacts,
    toggleVoice,
    completeOnboarding,
  }
}
