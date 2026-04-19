/** Pill-shaped on/off switch. Used as the per-channel enabled toggle
 *  and (future) for any other boolean knob in the admin panel. */
export function ToggleSwitch({
  value,
  onChange,
}: {
  value: boolean
  onChange: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      style={{
        width: 40,
        height: 22,
        borderRadius: 999,
        background: value ? 'var(--ink)' : 'var(--paper-4)',
        position: 'relative',
        transition: 'background 120ms',
      }}
      aria-pressed={value}
    >
      <span
        style={{
          position: 'absolute',
          top: 2,
          left: value ? 20 : 2,
          width: 18,
          height: 18,
          borderRadius: '50%',
          background: 'var(--paper)',
          transition: 'left 120ms',
        }}
      />
    </button>
  )
}
