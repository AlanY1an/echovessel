import { useTranslation } from 'react-i18next'

/**
 * Modal that fans out a persona-subject reflection's source events.
 * Day-1 skeleton — real wiring (``GET /api/admin/memory/thoughts/{id}/trace``)
 * lands once Worker A merges the contract. The wrapper handles
 * ``open=false`` by rendering nothing so callers can keep mounting it
 * unconditionally.
 */
export function FillingChainModal({
  open,
  thoughtId,
  onClose,
}: {
  open: boolean
  thoughtId: number | null
  onClose: () => void
}) {
  const { t } = useTranslation()
  if (!open || thoughtId === null) return null
  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 50,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="card"
        style={{
          padding: 22,
          maxWidth: 520,
          width: '90%',
          background: 'var(--paper)',
        }}
      >
        <div className="row g-2" style={{ alignItems: 'baseline' }}>
          <h3 className="title" style={{ margin: 0 }}>
            {t('admin.persona.reflection.filling_modal_title')}
          </h3>
          <div className="flex1" />
          <button type="button" className="btn ghost sm" onClick={onClose}>
            {t('admin.common.close')}
          </button>
        </div>
        <p
          style={{
            color: 'var(--ink-3)',
            fontSize: 12,
            marginTop: 12,
            fontFamily: 'var(--mono)',
          }}
        >
          {t('admin.persona.reflection.filling_modal_pending', {
            id: thoughtId,
          })}
        </p>
      </div>
    </div>
  )
}
