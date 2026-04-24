/**
 * useChat — hook that wraps chat send + streaming receive.
 *
 * Consumed by `<Chat.tsx>`. The hook owns the canonical timeline: a
 * flat list of `TimelineEntry`s that includes both chat messages
 * (user + persona) and session boundary markers. The boundary markers
 * are driven by the SSE `chat.session.boundary` event and let the UI
 * draw a thin divider when a session closes or a new one opens.
 *
 * Internals:
 *   - Uses `useSSE()` to receive the live stream
 *   - Tracks a flat timeline (messages + boundary markers) in one state
 *   - On `chat.message.typing_started`, inserts an empty persona
 *     placeholder with `streaming: true` so the UI can render a
 *     typing indicator ("正在输入...")
 *   - On `chat.message.done`, replaces the placeholder with the
 *     authoritative content from the server
 *   - On `chat.message.user_appended`, appends a user message — this
 *     keeps multiple browser tabs in sync when the user types from one
 *     of them
 *   - On `chat.session.boundary`, appends a `BoundaryEntry` so the
 *     timeline shows a session divider without a page refresh
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getChatHistory, postChatSend } from '../api/client'
import { ApiError } from '../api/types'
import type {
  ChatEvent,
  ChatHistoryMessage,
  MessageDelivery,
} from '../api/types'
import { useSSE } from './useSSE'

/**
 * UI-side view of a chat message. Note: distinct from the richer
 * `ChatMessage` in `src/types.ts` which carries paragraph arrays and
 * voice metadata — that type belongs to the prototype view layer.
 * `Chat.tsx` adapts between the two.
 */
export interface ChatMessage {
  /** Client-side UUID used as the React key. Stable across renders. */
  id: string
  role: 'user' | 'persona'
  content: string
  /** True between `chat.message.typing_started` and `chat.message.done`.
   *  Consumed by the UI to render a typing indicator ("正在输入..."). */
  streaming: boolean
  /**
   * Server-assigned numeric id. Only set on persona messages, and only
   * after the `chat.message.typing_started` event (or the
   * `chat.message.done` event if typing_started was skipped). User
   * messages do not carry a server id in MVP.
   */
  message_id?: number
  /** ISO-8601 timestamp (client time when the message entered state). */
  timestamp: string
  /**
   * Optional — only present on persona messages after `chat.message.done`.
   * Stage 4 uses this to pick how to render (text bubble vs voice card).
   */
  delivery?: MessageDelivery
  /** Set when `chat.message.voice_ready` arrives for this message. */
  voice_url?: string
  /**
   * Originating channel for this message. Populated from the history
   * backfill (Worker Y) so the UI can render a "📱 Discord" /
   * "🌐 Web" badge on messages that weren't typed from this tab.
   * Undefined for live messages that arrived via SSE — those are
   * implicitly on the Web channel (the tab you're watching), so
   * treat `undefined` as "web" at render time.
   */
  source_channel_id?: string
  /**
   * Spec 4 · turn id this message is part of. Set on persona bubbles
   * so the dev-mode ▸ trace entry can fetch the per-turn waterfall;
   * also populated for user history rows for completeness. Null for
   * pre-v0.3 history rows that predate the debounce turn tagging.
   */
  turn_id?: string | null
}

/**
 * Session boundary marker — rendered by `<Chat.tsx>` as a thin
 * horizontal line with a relative timestamp. Worker γ reinstated the
 * backend broadcast; the Web UI now actually consumes it.
 */
export interface BoundaryEntry {
  id: string
  kind: 'boundary'
  /** ISO-8601 timestamp from the server's `at` field. */
  timestamp: string
  closed_session_id: string | null
  new_session_id: string | null
}

export type TimelineEntry = ChatMessage | BoundaryEntry

export interface UseChatResult {
  messages: TimelineEntry[]
  send(content: string): Promise<void>
  error: string | null
  /** True when the most recent history fetch was truncated and the
   *  next "↑ 加载更早的消息" click can fetch an older page. */
  hasMoreHistory: boolean
  /** True while the initial bootstrap or a subsequent loadMore is
   *  in flight. UI renders a spinner + disables the button. */
  historyLoading: boolean
  loadMoreHistory(): Promise<void>
}

/**
 * Type guard — returns true if the entry is a boundary marker rather
 * than a real chat message. Exported so `<Chat.tsx>` can switch on it
 * at render time without importing the interfaces directly.
 */
export function isBoundaryEntry(entry: TimelineEntry): entry is BoundaryEntry {
  return (entry as BoundaryEntry).kind === 'boundary'
}

function newClientId(): string {
  return crypto.randomUUID()
}

function nowIso(): string {
  return new Date().toISOString()
}

/** Convert a server-side recall-message row into the UI's hook shape.
 *  History rows are always finalized (no streaming, no voice url at
 *  first paint — voice_ready SSE can later flip voice_url if the file
 *  is still cached). `source_channel_id` flows through so the UI can
 *  badge cross-channel messages. */
function historyToChatMessage(h: ChatHistoryMessage): ChatMessage {
  return {
    id: `hist-${h.id}`,
    role: h.role,
    content: h.content,
    streaming: false,
    message_id: h.id,
    timestamp: h.created_at ?? new Date().toISOString(),
    source_channel_id: h.source_channel_id,
    turn_id: h.turn_id,
  }
}

const HISTORY_PAGE_SIZE = 50

export function useChat(): UseChatResult {
  const [messages, setMessages] = useState<TimelineEntry[]>([])
  const [error, setError] = useState<string | null>(null)
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [historyLoading, setHistoryLoading] = useState(false)
  // The `oldest_turn_id` of the OLDEST page we've fetched so far. Used
  // as the `before=` cursor on the next loadMoreHistory() call. Kept
  // in a ref (not state) so the callback doesn't need to re-create on
  // every fetch.
  const oldestCursor = useRef<string | null>(null)
  // One-shot guard for the mount-time bootstrap: StrictMode double-
  // invokes effects in dev, and we don't want to append the history
  // twice. The ref flips on first run.
  const bootstrapped = useRef(false)

  const { subscribe } = useSSE()

  // Bootstrap: on mount, fetch the newest page of L2 history and
  // prepend it to the timeline BEFORE the SSE subscription has a
  // chance to append live events. The returned list is DESC
  // (newest-first); we reverse so chronological order (oldest → newest
  // top-to-bottom) matches how the rest of the timeline renders.
  useEffect(() => {
    if (bootstrapped.current) return
    bootstrapped.current = true
    setHistoryLoading(true)
    void getChatHistory(HISTORY_PAGE_SIZE)
      .then((resp) => {
        const ascending = [...resp.messages].reverse()
        const entries: TimelineEntry[] = ascending.map(historyToChatMessage)
        setMessages((prev) => [...entries, ...prev])
        setHasMoreHistory(resp.has_more)
        oldestCursor.current = resp.oldest_turn_id
      })
      .catch((err) => {
        // Don't fail hard — the rest of the screen still works even if
        // the backfill times out. Surface through the existing `error`
        // channel so the composer banner shows a hint.
        if (err instanceof ApiError) setError(err.detail)
        else if (err instanceof Error) setError(err.message)
      })
      .finally(() => {
        setHistoryLoading(false)
      })
  }, [])

  const loadMoreHistory = useCallback(async (): Promise<void> => {
    if (historyLoading || !hasMoreHistory) return
    const cursor = oldestCursor.current
    if (!cursor) return
    setHistoryLoading(true)
    try {
      const resp = await getChatHistory(HISTORY_PAGE_SIZE, cursor)
      const ascending = [...resp.messages].reverse()
      const entries: TimelineEntry[] = ascending.map(historyToChatMessage)
      setMessages((prev) => [...entries, ...prev])
      setHasMoreHistory(resp.has_more)
      oldestCursor.current = resp.oldest_turn_id
    } catch (err) {
      if (err instanceof ApiError) setError(err.detail)
      else if (err instanceof Error) setError(err.message)
    } finally {
      setHistoryLoading(false)
    }
  }, [historyLoading, hasMoreHistory])

  useEffect(() => {
    const unsubscribe = subscribe((event: ChatEvent) => {
      switch (event.event) {
        case 'chat.message.user_appended': {
          // Keep tabs in sync: if the user sent from another tab, append
          // the user message here too. We cannot dedupe against an
          // optimistic append because the backend does not echo our
          // external_ref back in any field the client can use to match;
          // Stage 4 proper may add a dedupe pass.
          //
          // Worker X · capture ``source_channel_id`` so Chat.tsx can
          // render a pill for cross-channel turns (Discord, …).
          setMessages((prev) => [
            ...prev,
            {
              id: newClientId(),
              role: 'user',
              content: event.data.content,
              streaming: false,
              timestamp: event.data.received_at,
              source_channel_id: event.data.source_channel_id,
            },
          ])
          return
        }

        case 'chat.message.typing_started': {
          const { message_id, source_channel_id, turn_id } = event.data
          setMessages((prev) => {
            // Idempotent: if a placeholder with this message_id is
            // already present (unlikely, but safe across reconnects),
            // don't insert a duplicate.
            const already = prev.some(
              (m) =>
                !isBoundaryEntry(m) &&
                m.role === 'persona' &&
                m.message_id === message_id,
            )
            if (already) return prev
            return [
              ...prev,
              {
                id: newClientId(),
                role: 'persona',
                content: '',
                streaming: true,
                message_id,
                timestamp: nowIso(),
                source_channel_id,
                turn_id,
              },
            ]
          })
          return
        }

        case 'chat.message.done': {
          const {
            message_id,
            content,
            delivery,
            source_channel_id,
            in_reply_to_turn_id,
          } = event.data
          setMessages((prev) => {
            const idx = prev.findIndex(
              (m) =>
                !isBoundaryEntry(m) &&
                m.role === 'persona' &&
                m.message_id === message_id,
            )
            if (idx === -1) {
              // No streaming tokens arrived — synthesise a final message
              // from the `done` payload alone.
              return [
                ...prev,
                {
                  id: newClientId(),
                  role: 'persona',
                  content,
                  streaming: false,
                  message_id,
                  timestamp: nowIso(),
                  delivery,
                  source_channel_id,
                  turn_id: in_reply_to_turn_id,
                },
              ]
            }
            const next = prev.slice()
            const existing = next[idx]
            if (!existing || isBoundaryEntry(existing)) return prev
            // Use the server's authoritative content on done to avoid
            // any client-side streaming drift.
            next[idx] = {
              ...existing,
              content,
              streaming: false,
              delivery,
              source_channel_id:
                source_channel_id ?? existing.source_channel_id,
              turn_id: in_reply_to_turn_id ?? existing.turn_id,
            }
            return next
          })
          return
        }

        case 'chat.message.error': {
          setError(event.data.error)
          // Clear any streaming placeholder for this turn so the typing
          // indicator doesn't hang next to an error banner. If
          // message_id is non-null, drop just that placeholder; if null,
          // drop any still-streaming persona message as a conservative
          // fallback (there is at most one at any time).
          const errorMessageId = event.data.message_id
          setMessages((prev) =>
            prev.filter((m) => {
              if (isBoundaryEntry(m)) return true
              if (m.role !== 'persona') return true
              if (!m.streaming) return true
              if (errorMessageId !== null && m.message_id !== errorMessageId) {
                return true
              }
              return false
            }),
          )
          return
        }

        case 'chat.message.voice_ready': {
          const { message_id, url } = event.data
          setMessages((prev) => {
            const idx = prev.findIndex(
              (m) =>
                !isBoundaryEntry(m) &&
                m.role === 'persona' &&
                m.message_id === message_id,
            )
            if (idx === -1) return prev
            const next = prev.slice()
            const existing = next[idx]
            if (!existing || isBoundaryEntry(existing)) return prev
            next[idx] = { ...existing, voice_url: url }
            return next
          })
          return
        }

        case 'chat.session.boundary': {
          // Append a boundary marker to the timeline. The backend fires
          // two separate events when a session flips (one on
          // on_session_closed, one on on_new_session_started). We append
          // both — the rendered divider is idempotent-looking even when
          // they land back-to-back.
          const { closed_session_id, new_session_id, at } = event.data
          setMessages((prev) => [
            ...prev,
            {
              id: newClientId(),
              kind: 'boundary',
              timestamp: at || nowIso(),
              closed_session_id,
              new_session_id,
            },
          ])
          return
        }

        default:
          // Other known events (connection, settings, mood.update) are
          // handled by other hooks.
          return
      }
    })

    return unsubscribe
  }, [subscribe])

  const send = useCallback(async (content: string): Promise<void> => {
    if (content.length === 0) return
    setError(null)

    // Do NOT optimistically append: the SSE `chat.message.user_appended`
    // echo is the single source of truth. An optimistic append here would
    // double the user message because the echo always arrives.
    try {
      await postChatSend({ content, user_id: 'self' })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.detail)
      } else if (err instanceof Error) {
        setError(err.message)
      } else {
        setError('send failed')
      }
    }
  }, [])

  return {
    messages,
    send,
    error,
    hasMoreHistory,
    historyLoading,
    loadMoreHistory,
  }
}
