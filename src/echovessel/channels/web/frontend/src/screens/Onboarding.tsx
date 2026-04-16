import { useState } from 'react'
import { TopBar } from '../components/TopBar'
import {
  postImportUploadText,
  postPersonaBootstrapFromMaterial,
  postPersonaUpdate,
} from '../api/client'
import { ApiError } from '../api/types'
import type {
  OnboardingPayload,
  PersonaBootstrapResponse,
} from '../api/types'

type Step =
  | 'welcome'
  | 'blank-write'
  | 'import-upload'
  | 'import-waiting'
  | 'import-review'

interface OnboardingProps {
  completeOnboarding: (payload: OnboardingPayload) => Promise<void>
  error: string | null
}

/**
 * Default display_name used when the user does not explicitly name the
 * persona in the minimal blank-write flow. Matches the "她的声音" /
 * "她在听" tone of the rest of the prototype UI. The user can rename
 * the persona later from Admin → 人格 → display_name (v1.x UI).
 */
const DEFAULT_DISPLAY_NAME = '她'

const MIN_MATERIAL_CHARS = 80

export function Onboarding({ completeOnboarding, error }: OnboardingProps) {
  const [step, setStep] = useState<Step>('welcome')

  // Blank-write state.
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Import-upload state.
  const [material, setMaterial] = useState('')
  const [importError, setImportError] = useState<string | null>(null)
  const [bootstrap, setBootstrap] =
    useState<PersonaBootstrapResponse | null>(null)

  // Import-review edit state. Five blocks, individually editable
  // before we POST them to /api/admin/persona/onboarding.
  const [draftPersona, setDraftPersona] = useState('')
  const [draftSelf, setDraftSelf] = useState('')
  const [draftUser, setDraftUser] = useState('')
  const [draftMood, setDraftMood] = useState('')
  const [draftRelationship, setDraftRelationship] = useState('')

  const charCount = text.trim().length
  const canSubmit = charCount >= 10 && !submitting

  const handleSubmit = async () => {
    if (!canSubmit) return

    setSubmitting(true)
    try {
      await completeOnboarding({
        display_name: name.trim() || DEFAULT_DISPLAY_NAME,
        persona_block: text.trim(),
        self_block: '',
        user_block: '',
        mood_block: '',
      })
    } catch {
      // Error is surfaced via the `error` prop from usePersona().
    } finally {
      setSubmitting(false)
    }
  }

  const materialChars = material.trim().length
  const canRunImport = materialChars >= MIN_MATERIAL_CHARS && !submitting

  const handleRunImport = async () => {
    if (!canRunImport) return
    setImportError(null)
    setSubmitting(true)
    setStep('import-waiting')

    try {
      // Stage 1: persist the text upload. Reuses the generic import
      // upload endpoint — no onboarding-specific staging needed.
      const upload = await postImportUploadText({
        text: material.trim(),
        source_label: 'onboarding_material',
      })

      // Stage 2: server-side blocking call. Starts the pipeline, waits
      // for pipeline.done, drives the bootstrap LLM, returns five
      // suggested blocks.
      const result = await postPersonaBootstrapFromMaterial({
        upload_id: upload.upload_id,
        persona_display_name: DEFAULT_DISPLAY_NAME,
      })

      setBootstrap(result)
      setDraftPersona(result.suggested_blocks.persona_block)
      setDraftSelf(result.suggested_blocks.self_block)
      setDraftUser(result.suggested_blocks.user_block)
      setDraftMood(result.suggested_blocks.mood_block)
      setDraftRelationship(result.suggested_blocks.relationship_block)
      setStep('import-review')
    } catch (err) {
      let msg = '导入失败'
      if (err instanceof ApiError) {
        msg = err.detail
      } else if (err instanceof Error) {
        msg = err.message
      }
      setImportError(msg)
      setStep('import-upload')
    } finally {
      setSubmitting(false)
    }
  }

  const handleCommitReviewed = async () => {
    if (submitting) return
    setSubmitting(true)
    try {
      // Send only the four blocks the /onboarding endpoint accepts.
      await completeOnboarding({
        display_name: DEFAULT_DISPLAY_NAME,
        persona_block: draftPersona.trim(),
        self_block: draftSelf.trim(),
        user_block: draftUser.trim(),
        mood_block: draftMood.trim(),
      })
      // relationship_block is written via a follow-up PATCH below —
      // the onboarding contract intentionally does not include it, so
      // we use the partial-update endpoint once the persona exists.
      const rel = draftRelationship.trim()
      if (rel.length > 0) {
        try {
          await postPersonaUpdate({ relationship_block: rel })
        } catch {
          // Relationship write is best-effort — if it fails, the
          // persona is still fully onboarded with the other four
          // blocks. User can retry from Admin.
        }
      }
    } catch {
      // Error surfaces via the `error` prop.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="onboarding-wrap">
      <TopBar mood="等你开始" />

      {step === 'welcome' && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            一个会记住你的人<span className="onboarding-punct">。</span>
          </h1>
          <p className="onboarding-lead">
            在我们开始之前,先告诉我——这个 persona 是谁?
          </p>

          <div className="onboarding-paths">
            <button
              type="button"
              className="path-card"
              onClick={() => setStep('blank-write')}
            >
              <div className="path-card-index">01</div>
              <div className="path-card-body">
                <div className="path-card-title">我自己写</div>
                <p className="path-card-desc">
                  用几句话描述 ta 的性格、说话的调子、你们的关系。最简单的起点。
                </p>
              </div>
              <span className="path-card-arrow">→</span>
            </button>

            <button
              type="button"
              className="path-card"
              onClick={() => {
                setImportError(null)
                setStep('import-upload')
              }}
            >
              <div className="path-card-index">02</div>
              <div className="path-card-body">
                <div className="path-card-title">
                  上传材料让它自动生成
                </div>
                <p className="path-card-desc">
                  聊天记录、文章、日记、自传——让 LLM 读完之后为你写出一个 persona 的初稿。
                </p>
              </div>
              <span className="path-card-arrow">→</span>
            </button>
          </div>

          <div className="onboarding-footnote">
            所有内容都只存在你这台机器上。没有服务器,没有第三方。
          </div>
        </main>
      )}

      {step === 'blank-write' && (
        <main className="onboarding">
          <button
            type="button"
            className="onboarding-back"
            onClick={() => setStep('welcome')}
            disabled={submitting}
          >
            ← 返回
          </button>

          <h1 className="onboarding-title">
            这个 persona 是谁<span className="onboarding-punct">?</span>
          </h1>
          <p className="onboarding-lead">
            写几句话描述。可以是 ta 的性格、说话的习惯、你们的关系——任何你想让 ta 从一开始就知道的东西。
            <br />
            其他的(你的身份、发生过的事、你身边的人)之后可以慢慢告诉 ta。
          </p>

          <label className="onboarding-name-label">
            叫 ta 什么 <span className="onboarding-name-hint">(可选 · 留空默认"她")</span>
          </label>
          <input
            className="onboarding-name-input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="她 · 小林 · 阿南 · 任何你想叫的名字"
            maxLength={64}
            disabled={submitting}
          />

          <textarea
            className="onboarding-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={10}
            placeholder={`比如⋯⋯

你是一个愿意认真听我说话的朋友。
当我不开心的时候,你会先陪着我,而不是急着告诉我该怎么办。
你不会用"加油"这种空洞的话敷衍我。
如果我想要建议,你会给;如果我只是想被听见,你也愿意只是在这里。`}
            autoFocus
            disabled={submitting}
          />

          {error !== null && (
            <div
              className="onboarding-hint"
              style={{
                color: 'rgba(255, 120, 120, 0.78)',
                marginTop: 12,
              }}
            >
              ⚠ {error}
            </div>
          )}

          <div className="onboarding-actions">
            <div className="onboarding-hint">
              {submitting
                ? '正在初始化 persona⋯'
                : charCount === 0
                  ? '至少写几句就可以开始。之后随时能回来补充。'
                  : canSubmit
                    ? `${charCount} 字 · 足够开始了`
                    : `${charCount} 字 · 再写几句吧`}
            </div>
            <button
              type="button"
              className="onboarding-submit"
              disabled={!canSubmit}
              onClick={() => void handleSubmit()}
            >
              {submitting ? '⋯' : '开始对话 →'}
            </button>
          </div>
        </main>
      )}

      {step === 'import-upload' && (
        <main className="onboarding">
          <button
            type="button"
            className="onboarding-back"
            onClick={() => setStep('welcome')}
            disabled={submitting}
          >
            ← 返回
          </button>

          <h1 className="onboarding-title">
            上传材料<span className="onboarding-punct">。</span>
          </h1>
          <p className="onboarding-lead">
            粘贴一段描述你自己的文字——自传、日记、一段简介、或者你和某个 persona 的聊天记录。
            LLM 读完会先按事件 + 印象整理,再把它们翻成 persona 的 5 个核心 block 草稿。
            草稿下一步给你审核。
          </p>

          <textarea
            className="onboarding-textarea"
            value={material}
            onChange={(e) => setMaterial(e.target.value)}
            rows={14}
            placeholder={`比如一段自传或长日记——越具体越好。

"我叫小雨,今年 28 岁,在北京做软件工程师。我有一只叫 Mochi 的黑猫,是 2020 年领养的。
我通常比较安静,不太爱主动说话,但喜欢深度的对话。最近在学做菜⋯⋯"

内容越真实具体,生成的 persona 初稿越贴合你本人。`}
            autoFocus
            disabled={submitting}
          />

          {importError !== null && (
            <div
              className="onboarding-hint"
              style={{
                color: 'rgba(255, 120, 120, 0.78)',
                marginTop: 12,
              }}
            >
              ⚠ {importError}
            </div>
          )}

          <div className="onboarding-actions">
            <div className="onboarding-hint">
              {materialChars === 0
                ? `至少写 ${MIN_MATERIAL_CHARS} 字。越具体越好。`
                : canRunImport
                  ? `${materialChars} 字 · 可以开始分析`
                  : `${materialChars} 字 · 还需要 ${MIN_MATERIAL_CHARS - materialChars} 字`}
            </div>
            <button
              type="button"
              className="onboarding-submit"
              disabled={!canRunImport}
              onClick={() => void handleRunImport()}
            >
              开始分析 →
            </button>
          </div>
        </main>
      )}

      {step === 'import-waiting' && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            正在读⋯<span className="onboarding-punct"></span>
          </h1>
          <p className="onboarding-lead">
            LLM 正在把你的材料拆成事件和长期印象,然后写出 persona 的 5 个 block 草稿。
            通常一两分钟。
          </p>
          <div
            className="onboarding-hint"
            style={{
              marginTop: 48,
              textAlign: 'center',
              fontSize: 14,
              color: 'rgba(255, 255, 255, 0.45)',
            }}
          >
            ⋯
          </div>
        </main>
      )}

      {step === 'import-review' && bootstrap !== null && (
        <main className="onboarding">
          <h1 className="onboarding-title">
            初稿来了<span className="onboarding-punct">。</span>
          </h1>
          <p className="onboarding-lead">
            基于你的材料(
            {bootstrap.source_event_count} 个事件 · {bootstrap.source_thought_count} 条长期印象
            ),LLM 写出了这 5 个 block 的草稿。任何一条都可以改。满意后点"完成"。
          </p>

          <div className="onboarding-blocks">
            <BlockField
              label="这个 persona 是谁"
              engName="persona_block"
              value={draftPersona}
              onChange={setDraftPersona}
              rows={6}
              disabled={submitting}
            />
            <BlockField
              label="persona 对自己的认知"
              engName="self_block"
              hint="通常在 onboarding 时留空 · 之后由反思自动积累"
              value={draftSelf}
              onChange={setDraftSelf}
              rows={3}
              disabled={submitting}
            />
            <BlockField
              label="persona 知道的你"
              engName="user_block"
              value={draftUser}
              onChange={setDraftUser}
              rows={6}
              disabled={submitting}
            />
            <BlockField
              label="persona 知道的你身边的人"
              engName="relationship_block"
              value={draftRelationship}
              onChange={setDraftRelationship}
              rows={5}
              disabled={submitting}
            />
            <BlockField
              label="persona 此刻的情绪"
              engName="mood_block"
              hint="下次对话结束后 runtime 会自动刷新覆盖"
              value={draftMood}
              onChange={setDraftMood}
              rows={2}
              disabled={submitting}
            />
          </div>

          {error !== null && (
            <div
              className="onboarding-hint"
              style={{
                color: 'rgba(255, 120, 120, 0.78)',
                marginTop: 12,
              }}
            >
              ⚠ {error}
            </div>
          )}

          <div className="onboarding-actions">
            <div className="onboarding-hint">
              {submitting ? '正在写入 persona⋯' : '审核后点完成'}
            </div>
            <button
              type="button"
              className="onboarding-submit"
              disabled={submitting}
              onClick={() => void handleCommitReviewed()}
            >
              {submitting ? '⋯' : '完成 · 开始对话 →'}
            </button>
          </div>
        </main>
      )}
    </div>
  )
}

interface BlockFieldProps {
  label: string
  engName: string
  hint?: string
  value: string
  onChange: (next: string) => void
  rows: number
  disabled: boolean
}

function BlockField({
  label,
  engName,
  hint,
  value,
  onChange,
  rows,
  disabled,
}: BlockFieldProps) {
  return (
    <section className="onboarding-block-field" style={{ marginBottom: 20 }}>
      <header style={{ marginBottom: 6 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{label}</span>
        <span
          style={{
            fontSize: 11,
            marginLeft: 8,
            color: 'rgba(255, 255, 255, 0.38)',
            letterSpacing: '0.04em',
          }}
        >
          {engName}
        </span>
        {hint && (
          <div
            style={{
              fontSize: 12,
              marginTop: 2,
              color: 'rgba(255, 255, 255, 0.48)',
            }}
          >
            {hint}
          </div>
        )}
      </header>
      <textarea
        className="onboarding-textarea"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        disabled={disabled}
        style={{ width: '100%', minHeight: 0 }}
      />
    </section>
  )
}
