import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { overviewApi, type OverviewResponse } from '../api';
import MetricCard from '../components/MetricCard';
import EmptyState from '../components/EmptyState';
import { formatDate, formatInstruction, formatPercent, truncate } from '../utils/format';

export default function DashboardPage() {
  const { t } = useTranslation();
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const overview = await overviewApi.getOverview();
        if (!cancelled) {
          setData(overview);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t('dashboard.failedToLoad'));
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
  }, [t]);

  if (loading) {
    return <div className="p-6 text-sm text-muted">{t('dashboard.loadingDashboard')}</div>;
  }

  if (error || !data) {
    return <div className="p-6 text-sm text-danger">{error ?? t('dashboard.dashboardUnavailable')}</div>;
  }

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-3xl font-bold font-serif">{t('dashboard.title')}</h1>
      <section className="metrics-row">
        <MetricCard label={t('dashboard.totalSkills')} value={data.skills.summary.total_skills_all} hint={t('dashboard.activeHint', { count: data.skills.summary.total_skills })} />
        <MetricCard label={t('dashboard.avgSkillScore')} value={data.skills.average_score.toFixed(1)} hint={t('dashboard.avgScoreHint')} />
        <MetricCard label={t('dashboard.workflowSessions')} value={data.workflows.total} hint={t('dashboard.recordedUnder', { location: data.health.db_path.includes('.openspace') ? t('dashboard.localRepo') : t('dashboard.workspace') })} />
        <MetricCard label={t('dashboard.workflowSuccess')} value={`${data.workflows.average_success_rate.toFixed(1)}%`} hint={t('dashboard.avgSuccessHint')} />
      </section>

      <section>
        <div className="panel-surface p-5 space-y-4">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('dashboard.health')}</div>
            <h2 className="text-2xl font-bold font-serif mt-1">{t('dashboard.runtimeSnapshot')}</h2>
          </div>
          <div className="space-y-3 text-sm">
            <div className="flex items-center justify-between"><span className="text-muted">{t('dashboard.status')}</span><span>{data.health.status}</span></div>
            <div className="flex items-center justify-between"><span className="text-muted">{t('dashboard.dbPath')}</span><span className="text-right break-all">{data.health.db_path}</span></div>
            <div className="flex items-center justify-between"><span className="text-muted">{t('dashboard.workflowCount')}</span><span>{data.health.workflow_count}</span></div>
            <div className="flex items-center justify-between"><span className="text-muted">{t('dashboard.builtFrontend')}</span><span>{data.health.frontend_dist_exists ? t('common.yes') : t('common.no')}</span></div>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-2 gap-6">
        <div className="panel-surface p-5 space-y-4">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('dashboard.skillsSection')}</div>
            <h2 className="text-2xl font-bold font-serif mt-1">{t('dashboard.topScoredSkills')}</h2>
          </div>
          {data.skills.top.length === 0 ? (
            <EmptyState title={t('dashboard.noSkillsYet')} description={t('dashboard.noSkillsDesc')} />
          ) : (
            <div className="space-y-3">
              {data.skills.top.map((skill) => (
                <Link key={skill.skill_id} to={`/skills/${encodeURIComponent(skill.skill_id)}`} className="record-card block p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 space-y-1">
                      <div className="font-bold truncate">{skill.name}</div>
                      <div className="text-sm text-muted">{truncate(skill.description || t('common.noDescription'), 110)}</div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="text-2xl font-bold font-serif">{skill.score.toFixed(1)}</div>
                      <div className="text-xs text-muted">{t('common.score')}</div>
                    </div>
                  </div>
                  <div className="mt-3 flex gap-3 text-xs text-muted">
                    <span>{t('dashboard.effective', { value: formatPercent(skill.effective_rate) })}</span>
                    <span>{t('dashboard.applied', { value: formatPercent(skill.applied_rate) })}</span>
                    <span>{t('dashboard.selections', { count: skill.total_selections })}</span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>

        <div className="panel-surface p-5 space-y-4">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('dashboard.workflowsSection')}</div>
            <h2 className="text-2xl font-bold font-serif mt-1">{t('dashboard.recentSessions')}</h2>
          </div>
          {data.workflows.recent.length === 0 ? (
            <EmptyState title={t('dashboard.noWorkflowSessions')} description={t('dashboard.noWorkflowDesc')} />
          ) : (
            <div className="space-y-3">
              {data.workflows.recent.map((workflow) => (
                <Link key={workflow.id} to={`/workflows/${encodeURIComponent(workflow.id)}`} className="record-card block p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 space-y-1">
                      <div className="font-bold truncate">{workflow.task_name}</div>
                      <div className="text-sm text-muted line-clamp-2">{formatInstruction(workflow.instruction, 160, t('format.noInstruction'))}</div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="text-lg font-bold font-serif">{(workflow.success_rate * 100).toFixed(1)}%</div>
                      <div className="text-xs text-muted">{t('common.success')}</div>
                    </div>
                  </div>
                  <div className="mt-3 flex gap-3 text-xs text-muted">
                    <span>{t('common.steps', { count: workflow.total_steps })}</span>
                    <span>{t('common.agentActions', { count: workflow.agent_action_count })}</span>
                    <span>{formatDate(workflow.start_time)}</span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
