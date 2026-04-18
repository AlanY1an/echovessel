import { useTranslation } from 'react-i18next'
import type { PersonaFacts } from '../api/types'

/**
 * Closed-enum vocabularies — mirrors
 * ``src/echovessel/prompts/persona_facts.py``. Keep in lockstep; the
 * backend drops any enum value outside these sets on the server side,
 * so an out-of-vocab UI value would silently become null.
 */
const ENUM_OPTIONS = {
  gender: ['female', 'male', 'non_binary'],
  education_level: ['high_school', 'bachelor', 'master', 'phd'],
  relationship_status: ['single', 'married', 'widowed', 'divorced'],
  life_stage: [
    'student',
    'working',
    'retired',
    'new_parent',
    'between_jobs',
  ],
  health_status: ['healthy', 'chronic_illness', 'recovering', 'serious'],
} as const

type EnumField = keyof typeof ENUM_OPTIONS
type FreeTextField =
  | 'full_name'
  | 'ethnicity'
  | 'nationality'
  | 'native_language'
  | 'locale_region'
  | 'occupation'
  | 'occupation_field'
  | 'location'
  | 'timezone'

export interface PersonaFactsEditorProps {
  value: PersonaFacts
  onChange: (next: PersonaFacts) => void
  disabled?: boolean
}

/**
 * Form for the fifteen biographic fact columns. Every field maps 1:1
 * to a column on the ``personas`` row. Missing values round-trip as
 * empty strings on the DOM side and are coerced back to ``null`` by
 * :func:`patchField` / :func:`patchDateField` below.
 */
export function PersonaFactsEditor({
  value,
  onChange,
  disabled = false,
}: PersonaFactsEditorProps) {
  const { t } = useTranslation()

  const patchField = (
    field: FreeTextField | EnumField,
    raw: string,
  ) => {
    onChange({ ...value, [field]: raw.trim() === '' ? null : raw })
  }

  const patchDateField = (raw: string) => {
    onChange({ ...value, birth_date: raw === '' ? null : raw })
  }

  return (
    <div className="facts-editor">
      <FreeTextRow
        field="full_name"
        label={t('facts.full_name')}
        value={value.full_name}
        placeholder={t('facts.full_name_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <EnumSelectRow
        field="gender"
        label={t('facts.gender')}
        value={value.gender}
        disabled={disabled}
        onPatch={patchField}
      />

      <DateRow
        label={t('facts.birth_date')}
        hint={t('facts.birth_date_hint')}
        value={value.birth_date}
        disabled={disabled}
        onPatch={patchDateField}
      />

      <FreeTextRow
        field="ethnicity"
        label={t('facts.ethnicity')}
        value={value.ethnicity}
        placeholder={t('facts.ethnicity_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="nationality"
        label={t('facts.nationality')}
        value={value.nationality}
        placeholder={t('facts.nationality_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="native_language"
        label={t('facts.native_language')}
        value={value.native_language}
        placeholder={t('facts.native_language_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="locale_region"
        label={t('facts.locale_region')}
        value={value.locale_region}
        placeholder={t('facts.locale_region_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <EnumSelectRow
        field="education_level"
        label={t('facts.education_level')}
        value={value.education_level}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="occupation"
        label={t('facts.occupation')}
        value={value.occupation}
        placeholder={t('facts.occupation_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="occupation_field"
        label={t('facts.occupation_field')}
        value={value.occupation_field}
        placeholder={t('facts.occupation_field_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="location"
        label={t('facts.location')}
        value={value.location}
        placeholder={t('facts.location_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <FreeTextRow
        field="timezone"
        label={t('facts.timezone')}
        value={value.timezone}
        placeholder={t('facts.timezone_placeholder')}
        disabled={disabled}
        onPatch={patchField}
      />

      <EnumSelectRow
        field="relationship_status"
        label={t('facts.relationship_status')}
        value={value.relationship_status}
        disabled={disabled}
        onPatch={patchField}
      />

      <EnumSelectRow
        field="life_stage"
        label={t('facts.life_stage')}
        value={value.life_stage}
        disabled={disabled}
        onPatch={patchField}
      />

      <EnumSelectRow
        field="health_status"
        label={t('facts.health_status')}
        value={value.health_status}
        disabled={disabled}
        onPatch={patchField}
      />
    </div>
  )
}

interface FreeTextRowProps {
  field: FreeTextField
  label: string
  placeholder: string
  value: string | null
  disabled: boolean
  onPatch: (field: FreeTextField, raw: string) => void
}

function FreeTextRow({
  field,
  label,
  placeholder,
  value,
  disabled,
  onPatch,
}: FreeTextRowProps) {
  return (
    <label className="facts-row">
      <span className="facts-row-label">{label}</span>
      <input
        type="text"
        className="facts-row-input"
        value={value ?? ''}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(e) => onPatch(field, e.target.value)}
      />
    </label>
  )
}

interface EnumSelectRowProps {
  field: EnumField
  label: string
  value: string | null
  disabled: boolean
  onPatch: (field: EnumField, raw: string) => void
}

function EnumSelectRow({
  field,
  label,
  value,
  disabled,
  onPatch,
}: EnumSelectRowProps) {
  const { t } = useTranslation()
  const options = ENUM_OPTIONS[field]
  return (
    <label className="facts-row">
      <span className="facts-row-label">{label}</span>
      <select
        className="facts-row-input"
        value={value ?? ''}
        disabled={disabled}
        onChange={(e) => onPatch(field, e.target.value)}
      >
        <option value="">{t('facts.unknown_option')}</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {t(`facts.enum.${field}.${opt}`)}
          </option>
        ))}
      </select>
    </label>
  )
}

interface DateRowProps {
  label: string
  hint: string
  value: string | null
  disabled: boolean
  onPatch: (raw: string) => void
}

function DateRow({ label, hint, value, disabled, onPatch }: DateRowProps) {
  return (
    <label className="facts-row">
      <span className="facts-row-label">
        {label}
        <span className="facts-row-hint">{hint}</span>
      </span>
      <input
        type="date"
        className="facts-row-input"
        value={value ?? ''}
        disabled={disabled}
        onChange={(e) => onPatch(e.target.value)}
      />
    </label>
  )
}
