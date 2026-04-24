import type { MemoryTimelineItem } from '../../../api/types'
import { relativeTime, type RelativeTimeT } from './relativeTime'

interface Props {
  item: MemoryTimelineItem
  now: Date
  t: RelativeTimeT
}

const KIND_ICON: Record<string, string> = {
  person: '👤',
  pet: '🐾',
  place: '📍',
  org: '🏢',
  other: '●',
}

export function TimelineItemEntity({ item, now, t }: Props) {
  const data = item.data as {
    canonical_name?: string
    kind?: string
    description?: string
  }
  const icon = KIND_ICON[data.kind ?? 'other'] ?? '●'
  const kindLabel =
    data.kind && data.kind in KIND_ICON
      ? t(`chat.timeline.entity_kind.${data.kind}`)
      : ''
  const isDescription = item.kind === 'entity_description'
  const textKey = isDescription
    ? 'chat.timeline.item.entity_description_updated'
    : 'chat.timeline.item.entity_new'
  return (
    <li className="mem-row mem-row-entity">
      <span className="mem-icon" aria-hidden="true">
        {icon}
      </span>
      <div className="mem-body">
        <div className="mem-ts">{relativeTime(item.timestamp, now, t)}</div>
        <div className="mem-text">
          {t(textKey, {
            kind: kindLabel,
            canonical_name: data.canonical_name ?? '',
            description: data.description ?? '',
          })}
        </div>
      </div>
    </li>
  )
}
