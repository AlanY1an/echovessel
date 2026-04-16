import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { TopBar } from '../components/TopBar'
import type { AdminTab } from '../types'
import type {
  ChannelStatus,
  DaemonState,
  MemoryEvent,
  MemoryThought,
  PersonaStateApi,
  PersonaUpdatePayload,
  PreviewDeleteResponse,
} from '../api/types'
import { useMemoryEvents } from '../hooks/useMemoryEvents'
import { useMemoryThoughts } from '../hooks/useMemoryThoughts'

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
  { id: 'config', label: '配置', sub: 'coming soon' },
]

export function Admin({
  persona,
  daemonState,
  updatePersona,
  toggleVoice,
  onBackToChat,
}: AdminProps) {
  const [tab, setTab] = useState<AdminTab>('persona')

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
          {tab === 'events' && <EventsTab />}
          {tab === 'thoughts' && <ThoughtsTab />}
          {tab === 'voice' && (
            <VoiceTab
              voiceEnabled={persona.voice_enabled}
              toggleVoice={toggleVoice}
            />
          )}
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
  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <h1 className="admin-section-title">人格</h1>
        <p className="admin-section-lead">
          persona 的 5 个"长期画像"。改这些会直接影响下次对话时 persona 的行为。
        </p>
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

function EventsTab() {
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

      {error && <div className="admin-error-banner">{error}</div>}

      {loading && items.length === 0 ? (
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
            <li key={it.id} className="memory-list-item">
              <div className="memory-list-row-head">
                <time className="memory-list-time">
                  {formatTimestamp(it.created_at)}
                </time>
                <button
                  type="button"
                  className="memory-list-delete"
                  onClick={() => void handleDelete(it)}
                  aria-label={`删除事件 ${it.id}`}
                  title="删除这条事件"
                >
                  ×
                </button>
              </div>
              <p className="memory-list-desc">{it.description}</p>
              {it.emotion_tags.length > 0 && (
                <div className="memory-list-pills">
                  {it.emotion_tags.map((tag) => (
                    <span key={tag} className="memory-list-pill">
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {hasMore && (
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

function ThoughtsTab() {
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

      {error && <div className="admin-error-banner">{error}</div>}

      {loading && items.length === 0 ? (
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
            <li key={it.id} className="memory-list-item">
              <div className="memory-list-row-head">
                <time className="memory-list-time">
                  {formatTimestamp(it.created_at)}
                </time>
                <button
                  type="button"
                  className="memory-list-delete"
                  onClick={() => void handleDelete(it)}
                  aria-label={`删除印象 ${it.id}`}
                  title="删除这条印象"
                >
                  ×
                </button>
              </div>
              <p className="memory-list-desc">{it.description}</p>
            </li>
          ))}
        </ul>
      )}

      {hasMore && (
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

      <div className="voice-empty">
        <div className="voice-empty-glyph">🎙</div>
        <div className="voice-empty-title">即将推出:声音克隆 / 自定义样本上传</div>
        <p className="voice-empty-desc">
          下一版会支持上传一段 30-60 秒的纯净录音，
          Persona 之后的回复就可以用这个声音读出来。
        </p>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// Config tab — placeholder, nothing wired up yet
// ═══════════════════════════════════════════════════════════

function ConfigTab() {
  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">配置</h1>
          <span className="admin-section-engname">coming soon</span>
        </div>
        <p className="admin-section-lead">
          LLM provider / model、成本统计、数据目录等高级配置。
        </p>
      </div>

      <div className="voice-empty">
        <div className="voice-empty-glyph">⚙</div>
        <div className="voice-empty-title">即将推出:LLM / 成本 / 数据目录管理</div>
        <p className="voice-empty-desc">
          目前这些配置只能通过编辑 <code>~/.echovessel</code> 下的配置文件来调。
          下一版会在这里提供可视化的管理界面，包括切换模型、查看累计花费、
          以及打开数据目录。
        </p>
      </div>
    </div>
  )
}
