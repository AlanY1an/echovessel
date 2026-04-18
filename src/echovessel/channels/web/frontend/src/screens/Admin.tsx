import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
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
  PersonaFacts,
  PersonaStateApi,
  PersonaUpdatePayload,
  PreviewDeleteResponse,
  TraceNode,
} from '../api/types'
import { PersonaFactsEditor } from '../components/PersonaFactsEditor'
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
  updateFacts: (facts: Partial<PersonaFacts>) => Promise<void>
  toggleVoice: (enabled: boolean) => Promise<void>
  onBackToChat: () => void
}

type TabDef = { id: AdminTab; labelKey: string; sub: string }

const TABS: TabDef[] = [
  { id: 'persona', labelKey: 'admin.tabs.persona', sub: 'persona · 5 blocks' },
  { id: 'events', labelKey: 'admin.tabs.events', sub: 'events · L3' },
  { id: 'thoughts', labelKey: 'admin.tabs.thoughts', sub: 'thoughts · L4' },
  { id: 'voice', labelKey: 'admin.tabs.voice', sub: 'voice toggle' },
  { id: 'cost', labelKey: 'admin.tabs.cost', sub: 'cost · 30d' },
  { id: 'config', labelKey: 'admin.tabs.config', sub: 'config' },
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
  updateFacts,
  toggleVoice,
  onBackToChat,
}: AdminProps) {
  const { t } = useTranslation()
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
        mood={t('admin.subtitle_prefix')}
        back={{ label: t('topbar.back_to_chat'), onClick: onBackToChat }}
      />

      <ChannelStatusStrip channels={daemonState.channels} />

      <div className="admin-layout">
        <aside className="admin-nav">
          <div className="admin-nav-heading">
            <div className="admin-nav-heading-label">{t('admin.page_title')}</div>
            <div className="admin-nav-heading-sub">Admin</div>
          </div>
          <ul className="admin-nav-list">
            {TABS.map((tab_def) => (
              <li key={tab_def.id}>
                <button
                  type="button"
                  className={`admin-nav-item ${tab === tab_def.id ? 'is-active' : ''}`}
                  onClick={() => setTab(tab_def.id)}
                >
                  <div className="admin-nav-item-label">{t(tab_def.labelKey)}</div>
                  <div className="admin-nav-item-sub">{tab_def.sub}</div>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <main className="admin-main">
          {tab === 'persona' && (
            <PersonaTab
              persona={persona}
              onUpdate={updatePersona}
              onUpdateFacts={updateFacts}
            />
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
  const { t } = useTranslation()
  // Dot color: green if ready, orange if enabled-but-not-ready
  // (handshake in progress / transient disconnect), grey if disabled.
  let tone: 'on' | 'warming' | 'off'
  let label: string
  if (!channel.enabled) {
    tone = 'off'
    label = t('admin.channel_status.disabled')
  } else if (!channel.ready) {
    tone = 'warming'
    label = t('admin.channel_status.connecting')
  } else {
    tone = 'on'
    label =
      channel.channel_id === 'discord'
        ? t('admin.channel_status.connected')
        : t('admin.channel_status.ready')
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
  labelKey: string
  engName: string
  hintKey: string
  warningKey?: string
  small?: boolean
}

const BLOCK_META: BlockMeta[] = [
  {
    shortKey: 'persona',
    labelKey: 'admin.persona_blocks.persona_label',
    engName: 'persona_block',
    hintKey: 'admin.persona_blocks.persona_hint',
  },
  {
    shortKey: 'self',
    labelKey: 'admin.persona_blocks.self_label',
    engName: 'self_block',
    hintKey: 'admin.persona_blocks.self_hint',
  },
  {
    shortKey: 'user',
    labelKey: 'admin.persona_blocks.user_label',
    engName: 'user_block',
    hintKey: 'admin.persona_blocks.user_hint',
  },
  {
    shortKey: 'relationship',
    labelKey: 'admin.persona_blocks.relationship_label',
    engName: 'relationship_block',
    hintKey: 'admin.persona_blocks.relationship_hint',
  },
  {
    shortKey: 'mood',
    labelKey: 'admin.persona_blocks.mood_label',
    engName: 'mood_block',
    hintKey: 'admin.persona_blocks.mood_hint',
    warningKey: 'admin.persona_blocks.mood_warning',
    small: true,
  },
]

/** Compare two facts objects field-by-field. Used to decide whether
 *  the save button should enable — a plain reference compare is not
 *  enough because ``setFactsDraft`` always clones on edit. */
function factsEqual(a: PersonaFacts, b: PersonaFacts): boolean {
  const keys: (keyof PersonaFacts)[] = [
    'full_name',
    'gender',
    'birth_date',
    'ethnicity',
    'nationality',
    'native_language',
    'locale_region',
    'education_level',
    'occupation',
    'occupation_field',
    'location',
    'timezone',
    'relationship_status',
    'life_stage',
    'health_status',
  ]
  for (const k of keys) {
    if ((a[k] ?? null) !== (b[k] ?? null)) return false
  }
  return true
}

function PersonaTab({
  persona,
  onUpdate,
  onUpdateFacts,
}: {
  persona: PersonaStateApi
  onUpdate: (payload: PersonaUpdatePayload) => Promise<void>
  onUpdateFacts: (facts: Partial<PersonaFacts>) => Promise<void>
}) {
  const { t } = useTranslation()
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

  const [factsDraft, setFactsDraft] = useState<PersonaFacts>(persona.facts)
  const [factsExpanded, setFactsExpanded] = useState(false)
  const [factsSaving, setFactsSaving] = useState(false)
  const [factsSavedAt, setFactsSavedAt] = useState<number | null>(null)
  // Re-sync the draft when the upstream persona refreshes (e.g. after
  // a successful save elsewhere). Cheap — fires only when the identity
  // of ``persona.facts`` changes.
  useEffect(() => {
    setFactsDraft(persona.facts)
  }, [persona.facts])
  const factsDirty = !factsEqual(factsDraft, persona.facts)

  const handleSaveFacts = async () => {
    if (!factsDirty || factsSaving) return
    setFactsSaving(true)
    try {
      await onUpdateFacts(factsDraft)
      setFactsSavedAt(Date.now())
      window.setTimeout(() => setFactsSavedAt(null), 2000)
    } finally {
      setFactsSaving(false)
    }
  }

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <h1 className="admin-section-title">
          {t('admin.persona_blocks.section_title')}
        </h1>
        <p className="admin-section-lead">
          {t('admin.persona_blocks.section_lead')}
        </p>
      </div>

      <div className="admin-name-row">
        <label className="admin-name-label">
          {t('admin.persona_blocks.display_name')}
          <span className="admin-name-hint">
            {t('admin.persona_blocks.display_name_hint')}
          </span>
        </label>
        <div className="admin-name-controls">
          <input
            className="admin-name-input"
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            placeholder={t('onboarding.name_placeholder')}
            maxLength={256}
            disabled={nameSaving}
          />
          <button
            type="button"
            className="block-editor-save"
            disabled={!nameDirty || nameSaving || !nameDraft.trim()}
            onClick={() => void handleSaveName()}
          >
            {nameSaving
              ? '⋯'
              : nameSavedAt
                ? t('admin.common.saved')
                : t('admin.persona_blocks.display_name_rename')}
          </button>
        </div>
      </div>

      <section className="facts-section">
        <header
          className="facts-section-header"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            cursor: 'pointer',
            userSelect: 'none',
          }}
          onClick={() => setFactsExpanded((x) => !x)}
        >
          <span>
            {t('facts.section_title')}
            <span
              style={{
                fontSize: 11,
                marginLeft: 8,
                color: 'rgba(255, 255, 255, 0.38)',
                textTransform: 'none',
                letterSpacing: 0,
              }}
            >
              {t('facts.section_hint')}
            </span>
          </span>
          <span
            style={{
              fontSize: 12,
              color: 'rgba(255, 255, 255, 0.55)',
            }}
          >
            {factsExpanded ? '▾' : '▸'}
          </span>
        </header>

        {factsExpanded && (
          <>
            <PersonaFactsEditor
              value={factsDraft}
              onChange={setFactsDraft}
              disabled={factsSaving}
            />
            <div className="block-editor-actions">
              <div className="block-editor-status">
                {factsSavedAt && (
                  <span className="block-editor-saved">
                    {t('admin.common.saved')}
                  </span>
                )}
                {!factsSavedAt && factsDirty && (
                  <span className="block-editor-dirty">
                    {t('admin.common.unsaved_warning')}
                  </span>
                )}
              </div>
              <button
                type="button"
                className="block-editor-save"
                disabled={!factsDirty || factsSaving}
                onClick={() => void handleSaveFacts()}
              >
                {factsSaving ? '⋯' : t('admin.common.save')}
              </button>
            </div>
          </>
        )}
      </section>

      <div className="admin-hint-card">
        <div className="admin-hint-glyph">📥</div>
        <div className="admin-hint-body">
          <div className="admin-hint-title">
            {t('admin.persona_blocks.import_prompt')}
          </div>
          <div
            className="admin-hint-desc"
            dangerouslySetInnerHTML={{
              __html: t('admin.persona_blocks.import_body'),
            }}
          />
        </div>
        <button
          type="button"
          className="admin-hint-btn"
          onClick={() => navigate('/admin/import')}
        >
          {t('admin.persona_blocks.import_cta')}
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
  const { t } = useTranslation()
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
          <h3 className="block-editor-label">{t(meta.labelKey)}</h3>
          <span className="block-editor-engname">{meta.engName}</span>
        </div>
        <p className="block-editor-hint">{t(meta.hintKey)}</p>
        {meta.warningKey && (
          <p className="block-editor-warning">⚠ {t(meta.warningKey)}</p>
        )}
      </header>
      <textarea
        className={`block-editor-textarea ${meta.small ? 'is-small' : ''}`}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={meta.small ? 2 : 6}
        placeholder={
          meta.small
            ? t('admin.persona_blocks.placeholder_small')
            : t('admin.persona_blocks.placeholder_default')
        }
        disabled={saving}
      />
      <div className="block-editor-actions">
        <div className="block-editor-status">
          {savedAt && (
            <span className="block-editor-saved">{t('admin.common.saved')}</span>
          )}
          {!savedAt && dirty && (
            <span className="block-editor-dirty">
              {t('admin.common.unsaved_warning')}
            </span>
          )}
          {!savedAt && !dirty && (
            <span className="block-editor-count">
              {t('admin.persona_blocks.char_count', {
                count: draft.length,
              })}
            </span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={() => void handleSave()}
        >
          {saving ? '⋯' : t('admin.common.save')}
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
  const { t } = useTranslation()
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
      const choice = await confirmDelete(item.description, preview, t)
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
          <h1 className="admin-section-title">
            {t('admin.events.section_title')}
          </h1>
          <span className="admin-section-engname">events · L3</span>
        </div>
        <p
          className="admin-section-lead"
          dangerouslySetInnerHTML={{
            __html: t('admin.events.lead', { count: total }),
          }}
        />
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
          <div className="memory-list-loading">
            {t('admin.events.searching')}
          </div>
        ) : visibleTotal === 0 ? (
          <div className="memory-list-empty">
            <div className="memory-list-empty-glyph">🔍</div>
            <div className="memory-list-empty-title">
              {t('admin.events.no_match_title')}
            </div>
            <p className="memory-list-empty-desc">
              {t('admin.events.no_match_body')}
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
        <div className="memory-list-loading">{t('admin.events.loading')}</div>
      ) : total === 0 ? (
        <div className="memory-list-empty">
          <div className="memory-list-empty-glyph">📖</div>
          <div className="memory-list-empty-title">
            {t('admin.events.empty_title')}
          </div>
          <p className="memory-list-empty-desc">
            {t('admin.events.empty_body')}
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
            {loadingMore
              ? t('admin.events.loading_more')
              : t('admin.events.load_more', {
                  remaining: total - items.length,
                })}
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
  const { t } = useTranslation()
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
      const choice = await confirmDelete(item.description, preview, t)
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
          <h1 className="admin-section-title">
            {t('admin.thoughts.section_title')}
          </h1>
          <span className="admin-section-engname">thoughts · L4</span>
        </div>
        <p
          className="admin-section-lead"
          dangerouslySetInnerHTML={{
            __html: t('admin.thoughts.lead', { count: total }),
          }}
        />
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
          <div className="memory-list-loading">
            {t('admin.events.searching')}
          </div>
        ) : visibleTotal === 0 ? (
          <div className="memory-list-empty">
            <div className="memory-list-empty-glyph">🔍</div>
            <div className="memory-list-empty-title">
              {t('admin.thoughts.no_match_title')}
            </div>
            <p className="memory-list-empty-desc">
              {t('admin.thoughts.no_match_body')}
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
        <div className="memory-list-loading">{t('admin.events.loading')}</div>
      ) : total === 0 ? (
        <div className="memory-list-empty">
          <div className="memory-list-empty-glyph">🪞</div>
          <div className="memory-list-empty-title">
            {t('admin.thoughts.empty_title')}
          </div>
          <p className="memory-list-empty-desc">
            {t('admin.thoughts.empty_body')}
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
            {loadingMore
              ? t('admin.events.loading_more')
              : t('admin.events.load_more', {
                  remaining: total - items.length,
                })}
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
  const { t } = useTranslation()
  return (
    <div className="memory-search-bar">
      <span className="memory-search-icon" aria-hidden>
        🔍
      </span>
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t('admin.memory_list.search_placeholder')}
        className="memory-search-input"
        aria-label={t('admin.memory_list.search_aria')}
      />
      {active && (
        <span className="memory-search-meta">
          {loading
            ? t('admin.events.searching')
            : t('admin.memory_list.count', { count: total })}
        </span>
      )}
      {value && (
        <button
          type="button"
          className="memory-search-clear"
          onClick={onClear}
          aria-label={t('admin.memory_list.clear_aria')}
        >
          {t('admin.memory_list.clear')}
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
  const { t } = useTranslation()
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
    kind === 'thought'
      ? t('admin.memory_list.lineage_sources')
      : t('admin.memory_list.lineage_thoughts')

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
          aria-label={
            kind === 'event'
              ? t('admin.events.delete_aria', { id: item.id })
              : t('admin.thoughts.delete_aria', { id: item.id })
          }
          title={
            kind === 'event'
              ? t('admin.events.delete_title')
              : t('admin.thoughts.delete_title')
          }
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
            ? t('admin.memory_list.lineage_searching')
            : `${toggleLabel}${
                trace.data
                  ? ' ' +
                    t('admin.memory_list.lineage_count', {
                      count: expandedNodes.length,
                    })
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
                  ? t('admin.memory_list.no_sources')
                  : t('admin.memory_list.no_derivatives')}
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
                    title={
                      kind === 'thought'
                        ? `${t('admin.memory_list.jump_to_event')} ${n.id}`
                        : `${t('admin.memory_list.jump_to_thought')} ${n.id}`
                    }
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
                {t('admin.memory_list.from_sessions', {
                  count: trace.data.response.source_sessions.length,
                })}
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
type TFn = (key: string, opts?: Record<string, unknown>) => string

function confirmDelete(
  description: string,
  preview: PreviewDeleteResponse,
  t: TFn,
): Promise<'orphan' | 'cascade' | null> {
  const truncated =
    description.length > 80 ? `${description.slice(0, 80)}…` : description

  if (!preview.has_dependents) {
    const ok = window.confirm(
      t('admin.memory_list.confirm_delete_simple', { preview: truncated }),
    )
    return Promise.resolve(ok ? 'orphan' : null)
  }

  const depCount = preview.dependent_thought_ids.length
  const depsList = preview.dependent_thought_descriptions
    .slice(0, 3)
    .map((d, i) => `${i + 1}. ${d.length > 60 ? `${d.slice(0, 60)}…` : d}`)
    .join('\n')

  const cascadeMsg = t('admin.memory_list.confirm_delete_cascade', {
    preview: truncated,
    depCount,
    depsList,
  })
  const cascade = window.confirm(cascadeMsg)
  // confirm=true → cascade, confirm=false → orphan. Esc-as-cancel
  // needs a real modal — flag for a later stage.
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
  const { t } = useTranslation()
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
          <h1 className="admin-section-title">
            {t('admin.voice_tab.section_title')}
          </h1>
          <span className="admin-section-engname">voice toggle</span>
        </div>
        <p className="admin-section-lead">{t('admin.voice_tab.section_lead')}</p>
      </div>

      <div className="admin-hint-card" style={{ marginBottom: 16 }}>
        <div className="admin-hint-glyph">🎙</div>
        <div className="admin-hint-body">
          <div className="admin-hint-title">
            {t('admin.voice_tab.clone_prompt')}
          </div>
          <div className="admin-hint-desc">
            {t('admin.voice_tab.clone_body')}
          </div>
        </div>
        <button
          type="button"
          className="admin-hint-btn"
          onClick={() => navigate('/admin/voice/clone')}
        >
          {t('admin.voice_tab.clone_cta')}
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
              {voiceEnabled
                ? t('admin.voice_tab.toggle_label_on')
                : t('admin.voice_tab.toggle_label_off')}
            </div>
            <div className="voice-card-meta">
              {voiceEnabled
                ? t('admin.voice_tab.toggle_help_on')
                : t('admin.voice_tab.toggle_help_off')}
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
            {toggling
              ? '⋯'
              : voiceEnabled
                ? t('admin.voice_tab.toggle_disable')
                : t('admin.voice_tab.toggle_enable')}
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
  const { t } = useTranslation()
  const { config, loading, saving, error, lastSaved, save } = useConfig()

  if (loading && config === null) {
    return (
      <div className="admin-section">
        <div className="admin-section-head">
          <h1 className="admin-section-title">
            {t('admin.config_tab.section_title')}
          </h1>
          <p className="admin-section-lead">{t('admin.config_tab.loading')}</p>
        </div>
      </div>
    )
  }

  if (config === null) {
    return (
      <div className="admin-section">
        <div className="admin-section-head">
          <h1 className="admin-section-title">
            {t('admin.config_tab.section_title')}
          </h1>
          <p className="admin-section-lead" style={{ color: 'rgba(255,120,120,0.78)' }}>
            ⚠ {error ?? t('admin.config_tab.load_error_fallback')}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">
            {t('admin.config_tab.section_title')}
          </h1>
          <span className="admin-section-engname">runtime config</span>
        </div>
        <p
          className="admin-section-lead"
          dangerouslySetInnerHTML={{ __html: t('admin.config_tab.lead') }}
        />
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
            <div className="admin-hint-title">
              {t('admin.config_tab.save_failed_title')}
            </div>
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
  const { t } = useTranslation()
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
          <h3 className="block-editor-label">
            {t('admin.config_tab.llm_section_title')}
          </h3>
          <span className="block-editor-engname">llm.*</span>
        </div>
        <p
          className="block-editor-hint"
          dangerouslySetInnerHTML={{
            __html: t('admin.config_tab.llm_lead'),
          }}
        />
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
          sub={t('admin.config_tab.llm_temperature_sub', {
            value: temperature.toFixed(2),
          })}
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

        <FormRow
          label="max_tokens"
          sub={t('admin.config_tab.llm_max_tokens_sub')}
        >
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

        <FormRow
          label="timeout_seconds"
          sub={t('admin.config_tab.llm_timeout_sub')}
        >
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

        <FormRow
          label="api_key_env"
          sub={t('admin.config_tab.llm_api_key_sub')}
        >
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
            <span className="block-editor-saved">
              {t('admin.config_tab.llm_saved_note')}
            </span>
          )}
          {!recentlySaved && dirty && (
            <span className="block-editor-dirty">
              {t('admin.common.unsaved_warning')}
            </span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={handleSave}
        >
          {saving ? '⋯' : t('admin.common.save')}
        </button>
      </div>
    </section>
  )
}

// ─── Card 2 · Memory tuning ─────────────────────────────────────────

function ConfigMemoryCard({ config, saving, save, lastSaved }: ConfigCardProps) {
  const { t } = useTranslation()
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
          <h3 className="block-editor-label">
            {t('admin.config_tab.memory_section_title')}
          </h3>
          <span className="block-editor-engname">memory.*</span>
        </div>
        <p className="block-editor-hint">
          {t('admin.config_tab.memory_lead')}
        </p>
      </header>

      <div style={{ display: 'grid', gap: 14 }}>
        <FormRow
          label="retrieve_k"
          sub={t('admin.config_tab.memory_retrieve_k_sub', {
            value: retrieveK,
          })}
        >
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
          sub={t('admin.config_tab.memory_bonus_sub', {
            value: bonus.toFixed(2),
          })}
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
          sub={t('admin.config_tab.memory_recent_sub', { value: recent })}
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
            <span className="block-editor-saved">
              {t('admin.common.saved')}
            </span>
          )}
          {!recentlySaved && dirty && (
            <span className="block-editor-dirty">
              {t('admin.common.unsaved_warning')}
            </span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={handleSave}
        >
          {saving ? '⋯' : t('admin.common.save')}
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
  const { t } = useTranslation()
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
          <h3 className="block-editor-label">
            {t('admin.config_tab.consolidate_section_title')}
          </h3>
          <span className="block-editor-engname">consolidate.*</span>
        </div>
        <p className="block-editor-hint">
          {t('admin.config_tab.consolidate_lead')}
        </p>
      </header>

      <div style={{ display: 'grid', gap: 14 }}>
        <FormRow
          label="trivial_message_count"
          sub={t('admin.config_tab.consolidate_trivial_msgs_sub')}
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
          sub={t('admin.config_tab.consolidate_trivial_tokens_sub')}
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
          sub={t('admin.config_tab.consolidate_reflection_limit_sub')}
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
            <span className="block-editor-saved">
              {t('admin.common.saved')}
            </span>
          )}
          {!recentlySaved && dirty && (
            <span className="block-editor-dirty">
              {t('admin.common.unsaved_warning')}
            </span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          disabled={!dirty || saving}
          onClick={handleSave}
        >
          {saving ? '⋯' : t('admin.common.save')}
        </button>
      </div>
    </section>
  )
}

// ─── Card 4 · System info (read-only) ──────────────────────────────

function ConfigSystemCard({ config }: { config: ConfigGetResponse }) {
  const { t } = useTranslation()
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
          <h3 className="block-editor-label">
            {t('admin.config_tab.system_section_title')}
          </h3>
          <span className="block-editor-engname">read-only</span>
        </div>
        <p
          className="block-editor-hint"
          dangerouslySetInnerHTML={{
            __html: t('admin.config_tab.system_lead'),
          }}
        />
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
          <code>
            {config.system.config_path ?? t('admin.config_tab.system_no_file')}
          </code>
        </dd>
      </dl>

      <div className="block-editor-actions">
        <div className="block-editor-status">
          {copied && (
            <span className="block-editor-saved">
              {t('admin.config_tab.system_copied')}
            </span>
          )}
        </div>
        <button
          type="button"
          className="block-editor-save"
          onClick={() => void handleCopy()}
          disabled={config.system.config_path === null}
        >
          {t('admin.config_tab.system_copy_cta')}
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
  const { t } = useTranslation()
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
          <h1 className="admin-section-title">{t('admin.cost.section_title')}</h1>
          <span className="admin-section-engname">cost · 30d window</span>
        </div>
        <p className="admin-section-lead">{t('admin.cost.lead')}</p>
      </div>

      {error && <div className="admin-error-banner">{error}</div>}

      <div className="cost-cards">
        <CostSummaryCard
          label={t('admin.cost.today')}
          windowSub="today"
          summary={today.data}
          loading={today.loading}
        />
        <CostSummaryCard
          label={t('admin.cost.last_7d')}
          windowSub="7d"
          summary={week.data}
          loading={week.loading}
        />
        <CostSummaryCard
          label={t('admin.cost.last_30d')}
          windowSub="30d"
          summary={month.data}
          loading={month.loading}
        />
      </div>

      {noCalls ? (
        <div className="memory-list-empty">
          <div className="memory-list-empty-glyph">💸</div>
          <div className="memory-list-empty-title">
            {t('admin.cost.empty_title')}
          </div>
          <p className="memory-list-empty-desc">{t('admin.cost.empty_body')}</p>
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
  const { t } = useTranslation()
  const buckets = Object.entries(summary.by_feature) as [
    string,
    CostFeatureBucket,
  ][]
  if (buckets.length === 0) return null
  const max = Math.max(...buckets.map(([, b]) => b.cost_usd), 0.000001)
  buckets.sort(([, a], [, b]) => b.cost_usd - a.cost_usd)

  return (
    <div className="cost-by-feature">
      <h2 className="cost-section-title">
        {t('admin.cost.feature_breakdown_title')}
      </h2>
      <ul className="cost-bar-list">
        {buckets.map(([feature, b]) => {
          const pct = max === 0 ? 0 : Math.round((b.cost_usd / max) * 100)
          return (
            <li key={feature} className="cost-bar-row">
              <div className="cost-bar-head">
                <span className="cost-bar-label">{labelForFeature(feature, t)}</span>
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
  const { t } = useTranslation()
  return (
    <div className="cost-recent">
      <h2 className="cost-section-title">
        {t('admin.cost.recent_calls_title')}
      </h2>
      <ul className="cost-recent-list">
        {items.map((it) => (
          <li key={it.id} className="cost-recent-row">
            <span className="cost-recent-time">
              {formatTimestamp(it.timestamp)}
            </span>
            <span className="cost-recent-feature">
              {labelForFeature(it.feature, t)}
            </span>
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

function labelForFeature(feature: string, t: TFn): string {
  switch (feature) {
    case 'chat':
      return t('admin.cost.feature_chat')
    case 'import':
      return t('admin.cost.feature_import')
    case 'consolidate':
      return t('admin.cost.feature_consolidate')
    case 'reflection':
      return t('admin.cost.feature_reflection')
    case 'proactive':
      return t('admin.cost.feature_proactive')
    default:
      return feature
  }
}
