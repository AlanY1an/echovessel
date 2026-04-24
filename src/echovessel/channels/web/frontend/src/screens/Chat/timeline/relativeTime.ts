/**
 * Relative-time formatter for Memory Timeline rows.
 *
 *   now          → "刚刚" / "just now"
 *   < 60m        → "N 分钟前" / "Nm ago"
 *   < 24h        → "N 小时前" / "Nh ago"
 *   today        → "今天 HH:MM" / "today HH:MM"
 *   yesterday    → "昨天" / "yesterday"
 *   < 30d        → "N 天前" / "Nd ago"
 *   otherwise    → absolute YYYY-MM-DD
 *
 * The translation callback is passed in so the hook stays i18n-aware
 * without pulling `react-i18next` into every leaf.
 */
export type RelativeTimeT = (key: string, opts?: Record<string, unknown>) => string

export function relativeTime(iso: string, now: Date, t: RelativeTimeT): string {
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return ''

  const diffMs = now.getTime() - when.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  const diffHr = Math.floor(diffMs / 3_600_000)

  if (diffMin < 1) return t('chat.timeline.time.just_now')
  if (diffMin < 60) return t('chat.timeline.time.minutes', { n: diffMin })
  if (diffHr < 24) return t('chat.timeline.time.hours', { n: diffHr })

  const sameDay =
    when.getFullYear() === now.getFullYear() &&
    when.getMonth() === now.getMonth() &&
    when.getDate() === now.getDate()
  if (sameDay) {
    const hh = when.getHours().toString().padStart(2, '0')
    const mm = when.getMinutes().toString().padStart(2, '0')
    return t('chat.timeline.time.today', { time: `${hh}:${mm}` })
  }

  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  const sameAsYesterday =
    when.getFullYear() === yesterday.getFullYear() &&
    when.getMonth() === yesterday.getMonth() &&
    when.getDate() === yesterday.getDate()
  if (sameAsYesterday) return t('chat.timeline.time.yesterday')

  const diffDay = Math.floor(diffMs / 86_400_000)
  if (diffDay < 30) return t('chat.timeline.time.days', { n: diffDay })

  const mm = (when.getMonth() + 1).toString().padStart(2, '0')
  const dd = when.getDate().toString().padStart(2, '0')
  return `${when.getFullYear()}-${mm}-${dd}`
}
