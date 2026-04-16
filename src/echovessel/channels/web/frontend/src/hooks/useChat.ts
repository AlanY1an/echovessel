/**
 * useChat — hook that wraps chat send + streaming receive.
 *
 * Stage 4 proper will call this from <Chat.tsx> to replace the
 * localStorage-backed mock. Component flow:
 *
 *   const { messages, send, error } = useChat()
 *   // render messages as a timeline
 *   // wire send() to the input box onSubmit
 *
 * Internals:
 *   - Uses `useSSE()` to receive the live stream
 *   - Tracks a flat message list (both user and persona turns)
 *   - Streams persona replies token-by-token via `chat.message.token`
 *   - On `chat.message.done`, finalises the persona message with the
 *     authoritative content from the server (the accumulated token
 *     buffer is discarded in favour of `done.content` to avoid any
 *     client-side drift)
 *   - On `chat.message.user_appended`, appends a user message — this
 *     keeps multiple browser tabs in sync when the user types from one
 *     of them
 *
 * This file is Stage 4-prep only — components are not yet wired.
 */

import { useCallback, useEffect, useState } from 'react'
import { postChatSend } from '../api/client'
import { ApiError } from '../api/types'
import type { ChatEvent, MessageDelivery } from '../api/types'
import { useSSE } from './useSSE'

/**
 * UI-side view of a chat message. Note: distinct from the richer
 * `ChatMessage` in `src/types.ts` which carries paragraph arrays and
 * voice metadata — that type belongs to the prototype view layer. Stage
 * 4 proper will adapt between the two where needed.
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

export interface UseChatResult {
  messages: ChatMessage[]
  send(content: string): Promise<void>
  error: string | null
}

function newClientId(): string {
  return crypto.randomUUID()
}

function nowIso(): string {
  return new Date().toISOString()
}

export function useChat(): UseChatResult {
  const [messages, setMessages] = useState<ChatMessage[]>([])
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
              (m) => m.role === 'persona' && m.message_id === message_id,
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
            if (!existing) return prev
            next[idx] = { ...existing, content: existing.content + delta }
            return next
          })
          return
        }

        case 'chat.message.done': {
          const { message_id, content, delivery } = event.data
          setMessages((prev) => {
            const idx = prev.findIndex(
              (m) => m.role === 'persona' && m.message_id === message_id,
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
            if (!existing) return prev
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
              (m) => m.role === 'persona' && m.message_id === message_id,
            )
            if (idx === -1) return prev
            const next = prev.slice()
            const existing = next[idx]
            if (!existing) return prev
            next[idx] = { ...existing, voice_url: url }
            return next
          })
          return
        }

        default:
          // Other known events (connection, settings) are handled by
          // other hooks.
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
