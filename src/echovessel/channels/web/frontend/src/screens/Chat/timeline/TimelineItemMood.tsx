import type { MemoryTimelineItem } from '../../../api/types'
import { relativeTime, type RelativeTimeT } from './relativeTime'

interface Props {
  item: MemoryTimelineItem
  now: Date
  t: RelativeTimeT
}

export function TimelineItemMood({ item, now, t }: Props) {
  const data = item.data as { mood?: string; mood_summary?: string }
  const summary = data.mood_summary ?? data.mood ?? ''
  return (
    <li className="mem-row mem-row-mood">
      <span className="mem-icon" aria-hidden="true">
        💓
      </span>
      <div className="mem-body">
        <div className="mem-ts">{relativeTime(item.timestamp, now, t)}</div>
        <div className="mem-text">
          {t('chat.timeline.item.mood_shift', { summary })}
        </div>
      </div>
    </li>
  )
}
