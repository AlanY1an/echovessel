/**
 * Shared visual primitives ported from the design prototype's
 * hi/shared.jsx + admin.jsx. These are pure presentational helpers —
 * they take plain props and render the paper/ink/rust visual language
 * declared in styles.css.
 */

import { useMemo } from 'react'

// ─── Waveform bars (voice bubble, sample rows, record stage) ──

interface WaveProps {
  bars?: number
  seed?: number
  color?: string
  heights?: number[]
}

export function Wave({ bars = 42, seed = 0, color = 'var(--ink-2)', heights }: WaveProps) {
  const bucket = useMemo(() => {
    if (heights) return heights
    return Array.from({ length: bars }).map(
      (_, i) =>
        3 +
        Math.abs(
          Math.sin((i + seed) * 0.6) * 8 + Math.sin((i + seed) * 1.3) * 4,
        ),
    )
  }, [bars, seed, heights])

  return (
    <>
      {bucket.map((h, i) => (
        <i key={i} style={{ height: h, background: color }} />
      ))}
    </>
  )
}

// ─── Avatar letter ──────────────────────────────────────────

interface AvatarProps {
  letter: string
  size?: 'sm' | 'md' | 'lg'
  /** Optional image URL. When present we render the picture inside
   *  the .w-avatar ring; when absent we fall back to the letter. */
  url?: string | null
}

export function Avatar({ letter, size = 'md', url }: AvatarProps) {
  const cls = size === 'lg' ? 'w-avatar lg' : size === 'sm' ? 'w-avatar sm' : 'w-avatar'
  if (url) {
    return (
      <div className={cls} data-has-image="true" aria-label={letter}>
        <img src={url} alt="" draggable={false} />
      </div>
    )
  }
  return <div className={cls}>{letter}</div>
}

// ─── Presence dot wrapper for top-bar avatars ───────────────

export function Presence({ children }: { children: React.ReactNode }) {
  return <div className="pres">{children}</div>
}

// ─── mm:ss duration formatter ───────────────────────────────

export function fmtT(secs: number | null | undefined): string {
  if (!secs || !Number.isFinite(secs)) return '—'
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

// ─── Emotional impact bar (−1..+1) ──────────────────────────

export function EmotionBar({ v }: { v: number }) {
  const pct = Math.abs(v) * 100
  const neg = v < 0
  const color = neg ? 'oklch(58% 0.16 20)' : 'oklch(58% 0.14 140)'
  return (
    <div className="row" style={{ width: 120, alignItems: 'center', gap: 6 }}>
      <span
        style={{
          fontFamily: 'var(--mono)',
          fontSize: 10,
          color,
        }}
      >
        {v > 0 ? '+' : ''}
        {v.toFixed(2)}
      </span>
      <div
        style={{
          flex: 1,
          height: 4,
          background: 'var(--paper-3)',
          borderRadius: 2,
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            position: 'absolute',
            left: neg ? 0 : '50%',
            right: neg ? '50%' : 0,
            top: 0,
            bottom: 0,
            background: color,
            opacity: 0.25,
          }}
        />
        <div
          style={{
            position: 'absolute',
            left: neg ? 50 - pct / 2 + '%' : '50%',
            width: pct / 2 + '%',
            top: 0,
            bottom: 0,
            background: color,
          }}
        />
      </div>
    </div>
  )
}

// ─── Brand mark (wordmark used in Setup + boot) ─────────────

export function BrandMark({ name = 'EchoVessel' }: { name?: string }) {
  return (
    <div className="brand">
      <div className="brand-mark" />
      <div className="brand-name">{name}</div>
    </div>
  )
}
