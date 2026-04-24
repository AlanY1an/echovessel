/**
 * useDevMode — toggleable developer flag controlling the per-turn
 * trace drawer entry point on chat bubbles.
 *
 * Activation rules (Spec 4 · plan §4.4):
 *
 *   1. ``?dev=1`` query param on first load writes
 *      ``localStorage['echovessel.dev_mode']='1'`` and stays sticky —
 *      stripping the query later does NOT turn dev-mode off.
 *   2. ``?dev=0`` query param force-clears the flag.
 *   3. Explicit toggle via the returned ``toggle()`` callback flips and
 *      persists. Once dev-mode is ON, the only way to turn it OFF is
 *      this toggle (or ``?dev=0``).
 *
 * Returns a stable ``{ enabled, toggle }`` object. Components that only
 * gate rendering on ``enabled`` need not memoize.
 */

import { useCallback, useState } from 'react'

const STORAGE_KEY = 'echovessel.dev_mode'

function readInitialState(): boolean {
  if (typeof window === 'undefined') return false
  try {
    const url = new URL(window.location.href)
    const flag = url.searchParams.get('dev')
    if (flag === '1') {
      window.localStorage.setItem(STORAGE_KEY, '1')
      return true
    }
    if (flag === '0') {
      window.localStorage.setItem(STORAGE_KEY, '0')
      return false
    }
    return window.localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export interface UseDevModeResult {
  enabled: boolean
  toggle: () => void
}

export function useDevMode(): UseDevModeResult {
  const [enabled, setEnabled] = useState<boolean>(readInitialState)

  const toggle = useCallback(() => {
    setEnabled((prev) => {
      const next = !prev
      try {
        if (typeof window !== 'undefined') {
          window.localStorage.setItem(STORAGE_KEY, next ? '1' : '0')
        }
      } catch {
        // localStorage unavailable (private mode quota) — fall through;
        // the flag is still tracked in component state.
      }
      return next
    })
  }, [])

  return { enabled, toggle }
}
