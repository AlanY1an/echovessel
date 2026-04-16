import { useState } from 'react'
import { TopBar } from '../components/TopBar'
import { useChat, isBoundaryEntry } from '../hooks/useChat'
import type {
  BoundaryEntry,
  ChatMessage as HookMessage,
  TimelineEntry,
} from '../hooks/useChat'
import type {
  ChatMessage as PrototypeMessage,
  VoiceMeta,
} from '../types'

interface ChatProps {
  moodBlock: string
  onOpenAdmin: () => void
}

export function Chat({ moodBlock, onOpenAdmin }: ChatProps) {
  const { messages, send, error } = useChat()
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)

  const moodSummary = moodBlock.trim().split(/[\n。]/)[0] || '平静、愿意倾听'

  const handleSend = async () => {
    const text = draft.trim()
    if (text.length === 0 || sending) return
    setDraft('')
    setSending(true)
    try {
      await send(text)
    } finally {
      setSending(false)
    }
  }

  // Adapt each hook entry into either a prototype-shape chat message or
  // a BoundaryEntry (passed through unchanged) so we can render both
  // kinds from a single map below.
  const timeline: RenderedEntry[] = messages.map(toRendered)

  return (
    <div className="chat-wrap">
      <TopBar
        mood={moodSummary}
        primary={{ label: 'Admin', onClick: onOpenAdmin }}
      />

      <main className="chat-main">
        {timeline.length === 0 && <EmptyState />}
        {timeline.map((entry, idx) => {
          if (entry.kind === 'boundary') {
            return <SessionBoundary key={entry.data.id} entry={entry.data} />
          }
          return (
            <Exchange
              key={entry.data.id}
              message={entry.data}
              index={idx}
              isFirstOfTurn={isFirstOfTurn(timeline, idx)}
            />
          )
        })}
      </main>

      <div className="composer">
        <div className="composer-inner">
          {error !== null && (
            <div
              className="composer-field"
              style={{
                justifyContent: 'center',
                color: 'rgba(255, 120, 120, 0.78)',
                fontSize: 13,
                marginBottom: 8,
                background: 'rgba(255, 80, 80, 0.08)',
              }}
            >
              ⚠ {error}
            </div>
          )}
          <div className="composer-field">
            <input
              className="composer-input"
              type="text"
              placeholder="想说点什么⋯"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && draft.trim()) void handleSend()
              }}
              disabled={sending}
            />
            <button
              type="button"
              className="composer-send"
              onClick={() => void handleSend()}
              disabled={sending || draft.trim().length === 0}
            >
              {sending ? '⋯' : '发送'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Adapter: hook shape → prototype shape ──────────────────────────────
//
// Why adapter (not rewrite):
//   The prototype's render tree (Exchange / YouBubble / Letter /
//   VoiceButton) was designed around the prototype's rich ChatMessage
//   shape (paragraph array + VoiceMeta). Rewriting to render flat
//   strings would throw away the typography (paragraph breaks, letter
//   layout, cursor-on-last-line streaming indicator). An adapter
//   function is ~15 lines and keeps the whole render subtree intact.
//
// `RenderedEntry` is a discriminated union at the render layer only —
// it lets the timeline.map render boundaries and messages side-by-side
// without leaking the hook's `TimelineEntry` shape past the Chat.tsx
// boundary.

type RenderedEntry =
  | { kind: 'message'; data: PrototypeMessage }
  | { kind: 'boundary'; data: BoundaryEntry }

function toRendered(entry: TimelineEntry): RenderedEntry {
  if (isBoundaryEntry(entry)) {
    return { kind: 'boundary', data: entry }
  }
  return { kind: 'message', data: toPrototypeShape(entry) }
}

function toPrototypeShape(m: HookMessage): PrototypeMessage {
  return {
    id: m.id,
    // Prototype roles are 'you' (user) / 'them' (persona). Hook roles
    // are 'user' / 'persona'. Straight mapping.
    role: m.role === 'user' ? 'you' : 'them',
    // Use message_id when available (stable within a turn) otherwise
    // fall back to the client uuid. No true turn-grouping exists in
    // v1 — every hook message is its own turn for display purposes.
    turnId: m.message_id !== undefined ? `srv-${m.message_id}` : m.id,
    timestampLabel: formatTimestamp(m.timestamp),
    // Split on blank-line paragraph breaks to keep the letter-style
    // rendering. Fallback to a single paragraph for short replies.
    content: splitParagraphs(m.content),
    streaming: m.streaming,
    // Voice: if the hook message carries a voice_url (from
    // chat.message.voice_ready SSE), surface it through the prototype's
    // VoiceMeta shape so the VoiceButton component renders a real
    // <audio> player.
    voice: m.voice_url
      ? { duration: '', toneLabel: '她的声音', url: m.voice_url }
      : undefined,
  }
}

function splitParagraphs(content: string): string[] {
  if (content.length === 0) return ['']
  // Prefer double-newline paragraph breaks; fall back to single-line
  // splits for short streaming replies so the cursor blink lands on
  // the correct line.
  const paras = content.split(/\n{2,}/).map((p) => p.trim()).filter((p) => p.length > 0)
  if (paras.length === 0) return [content]
  return paras
}

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    const hh = d.getHours().toString().padStart(2, '0')
    const mm = d.getMinutes().toString().padStart(2, '0')
    return `${hh}:${mm}`
  } catch {
    return ''
  }
}

function isFirstOfTurn(
  timeline: RenderedEntry[],
  idx: number,
): boolean {
  if (idx === 0) return true
  const prev = timeline[idx - 1]
  const curr = timeline[idx]
  if (!prev || !curr) return true
  // A boundary always starts a fresh visual turn on its neighbours.
  if (prev.kind === 'boundary' || curr.kind === 'boundary') return true
  return prev.data.turnId !== curr.data.turnId
}

// ─── Session boundary marker ────────────────────────────────────────────
//
// Rendered as a thin horizontal line with a relative timestamp
// ("2 分钟前" / "1 小时前" / ...). Intentionally quiet — boundaries
// should feel like a page break, not an event.

function SessionBoundary({ entry }: { entry: BoundaryEntry }) {
  const relative = formatRelativeTime(entry.timestamp)
  return (
    <div
      className="session-boundary"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        margin: '24px 0',
        color: 'rgba(255, 255, 255, 0.32)',
        fontSize: 12,
        letterSpacing: '0.1em',
      }}
    >
      <div
        style={{
          flex: 1,
          height: 1,
          background:
            'linear-gradient(to right, transparent, rgba(255,255,255,0.12), transparent)',
        }}
      />
      <span style={{ whiteSpace: 'nowrap' }}>
        ── {relative} ──
      </span>
      <div
        style={{
          flex: 1,
          height: 1,
          background:
            'linear-gradient(to right, transparent, rgba(255,255,255,0.12), transparent)',
        }}
      />
    </div>
  )
}

function formatRelativeTime(iso: string): string {
  try {
    const ts = new Date(iso).getTime()
    if (Number.isNaN(ts)) return '——'
    const diffMs = Date.now() - ts
    if (diffMs < 60_000) return '刚刚'
    const minutes = Math.floor(diffMs / 60_000)
    if (minutes < 60) return `${minutes} 分钟前`
    const hours = Math.floor(minutes / 60)
    if (hours < 24) return `${hours} 小时前`
    const days = Math.floor(hours / 24)
    if (days < 7) return `${days} 天前`
    // Fall back to an absolute MM-DD for older boundaries.
    const d = new Date(iso)
    const mm = (d.getMonth() + 1).toString().padStart(2, '0')
    const dd = d.getDate().toString().padStart(2, '0')
    return `${mm}-${dd}`
  } catch {
    return '——'
  }
}

// ─── Empty-state + render components (unchanged logic, trimmed) ─────────

function EmptyState() {
  return (
    <div
      style={{
        textAlign: 'center',
        color: 'rgba(255, 255, 255, 0.38)',
        fontSize: 14,
        letterSpacing: '0.06em',
        padding: '120px 32px 0',
        lineHeight: 1.8,
      }}
    >
      还没有消息。
      <br />
      随便说点什么开始吧。
    </div>
  )
}

interface ExchangeProps {
  message: PrototypeMessage
  index: number
  isFirstOfTurn: boolean
}

function Exchange({ message, index, isFirstOfTurn }: ExchangeProps) {
  const sideClass = message.role === 'you' ? 'you' : 'them'
  const turnClass = isFirstOfTurn ? 'turn-break' : ''
  const delay = Math.min(index * 0.08, 1.2)

  return (
    <div
      className={`exchange ${sideClass} ${turnClass}`.trim()}
      style={{ animationDelay: `${delay}s` }}
    >
      <div className="msg-wrap">
        {message.timestampLabel && (
          <div className="meta">{message.timestampLabel}</div>
        )}
        {message.role === 'you' ? (
          <YouBubble content={message.content} />
        ) : (
          <Letter
            content={message.content}
            voice={message.voice}
            streaming={message.streaming}
          />
        )}
      </div>
    </div>
  )
}

function YouBubble({ content }: { content: string[] }) {
  return (
    <div className="you-bubble">
      {content.map((p, i) => (
        <p key={i}>{p}</p>
      ))}
    </div>
  )
}

interface LetterProps {
  content: string[]
  voice?: VoiceMeta
  streaming?: boolean
}

function Letter({ content, voice, streaming }: LetterProps) {
  return (
    <div className="letter">
      {content.map((p, i) => {
        const isLast = i === content.length - 1
        return (
          <p key={i}>
            {p}
            {streaming && isLast && <span className="cursor" />}
          </p>
        )
      })}
      {voice && <VoiceButton meta={voice} />}
    </div>
  )
}

function VoiceButton({ meta }: { meta: VoiceMeta }) {
  const [playing, setPlaying] = useState(false)
  const [audioEl] = useState(() => {
    if (typeof Audio === 'undefined') return null
    return new Audio()
  })

  const handleClick = () => {
    if (!audioEl) return
    if (playing) {
      audioEl.pause()
      audioEl.currentTime = 0
      setPlaying(false)
      return
    }
    if (meta.url) {
      audioEl.src = meta.url
      audioEl.onended = () => setPlaying(false)
      audioEl.onerror = () => setPlaying(false)
      audioEl.play().catch(() => setPlaying(false))
      setPlaying(true)
    }
  }

  if (!meta.url) return null

  return (
    <button type="button" className="voice" onClick={handleClick}>
      <div className="voice-dot">
        <svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg">
          <path d="M2 1 L10 6 L2 11 Z" />
        </svg>
      </div>
      <div className="voice-label">
        {playing ? '播放中 ' : '语音 '}
        <em>{playing ? '⋯' : meta.toneLabel}</em>
      </div>
    </button>
  )
}

