import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useChat, isBoundaryEntry } from '../hooks/useChat'
import type {
  BoundaryEntry,
  ChatMessage as HookMessage,
  TimelineEntry,
} from '../hooks/useChat'
import { avatarUrl, postVoicePreview } from '../api/client'
import { Avatar, Presence, Wave, fmtT } from '../components/primitives'
import { LanguageToggle } from '../components/LanguageToggle'

interface ChatProps {
  moodBlock: string
  displayName: string
  voiceEnabled: boolean
  voiceId: string | null
  hasAvatar: boolean
  onOpenAdmin: () => void
}

type TFn = (key: string, opts?: Record<string, unknown>) => string

export function Chat({
  moodBlock,
  displayName,
  voiceEnabled,
  voiceId,
  hasAvatar,
  onOpenAdmin,
}: ChatProps) {
  const { t } = useTranslation()
  const canPlayVoice = voiceEnabled && voiceId !== null && voiceId.length > 0
  // Version-pinned avatar URL. Pinning to display_name makes the img
  // reload when Admin edits land via usePersona refresh; a separate
  // counter isn't needed for the MVP.
  const personaAvatar = hasAvatar ? avatarUrl(displayName) : null
  const {
    messages,
    send,
    error,
    hasMoreHistory,
    historyLoading,
    loadMoreHistory,
  } = useChat()
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [journalOpen, setJournalOpen] = useState(false)
  const [journalText, setJournalText] = useState('')
  const logRef = useRef<HTMLDivElement | null>(null)

  const moodSummary = moodBlock.trim().split(/[\n。]/)[0] || t('chat.mood_default')

  // Auto-scroll to bottom whenever the timeline grows. Using scrollHeight
  // is sufficient — the log is the flex column owner and its own scroll
  // container.
  useEffect(() => {
    const el = logRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages])

  const handleSend = async (textOverride?: string) => {
    const text = (textOverride ?? draft).trim()
    if (text.length === 0 || sending) return
    if (textOverride === undefined) setDraft('')
    setSending(true)
    try {
      await send(text)
    } finally {
      setSending(false)
    }
  }

  const sendJournal = async () => {
    const text = journalText.trim()
    if (text.length === 0 || sending) return
    setJournalText('')
    setJournalOpen(false)
    // MVP choice: send journal text as a plain message — no prefix
    // marker. The composer UX already makes the intent explicit; adding
    // a synthetic "[journal]" prefix would leak into the persona's
    // memory as literal text with no corresponding extraction path on
    // the backend yet.
    await handleSend(text)
  }

  // Build a flat render list interleaving day chips, boundaries, and
  // messages. Day bucketing is purely presentational — the hook's
  // timeline carries no day info.
  const rendered = useMemo(() => buildRenderList(messages, t), [messages, t])

  const showLoadMore = hasMoreHistory || historyLoading

  return (
    <div className="chat">
      <div className="chat-top">
        <div className="brand" style={{ paddingBottom: 0, gap: 8 }}>
          <div className="brand-mark" style={{ width: 18, height: 18 }} />
        </div>
        <Presence>
          <Avatar letter={initialOf(displayName)} url={personaAvatar} />
        </Presence>
        <div className="stack" style={{ gap: 0 }}>
          <div className="name">{displayName}</div>
          <div className="sub">
            {t('chat.persona_sub', { mood: moodSummary })}
          </div>
        </div>
        <div className="flex1" />
        <span className="chip">
          {t('chat.chip_mood', { mood: moodSummary })}
        </span>
        <span className="chip">{t('chat.chip_session')}</span>
        <LanguageToggle />
        <button
          type="button"
          className="btn ghost sm"
          onClick={onOpenAdmin}
        >
          {t('topbar.admin')}
        </button>
      </div>

      <div className="chat-log" ref={logRef}>
        {showLoadMore && (
          <button
            type="button"
            className="load-more"
            onClick={() => void loadMoreHistory()}
            disabled={historyLoading || !hasMoreHistory}
          >
            {historyLoading
              ? t('chat.loading')
              : hasMoreHistory
                ? t('chat.load_more')
                : t('chat.at_oldest')}
          </button>
        )}

        {rendered.map((entry, idx) => {
          if (entry.kind === 'day') {
            return (
              <div key={entry.key} className="date-chip">
                {entry.label}
              </div>
            )
          }
          if (entry.kind === 'boundary') {
            return <SessionBoundary key={entry.data.id} entry={entry.data} t={t} />
          }
          // Collapse adjacent same-sender rows so only the first of a
          // run renders the avatar gutter — keeps bubbles tight like
          // iMessage / Telegram.
          const prev = idx > 0 ? rendered[idx - 1] : null
          const leadPersonaRow =
            entry.data.role === 'persona' &&
            (!prev || prev.kind !== 'message' || prev.data.role !== 'persona')
          return (
            <MessageRow
              key={entry.data.id}
              m={entry.data}
              seed={idx}
              t={t}
              voiceId={canPlayVoice ? voiceId : null}
              avatarUrl={leadPersonaRow ? personaAvatar : null}
              avatarLetter={leadPersonaRow ? initialOf(displayName) : null}
            />
          )
        })}

        {rendered.length === 0 && !historyLoading && <EmptyState />}
      </div>

      <div className="composer">
        <div className="composer-inner">
          {error !== null && (
            <div className="chat-error" role="alert">
              ⚠ {error}
            </div>
          )}

          {journalOpen && (
            <div className="journal-panel">
              <div className="row g-2" style={{ alignItems: 'center' }}>
                <span className="label" style={{ color: 'var(--accent)' }}>
                  {t('chat.journal_label')}
                </span>
                <div className="flex1" />
                <button
                  type="button"
                  className="btn ghost sm"
                  onClick={() => setJournalOpen(false)}
                  aria-label={t('admin.common.close')}
                >
                  ✕
                </button>
              </div>
              <textarea
                className="bare"
                rows={5}
                autoFocus
                placeholder={t('chat.journal_placeholder')}
                value={journalText}
                onChange={(e) => setJournalText(e.target.value)}
                style={{ fontSize: 15, lineHeight: 1.6 }}
              />
              <div className="row" style={{ alignItems: 'center' }}>
                <div className="flex1" />
                <button
                  type="button"
                  className="btn accent"
                  onClick={() => void sendJournal()}
                  disabled={sending || journalText.trim().length === 0}
                >
                  {t('chat.journal_send')} →
                </button>
              </div>
            </div>
          )}

          <div className="composer-field">
            <button
              type="button"
              className="icbtn"
              onClick={() => setJournalOpen((v) => !v)}
              aria-label={t('chat.journal_toggle_aria')}
            >
              +
            </button>
            <textarea
              rows={1}
              placeholder={t('chat.placeholder')}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void handleSend()
                }
              }}
              disabled={sending}
            />
            <button
              type="button"
              className="icbtn"
              aria-label={t('chat.voice_input_aria')}
              // Voice input is a v1 feature — render the icon now so the
              // composer layout matches the prototype, but no-op on
              // click to avoid the appearance of a broken control.
              disabled
            >
              ◉
            </button>
          </div>
          <button
            type="button"
            className="sendbtn"
            onClick={() => void handleSend()}
            disabled={sending || draft.trim().length === 0}
            aria-label={t('chat.send')}
          >
            ↑
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Render-list builder ─────────────────────────────────────

type RenderEntry =
  | { kind: 'day'; key: string; label: string }
  | { kind: 'boundary'; data: BoundaryEntry }
  | { kind: 'message'; data: HookMessage }

function buildRenderList(
  messages: TimelineEntry[],
  t: TFn,
): RenderEntry[] {
  const out: RenderEntry[] = []
  let currentDayKey: string | null = null
  for (const entry of messages) {
    if (!isBoundaryEntry(entry)) {
      const dayKey = toDayKey(entry.timestamp)
      if (dayKey !== currentDayKey) {
        out.push({
          kind: 'day',
          key: `day-${dayKey}-${entry.id}`,
          label: formatDayLabel(entry.timestamp, t),
        })
        currentDayKey = dayKey
      }
      out.push({ kind: 'message', data: entry })
    } else {
      out.push({ kind: 'boundary', data: entry })
    }
  }
  return out
}

function toDayKey(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return 'unknown'
    return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
  } catch {
    return 'unknown'
  }
}

function formatDayLabel(iso: string, t: TFn): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    const now = new Date()
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    if (sameDay) return t('chat.day_today')
    const yesterday = new Date(now)
    yesterday.setDate(now.getDate() - 1)
    const isYesterday =
      d.getFullYear() === yesterday.getFullYear() &&
      d.getMonth() === yesterday.getMonth() &&
      d.getDate() === yesterday.getDate()
    if (isYesterday) return t('chat.day_yesterday')
    const mm = (d.getMonth() + 1).toString().padStart(2, '0')
    const dd = d.getDate().toString().padStart(2, '0')
    return `${d.getFullYear()}-${mm}-${dd}`
  } catch {
    return ''
  }
}

// ─── Row components ──────────────────────────────────────────

function initialOf(name: string): string {
  const trimmed = name.trim()
  if (trimmed.length === 0) return 'A'
  return trimmed[0]!.toUpperCase()
}

function MessageRow({
  m,
  seed,
  t,
  voiceId,
  avatarUrl,
  avatarLetter,
}: {
  m: HookMessage
  seed: number
  t: TFn
  voiceId: string | null
  /** When non-null, the row renders a persona avatar in its left
   *  gutter. Callers pass null for every non-leading persona row in a
   *  run so only the first bubble of a burst carries the picture. */
  avatarUrl: string | null
  avatarLetter: string | null
}) {
  const side = m.role === 'user' ? 'me' : 'them'

  // Typing bubble: persona-side streaming placeholder with no content
  // yet. Rendered as a bare `.typing` element (no outer `.bubble` —
  // `.typing` is its own bubble-equivalent in the design system).
  const isTypingPlaceholder =
    m.role === 'persona' && m.streaming === true && m.content === ''

  // Persona rows always reserve the avatar gutter so bubbles stay
  // vertically aligned in a run; only the lead row paints the picture.
  const showGutter = m.role === 'persona'
  const renderAvatar = avatarLetter !== null

  if (isTypingPlaceholder) {
    return (
      <div className={`msg-row ${side}`}>
        {showGutter && (
          <div className="msg-gutter">
            {renderAvatar && (
              <Avatar letter={avatarLetter ?? 'A'} size="sm" url={avatarUrl} />
            )}
          </div>
        )}
        <div className={`msg ${side}`}>
          <div className="typing" aria-live="polite">
            <i />
            <i />
            <i />
          </div>
        </div>
      </div>
    )
  }

  const time = formatTime(m.timestamp)
  const source = sourceLabel(m.source_channel_id, t)

  // Voice bubble is reserved for messages that arrived with a ready-made
  // audio URL via the live `voice_ready` SSE event. If voice is enabled
  // but no URL exists yet (typical for history rows), we still render
  // the normal text bubble and offer an on-demand 🔊 button beside it.
  if (m.voice_url) {
    return (
      <div className={`msg-row ${side}`}>
        {showGutter && (
          <div className="msg-gutter">
            {renderAvatar && (
              <Avatar letter={avatarLetter ?? 'A'} size="sm" url={avatarUrl} />
            )}
          </div>
        )}
        <div className={`msg ${side}`}>
          <VoiceBubble url={m.voice_url} seed={seed} />
          <div className="meta">
            {time && <span>{time}</span>}
            {time && source && <span>·</span>}
            {source && <span>{source}</span>}
          </div>
        </div>
      </div>
    )
  }

  const offerVoice =
    m.role === 'persona' && voiceId !== null && m.content.trim().length > 0

  return (
    <div className={`msg-row ${side}`}>
      {showGutter && (
        <div className="msg-gutter">
          {renderAvatar && (
            <Avatar letter={avatarLetter ?? 'A'} size="sm" url={avatarUrl} />
          )}
        </div>
      )}
      <div className={`msg ${side}`}>
        <div className="bubble">
          {m.content}
          {offerVoice && <OnDemandVoice text={m.content} voiceId={voiceId} />}
        </div>
        <div className="meta">
          {time && <span>{time}</span>}
          {time && source && <span>·</span>}
          {source && <span>{source}</span>}
        </div>
      </div>
    </div>
  )
}

function VoiceBubble({ url, seed }: { url: string; seed: number }) {
  const { t } = useTranslation()
  const [playing, setPlaying] = useState(false)
  const [audioEl] = useState(() =>
    typeof Audio === 'undefined' ? null : new Audio(),
  )

  const toggle = () => {
    if (!audioEl || !url) return
    if (playing) {
      audioEl.pause()
      audioEl.currentTime = 0
      setPlaying(false)
      return
    }
    audioEl.src = url
    audioEl.onended = () => setPlaying(false)
    audioEl.onerror = () => setPlaying(false)
    audioEl.play().catch(() => setPlaying(false))
    setPlaying(true)
  }

  return (
    <div className="bubble voice">
      <button
        type="button"
        className="play"
        onClick={toggle}
        aria-label={playing ? t('chat.voice_playing') : t('chat.voice_label')}
      >
        {playing ? '❚❚' : '▶'}
      </button>
      <div className="wave">
        <Wave bars={36} seed={seed} />
      </div>
      <div className="dur">{fmtT(null)}</div>
    </div>
  )
}

/**
 * Inline 🔊 affordance attached to a persona text bubble. On click we
 * POST /api/admin/voice/preview with the bubble's text and the active
 * voice_id, blob-URL the returned MP3, and play it. Each bubble caches
 * its generated URL so re-clicks don't re-bill TTS.
 */
function OnDemandVoice({
  text,
  voiceId,
}: {
  text: string
  voiceId: string
}) {
  const { t } = useTranslation()
  const [state, setState] = useState<'idle' | 'loading' | 'playing' | 'error'>(
    'idle',
  )
  const urlRef = useRef<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    return () => {
      if (urlRef.current !== null) URL.revokeObjectURL(urlRef.current)
    }
  }, [])

  const click = async () => {
    if (state === 'loading') return
    if (state === 'playing') {
      audioRef.current?.pause()
      if (audioRef.current) audioRef.current.currentTime = 0
      setState('idle')
      return
    }
    try {
      if (urlRef.current === null) {
        setState('loading')
        const blob = await postVoicePreview(voiceId, text)
        urlRef.current = URL.createObjectURL(blob)
      }
      const el = audioRef.current ?? new Audio()
      audioRef.current = el
      el.src = urlRef.current
      el.onended = () => setState('idle')
      el.onerror = () => setState('error')
      await el.play()
      setState('playing')
    } catch {
      setState('error')
    }
  }

  const label =
    state === 'playing'
      ? t('chat.voice_playing')
      : state === 'loading'
        ? t('chat.voice_loading')
        : state === 'error'
          ? t('chat.voice_error')
          : t('chat.voice_play_aria')

  return (
    <button
      type="button"
      className="msg-voice-btn"
      onClick={() => void click()}
      aria-label={label}
      title={label}
    >
      {state === 'playing' ? '❚❚' : state === 'loading' ? '⋯' : '♪'}
    </button>
  )
}

function SessionBoundary({
  entry,
  t,
}: {
  entry: BoundaryEntry
  t: TFn
}) {
  const relative = formatRelativeTime(entry.timestamp, t)
  const label =
    entry.closed_session_id !== null
      ? t('chat.boundary_session_ended')
      : t('chat.boundary_session_started')
  return (
    <div className="session-boundary">
      <span>{label}</span>
      <span>·</span>
      <span>{relative}</span>
    </div>
  )
}

function EmptyState() {
  const { t } = useTranslation()
  return (
    <div
      style={{
        textAlign: 'center',
        color: 'var(--ink-3)',
        fontSize: 13,
        letterSpacing: '0.04em',
        padding: '80px 24px 0',
        lineHeight: 1.8,
      }}
    >
      {t('chat.empty')}
    </div>
  )
}

// ─── Formatters ──────────────────────────────────────────────

function formatTime(iso: string): string {
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

function sourceLabel(sourceChannelId: string | undefined, t: TFn): string {
  if (!sourceChannelId || sourceChannelId === 'web') return t('chat.source_web')
  if (sourceChannelId.startsWith('discord')) return t('chat.source_discord')
  if (sourceChannelId.startsWith('imessage')) return t('chat.source_imessage')
  if (sourceChannelId.startsWith('wechat')) return '💭 WeChat'
  return sourceChannelId
}

function formatRelativeTime(iso: string, t: TFn): string {
  try {
    const ts = new Date(iso).getTime()
    if (Number.isNaN(ts)) return '——'
    const diffMs = Date.now() - ts
    if (diffMs < 60_000) return t('time.just_now')
    const minutes = Math.floor(diffMs / 60_000)
    if (minutes < 60) return t('time.minutes_ago', { count: minutes })
    const hours = Math.floor(minutes / 60)
    if (hours < 24) return t('time.hours_ago', { count: hours })
    const days = Math.floor(hours / 24)
    if (days < 7) return t('time.days_ago', { count: days })
    const d = new Date(iso)
    const mm = (d.getMonth() + 1).toString().padStart(2, '0')
    const dd = d.getDate().toString().padStart(2, '0')
    return `${mm}-${dd}`
  } catch {
    return '——'
  }
}
