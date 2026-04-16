import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { TopBar } from '../components/TopBar'
import type { AdminTab } from '../types'
import type {
  DaemonState,
  PersonaStateApi,
  PersonaUpdatePayload,
} from '../api/types'

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
            <EventsTab count={daemonState.memory_counts.events} />
          )}
          {tab === 'thoughts' && (
            <ThoughtsTab count={daemonState.memory_counts.thoughts} />
          )}
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
// Events tab — L3 real count + coming-soon card for browser/delete UI
// ═══════════════════════════════════════════════════════════

function EventsTab({ count }: { count: number }) {
  return (
    <div className="admin-section">
      <div className="admin-section-head">
        <div className="admin-section-title-row">
          <h1 className="admin-section-title">发生过的事</h1>
          <span className="admin-section-engname">events · L3</span>
        </div>
        <p className="admin-section-lead">
          persona 记得的具体事件。每一条带时间、情感强度、相关的人和情绪。
          服务器上共 <strong>{count}</strong> 条。
        </p>
      </div>

      <div className="voice-empty">
        <div className="voice-empty-glyph">📖</div>
        <div className="voice-empty-title">记忆浏览功能即将推出</div>
        <p className="voice-empty-desc">
          之后你可以在这里翻阅 persona 记得的每一条具体事件、按情绪或人物筛选、
          也能删掉某条记忆。目前只能看到总数。
        </p>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// Thoughts tab — L4 real count + coming-soon card
// ═══════════════════════════════════════════════════════════

function ThoughtsTab({ count }: { count: number }) {
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
          服务器上共 <strong>{count}</strong> 条。
        </p>
      </div>

      <div className="voice-empty">
        <div className="voice-empty-glyph">🪞</div>
        <div className="voice-empty-title">记忆浏览功能即将推出</div>
        <p className="voice-empty-desc">
          之后你可以在这里看 persona 沉淀下来的每一条长期印象、
          也能删掉不准确的观察。目前只能看到总数。
        </p>
      </div>
    </div>
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
