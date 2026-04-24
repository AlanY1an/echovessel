import { useTranslation } from 'react-i18next'
import type { TimelineFilterSet } from '../../../hooks/useMemoryTimeline'

interface Props {
  filters: TimelineFilterSet
  onToggle(key: keyof TimelineFilterSet, value: boolean): void
  onClearCache(): void
  onClose(): void
}

const FILTER_ORDER: (keyof TimelineFilterSet)[] = [
  'event',
  'thought_reflection',
  'thought_slow_tick',
  'intention',
  'expectation',
  'entity',
  'mood',
  'session_close',
]

const FILTER_I18N_KEY: Record<keyof TimelineFilterSet, string> = {
  event: 'chat.timeline.filter.events',
  thought_reflection: 'chat.timeline.filter.thoughts',
  thought_slow_tick: 'chat.timeline.filter.slow_tick',
  intention: 'chat.timeline.filter.intentions',
  expectation: 'chat.timeline.filter.expectations',
  entity: 'chat.timeline.filter.entities',
  mood: 'chat.timeline.filter.mood',
  session_close: 'chat.timeline.filter.session_close',
}

export function TimelineFilters({
  filters,
  onToggle,
  onClearCache,
  onClose,
}: Props) {
  const { t } = useTranslation()
  return (
    <div className="mem-filters" role="menu">
      <div className="mem-filters-head">
        <span className="label">{t('chat.timeline.filter.title')}</span>
        <button
          type="button"
          className="btn ghost sm"
          onClick={onClose}
          aria-label={t('admin.common.close')}
        >
          ✕
        </button>
      </div>
      <ul className="mem-filters-list">
        {FILTER_ORDER.map((key) => (
          <li key={key}>
            <label>
              <input
                type="checkbox"
                checked={filters[key]}
                onChange={(e) => onToggle(key, e.target.checked)}
              />
              <span>{t(FILTER_I18N_KEY[key])}</span>
            </label>
          </li>
        ))}
      </ul>
      <div className="mem-filters-foot">
        <button type="button" className="btn ghost sm" onClick={onClearCache}>
          {t('chat.timeline.filter.clear_cache')}
        </button>
      </div>
    </div>
  )
}
