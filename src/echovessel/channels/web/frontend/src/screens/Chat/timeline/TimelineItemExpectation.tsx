import type { MemoryTimelineItem } from '../../../api/types'
import { relativeTime, type RelativeTimeT } from './relativeTime'

interface Props {
  item: MemoryTimelineItem
  now: Date
  t: RelativeTimeT
}

export function TimelineItemExpectation({ item, now, t }: Props) {
  const data = item.data as { description?: string }
  return (
    <li className="mem-row mem-row-expectation">
      <span className="mem-icon" aria-hidden="true">
        ⏳
      </span>
      <div className="mem-body">
        <div className="mem-ts">{relativeTime(item.timestamp, now, t)}</div>
        <div className="mem-text">
          {t('chat.timeline.item.expectation', {
            description: data.description ?? '',
          })}
        </div>
      </div>
    </li>
  )
}
