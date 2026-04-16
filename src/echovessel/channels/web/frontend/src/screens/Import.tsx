/**
 * Import screen — 3-state wizard for the `/api/admin/import/*` pipeline.
 *
 * Flow:
 *
 *   Upload     ──submitText()──▶  (auto-estimate)  ──▶  Estimate
 *   Estimate   ──startImport()──▶  (SSE stream opens) ──▶  Running
 *   Running    ──cancelImport()?── import.done / import.error
 *
 * The `useImport` hook owns the state machine + the EventSource; this
 * component is a pure renderer that switches between three sub-views
 * based on `phase`.
 *
 * Out of scope (deferred per worker F tracker):
 *   - File drag-drop (MVP is paste-text-only; the file picker UI is
 *     present but shows a disabled placeholder)
 *   - Dropped-items drawer (dropped_count is surfaced as a number only)
 *   - Resume support, audio transcription, multi-file batches
 */

import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { TopBar } from '../components/TopBar'
import { useImport } from '../hooks/useImport'
import type {
  ImportDoneSummary,
  ImportEstimateResponse,
  ImportProgressSnapshot,
  ImportUploadResponse,
} from '../api/types'
import type { ImportPhase } from '../hooks/useImport'

const MAX_PASTE_CHARS = 50_000

const TARGET_LABELS: Record<string, string> = {
  persona_traits: '人格特质',
  user_identity_facts: '你的身份',
  user_events: '发生过的事',
  user_reflections: '长期印象',
  relationship_facts: '你身边的人',
}

interface ImportScreenProps {
  onBack: () => void
}

export function ImportScreen({ onBack }: ImportScreenProps) {
  const {
    phase,
    upload,
    estimate,
    progress,
    writesByTarget,
    dropped,
    report,
    error,
    submitText,
    startImport,
    cancelImport,
    reset,
  } = useImport()

  const navigate = useNavigate()

  const view = phaseToView(phase)

  return (
    <div className="import-wrap">
      <TopBar
        mood="在导入材料"
        back={{ label: 'Admin', onClick: onBack }}
      />

      <main className="import-main">
        <Breadcrumb view={view} />

        {view === 'upload' && (
          <UploadStep
            busy={phase === 'uploading'}
            onSubmit={submitText}
            error={error}
          />
        )}

        {view === 'estimate' && (
          <EstimateStep
            phase={phase}
            upload={upload}
            estimate={estimate}
            onConfirm={() => void startImport()}
            onBack={() => reset()}
            error={error}
          />
        )}

        {view === 'running' && (
          <RunningStep
            phase={phase}
            upload={upload}
            progress={progress}
            writesByTarget={writesByTarget}
            droppedCount={dropped.length}
            report={report}
            error={error}
            onCancel={() => void cancelImport()}
            onDone={() => {
              reset()
              navigate('/admin')
            }}
            onRetry={() => reset()}
          />
        )}
      </main>
    </div>
  )
}

// ─── View selector ─────────────────────────────────────────────────────

type View = 'upload' | 'estimate' | 'running'

function phaseToView(phase: ImportPhase): View {
  if (phase === 'estimating' || phase === 'estimate' || phase === 'starting') {
    return 'estimate'
  }
  if (phase === 'running' || phase === 'done') {
    return 'running'
  }
  // idle / uploading / error (no upload yet) → upload step
  return 'upload'
}

// ─── Breadcrumb ────────────────────────────────────────────────────────

function Breadcrumb({ view }: { view: View }) {
  const steps: { idx: string; label: string; key: View }[] = [
    { idx: '01', label: '选择内容', key: 'upload' },
    { idx: '02', label: '估算规模', key: 'estimate' },
    { idx: '03', label: '导入中', key: 'running' },
  ]
  const order: View[] = ['upload', 'estimate', 'running']
  const currentIdx = order.indexOf(view)

  return (
    <ul className="import-breadcrumb">
      {steps.map((s, i) => {
        const isActive = s.key === view
        const isDone = i < currentIdx
        const cls = [
          'import-breadcrumb-step',
          isActive ? 'is-active' : '',
          isDone ? 'is-done' : '',
        ]
          .filter(Boolean)
          .join(' ')
        return (
          <li key={s.key} className={cls}>
            <span className="import-breadcrumb-idx">{s.idx}</span>
            <span className="import-breadcrumb-label">{s.label}</span>
          </li>
        )
      })}
    </ul>
  )
}

// ─── Step 1 · Upload ──────────────────────────────────────────────────

function UploadStep({
  busy,
  onSubmit,
  error,
}: {
  busy: boolean
  onSubmit: (content: string, sourceLabel: string) => Promise<void>
  error: string | null
}) {
  const [mode, setMode] = useState<'paste' | 'file'>('paste')
  const [text, setText] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)

  const overLimit = text.length > MAX_PASTE_CHARS
  const canSubmit =
    mode === 'paste' && text.trim().length > 0 && !overLimit && !busy

  const handleFilePick = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      if (!file) return
      setFileName(file.name)
      try {
        const body = await file.text()
        setText(body)
        setMode('paste')
      } catch (err) {
        console.warn('file read failed', err)
      }
    },
    [],
  )

  const handleSubmit = async () => {
    if (!canSubmit) return
    const label =
      fileName ?? `粘贴 ${new Date().toISOString().slice(0, 10)}`
    await onSubmit(text.trim(), label)
  }

  return (
    <section className="import-step">
      <h1 className="import-step-title">导入历史材料</h1>
      <p className="import-step-lead">
        把<strong>聊天记录 / 日记 / 文档</strong>
        粘贴或上传进来，persona 会从里面提取"发生过的事"、
        "你身边的人"、"长期印象"等记忆，自动写到对应的层级。
        <br />
        所有内容只停留在这台机器上。
      </p>

      <div className="import-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          className={`import-tab ${mode === 'paste' ? 'is-active' : ''}`}
          onClick={() => setMode('paste')}
        >
          粘贴文本
        </button>
        <button
          type="button"
          role="tab"
          className={`import-tab ${mode === 'file' ? 'is-active' : ''}`}
          onClick={() => setMode('file')}
        >
          上传文件
        </button>
      </div>

      {mode === 'file' && (
        <>
          <label className={`import-drop ${fileName ? 'has-file' : ''}`}>
            <input
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              style={{ display: 'none' }}
              onChange={(e) => void handleFilePick(e)}
            />
            <div className="import-drop-icon">📄</div>
            <div className="import-drop-title">
              {fileName ?? '点击选择 .txt / .md 文件'}
            </div>
            <div className="import-drop-sub">
              {fileName
                ? `${text.length.toLocaleString()} 字 · 可编辑后再提交`
                : '或把文件拖进来'}
            </div>
          </label>
          <div className="import-formats">
            接受：<code>.txt</code>
            <code>.md</code>
            音频与二进制暂不支持
          </div>
        </>
      )}

      {mode === 'paste' && (
        <>
          <textarea
            className="import-paste"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={`把想让 persona 记住的历史材料粘到这里。

比如一段聊天记录、一篇日记、几段关于你们关系的描述⋯`}
            disabled={busy}
          />
          <div
            className={`import-paste-meta ${
              overLimit ? 'import-paste-warn' : ''
            }`}
          >
            {text.length.toLocaleString()} / {MAX_PASTE_CHARS.toLocaleString()} 字
            {overLimit && ' · 建议拆成多个文件分批导入'}
          </div>
        </>
      )}

      {error !== null && <ErrorCard message={error} />}

      <div className="import-actions">
        <button
          type="button"
          className="import-primary"
          disabled={!canSubmit}
          onClick={() => void handleSubmit()}
        >
          {busy ? '上传中⋯' : '下一步 · 估算规模 →'}
        </button>
      </div>
    </section>
  )
}

// ─── Step 2 · Estimate ────────────────────────────────────────────────

function EstimateStep({
  phase,
  upload,
  estimate,
  onConfirm,
  onBack,
  error,
}: {
  phase: ImportPhase
  upload: ImportUploadResponse | null
  estimate: ImportEstimateResponse | null
  onConfirm: () => void
  onBack: () => void
  error: string | null
}) {
  const estimating = phase === 'estimating'
  const starting = phase === 'starting'

  return (
    <section className="import-step">
      <h1 className="import-step-title">
        估算这次导入<span className="import-step-check">⋯</span>
      </h1>
      <p className="import-step-lead">
        在真正开始写记忆之前，先估算这次会用多少 tokens、大概多少美金。
        <strong>真实调用 LLM 之前你都可以随时取消。</strong>
      </p>

      <div className="import-summary">
        {upload !== null && (
          <div className="import-summary-file">
            <div className="import-summary-file-icon">📄</div>
            <div>
              <div className="import-summary-file-name">
                {upload.source_label}
              </div>
              <div className="import-summary-file-meta">
                {formatBytes(upload.size_bytes)}
                {upload.suffix ? ` · ${upload.suffix.replace(/^\./, '')}` : ''}
              </div>
            </div>
          </div>
        )}

        {estimating && (
          <div className="import-cost-card">
            <div className="import-cost-title">估算中⋯</div>
          </div>
        )}

        {!estimating && estimate !== null && (
          <div className="import-cost-card">
            <div className="import-cost-title">预计消耗 · LLM 抽取阶段</div>
            <table className="import-cost-table">
              <tbody>
                <tr>
                  <td>输入</td>
                  <td className="import-cost-amount">
                    {estimate.tokens_in.toLocaleString()} tokens
                  </td>
                </tr>
                <tr>
                  <td>输出 (预估)</td>
                  <td className="import-cost-amount">
                    {estimate.tokens_out_est.toLocaleString()} tokens
                  </td>
                </tr>
                <tr className="import-cost-total">
                  <td>预计</td>
                  <td className="import-cost-amount">
                    ${estimate.cost_usd_est.toFixed(4)}
                  </td>
                </tr>
              </tbody>
            </table>
            <div className="import-cost-footer">
              {estimate.note
                ? estimate.note
                : '实际成本以 provider 账单为准'}
            </div>
          </div>
        )}
      </div>

      {error !== null && <ErrorCard message={error} />}

      <div className="import-actions">
        <button
          type="button"
          className="import-secondary"
          onClick={onBack}
          disabled={starting}
        >
          ← 返回修改
        </button>
        <button
          type="button"
          className="import-primary"
          onClick={onConfirm}
          disabled={estimating || starting || estimate === null}
        >
          {starting ? '启动中⋯' : '开始导入 →'}
        </button>
      </div>
    </section>
  )
}

// ─── Step 3 · Running / Done / Error ─────────────────────────────────

function RunningStep({
  phase,
  upload,
  progress,
  writesByTarget,
  droppedCount,
  report,
  error,
  onCancel,
  onDone,
  onRetry,
}: {
  phase: ImportPhase
  upload: ImportUploadResponse | null
  progress: ImportProgressSnapshot | null
  writesByTarget: Record<string, number>
  droppedCount: number
  report: ImportDoneSummary | null
  error: string | null
  onCancel: () => void
  onDone: () => void
  onRetry: () => void
}) {
  const isDone = phase === 'done' && report !== null
  const isError = phase === 'error'
  const isRunning = phase === 'running'

  const pct =
    progress && progress.total_chunks > 0
      ? Math.min(
          100,
          Math.round((progress.current_chunk / progress.total_chunks) * 100),
        )
      : 0

  if (isError) {
    return (
      <section className="import-step">
        <h1 className="import-step-title">导入失败</h1>
        <p className="import-step-lead">
          这次导入没能完成。已经写入的记忆保留；可以修改后重试。
        </p>
        <ErrorCard message={error ?? '未知错误'} />
        <div className="import-actions">
          <button type="button" className="import-secondary" onClick={onRetry}>
            ← 重新开始
          </button>
        </div>
      </section>
    )
  }

  if (isDone) {
    const totalWrites = Object.values(report.writes_by_target).reduce(
      (a, b) => a + b,
      0,
    )
    return (
      <section className="import-step import-summary--result">
        <h1 className="import-step-title">
          {report.status === 'cancelled' ? '已取消' : '导入完成'}
          <span className="import-step-check">
            {report.status === 'cancelled' ? '⋯' : '✓'}
          </span>
        </h1>
        <p className="import-step-lead">
          {report.status === 'cancelled'
            ? '取消前写入的记忆已保留。'
            : `处理了 ${report.processed_chunks} / ${report.total_chunks} 块`}
        </p>

        <div className="import-writes">
          <div className="import-writes-title">写入总览</div>
          {totalWrites === 0 ? (
            <div className="import-writes-empty">
              没有产生新的记忆（可能内容不够具体）
            </div>
          ) : (
            <ul className="import-writes-list import-writes-list--result">
              {Object.entries(report.writes_by_target).map(([target, n]) => (
                <li key={target}>
                  <span className="import-writes-count">{n}</span>
                  <span className="import-writes-label">
                    {TARGET_LABELS[target] ?? target}
                  </span>
                  <span className="import-writes-eng">{target}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {report.dropped_count > 0 && (
          <div className="import-dropped">
            <div className="import-dropped-meta">
              <code>{report.dropped_count}</code> 条由于 schema 或验证失败被跳过
            </div>
          </div>
        )}

        <div className="import-actions">
          <button type="button" className="import-primary" onClick={onDone}>
            返回 Admin
          </button>
        </div>
      </section>
    )
  }

  // Running
  const totalWrites = Object.values(writesByTarget).reduce(
    (a, b) => a + b,
    0,
  )

  return (
    <section className="import-step">
      <h1 className="import-step-title">导入中⋯</h1>
      <p className="import-step-lead">
        {upload !== null
          ? `${upload.source_label} · ${formatBytes(upload.size_bytes)}`
          : '处理中'}
      </p>

      <div className="import-running">
        <div className="import-running-meta">
          <div className="import-running-chunk">
            {progress
              ? `${progress.current_chunk} / ${progress.total_chunks} 块`
              : '准备中⋯'}
          </div>
          <div className="import-running-timer">{pct}%</div>
        </div>
        <div className="import-progress">
          <div className="import-progress-label">
            <span>进度</span>
            <span>{pct}%</span>
          </div>
          <div className="import-progress-track">
            <div
              className="import-progress-bar"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
        {droppedCount > 0 && (
          <div className="import-running-cost">
            已跳过 <strong>{droppedCount}</strong> 条不合法 LLM 输出
          </div>
        )}
      </div>

      <div className="import-writes">
        <div className="import-writes-title">实时写入</div>
        {totalWrites === 0 ? (
          <div className="import-writes-empty">等待第一条写入⋯</div>
        ) : (
          <ul className="import-writes-list">
            {Object.entries(writesByTarget).map(([target, n]) => (
              <li key={target}>
                <span className="import-writes-count">{n}</span>
                <span className="import-writes-label">
                  {TARGET_LABELS[target] ?? target}
                </span>
                <span className="import-writes-eng">{target}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="import-actions">
        <button
          type="button"
          className="import-danger"
          onClick={onCancel}
          disabled={!isRunning}
        >
          取消本次导入
        </button>
      </div>
    </section>
  )
}

// ─── Shared ───────────────────────────────────────────────────────────

function ErrorCard({ message }: { message: string }) {
  return (
    <div className="import-cost-warning" style={{ marginBottom: 16 }}>
      ⚠ {message}
    </div>
  )
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}
