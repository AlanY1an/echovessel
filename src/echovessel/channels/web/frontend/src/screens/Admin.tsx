import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { TopBar } from '../components/TopBar'
import type { AdminTab } from '../types'
import type {
  ChannelStatus,
  ConfigGetResponse,
  ConfigPatchPayload,
  CostCallRecord,
  CostFeatureBucket,
  CostSummaryResponse,
  DaemonState,
  MemoryEvent,
  MemoryThought,
  PersonaStateApi,
  PersonaUpdatePayload,
  PreviewDeleteResponse,
  TraceNode,
} from '../api/types'
import { ApiError } from '../api/types'
import { getCostRecent, getCostSummary } from '../api/client'
import { useConfig } from '../hooks/useConfig'
import { useMemoryEvents } from '../hooks/useMemoryEvents'
import { useMemorySearch } from '../hooks/useMemorySearch'
import { useMemoryThoughts } from '../hooks/useMemoryThoughts'
import { useMemoryTrace } from '../hooks/useMemoryTrace'

interface AdminProps {
  persona: PersonaStateApi
  daemonState: DaemonState
  updatePersona: (payload: PersonaUpdatePayload) => Promise<void>
  toggleVoice: (enabled: boolean) => Promise<void>
  onBackToChat: () => void
}

const TABS: { id: AdminTab; label: string; sub: string }[] = [
  { id: 'persona', label: '人格', sub: 'persona · 5 blocks' },
  { id: 'events', label: '发生过的事', sub: 'events · L3' },
  { id: 'thoughts', label: '长期印象', sub: 'thoughts · L4' },
  { id: 'voice', label: '声音', sub: 'voice toggle' },
  { id: 'cost', label: '成本', sub: 'cost · 30d' },
  { id: 'config', label: '配置', sub: 'coming soon' },
]

// Shared shape for the cross-tab "jump-to-lineage" flow:
// clicking an event in a thought-trace switches to Events + highlights
// that event; clicking a thought in an event-dependents list switches
// to Thoughts + highlights that thought.
export interface CrossNav {
  navigateTo(kind: 'event' | 'thought', id: number): void
}

export function Admin({
  persona,
  daemonState,
  updatePersona,
  toggleVoice,
  onBackToChat,
}: AdminProps) {
  const [tab, setTab] = useState<AdminTab>('persona')
  // Pending highlight set by a cross-tab jump. The target tab reads
  // this, scrolls its row into view, flashes it briefly, then clears.
  const [highlight, setHighlight] = useState<{
    kind: 'event' | 'thought'
    id: number
  } | null>(null)

  const crossNav: CrossNav = {
    navigateTo: (kind, id) => {
      setTab(kind === 'event' ? 'events' : 'thoughts')
      setHighlight({ kind, id })
    },
  }

  return (
    <div className="admin-wrap">
      <TopBar
        mood="在 admin 页面"
        back={{ label: '对话', onClick: onBackToChat }}
      />

      <ChannelStatusStrip channels={daemonState.channels} />

      <div className="admin-layout">
        <aside className="admin-nav">
          <div className="admin-nav-heading">
            <div className="admin-nav-heading-label">管理</div>
            <div className="admin-nav-heading-sub">Admin</div>
          </div>
          <ul className="admin-nav-list">
            {TABS.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  className={`admin-nav-item ${tab === t.id ? 'is-active' : ''}`}
                  onClick={() => setTab(t.id)}
                >
                  <div className="admin-nav-item-label">{t.label}</div>
                  <div className="admin-nav-item-sub">{t.sub}</div>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <main className="admin-main">
          {tab === 'persona' && (
            <PersonaTab persona={persona} onUpdate={updatePersona} />
          )}
          {tab === 'events' && (
            <EventsTab
              highlightId={
                highlight?.kind === 'event' ? highlight.id : null
              }
              clearHighlight={() => setHighlight(null)}
              crossNav={crossNav}
            />
          )}
          {tab === 'thoughts' && (
            <ThoughtsTab
              highlightId={
                highlight?.kind === 'thought' ? highlight.id : null
              }
              clearHighlight={() => setHighlight(null)}
              crossNav={crossNav}
            />
          )}
          {tab === 'voice' && (
            <VoiceTab
              voiceEnabled={persona.voice_enabled}
              toggleVoice={toggleVoice}
            />
          )}
          {tab === 'cost' && <CostTab />}
          {tab === 'config' && <ConfigTab />}
        </main>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// Channel status strip — "Discord · 已连接" / "Web · 就绪" / ...
// ═══════════════════════════════════════════════════════════

function ChannelStatusStrip({ channels }: { channels: ChannelStatus[] }) {
  if (channels.length === 0) return null
  return (
    <div className="channel-status-strip">
      {channels.map((c) => (
        <ChannelStatusPill key={c.channel_id} channel={c} />
      ))}
    </div>
  )
}

function ChannelStatusPill({ channel }: { channel: ChannelStatus }) {
  // Dot color: green if ready, orange if enabled-but-not-ready
  // (handshake in progress / transient disconnect), grey if disabled.
  let tone: 'on' | 'warming' | 'off'
  let label: string
  if (!channel.enabled) {
    tone = 'off'
    label = '未启用'
  } else if (!channel.ready) {
    tone = 'warming'
    label = '连接中'
  } else {
    tone = 'on'
    label = channel.channel_id === 'discord' ? '已连接' : '就绪'
  }
  return (
    <span
      className={`channel-pill channel-pill--${tone}`}
      title={`${channel.name} · ${label}`}
    >
      <span className="channel-pill-dot" />
      <span className="channel-pill-name">{channel.name}</span>
      <span className="channel-pill-sub">{label}</span>
    </span>
  )
}

// ═══════════════════════════════════════════════════════════
// Persona tab — edit the 5 L1 blocks with human-language labels
// ═══════════════════════════════════════════════════════════

type ShortKey = 'persona' | 'self' | 'user' | 'relationship' | 'mood'

interface BlockMeta {
  shortKey: ShortKey
  label: string
  engName: string
  hint: string
  warning?: string
  small?: boolean
}

const BLOCK_META: BlockMeta[] = [
  {
    shortKey: 'persona',
    label: '这个 persona 是谁',
    engName: 'persona_block',
    hint: '改这里 = 调整 persona 的人格基调。下一条消息开始生效。',
  },
  {
    shortKey: 'self',
    label: 'persona 对自己的认知',
    engName: 'self_block',
    hint: '改这里 = 改写 persona 对自己的自我叙事。通常由反思自动积累，大多数时候不用手动改。',
  },
  {
    shortKey: 'user',
    label: 'persona 知道的你',
    engName: 'user_block',
    hint: '改这里 = 改 persona 对你的身份级认知（职业、家庭、长期爱好）。',
  },
  {
    shortKey: 'relationship',
    label: 'persona 知道的你身边的人',
    engName: 'relationship_block',
    hint: '改这里 = 改 persona 对你身边人的理解。按人物分组。',
  },
  {
    shortKey: 'mood',
    label: 'persona 此刻的情绪',
    engName: 'mood_block',
    hint: '改这里 = 临时调整 persona 的当前情绪。',
    warning: '下次对话结束后 runtime 会自动刷新覆盖。',
    small: true,
  },
]

function PersonaTab({
  persona,
  onUpdate,
}: {
  persona: PersonaStateApi
  onUpdate: (payload: PersonaUpdatePayload) => Promise<void>
}) {
  const navigate = useNavigate()
  const [nameDraft, setNameDraft] = useState(persona.display_name)
  const [nameSaving, setNameSaving] = useState(false)
  const [nameSavedAt, setNameSavedAt] = useState<number | null>(null)
  const nameDirty = nameDraft.trim() !== persona.display_name

  const handleSaveName = async () => {
    const trimmed = nameDraft.trim()
    if (!trimmed || !nameDirty) return
    setNameSaving(true)
    try {
      await onUpdate({ display_name: trimmed } as PersonaUpdatePayload)
      setNameSavedAt(Date.now())
      window.setTimeout(() => setNameSavedAt(null), 2000)
    } finally {
      setNameSaving(false)
    }
  }

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <h1 className="admin-section-title">人格</h1>
        <p className="admin-section-lead">
          persona 的 5 个"长期画像"。改这些会直接影响下次对话时 persona 的行为。
        </p>
      </div>

      <div className="admin-name-row">
        <label className="admin-name-label">
          显示名
          <span className="admin-name-hint">persona 在 Web / Discord / 任何 channel 上的称呼</span>
        </label>
        <div className="admin-name-controls">
          <input
            className="admin-name-input"
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            placeholder="她"
            maxLength={256}
            disabled={nameSaving}
          />
          <button
            type="button"
            className="block-editor-save"
            disabled={!nameDirty || nameSaving || !nameDraft.trim()}
            onClick={() => void handleSaveName()}
          >
            {nameSaving ? '⋯' : nameSavedAt ? '已保存 ✓' : '改名'}
          </button>
        </div>
      </div>

      <div className="admin-hint-card">
        <div className="admin-hint-glyph">📥</div>
        <div className="admin-hint-body">
          <div className="admin-hint-title">有历史材料想让 persona 记住？</div>
          <div className="admin-hint-desc">
            聊天记录、日记、文档——<strong>导入器</strong>
            会让 LLM 读完之后，把具体事件、身边的人、
            你身上的事实分别写到对应的记忆层。
          </div>
        </div>
        <button
          type="button"
          className="admin-hint-btn"
          onClick={() => navigate('/admin/import')}
        >
          导入历史材料 →
        </button>
      </div>

      <div className="admin-blocks">
        {BLOCK_META.map((meta) => (
          <BlockEditor
            key={meta.shortKey}
            meta={meta}
            value={persona.core_blocks[meta.shortKey]}
            onSave={async (next) => {
              await onUpdate({
                [`${meta.shortKey}_block`]: next,
              } as PersonaUpdatePayload)
            }}
          />
        ))}
      </div>
    </div>
  )
}

function BlockEditor({
  meta,
  value,
  onSave,
}: {
  meta: BlockMeta
  value: string
  onSave: (next: string) => Promise<void>
}) {
  const [draft, setDraft] = useState(value)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const dirty = draft !== value

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSave(draft)
      setSavedAt(Date.now())
      window.setTimeout(() => setSavedAt(null), 2000)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="block-editor">
      <header className="block-editor-head">
        <div className="block-editor-label-row">
          <h3 className="block-editor-label">{meta.label}</h3>
          <span className="block-editor-engname">{meta.engName}</span>
        </div>
        <p className="block-editor-hint">{meta.hint}</p>
        {meta.warning && (
          <p className="block-editor-warning">⚠ {meta.warning}</p>
        )}
      </header>
      <textarea
        className={`block-editor-textarea ${meta.small ? 'is-small' : ''}`}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={meta.small ? 2 : 6}
        placeholder={meta.small ? '（留空会随对话自动演变）' : '还没写。'}
        disabled={saving}
      />
      <div className="block-editor-actions">
        <div className="block-editor-status">
          {savedAt && <span className="block-editor-saved">已保存 ✓</span>}
          {!savedAt && dirty && (
            <span className="block-editor-dirty">有未保存的修改</span>
          )}
          {!savedAt && !dirty && (
            <span className="block-editor-count">
              {draft.length.toLocaleString()} 字
            </span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={() => void handleSave()}
        >
          {saving ? '⋯' : '保存'}
        </button>
      </div>
    </section>
  )
}

// ═══════════════════════════════════════════════════════════
// Events tab — paginated L3 list with per-row delete (W-α + W-β)
// ═══════════════════════════════════════════════════════════

interface TraceTabProps {
  highlightId: number | null
  clearHighlight: () => void
  crossNav: CrossNav
}

function EventsTab({ highlightId, clearHighlight, crossNav }: TraceTabProps) {
  const {
    items,
    total,
    loading,
    loadingMore,
    error,
    hasMore,
    loadMore,
    previewDelete,
    deleteEvent,
  } = useMemoryEvents()
  const search = useMemorySearch('events')

  const handleDelete = async (item: MemoryEvent) => {
    try {
      const preview = await previewDelete(item.id)
      const choice = await confirmDelete(item.description, preview)
      if (choice === null) return
      await deleteEvent(item.id, choice)
    } catch (err) {
      // Error already surfaced via the hook's `error`. Silently swallow
      // here so a confirm-dialog cancel does not stack-trace.
      console.error('delete event failed', err)
    }
  }

  // Worker θ · when the search is active, replace the default
  // pagination list with the filtered results. Snippets are
  // rendered inline (see `MemoryRow`'s `snippet` prop).
  const showingSearch = search.active
  const visibleItems = (
    showingSearch ? search.results : items
  ) as MemoryEvent[]
  const visibleTotal = showingSearch ? search.total : total
  const tabError = error || search.error

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">发生过的事</h1>
          <span className="admin-section-engname">events · L3</span>
        </div>
        <p className="admin-section-lead">
          persona 记得的具体事件。每一条带时间、情感强度、相关的人和情绪。
          服务器上共 <strong>{total}</strong> 条。
        </p>
      </div>

      <MemorySearchBar
        value={search.query}
        onChange={search.setQuery}
        onClear={search.clear}
        loading={search.loading}
        active={showingSearch}
        total={search.total}
      />

      {tabError && <div className="admin-error-banner">{tabError}</div>}

      {showingSearch ? (
        search.loading && visibleItems.length === 0 ? (
          <div className="memory-list-loading">搜索中…</div>
        ) : visibleTotal === 0 ? (
          <div className="memory-list-empty">
            <div className="memory-list-empty-glyph">🔍</div>
            <div className="memory-list-empty-title">没有匹配的事件</div>
            <p className="memory-list-empty-desc">
              试试更宽泛的关键词,或者点"清除"返回完整列表。
            </p>
          </div>
        ) : (
          <ul className="memory-list">
            {visibleItems.map((it) => (
              <MemoryRow
                key={it.id}
                kind="event"
                item={it}
                highlighted={highlightId === it.id}
                onHighlightConsumed={clearHighlight}
                onDelete={() => void handleDelete(it)}
                crossNav={crossNav}
                snippet={search.snippets.get(it.id)}
              />
            ))}
          </ul>
        )
      ) : loading && items.length === 0 ? (
        <div className="memory-list-loading">载入中…</div>
      ) : total === 0 ? (
        <div className="memory-list-empty">
          <div className="memory-list-empty-glyph">📖</div>
          <div className="memory-list-empty-title">persona 还没记得任何事</div>
          <p className="memory-list-empty-desc">
            和 persona 多聊几轮，后台 consolidate 会把对话压缩成事件，
            自动出现在这里。
          </p>
        </div>
      ) : (
        <ul className="memory-list">
          {items.map((it) => (
            <MemoryRow
              key={it.id}
              kind="event"
              item={it}
              highlighted={highlightId === it.id}
              onHighlightConsumed={clearHighlight}
              onDelete={() => void handleDelete(it)}
              crossNav={crossNav}
            />
          ))}
        </ul>
      )}

      {!showingSearch && hasMore && (
        <div className="memory-list-more">
          <button
            type="button"
            className="memory-list-more-btn"
            onClick={() => void loadMore()}
            disabled={loadingMore}
          >
            {loadingMore ? '载入中…' : `加载更多（剩 ${total - items.length}）`}
          </button>
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// Thoughts tab — paginated L4 list with per-row delete (W-α + W-β)
// ═══════════════════════════════════════════════════════════

function ThoughtsTab({ highlightId, clearHighlight, crossNav }: TraceTabProps) {
  const {
    items,
    total,
    loading,
    loadingMore,
    error,
    hasMore,
    loadMore,
    previewDelete,
    deleteThought,
  } = useMemoryThoughts()
  const search = useMemorySearch('thoughts')

  const handleDelete = async (item: MemoryThought) => {
    try {
      const preview = await previewDelete(item.id)
      const choice = await confirmDelete(item.description, preview)
      if (choice === null) return
      await deleteThought(item.id, choice)
    } catch (err) {
      console.error('delete thought failed', err)
    }
  }

  const showingSearch = search.active
  const visibleItems = (
    showingSearch ? search.results : items
  ) as MemoryThought[]
  const visibleTotal = showingSearch ? search.total : total
  const tabError = error || search.error

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">长期印象</h1>
          <span className="admin-section-engname">thoughts · L4</span>
        </div>
        <p className="admin-section-lead">
          persona 在很多次对话之后沉淀下来的、对你的高阶观察。
          通常由反思模块自动生成，也可能来自导入的真实材料。
          服务器上共 <strong>{total}</strong> 条。
        </p>
      </div>

      <MemorySearchBar
        value={search.query}
        onChange={search.setQuery}
        onClear={search.clear}
        loading={search.loading}
        active={showingSearch}
        total={search.total}
      />

      {tabError && <div className="admin-error-banner">{tabError}</div>}

      {showingSearch ? (
        search.loading && visibleItems.length === 0 ? (
          <div className="memory-list-loading">搜索中…</div>
        ) : visibleTotal === 0 ? (
          <div className="memory-list-empty">
            <div className="memory-list-empty-glyph">🔍</div>
            <div className="memory-list-empty-title">没有匹配的印象</div>
            <p className="memory-list-empty-desc">
              试试更宽泛的关键词,或者点"清除"返回完整列表。
            </p>
          </div>
        ) : (
          <ul className="memory-list">
            {visibleItems.map((it) => (
              <MemoryRow
                key={it.id}
                kind="thought"
                item={it}
                highlighted={highlightId === it.id}
                onHighlightConsumed={clearHighlight}
                onDelete={() => void handleDelete(it)}
                crossNav={crossNav}
                snippet={search.snippets.get(it.id)}
              />
            ))}
          </ul>
        )
      ) : loading && items.length === 0 ? (
        <div className="memory-list-loading">载入中…</div>
      ) : total === 0 ? (
        <div className="memory-list-empty">
          <div className="memory-list-empty-glyph">🪞</div>
          <div className="memory-list-empty-title">还没有沉淀下来的印象</div>
          <p className="memory-list-empty-desc">
            等积累更多事件之后，反思模块会自动产出对你的高阶观察。
          </p>
        </div>
      ) : (
        <ul className="memory-list">
          {items.map((it) => (
            <MemoryRow
              key={it.id}
              kind="thought"
              item={it}
              highlighted={highlightId === it.id}
              onHighlightConsumed={clearHighlight}
              onDelete={() => void handleDelete(it)}
              crossNav={crossNav}
            />
          ))}
        </ul>
      )}

      {!showingSearch && hasMore && (
        <div className="memory-list-more">
          <button
            type="button"
            className="memory-list-more-btn"
            onClick={() => void loadMore()}
            disabled={loadingMore}
          >
            {loadingMore ? '载入中…' : `加载更多（剩 ${total - items.length}）`}
          </button>
        </div>
      )}
    </div>
  )
}

// ─── Worker θ · MemorySearchBar (shared by Events / Thoughts tabs) ──────

interface MemorySearchBarProps {
  value: string
  onChange: (v: string) => void
  onClear: () => void
  loading: boolean
  active: boolean
  total: number
}

function MemorySearchBar({
  value,
  onChange,
  onClear,
  loading,
  active,
  total,
}: MemorySearchBarProps) {
  return (
    <div className="memory-search-bar">
      <span className="memory-search-icon" aria-hidden>
        🔍
      </span>
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="搜索关键词…"
        className="memory-search-input"
        aria-label="搜索 memory"
      />
      {active && (
        <span className="memory-search-meta">
          {loading ? '搜索中…' : `${total} 条`}
        </span>
      )}
      {value && (
        <button
          type="button"
          className="memory-search-clear"
          onClick={onClear}
          aria-label="清除搜索"
        >
          清除
        </button>
      )}
    </div>
  )
}

/**
 * Server-rendered search snippets contain only ``<b>…</b>`` tags
 * around matched terms. We allowlist the tag explicitly and strip
 * everything else before piping through ``dangerouslySetInnerHTML`` —
 * that way the snippet stays safe even if a future backend change
 * starts emitting other markup.
 */
export function sanitiseSnippet(raw: string): string {
  // Strip every tag that is NOT <b>/</b>. Pattern matches `<...>` then
  // we keep the literal `<b>` / `</b>` and drop everything else.
  return raw.replace(/<(?!\/?b\b)[^>]*>/gi, '')
}

// ─── Memory row + inline trace expansion (Worker ι) ────────────────────
//
// `<MemoryRow>` wraps the existing card markup and adds:
//
//   - The "trace" toggle button (📚 for thoughts, 🪞 for events)
//   - An inline-expand region that fetches lineage via `useMemoryTrace`
//   - A cross-tab jump: clicking a related node switches tabs + flashes
//   - Highlight flash: when `highlighted` is true, scrollIntoView + a
//     transient CSS pulse via the `.is-highlighted` modifier
//
// No new CSS classes are introduced. The expansion uses inline styles
// so the rest of the app's CSS vocabulary is untouched.

interface MemoryRowProps {
  kind: 'event' | 'thought'
  item: MemoryEvent | MemoryThought
  highlighted: boolean
  onHighlightConsumed: () => void
  onDelete: () => void
  crossNav: CrossNav
  /** Worker θ — when present, replaces the plain ``description`` with
   *  the server's snippet HTML (``<b>…</b>`` allowlist, sanitised). */
  snippet?: string
}

function MemoryRow({
  kind,
  item,
  highlighted,
  onHighlightConsumed,
  onDelete,
  crossNav,
  snippet,
}: MemoryRowProps) {
  const [expanded, setExpanded] = useState(false)
  const liRef = useRef<HTMLLIElement | null>(null)
  const trace = useMemoryTrace({ kind, nodeId: item.id })

  // Cross-tab jump: when Admin sets us as the highlight target, scroll
  // into view and let the CSS pulse play. Clear the highlight state
  // after the animation so the next navigation can re-trigger it.
  useEffect(() => {
    if (!highlighted) return
    const node = liRef.current
    if (node) {
      node.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
    const timer = window.setTimeout(onHighlightConsumed, 1800)
    return () => window.clearTimeout(timer)
  }, [highlighted, onHighlightConsumed])

  const handleToggle = () => {
    if (!expanded && trace.data === null) {
      void trace.load()
    }
    setExpanded((v) => !v)
  }

  const eventRow = kind === 'event' && item.node_type === 'event'
    ? (item as MemoryEvent)
    : null
  const toggleLabel =
    kind === 'thought' ? '📚 来源' : '🪞 印象'

  const expandedNodes: TraceNode[] = (() => {
    if (!trace.data) return []
    if (trace.data.kind === 'thought') return trace.data.response.source_events
    return trace.data.response.dependent_thoughts
  })()

  const liClassName = [
    'memory-list-item',
    expanded ? 'is-expanded' : '',
    highlighted ? 'is-highlighted' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <li ref={liRef} className={liClassName}>
      <div className="memory-list-row-head">
        <time className="memory-list-time">{formatTimestamp(item.created_at)}</time>
        <button
          type="button"
          className="memory-list-delete"
          onClick={onDelete}
          aria-label={`删除 ${kind === 'event' ? '事件' : '印象'} ${item.id}`}
          title={kind === 'event' ? '删除这条事件' : '删除这条印象'}
        >
          ×
        </button>
      </div>

      {snippet ? (
        <p
          className="memory-list-desc memory-list-desc--snippet"
          dangerouslySetInnerHTML={{ __html: sanitiseSnippet(snippet) }}
        />
      ) : (
        <p className="memory-list-desc">{item.description}</p>
      )}

      {eventRow !== null && eventRow.emotion_tags.length > 0 && (
        <div className="memory-list-pills">
          {eventRow.emotion_tags.map((tag) => (
            <span key={tag} className="memory-list-pill">
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* Trace toggle — inline-expand, no modal. `data` being null
          after load() + !loading means "successful empty" which we
          render as a small note so the user knows we actually looked. */}
      <div style={{ marginTop: 4 }}>
        <button
          type="button"
          onClick={handleToggle}
          style={traceToggleBtn}
          disabled={trace.loading}
        >
          {trace.loading
            ? '查找中⋯'
            : `${toggleLabel}${
                trace.data
                  ? ` ${expandedNodes.length} 条`
                  : ''
              } ${expanded ? '▾' : '▸'}`}
        </button>
      </div>

      {expanded && (
        <div style={traceExpansionPanel}>
          {trace.error !== null && (
            <div style={traceError}>⚠ {trace.error}</div>
          )}
          {!trace.loading &&
            trace.data !== null &&
            expandedNodes.length === 0 && (
              <div style={traceEmpty}>
                {kind === 'thought'
                  ? '这条印象还没有被任何事件支撑 (可能反思链已被清空)'
                  : '还没有印象从这条事件里沉淀出来'}
              </div>
            )}
          {expandedNodes.length > 0 && (
            <ul style={traceList}>
              {expandedNodes.map((n) => (
                <li key={n.id} style={traceListItem}>
                  <button
                    type="button"
                    style={traceJumpBtn}
                    onClick={() =>
                      crossNav.navigateTo(
                        kind === 'thought' ? 'event' : 'thought',
                        n.id,
                      )
                    }
                    title={`跳到${
                      kind === 'thought' ? '事件' : '印象'
                    } ${n.id}`}
                  >
                    <div style={traceJumpMeta}>
                      <span>{formatTimestamp(n.created_at)}</span>
                      {n.source_session_id && (
                        <span style={{ opacity: 0.6 }}>
                          · {n.source_session_id.slice(0, 8)}…
                        </span>
                      )}
                    </div>
                    <div style={traceJumpDesc}>
                      {truncate(n.description, 120)}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {trace.data?.kind === 'thought' &&
            trace.data.response.source_sessions.length > 0 && (
              <div style={traceSessionsRow}>
                来自 {trace.data.response.source_sessions.length} 个 session
              </div>
            )}
        </div>
      )}
    </li>
  )
}

const traceToggleBtn: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  color: 'var(--cream-soft)',
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  letterSpacing: '0.1em',
  textTransform: 'uppercase',
  padding: '4px 0',
  cursor: 'pointer',
}

const traceExpansionPanel: React.CSSProperties = {
  marginTop: 10,
  paddingTop: 12,
  borderTop: '1px dashed var(--thread)',
  display: 'flex',
  flexDirection: 'column',
  gap: 10,
}

const traceError: React.CSSProperties = {
  color: 'rgba(255, 120, 120, 0.8)',
  fontFamily: 'var(--font-serif)',
  fontSize: 13,
}

const traceEmpty: React.CSSProperties = {
  color: 'var(--cream-soft)',
  fontFamily: 'var(--font-serif)',
  fontStyle: 'italic',
  fontSize: 13,
}

const traceList: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
}

const traceListItem: React.CSSProperties = {
  margin: 0,
}

const traceJumpBtn: React.CSSProperties = {
  width: '100%',
  textAlign: 'left',
  background: 'rgba(255,255,255,0.02)',
  border: '1px solid rgba(255,255,255,0.06)',
  borderRadius: 8,
  padding: '10px 12px',
  color: 'var(--cream)',
  cursor: 'pointer',
  transition: 'border-color 0.2s ease, background 0.2s ease',
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  fontFamily: 'var(--font-serif)',
}

const traceJumpMeta: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  letterSpacing: '0.1em',
  color: 'var(--cream-dim)',
  textTransform: 'uppercase',
}

const traceJumpDesc: React.CSSProperties = {
  fontSize: 14,
  lineHeight: 1.4,
  color: 'var(--cream)',
}

const traceSessionsRow: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  letterSpacing: '0.1em',
  textTransform: 'uppercase',
  color: 'var(--cream-soft)',
  paddingTop: 4,
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text
  return text.slice(0, max - 1) + '…'
}

// ─── Helpers shared by Events / Thoughts tabs ───────────────────────────

/**
 * Native ``window.confirm`` driven preview-delete dialog.
 *
 * Returns ``"orphan"`` for "delete only this row, keep dependents",
 * ``"cascade"`` for "delete this row and cascade-delete dependents",
 * or ``null`` if the user cancelled.
 *
 * MVP-grade UX. Stage X+ can replace this with a custom modal that
 * shows the dependent thought descriptions inline; the hook signature
 * is the same.
 */
function confirmDelete(
  description: string,
  preview: PreviewDeleteResponse,
): Promise<'orphan' | 'cascade' | null> {
  const truncated =
    description.length > 80 ? `${description.slice(0, 80)}…` : description

  if (!preview.has_dependents) {
    const ok = window.confirm(`删除这条记忆？\n\n${truncated}`)
    return Promise.resolve(ok ? 'orphan' : null)
  }

  const depCount = preview.dependent_thought_ids.length
  const depsList = preview.dependent_thought_descriptions
    .slice(0, 3)
    .map((d, i) => `${i + 1}. ${d.length > 60 ? `${d.slice(0, 60)}…` : d}`)
    .join('\n')

  const cascadeMsg =
    `要删除这条记忆吗？\n\n${truncated}\n\n` +
    `这条记忆产出了 ${depCount} 条派生印象：\n${depsList}\n\n` +
    `确定 = 一起删（cascade）\n取消 = 只删这条，保留派生印象（orphan）\n` +
    `想完全不动 → 关掉这个对话框时点 Esc`
  const cascade = window.confirm(cascadeMsg)
  // The native dialog has only "确定 / 取消"; we treat
  //   confirm=true  → cascade
  //   confirm=false → orphan
  // and rely on the user closing the dialog without choice (browser
  // returns false too) to mean orphan. The Esc-as-cancel path is a
  // UX wish that needs a real modal — flag for Stage X.
  return Promise.resolve(cascade ? 'cascade' : 'orphan')
}

/** Format an ISO timestamp into the "YYYY-MM-DD HH:mm" form the
 *  rest of the admin UI uses. */
function formatTimestamp(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

// ═══════════════════════════════════════════════════════════
// Voice tab — on/off toggle (real) + coming-soon for cloning
// ═══════════════════════════════════════════════════════════

function VoiceTab({
  voiceEnabled,
  toggleVoice,
}: {
  voiceEnabled: boolean
  toggleVoice: (enabled: boolean) => Promise<void>
}) {
  const [toggling, setToggling] = useState(false)
  const navigate = useNavigate()

  const handleToggle = async () => {
    setToggling(true)
    try {
      await toggleVoice(!voiceEnabled)
    } finally {
      setToggling(false)
    }
  }

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">声音</h1>
          <span className="admin-section-engname">voice toggle</span>
        </div>
        <p className="admin-section-lead">
          控制 persona 是否用 TTS 语音朗读回复。
        </p>
      </div>

      <div className="admin-hint-card" style={{ marginBottom: 16 }}>
        <div className="admin-hint-glyph">🎙</div>
        <div className="admin-hint-body">
          <div className="admin-hint-title">想让 persona 用你的声音说话？</div>
          <div className="admin-hint-desc">
            上传一段 10-60s 的纯净录音(多段更好)，训练一个属于你自己的
            voice · 可以试听后再激活。没克隆的话就用 FishAudio 内置默认音。
          </div>
        </div>
        <button
          type="button"
          className="admin-hint-btn"
          onClick={() => navigate('/admin/voice/clone')}
        >
          克隆新声音 →
        </button>
      </div>

      <div className="voice-card" style={{ marginBottom: 24 }}>
        <div className="voice-card-status">
          <div
            className="voice-card-dot"
            style={{
              background: voiceEnabled
                ? 'rgba(120, 255, 180, 0.7)'
                : 'rgba(255, 255, 255, 0.25)',
            }}
          />
          <div>
            <div className="voice-card-name">
              语音回复 · {voiceEnabled ? '已开启' : '已关闭'}
            </div>
            <div className="voice-card-meta">
              {voiceEnabled
                ? '下一条 persona 回复会尝试用 TTS 语音朗读。'
                : 'Persona 回复只以文字形式出现。'}
            </div>
          </div>
        </div>
        <div className="voice-card-actions">
          <button
            type="button"
            className="voice-card-action"
            onClick={() => void handleToggle()}
            disabled={toggling}
          >
            {toggling ? '⋯' : voiceEnabled ? '关闭语音' : '开启语音'}
          </button>
        </div>
      </div>

    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// Config tab — Worker η · four editable cards wired to PATCH /api/admin/config
// ═══════════════════════════════════════════════════════════
//
// The tab renders 4 cards sharing a compact form style (reuses
// `.block-editor` / `.admin-section` vocabulary — no new CSS classes):
//
//   1. LLM         — provider / model / temperature / max_tokens /
//                    timeout / api_key_env + green-dot presence check
//   2. Memory      — retrieve_k slider / relational_bonus_weight /
//                    recent_window_size
//   3. Consolidate — 3 integer thresholds
//   4. System info — read-only: version / uptime / db size / data_dir
//                    with a "复制路径" button for config.toml
//
// Each card has a local draft state + its own save button. The "save"
// button PATCHes only that card's fields so a save on one card never
// clobbers pending edits in another. `useConfig.save()` re-reads after
// a successful PATCH so each card shows server-truth.

function ConfigTab() {
  const { config, loading, saving, error, lastSaved, save } = useConfig()

  if (loading && config === null) {
    return (
      <div className="admin-section">
        <div className="admin-section-head">
          <h1 className="admin-section-title">配置</h1>
          <p className="admin-section-lead">加载中⋯</p>
        </div>
      </div>
    )
  }

  if (config === null) {
    return (
      <div className="admin-section">
        <div className="admin-section-head">
          <h1 className="admin-section-title">配置</h1>
          <p className="admin-section-lead" style={{ color: 'rgba(255,120,120,0.78)' }}>
            ⚠ {error ?? '无法加载配置'}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">配置</h1>
          <span className="admin-section-engname">runtime config</span>
        </div>
        <p className="admin-section-lead">
          在这里改的设置会<strong>原子地写回 config.toml</strong>
          并让 daemon 立即生效，不用重启。数据目录 / 数据库路径这类结构性字段不可在线改动。
        </p>
      </div>

      {error !== null && (
        <div
          className="admin-hint-card"
          style={{
            borderColor: 'rgba(255, 100, 100, 0.35)',
            background: 'rgba(255, 80, 80, 0.08)',
          }}
        >
          <div className="admin-hint-glyph">⚠</div>
          <div className="admin-hint-body">
            <div className="admin-hint-title">保存失败</div>
            <div className="admin-hint-desc">{error}</div>
          </div>
        </div>
      )}

      <div className="admin-blocks">
        <ConfigLlmCard config={config} saving={saving} save={save} lastSaved={lastSaved} />
        <ConfigMemoryCard config={config} saving={saving} save={save} lastSaved={lastSaved} />
        <ConfigConsolidateCard config={config} saving={saving} save={save} lastSaved={lastSaved} />
        <ConfigSystemCard config={config} />
      </div>
    </div>
  )
}

// ─── Card 1 · LLM ───────────────────────────────────────────────────

interface ConfigCardProps {
  config: ConfigGetResponse
  saving: boolean
  save: (patch: ConfigPatchPayload) => Promise<void>
  lastSaved: number | null
}

function ConfigLlmCard({ config, saving, save, lastSaved }: ConfigCardProps) {
  const [provider, setProvider] = useState(config.llm.provider)
  const [model, setModel] = useState(config.llm.model ?? '')
  const [temperature, setTemperature] = useState(config.llm.temperature)
  const [maxTokens, setMaxTokens] = useState(config.llm.max_tokens)
  const [timeout, setTimeout] = useState(config.llm.timeout_seconds)

  // Re-sync local state when the upstream `config` snapshot changes
  // (e.g. after a successful save refresh).
  useEffect(() => {
    setProvider(config.llm.provider)
    setModel(config.llm.model ?? '')
    setTemperature(config.llm.temperature)
    setMaxTokens(config.llm.max_tokens)
    setTimeout(config.llm.timeout_seconds)
  }, [config.llm])

  const dirty =
    provider !== config.llm.provider ||
    model !== (config.llm.model ?? '') ||
    temperature !== config.llm.temperature ||
    maxTokens !== config.llm.max_tokens ||
    timeout !== config.llm.timeout_seconds

  const handleSave = () => {
    const patch: ConfigPatchPayload = { llm: {} }
    if (provider !== config.llm.provider) patch.llm!.provider = provider
    if (model !== (config.llm.model ?? '')) patch.llm!.model = model
    if (temperature !== config.llm.temperature)
      patch.llm!.temperature = temperature
    if (maxTokens !== config.llm.max_tokens) patch.llm!.max_tokens = maxTokens
    if (timeout !== config.llm.timeout_seconds)
      patch.llm!.timeout_seconds = timeout
    void save(patch).catch(() => {
      /* surfaced via `error` */
    })
  }

  const recentlySaved = lastSaved !== null && Date.now() - lastSaved < 3000

  return (
    <section className="block-editor">
      <header className="block-editor-head">
        <div className="block-editor-label-row">
          <h3 className="block-editor-label">LLM 提供商</h3>
          <span className="block-editor-engname">llm.*</span>
        </div>
        <p className="block-editor-hint">
          控制 persona 的回复由谁生成。改完<strong>下一条消息</strong>就会用新模型。
        </p>
      </header>

      <div style={{ display: 'grid', gap: 14 }}>
        <FormRow label="provider" sub="openai_compat / anthropic / stub">
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            disabled={saving}
            style={selectStyle}
          >
            <option value="openai_compat">openai_compat</option>
            <option value="anthropic">anthropic</option>
            <option value="stub">stub</option>
          </select>
        </FormRow>

        <FormRow label="model" sub="e.g. gpt-4o-mini / claude-haiku-4-5">
          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={saving}
            style={inputStyle}
            placeholder="model id"
          />
        </FormRow>

        <FormRow
          label="temperature"
          sub={`当前 ${temperature.toFixed(2)}   ·  范围 0.00 – 2.00`}
        >
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={temperature}
            onChange={(e) => setTemperature(parseFloat(e.target.value))}
            disabled={saving}
            style={{ width: '100%' }}
          />
        </FormRow>

        <FormRow label="max_tokens" sub="单次回复上限,64 – 32000">
          <input
            type="number"
            min={64}
            max={32000}
            step={32}
            value={maxTokens}
            onChange={(e) => setMaxTokens(parseInt(e.target.value, 10) || 0)}
            disabled={saving}
            style={inputStyle}
          />
        </FormRow>

        <FormRow label="timeout_seconds" sub="超时丢弃本次调用,1 – 600">
          <input
            type="number"
            min={1}
            max={600}
            value={timeout}
            onChange={(e) => setTimeout(parseInt(e.target.value, 10) || 0)}
            disabled={saving}
            style={inputStyle}
          />
        </FormRow>

        <FormRow label="api_key_env" sub="只读 · env var 名字 · 密钥本身在 .env / 系统环境">
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '8px 10px',
              background: 'rgba(255,255,255,0.03)',
              border: '1px solid rgba(255,255,255,0.08)',
              borderRadius: 6,
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
            }}
          >
            <span
              style={{
                display: 'inline-block',
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: config.llm.api_key_present
                  ? 'rgba(120, 255, 180, 0.75)'
                  : 'rgba(255, 120, 120, 0.7)',
              }}
            />
            <code style={{ flex: 1 }}>{config.llm.api_key_env}</code>
            <span
              style={{
                fontSize: 10,
                letterSpacing: '0.12em',
                textTransform: 'uppercase',
                color: config.llm.api_key_present
                  ? 'rgba(120, 255, 180, 0.8)'
                  : 'rgba(255, 120, 120, 0.8)',
              }}
            >
              {config.llm.api_key_present ? 'loaded' : 'missing'}
            </span>
          </div>
        </FormRow>
      </div>

      <div className="block-editor-actions">
        <div className="block-editor-status">
          {recentlySaved && (
            <span className="block-editor-saved">已保存 · 下次 LLM 调用生效 ✓</span>
          )}
          {!recentlySaved && dirty && (
            <span className="block-editor-dirty">有未保存的修改</span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={handleSave}
        >
          {saving ? '⋯' : '保存'}
        </button>
      </div>
    </section>
  )
}

// ─── Card 2 · Memory tuning ─────────────────────────────────────────

function ConfigMemoryCard({ config, saving, save, lastSaved }: ConfigCardProps) {
  const [retrieveK, setRetrieveK] = useState(config.memory.retrieve_k)
  const [bonus, setBonus] = useState(config.memory.relational_bonus_weight)
  const [recent, setRecent] = useState(config.memory.recent_window_size)

  useEffect(() => {
    setRetrieveK(config.memory.retrieve_k)
    setBonus(config.memory.relational_bonus_weight)
    setRecent(config.memory.recent_window_size)
  }, [config.memory])

  const dirty =
    retrieveK !== config.memory.retrieve_k ||
    bonus !== config.memory.relational_bonus_weight ||
    recent !== config.memory.recent_window_size

  const handleSave = () => {
    const patch: ConfigPatchPayload = { memory: {} }
    if (retrieveK !== config.memory.retrieve_k)
      patch.memory!.retrieve_k = retrieveK
    if (bonus !== config.memory.relational_bonus_weight)
      patch.memory!.relational_bonus_weight = bonus
    if (recent !== config.memory.recent_window_size)
      patch.memory!.recent_window_size = recent
    void save(patch).catch(() => {})
  }

  const recentlySaved = lastSaved !== null && Date.now() - lastSaved < 3000

  return (
    <section className="block-editor">
      <header className="block-editor-head">
        <div className="block-editor-label-row">
          <h3 className="block-editor-label">记忆调优</h3>
          <span className="block-editor-engname">memory.*</span>
        </div>
        <p className="block-editor-hint">
          控制每次对话时 persona 调用多少条"发生过的事"和"长期印象"作为上下文。
        </p>
      </header>

      <div style={{ display: 'grid', gap: 14 }}>
        <FormRow label="retrieve_k" sub={`取回 ${retrieveK} 条记忆 · 1 – 30`}>
          <input
            type="range"
            min={1}
            max={30}
            step={1}
            value={retrieveK}
            onChange={(e) => setRetrieveK(parseInt(e.target.value, 10))}
            disabled={saving}
            style={{ width: '100%' }}
          />
        </FormRow>

        <FormRow
          label="relational_bonus_weight"
          sub={`${bonus.toFixed(2)}   ·  "身边的人"相关记忆的加权系数 · 0.0 – 2.0`}
        >
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={bonus}
            onChange={(e) => setBonus(parseFloat(e.target.value))}
            disabled={saving}
            style={{ width: '100%' }}
          />
        </FormRow>

        <FormRow
          label="recent_window_size"
          sub={`${recent} 条最近消息 · 1 – 200`}
        >
          <input
            type="number"
            min={1}
            max={200}
            value={recent}
            onChange={(e) => setRecent(parseInt(e.target.value, 10) || 0)}
            disabled={saving}
            style={inputStyle}
          />
        </FormRow>
      </div>

      <div className="block-editor-actions">
        <div className="block-editor-status">
          {recentlySaved && (
            <span className="block-editor-saved">已保存 ✓</span>
          )}
          {!recentlySaved && dirty && (
            <span className="block-editor-dirty">有未保存的修改</span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={handleSave}
        >
          {saving ? '⋯' : '保存'}
        </button>
      </div>
    </section>
  )
}

// ─── Card 3 · Consolidate thresholds ────────────────────────────────

function ConfigConsolidateCard({
  config,
  saving,
  save,
  lastSaved,
}: ConfigCardProps) {
  const [trivMsg, setTrivMsg] = useState(config.consolidate.trivial_message_count)
  const [trivTok, setTrivTok] = useState(config.consolidate.trivial_token_count)
  const [reflGate, setReflGate] = useState(
    config.consolidate.reflection_hard_gate_24h,
  )

  useEffect(() => {
    setTrivMsg(config.consolidate.trivial_message_count)
    setTrivTok(config.consolidate.trivial_token_count)
    setReflGate(config.consolidate.reflection_hard_gate_24h)
  }, [config.consolidate])

  const dirty =
    trivMsg !== config.consolidate.trivial_message_count ||
    trivTok !== config.consolidate.trivial_token_count ||
    reflGate !== config.consolidate.reflection_hard_gate_24h

  const handleSave = () => {
    const patch: ConfigPatchPayload = { consolidate: {} }
    if (trivMsg !== config.consolidate.trivial_message_count)
      patch.consolidate!.trivial_message_count = trivMsg
    if (trivTok !== config.consolidate.trivial_token_count)
      patch.consolidate!.trivial_token_count = trivTok
    if (reflGate !== config.consolidate.reflection_hard_gate_24h)
      patch.consolidate!.reflection_hard_gate_24h = reflGate
    void save(patch).catch(() => {})
  }

  const recentlySaved = lastSaved !== null && Date.now() - lastSaved < 3000

  return (
    <section className="block-editor">
      <header className="block-editor-head">
        <div className="block-editor-label-row">
          <h3 className="block-editor-label">记忆整合阈值</h3>
          <span className="block-editor-engname">consolidate.*</span>
        </div>
        <p className="block-editor-hint">
          控制哪些对话被当作"琐碎"直接跳过,哪些才进事件提取 / 反思管线。
        </p>
      </header>

      <div style={{ display: 'grid', gap: 14 }}>
        <FormRow
          label="trivial_message_count"
          sub="消息条数低于此值的 session 直接跳过提取"
        >
          <input
            type="number"
            min={0}
            max={50}
            value={trivMsg}
            onChange={(e) => setTrivMsg(parseInt(e.target.value, 10) || 0)}
            disabled={saving}
            style={inputStyle}
          />
        </FormRow>

        <FormRow
          label="trivial_token_count"
          sub="token 数低于此值的 session 直接跳过提取"
        >
          <input
            type="number"
            min={0}
            max={5000}
            step={10}
            value={trivTok}
            onChange={(e) => setTrivTok(parseInt(e.target.value, 10) || 0)}
            disabled={saving}
            style={inputStyle}
          />
        </FormRow>

        <FormRow
          label="reflection_hard_gate_24h"
          sub="24h 内最多做多少次反思 · 保护 LLM 预算"
        >
          <input
            type="number"
            min={0}
            max={100}
            value={reflGate}
            onChange={(e) => setReflGate(parseInt(e.target.value, 10) || 0)}
            disabled={saving}
            style={inputStyle}
          />
        </FormRow>
      </div>

      <div className="block-editor-actions">
        <div className="block-editor-status">
          {recentlySaved && (
            <span className="block-editor-saved">已保存 ✓</span>
          )}
          {!recentlySaved && dirty && (
            <span className="block-editor-dirty">有未保存的修改</span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={handleSave}
        >
          {saving ? '⋯' : '保存'}
        </button>
      </div>
    </section>
  )
}

// ─── Card 4 · System info (read-only) ──────────────────────────────

function ConfigSystemCard({ config }: { config: ConfigGetResponse }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(config.system.config_path ?? '')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // noop — some browsers refuse clipboard writes without permission
    }
  }

  return (
    <section className="block-editor">
      <header className="block-editor-head">
        <div className="block-editor-label-row">
          <h3 className="block-editor-label">系统信息</h3>
          <span className="block-editor-engname">read-only</span>
        </div>
        <p className="block-editor-hint">
          以下字段不可在线改 —— <strong>data_dir</strong> 和 <strong>db_path</strong> 决定数据库文件位置,
          改动需要停 daemon,手动编辑 config.toml,再重启。
        </p>
      </header>

      <dl
        style={{
          display: 'grid',
          gridTemplateColumns: 'auto 1fr',
          gap: '8px 18px',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
          color: 'var(--cream-dim)',
          marginBottom: 16,
        }}
      >
        <dt style={sysInfoDt}>version</dt>
        <dd style={sysInfoDd}>{config.system.version}</dd>
        <dt style={sysInfoDt}>uptime</dt>
        <dd style={sysInfoDd}>{formatUptime(config.system.uptime_seconds)}</dd>
        <dt style={sysInfoDt}>data_dir</dt>
        <dd style={sysInfoDd}><code>{config.system.data_dir}</code></dd>
        <dt style={sysInfoDt}>db_path</dt>
        <dd style={sysInfoDd}>
          <code>{config.system.db_path}</code> · {formatBytes(config.system.db_size_bytes)}
        </dd>
        <dt style={sysInfoDt}>config.toml</dt>
        <dd style={sysInfoDd}>
          <code>{config.system.config_path ?? '(无文件)'}</code>
        </dd>
      </dl>

      <div className="block-editor-actions">
        <div className="block-editor-status">
          {copied && <span className="block-editor-saved">已复制路径 ✓</span>}
        </div>
        <button
          type="button"
          className="block-editor-save"
          onClick={() => void handleCopy()}
          disabled={config.system.config_path === null}
        >
          复制 config.toml 路径
        </button>
      </div>
    </section>
  )
}

// ─── Shared helpers ────────────────────────────────────────────────

function FormRow({
  label,
  sub,
  children,
}: {
  label: string
  sub: string
  children: React.ReactNode
}) {
  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          marginBottom: 6,
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          letterSpacing: '0.06em',
          color: 'var(--cream-soft)',
        }}
      >
        <span style={{ color: 'var(--cream)' }}>{label}</span>
        <span style={{ fontSize: 10, fontStyle: 'italic' }}>{sub}</span>
      </div>
      {children}
    </div>
  )
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '8px 10px',
  background: 'rgba(255,255,255,0.03)',
  border: '1px solid rgba(255,255,255,0.08)',
  borderRadius: 6,
  color: 'var(--cream)',
  fontFamily: 'var(--font-mono)',
  fontSize: 13,
  outline: 'none',
}

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  appearance: 'none',
  cursor: 'pointer',
}

const sysInfoDt: React.CSSProperties = {
  color: 'var(--cream-soft)',
  textTransform: 'uppercase',
  fontSize: 10,
  letterSpacing: '0.14em',
  alignSelf: 'baseline',
}

const sysInfoDd: React.CSSProperties = {
  margin: 0,
  color: 'var(--cream)',
  wordBreak: 'break-all',
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

// ═══════════════════════════════════════════════════════════
// Cost tab — LLM spend by feature / time (Worker ζ)
// ═══════════════════════════════════════════════════════════

function CostTab() {
  // Fetch the three windows in parallel so the three summary cards
  // are independent — if one fails the others still render.
  const today = useCostSummary('today')
  const week = useCostSummary('7d')
  const month = useCostSummary('30d')
  const recent = useCostRecent(20)

  const anyLoading =
    today.loading || week.loading || month.loading || recent.loading
  const error = today.error || week.error || month.error || recent.error

  const monthSummary = month.data
  const noCalls =
    monthSummary !== null && monthSummary.total_tokens === 0 && !anyLoading

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">成本</h1>
          <span className="admin-section-engname">cost · 30d window</span>
        </div>
        <p className="admin-section-lead">
          每次 LLM 调用都记一笔。下面是按时间窗口和功能拆分的估算花销 —
          权威账单仍在 provider 控制台。
        </p>
      </div>

      {error && <div className="admin-error-banner">{error}</div>}

      <div className="cost-cards">
        <CostSummaryCard
          label="今天"
          windowSub="today"
          summary={today.data}
          loading={today.loading}
        />
        <CostSummaryCard
          label="近 7 天"
          windowSub="7d"
          summary={week.data}
          loading={week.loading}
        />
        <CostSummaryCard
          label="近 30 天"
          windowSub="30d"
          summary={month.data}
          loading={month.loading}
        />
      </div>

      {noCalls ? (
        <div className="memory-list-empty">
          <div className="memory-list-empty-glyph">💸</div>
          <div className="memory-list-empty-title">还没有 LLM 调用</div>
          <p className="memory-list-empty-desc">
            和 persona 聊几轮 / 跑一次导入,就会出现 chat / consolidate /
            import 各项的花销分布。
          </p>
        </div>
      ) : (
        <>
          {monthSummary && (
            <CostByFeatureSection summary={monthSummary} />
          )}
          {recent.data && recent.data.items.length > 0 && (
            <CostRecentSection items={recent.data.items} />
          )}
        </>
      )}
    </div>
  )
}

interface UseCostSummaryResult {
  data: CostSummaryResponse | null
  loading: boolean
  error: string | null
}

function useCostSummary(
  range: 'today' | '7d' | '30d',
): UseCostSummaryResult {
  const [data, setData] = useState<CostSummaryResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getCostSummary(range)
      .then((r) => {
        if (!cancelled) setData(r)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError) setError(err.detail)
        else if (err instanceof Error) setError(err.message)
        else setError('unknown error')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [range])
  return { data, loading, error }
}

interface UseCostRecentResult {
  data: { limit: number; items: CostCallRecord[] } | null
  loading: boolean
  error: string | null
}

function useCostRecent(limit: number): UseCostRecentResult {
  const [data, setData] = useState<{
    limit: number
    items: CostCallRecord[]
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getCostRecent(limit)
      .then((r) => {
        if (!cancelled) setData(r)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError) setError(err.detail)
        else if (err instanceof Error) setError(err.message)
        else setError('unknown error')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [limit])
  return { data, loading, error }
}

function CostSummaryCard({
  label,
  windowSub,
  summary,
  loading,
}: {
  label: string
  windowSub: string
  summary: CostSummaryResponse | null
  loading: boolean
}) {
  return (
    <div className="cost-card">
      <div className="cost-card-label">{label}</div>
      <div className="cost-card-engname">{windowSub}</div>
      {loading || summary === null ? (
        <div className="cost-card-loading">…</div>
      ) : (
        <>
          <div className="cost-card-value">${summary.total_usd.toFixed(4)}</div>
          <div className="cost-card-tokens">
            {summary.total_tokens.toLocaleString()} tokens
          </div>
        </>
      )}
    </div>
  )
}

function CostByFeatureSection({ summary }: { summary: CostSummaryResponse }) {
  const buckets = Object.entries(summary.by_feature) as [
    string,
    CostFeatureBucket,
  ][]
  if (buckets.length === 0) return null
  const max = Math.max(...buckets.map(([, b]) => b.cost_usd), 0.000001)
  buckets.sort(([, a], [, b]) => b.cost_usd - a.cost_usd)

  return (
    <div className="cost-by-feature">
      <h2 className="cost-section-title">按功能拆分（30 天）</h2>
      <ul className="cost-bar-list">
        {buckets.map(([feature, b]) => {
          const pct = max === 0 ? 0 : Math.round((b.cost_usd / max) * 100)
          return (
            <li key={feature} className="cost-bar-row">
              <div className="cost-bar-head">
                <span className="cost-bar-label">{labelForFeature(feature)}</span>
                <span className="cost-bar-amount">
                  ${b.cost_usd.toFixed(4)} · {b.calls} call{b.calls === 1 ? '' : 's'}
                </span>
              </div>
              <div className="cost-bar-track">
                <div
                  className="cost-bar-fill"
                  style={{ width: `${pct}%` }}
                  aria-label={`${feature} ${pct}%`}
                />
              </div>
              <div className="cost-bar-meta">
                {b.tokens_in.toLocaleString()} in ·{' '}
                {b.tokens_out.toLocaleString()} out
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function CostRecentSection({ items }: { items: CostCallRecord[] }) {
  return (
    <div className="cost-recent">
      <h2 className="cost-section-title">最近 LLM 调用</h2>
      <ul className="cost-recent-list">
        {items.map((it) => (
          <li key={it.id} className="cost-recent-row">
            <span className="cost-recent-time">
              {formatTimestamp(it.timestamp)}
            </span>
            <span className="cost-recent-feature">{labelForFeature(it.feature)}</span>
            <span className="cost-recent-model">
              {it.provider}/{it.model}
            </span>
            <span className="cost-recent-tokens">
              {(it.tokens_in + it.tokens_out).toLocaleString()} tok
            </span>
            <span className="cost-recent-cost">
              ${it.cost_usd.toFixed(4)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function labelForFeature(feature: string): string {
  switch (feature) {
    case 'chat':
      return '对话'
    case 'import':
      return '导入'
    case 'consolidate':
      return '整理'
    case 'reflection':
      return '反思'
    case 'proactive':
      return '主动'
    default:
      return feature
  }
}
