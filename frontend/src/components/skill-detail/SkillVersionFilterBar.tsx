import { useTranslation } from 'react-i18next';

interface SkillVersionFilterBarProps {
  originFilter: string;
  onOriginFilterChange: (value: string) => void;
  tagFilter: string;
  onTagFilterChange: (value: string) => void;
  allOrigins: string[];
  allTags: string[];
}

export default function SkillVersionFilterBar({
  originFilter,
  onOriginFilterChange,
  tagFilter,
  onTagFilterChange,
  allOrigins,
  allTags,
}: SkillVersionFilterBarProps) {
  const { t } = useTranslation();

  return (
    <div className="flex items-center gap-4 flex-wrap">
      <div className="flex items-center gap-2">
        <label className="text-sm font-medium">{t('filter.origin')}</label>
        <select
          value={originFilter}
          onChange={(event) => onOriginFilterChange(event.target.value)}
          className="border border-[color:var(--color-ink)] bg-transparent px-2 py-1 text-sm"
        >
          <option value="all">{t('filter.allOrigins')}</option>
          {allOrigins.map((origin) => (
            <option key={origin} value={origin}>{origin}</option>
          ))}
        </select>
      </div>

      <div className="flex items-center gap-2">
        <label className="text-sm font-medium">{t('filter.tags')}</label>
        <select
          value={tagFilter}
          onChange={(event) => onTagFilterChange(event.target.value)}
          className="border border-[color:var(--color-ink)] bg-transparent px-2 py-1 text-sm"
        >
          <option value="all">{t('filter.allTags')}</option>
          {allTags.map((tag) => (
            <option key={tag} value={tag}>{tag}</option>
          ))}
        </select>
      </div>
    </div>
  );
}
