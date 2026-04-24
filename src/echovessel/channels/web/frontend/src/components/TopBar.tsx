import { LanguageToggle } from './LanguageToggle'

interface TopBarAction {
  label: string
  onClick: () => void
}

interface TopBarProps {
  primary?: TopBarAction // right-side primary action
  back?: TopBarAction // optional back button on the left
}

export function TopBar({ primary, back }: TopBarProps) {
  return (
    <header className="bar">
      <div className="bar-inner">
        {back && (
          <button type="button" className="top-back" onClick={back.onClick}>
            ← {back.label}
          </button>
        )}
        <div className="brand">
          Echo<span className="brand-dot" />
          <span className="brand-suffix">Vessel</span>
        </div>
        <LanguageToggle />
        {primary && (
          <button
            type="button"
            className="admin-link admin-link--action"
            onClick={primary.onClick}
          >
            {primary.label}
          </button>
        )}
      </div>
    </header>
  )
}
