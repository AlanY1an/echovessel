import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { avatarUrl, deletePersonaAvatar, postPersonaAvatar } from '../../../api/client'
import { ApiError } from '../../../api/types'

/**
 * Profile picture card on the Admin → Persona tab. Self-contained:
 * owns its own loading/error/version state and doesn't require any
 * prop threading back up through usePersona. The cache-bust counter
 * (`version`) is bumped on every mutation so the <img> reloads
 * without the browser's HTTP cache getting in the way.
 */
export function AvatarCard({
  initialHasAvatar,
  displayName,
}: {
  initialHasAvatar: boolean
  displayName: string
}) {
  const { t } = useTranslation()
  const [hasAvatar, setHasAvatar] = useState(initialHasAvatar)
  const [version, setVersion] = useState(0)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    setHasAvatar(initialHasAvatar)
  }, [initialHasAvatar])

  const src = hasAvatar ? avatarUrl(`${displayName}-${version}`) : null

  const pickFile = () => {
    fileRef.current?.click()
  }

  const onFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    // Reset the input so choosing the same file twice still triggers.
    e.target.value = ''
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      await postPersonaAvatar(file)
      setHasAvatar(true)
      setVersion((v) => v + 1)
    } catch (err) {
      if (err instanceof ApiError) setError(err.detail)
      else if (err instanceof Error) setError(err.message)
      else setError(t('admin.avatar.upload_failed'))
    } finally {
      setUploading(false)
    }
  }

  const onRemove = async () => {
    setUploading(true)
    setError(null)
    try {
      await deletePersonaAvatar()
      setHasAvatar(false)
      setVersion((v) => v + 1)
    } catch (err) {
      if (err instanceof ApiError) setError(err.detail)
      else if (err instanceof Error) setError(err.message)
      else setError(t('admin.avatar.remove_failed'))
    } finally {
      setUploading(false)
    }
  }

  const initial =
    displayName.trim().length > 0 ? displayName.trim()[0]!.toUpperCase() : 'A'

  return (
    <div className="card" style={{ padding: 14 }}>
      <div className="row g-3" style={{ alignItems: 'center' }}>
        <div
          className="w-avatar lg"
          style={{ overflow: 'hidden' }}
          aria-label={initial}
        >
          {src ? (
            <img src={src} alt="" draggable={false} />
          ) : (
            <span>{initial}</span>
          )}
        </div>
        <div className="stack g-1" style={{ flex: 1 }}>
          <span className="label">{t('admin.avatar.label')}</span>
          <div style={{ fontSize: 12, color: 'var(--ink-3)' }}>
            {hasAvatar
              ? t('admin.avatar.current_hint')
              : t('admin.avatar.empty_hint')}
          </div>
          {error !== null && (
            <div style={{ fontSize: 11, color: 'var(--accent)', fontFamily: 'var(--mono)' }}>
              ⚠ {error}
            </div>
          )}
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          style={{ display: 'none' }}
          onChange={(e) => void onFileChange(e)}
        />
        <button
          className="btn ghost sm"
          onClick={pickFile}
          disabled={uploading}
        >
          {uploading
            ? '⋯'
            : hasAvatar
              ? t('admin.avatar.replace_cta')
              : t('admin.avatar.upload_cta')}
        </button>
        {hasAvatar && (
          <button
            className="btn ghost sm"
            onClick={() => void onRemove()}
            disabled={uploading}
            style={{ color: 'var(--accent)', borderColor: 'var(--accent)' }}
          >
            {t('admin.common.delete')}
          </button>
        )}
      </div>
    </div>
  )
}
