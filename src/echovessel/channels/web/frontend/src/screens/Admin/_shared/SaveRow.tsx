import { useTranslation } from 'react-i18next'

/** Footer row for per-channel editor cards: error badge on the left,
 *  Save button on the right. Disabled until the form is dirty. */
export function SaveRow({
  dirty,
  saving,
  error,
  onSave,
}: {
  dirty: boolean
  saving: boolean
  error: string | null
  onSave: () => Promise<void>
}) {
  const { t } = useTranslation()
  return (
    <div
      className="row g-2"
      style={{
        marginTop: 14,
        borderTop: '1px solid var(--rule)',
        paddingTop: 12,
        alignItems: 'center',
      }}
    >
      {error !== null && (
        <span
          style={{
            fontSize: 11,
            color: 'var(--accent)',
            fontFamily: 'var(--mono)',
          }}
        >
          ⚠ {error}
        </span>
      )}
      <div className="flex1" />
      <button
        type="button"
        className="btn"
        disabled={!dirty || saving}
        onClick={() => void onSave()}
      >
        {saving ? t('admin.common.saving') : t('admin.channels.save_cta')}
      </button>
    </div>
  )
}
