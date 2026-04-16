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
 *   - Streams persona replies token-by-token via `chat.message.token`
 *   - On `chat.message.done`, finalises the persona message with the
 *     authoritative content from the server (the accumulated token
 *     buffer is discarded in favour of `done.content` to avoid any
 *     client-side drift)
 *   - On `chat.message.user_appended`, appends a user message — this
 *     keeps multiple browser tabs in sync when the user types from one
 *     of them
 *   - On `chat.session.boundary`, appends a `BoundaryEntry` so the
 *     timeline shows a session divider without a page refresh
 */

import { useCallback, useEffect, useState } from 'react'
import { postChatSend } from '../api/client'
import { ApiError } from '../api/types'
import type { ChatEvent, MessageDelivery } from '../api/types'
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
  /** True while `chat.message.token` events are still arriving. */
  streaming: boolean
  /**
   * Server-assigned numeric id. Only set on persona messages, and only
   * after the first `chat.message.token` event arrives (or the
   * `chat.message.done` event if streaming was skipped). User messages
   * do not carry a server id in MVP.
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

export function useChat(): UseChatResult {
  const [messages, setMessages] = useState<TimelineEntry[]>([])
  const [error, setError] = useState<string | null>(null)

  const { subscribe } = useSSE()

  useEffect(() => {
    const unsubscribe = subscribe((event: ChatEvent) => {
      switch (event.event) {
        case 'chat.message.user_appended': {
          // Keep tabs in sync: if the user sent from another tab, append
          // the user message here too. We cannot dedupe against an
          // optimistic append because the backend does not echo our
          // external_ref back in any field the client can use to match;
          // Stage 4 proper may add a dedupe pass.
          setMessages((prev) => [
            ...prev,
            {
              id: newClientId(),
              role: 'user',
              content: event.data.content,
              streaming: false,
              timestamp: event.data.received_at,
            },
          ])
          return
        }

        case 'chat.message.token': {
          const { message_id, delta } = event.data
          setMessages((prev) => {
            // Find an existing persona message with this id and append
            // the delta. If none exists yet (first token of the turn),
            // create one.
            const idx = prev.findIndex(
              (m) =>
                !isBoundaryEntry(m) &&
                m.role === 'persona' &&
                m.message_id === message_id,
            )
            if (idx === -1) {
              return [
                ...prev,
                {
                  id: newClientId(),
                  role: 'persona',
                  content: delta,
                  streaming: true,
                  message_id,
                  timestamp: nowIso(),
                },
              ]
            }
            const next = prev.slice()
            const existing = next[idx]
            if (!existing || isBoundaryEntry(existing)) return prev
            next[idx] = { ...existing, content: existing.content + delta }
            return next
          })
          return
        }

        case 'chat.message.done': {
          const { message_id, content, delivery } = event.data
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
            }
            return next
          })
          return
        }

        case 'chat.message.error': {
          setError(event.data.error)
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

  return { messages, send, error }
}
