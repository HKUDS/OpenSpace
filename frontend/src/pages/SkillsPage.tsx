import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { skillsApi, type Skill, type SkillStats } from '../api';
import EmptyState from '../components/EmptyState';
import MetricCard from '../components/MetricCard';
import { formatDate, truncate } from '../utils/format';
import { buildSkillClasses } from '../utils/skillClasses';

export default function SkillsPage() {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [stats, setStats] = useState<SkillStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<'score' | 'updated' | 'name'>('score');

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const [skillItems, skillStats] = await Promise.all([
          skillsApi.listSkills({ activeOnly: false, sort, limit: 200 }),
          skillsApi.getSkillStats(),
        ]);
        if (!cancelled) {
          setSkills(skillItems);
          setStats(skillStats);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t('skills.failedToLoad'));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [sort, t]);

  const skillClasses = useMemo(() => buildSkillClasses(skills), [skills]);

  const filteredClasses = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    const base = !normalized
      ? skillClasses
      : skillClasses.filter((skillClass) => {
        const searchCorpus = [
          skillClass.representative.name,
          skillClass.representative.skill_id,
          skillClass.representative.description,
          ...skillClass.tags,
          ...skillClass.origins,
          ...skillClass.versions.flatMap((version) => [version.skill_id, version.name, version.description, ...version.tags]),
        ].join('\n').toLowerCase();
        return searchCorpus.includes(normalized);
      });

    return [...base].sort((left, right) => {
      if (sort === 'name') {
        return left.representative.name.localeCompare(right.representative.name);
      }
      if (sort === 'updated') {
        return Date.parse(right.latest_updated) - Date.parse(left.latest_updated);
      }
      return right.best_score - left.best_score;
    });
  }, [query, skillClasses, sort]);

  const totalActiveVersions = useMemo(
    () => skillClasses.reduce((sum, skillClass) => sum + skillClass.active_count, 0),
    [skillClasses],
  );

  const averageBestScore = useMemo(() => {
    if (skillClasses.length === 0) {
      return 0;
    }
    return skillClasses.reduce((sum, skillClass) => sum + skillClass.best_score, 0) / skillClasses.length;
  }, [skillClasses]);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold font-serif">{t('skills.title')}</h1>
        </div>
        <div className="flex gap-3 items-center">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('skills.searchPlaceholder')}
            className="px-3 py-2 min-w-[320px]"
          />
          <select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)} className="px-3 py-2">
            <option value="score">{t('skills.sortByScore')}</option>
            <option value="updated">{t('skills.sortByUpdated')}</option>
            <option value="name">{t('skills.sortByName')}</option>
          </select>
        </div>
      </div>

      {stats ? (
        <section className="metrics-row">
          <MetricCard label={t('skills.skillClasses')} value={skillClasses.length} hint={t('skills.versionsHint', { count: stats.total_skills_all })} />
          <MetricCard label={t('skills.activeVersions')} value={totalActiveVersions} hint={t('skills.withActivity', { count: stats.skills_with_activity })} />
          <MetricCard label={t('skills.avgBestScore')} value={averageBestScore.toFixed(1)} hint={t('skills.bestNodeScoreHint')} />
          <MetricCard label={t('skills.selections')} value={stats.total_selections} hint={t('skills.completionsHint', { count: stats.total_completions })} />
        </section>
      ) : null}

      {loading ? <div className="text-sm text-muted">{t('skills.loadingSkills')}</div> : null}
      {error ? <div className="text-sm text-danger">{error}</div> : null}

      {!loading && !error && filteredClasses.length === 0 ? (
        <EmptyState title={t('skills.noSkillsMatch')} description={t('skills.noSkillsMatchDesc')} />
      ) : null}

      {!loading && !error && filteredClasses.length > 0 ? (
        <div className="grid grid-cols-2 gap-4">
          {filteredClasses.map((skillClass) => (
            <Link
              key={skillClass.class_id}
              to={`/skills/${encodeURIComponent(skillClass.representative.skill_id)}`}
              className="record-card bg-surface p-4 space-y-4 hover:border-primary transition-colors"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 space-y-1">
                  <div className="font-bold truncate">{skillClass.representative.name}</div>
                  <div className="text-xs text-muted truncate">{skillClass.representative.skill_id}</div>
                </div>
                <div className="text-right shrink-0">
                  <div className="text-3xl font-bold font-serif leading-none">{skillClass.best_score.toFixed(1)}</div>
                  <div className="text-xs text-muted">{t('skills.bestScore')}</div>
                </div>
              </div>

              <div className="text-sm text-muted">
                {truncate(skillClass.representative.description || t('skills.noClassDescription'), 160)}
              </div>

              <div className="grid grid-cols-4 gap-3 text-xs text-muted">
                <div>{t('skills.versions', { count: skillClass.version_count })}</div>
                <div>{t('skills.active', { count: skillClass.active_count })}</div>
                <div>{t('skills.selectionsCount', { count: skillClass.total_selections })}</div>
                <div>{formatDate(skillClass.latest_updated)}</div>
              </div>

              <div className="flex flex-wrap gap-2 text-xs">
                {skillClass.origins.map((origin) => (
                  <span key={`${skillClass.class_id}-${origin}`} className="tag px-2 py-1">{origin}</span>
                ))}
                {skillClass.tags.slice(0, 5).map((tag) => (
                  <span key={`${skillClass.class_id}-${tag}`} className="tag px-2 py-1">{tag}</span>
                ))}
                {skillClass.tags.length > 5 ? (
                  <span className="tag px-2 py-1">{t('common.tags', { count: skillClass.tags.length - 5 })}</span>
                ) : null}
              </div>
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  );
}
