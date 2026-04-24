import type { MemoryTimelineItem } from '../../../api/types'
import { relativeTime, type RelativeTimeT } from './relativeTime'

interface Props {
  item: MemoryTimelineItem
  now: Date
  t: RelativeTimeT
}

/**
 * Renders an L4 `type='thought'` row. Icon + i18n string branch on
 * `data.source` — slow_tick is the parallel-existing reflection path
 * (🧠) and should feel distinct from the in-session SHOCK / TIMER /
 * summary reflection (💭).
 */
export function TimelineItemThought({ item, now, t }: Props) {
  const data = item.data as { description?: string; source?: string }
  const isSlowTick = data.source === 'slow_tick'
  const icon = isSlowTick ? '🧠' : '💭'
  const key = isSlowTick
    ? 'chat.timeline.item.thought_slow_tick'
    : 'chat.timeline.item.thought_reflection'
  return (
    <li className="mem-row mem-row-thought">
      <span className="mem-icon" aria-hidden="true">
        {icon}
      </span>
      <div className="mem-body">
        <div className="mem-ts">{relativeTime(item.timestamp, now, t)}</div>
        <div className="mem-text">
          {t(key, { description: data.description ?? '' })}
        </div>
      </div>
    </li>
  )
}
