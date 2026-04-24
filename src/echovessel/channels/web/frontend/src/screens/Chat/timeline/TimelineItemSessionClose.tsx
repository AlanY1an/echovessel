import type { MemoryTimelineItem } from '../../../api/types'
import { relativeTime, type RelativeTimeT } from './relativeTime'

interface Props {
  item: MemoryTimelineItem
  now: Date
  t: RelativeTimeT
}

function formatDuration(seconds: number | null | undefined, t: RelativeTimeT): string {
  if (!seconds || seconds <= 0) return t('chat.timeline.session.duration_unknown')
  const minutes = Math.round(seconds / 60)
  if (minutes < 1) return t('chat.timeline.session.duration_seconds', { n: seconds })
  return t('chat.timeline.session.duration_minutes', { n: minutes })
}

export function TimelineItemSessionClose({ item, now, t }: Props) {
  const data = item.data as {
    duration_seconds?: number | null
    events_count?: number | null
    thoughts_count?: number | null
  }
  return (
    <li className="mem-row mem-row-session-close">
      <span className="mem-icon" aria-hidden="true">
        💾
      </span>
      <div className="mem-body">
        <div className="mem-ts">{relativeTime(item.timestamp, now, t)}</div>
        <div className="mem-text">
          {t('chat.timeline.item.session_close', {
            duration: formatDuration(data.duration_seconds, t),
            events: data.events_count ?? 0,
            thoughts: data.thoughts_count ?? 0,
          })}
        </div>
      </div>
    </li>
  )
}
