import { useTranslation } from 'react-i18next'

export function LanguageToggle() {
  const { i18n, t } = useTranslation()
  const current = i18n.language?.split('-')[0] ?? 'zh'

  const switchTo = (lang: 'en' | 'zh') => {
    if (current === lang) return
    void i18n.changeLanguage(lang)
  }

  return (
    <div role="group" aria-label="Language" className="lang-toggle">
      <button
        type="button"
        onClick={() => switchTo('zh')}
        aria-pressed={current === 'zh'}
        aria-label={t('language.switch_to_zh_aria')}
      >
        中
      </button>
      <button
        type="button"
        onClick={() => switchTo('en')}
        aria-pressed={current === 'en'}
        aria-label={t('language.switch_to_en_aria')}
      >
        EN
      </button>
    </div>
  )
}
