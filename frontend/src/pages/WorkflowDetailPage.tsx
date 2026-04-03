import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { workflowsApi, type WorkflowDetail, type WorkflowTimelineEvent } from '../api';
import { formatDate, formatInstruction } from '../utils/format';

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function getStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

function formatDurationSeconds(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    return '—';
  }
  if (value === 0) {
    return '0s';
  }
  if (value < 60) {
    return `${value.toFixed(value >= 10 ? 0 : 1)}s`;
  }

  const totalSeconds = Math.round(value);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m ${seconds}s`;
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function getString(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value : null;
}

function collapseWhitespace(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function truncateText(value: string, max = 180): string {
  const compact = collapseWhitespace(value);
  if (compact.length <= max) {
    return compact;
  }
  return `${compact.slice(0, max).trimEnd()}…`;
}

function firstMeaningfulLine(value: string): string | null {
  const lines = value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith('```'));

  return lines[0] ?? null;
}

function humanizeToken(value: string): string {
  const normalized = value.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!normalized) {
    return 'Unknown';
  }

  return normalized.replace(/\b\w/g, (char) => char.toUpperCase());
}

function parseTimestamp(value?: string | null): Date | null {
  if (!value) {
    return null;
  }

  const normalized = value.replace(/(\.\d{3})\d+/, '$1');
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return date;
}

function formatTimeLabel(value?: string | null): string {
  if (!value) {
    return '—';
  }

  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/);
  if (match) {
    const [, , month, day, hour, minute, second = '00'] = match;
    return `${month}/${day} ${hour}:${minute}:${second}`;
  }

  const date = parseTimestamp(value);
  if (!date) {
    return value;
  }
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

function getTimestampSortValue(value?: string | null): number | null {
  const date = parseTimestamp(value);
  return date ? date.getTime() : null;
}

function getCommandTitle(command?: string | null): string | null {
  if (!command) {
    return null;
  }

  const compact = collapseWhitespace(command);
  if (!compact || compact.startsWith('```') || compact.length > 48 || /[\\/]/.test(compact)) {
    return null;
  }

  return /^[a-z0-9_-]+$/i.test(compact) ? humanizeToken(compact) : compact;
}

function getCommandPreview(command?: string | null, max = 140): string | null {
  if (!command) {
    return null;
  }

  return truncateText(firstMeaningfulLine(command) ?? command, max);
}

type TimelineTone = 'default' | 'primary' | 'accent' | 'danger';

interface TimelineFact {
  label: string;
  value: string;
}

interface TimelinePresentation {
  title: string;
  summary?: string;
  secondary?: string;
  primaryFact?: TimelineFact;
  secondaryFact?: TimelineFact;
  facts: TimelineFact[];
}

interface TimelineSummary {
  total: number;
  byType: Record<string, number>;
  byAgentType: Record<string, number>;
  byBackend: Record<string, number>;
  firstTimestamp: string | null;
  lastTimestamp: string | null;
}

interface SummaryMetricProps {
  label: string;
  value: ReactNode;
  hint: string;
}

function SummaryMetric({ label, value, hint }: SummaryMetricProps) {
  return (
    <div className="workflow-metric-card">
      <div className="workflow-metric-header">
        <div className="workflow-kicker">{label}</div>
      </div>
      <div className="workflow-metric-value">{value}</div>
      <div className="workflow-metric-divider" />
      <div className="workflow-metric-hint">{hint}</div>
    </div>
  );
}

interface SidebarRowProps {
  label: string;
  children: ReactNode;
}

function SidebarRow({ label, children }: SidebarRowProps) {
  return (
    <div className="space-y-1.5">
      <div className="workflow-kicker">{label}</div>
      <div className="min-w-0 text-sm leading-6 text-ink">{children}</div>
    </div>
  );
}

interface WorkflowChipProps {
  children: ReactNode;
  className?: string;
}

function WorkflowChip({ children, className = '' }: WorkflowChipProps) {
  return <span className={`workflow-chip text-xs ${className}`.trim()}>{children}</span>;
}

interface QuietEmptyStateProps {
  title: string;
  description: string;
}

function QuietEmptyState({ title, description }: QuietEmptyStateProps) {
  return (
    <div className="workflow-soft-card p-6 space-y-2">
      <div className="text-lg font-semibold tracking-[-0.02em] text-ink">{title}</div>
      <p className="workflow-copy text-sm leading-6 text-muted">{description}</p>
    </div>
  );
}

function incrementCount(counter: Record<string, number>, key?: string | null): void {
  if (!key) {
    return;
  }

  const normalized = key.trim();
  if (!normalized) {
    return;
  }

  counter[normalized] = (counter[normalized] ?? 0) + 1;
}

function getStatusTone(status?: string): TimelineTone {
  if (status === 'success') {
    return 'accent';
  }
  if (status === 'error') {
    return 'danger';
  }
  return 'default';
}

function getEventTone(event: WorkflowTimelineEvent): TimelineTone {
  if (event.type === 'agent_action') {
    return 'primary';
  }
  return getStatusTone(event.status);
}

function getMarkerClasses(tone: TimelineTone): string {
  switch (tone) {
    case 'primary':
      return 'border-[color:var(--color-primary)] bg-[color:var(--color-bg-page)] text-primary';
    case 'accent':
      return 'border-[color:var(--color-diff-add)] bg-[color:var(--color-diff-add)] text-ink';
    case 'danger':
      return 'border-[color:var(--color-diff-del)] bg-[color:var(--color-diff-del)] text-ink';
    default:
      return 'border-[color:var(--color-border-dark)] bg-surface text-muted';
  }
}

function getStatusChipClasses(status?: string): string {
  switch (status) {
    case 'success':
      return '[--workflow-chip-border:var(--color-diff-add)] [--workflow-chip-bg:var(--color-diff-add)] [--workflow-chip-text:var(--color-ink)]';
    case 'error':
      return '[--workflow-chip-border:var(--color-diff-del)] [--workflow-chip-bg:var(--color-diff-del)] [--workflow-chip-text:var(--color-ink)]';
    default:
      return '';
  }
}

function isLowSignalPreview(value: string): boolean {
  const compact = collapseWhitespace(value);
  return compact.length <= 3 && /^(\/\*\*?|\/\/|#|[{[(])$/.test(compact);
}

function describeTimelineEvent(event: WorkflowTimelineEvent): TimelinePresentation {
  const details = isRecord(event.details) ? event.details : null;

  if (event.type === 'agent_action') {
    const input = details && isRecord(details.input) ? details.input : null;
    const reasoning = details && isRecord(details.reasoning) ? details.reasoning : null;
    const instruction = getString(input?.instruction);
    const response = getString(reasoning?.response);
    const thought = getString(reasoning?.thought);
    const responsePreview = response ? firstMeaningfulLine(response) ?? response : null;
    const thoughtPreview = thought ? firstMeaningfulLine(thought) ?? thought : null;

    const facts: TimelineFact[] = [];
    const primaryFact = event.agent_name ? { label: 'Agent', value: event.agent_name } : undefined;
    const secondaryFact = event.agent_type ? { label: 'Type', value: humanizeToken(event.agent_type) } : undefined;

    return {
      title: humanizeToken(event.label || 'agent_action'),
      summary: instruction ? truncateText(instruction, 220) : undefined,
      secondary: responsePreview
        ? `Response · ${truncateText(responsePreview, 180)}`
        : thoughtPreview
          ? `Thought · ${truncateText(thoughtPreview, 180)}`
          : undefined,
      primaryFact,
      secondaryFact,
      facts,
    };
  }

  const result = details && isRecord(details.result) ? details.result : null;
  const command = getString(details?.command);
  const commandTitle = getCommandTitle(command);
  const commandPreview = getCommandPreview(command);
  const toolName = event.label ? humanizeToken(event.label) : null;
  const stdout = getString(result?.stdout);
  const stderr = getString(result?.stderr);
  const output = getString(result?.output);
  const content = getString(result?.content);
  const outputPreviewSource = stderr ?? output ?? stdout ?? content;
  const outputPreview = outputPreviewSource
    ? firstMeaningfulLine(outputPreviewSource) ?? outputPreviewSource
    : null;
  const resolvedTitle = commandTitle ?? toolName ?? humanizeToken(event.backend ? `${event.backend}_execution` : 'tool_execution');
  const primaryFact = toolName && toolName !== commandTitle
    ? { label: 'Tool', value: toolName }
    : event.backend
      ? { label: 'Backend', value: humanizeToken(event.backend) }
      : undefined;
  const secondaryFact = commandPreview && commandPreview !== resolvedTitle
    ? { label: 'Command', value: truncateText(commandPreview, 72) }
    : undefined;
  const shouldHideSummary = outputPreview ? isLowSignalPreview(outputPreview) : false;

  const facts: TimelineFact[] = [];
  if (event.backend) {
    facts.push({ label: 'Backend', value: humanizeToken(event.backend) });
  }
  if (toolName && toolName !== commandTitle && primaryFact?.value !== toolName) {
    facts.push({ label: 'Tool', value: toolName });
  }
  if (typeof result?.exit_code === 'number') {
    facts.push({ label: 'Exit', value: String(result.exit_code) });
  }

  return {
    title: resolvedTitle,
    summary: outputPreview && !shouldHideSummary
      ? `${stderr ? 'stderr · ' : ''}${truncateText(outputPreview, 220)}`
      : undefined,
    secondary: undefined,
    primaryFact,
    secondaryFact,
    facts,
  };
}

export default function WorkflowDetailPage() {
  const { t } = useTranslation();
  const { workflowId = '' } = useParams();
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const detail = await workflowsApi.getWorkflow(workflowId);
        if (!cancelled) {
          setWorkflow(detail);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t('workflowDetail.failedToLoad'));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };
    if (workflowId) {
      void load();
    }
    return () => {
      cancelled = true;
    };
  }, [workflowId, t]);

  const timeline = useMemo(() => {
    const events = workflow?.timeline ?? [];

    return events
      .map((event, originalIndex) => ({
        event,
        originalIndex,
        timestampValue: getTimestampSortValue(event.timestamp),
      }))
      .sort((left, right) => {
        if (left.timestampValue !== null && right.timestampValue !== null && left.timestampValue !== right.timestampValue) {
          return left.timestampValue - right.timestampValue;
        }
        if (left.timestampValue !== null && right.timestampValue === null) {
          return -1;
        }
        if (left.timestampValue === null && right.timestampValue !== null) {
          return 1;
        }

        const rawTimestampDiff = (left.event.timestamp ?? '').localeCompare(right.event.timestamp ?? '');
        if (rawTimestampDiff !== 0) {
          return rawTimestampDiff;
        }

        return left.originalIndex - right.originalIndex;
      })
      .map(({ event }) => event);
  }, [workflow]);

  const timelineSummary = useMemo<TimelineSummary>(() => {
    const byType: Record<string, number> = {};
    const byAgentType: Record<string, number> = {};
    const byBackend: Record<string, number> = {};
    let firstTimestamp: string | null = null;
    let lastTimestamp: string | null = null;

    for (const event of timeline) {
      incrementCount(byType, event.type);
      incrementCount(byAgentType, event.agent_type);
      incrementCount(byBackend, event.backend);
      if (getTimestampSortValue(event.timestamp) !== null) {
        if (!firstTimestamp) {
          firstTimestamp = event.timestamp;
        }
        lastTimestamp = event.timestamp;
      }
    }

    return {
      total: timeline.length,
      byType,
      byAgentType,
      byBackend,
      firstTimestamp,
      lastTimestamp,
    };
  }, [timeline]);

  if (loading) {
    return (
      <div className="workflow-detail-page p-6">
        <div className="mx-auto max-w-[1480px]">
          <div className="workflow-panel p-6 text-sm text-muted">{t('workflowDetail.loadingDetail')}</div>
        </div>
      </div>
    );
  }

  if (error || !workflow) {
    return (
      <div className="workflow-detail-page p-6">
        <div className="mx-auto max-w-[1480px]">
          <div className="workflow-panel p-6 text-sm text-danger">{error ?? t('workflowDetail.workflowNotFound')}</div>
        </div>
      </div>
    );
  }

  const metadata = workflow.metadata ?? {};
  const enabledBackends = getStringArray(metadata.backends).sort((left, right) => left.localeCompare(right));
  const activityEntries = Object.entries(workflow.backend_counts).sort(([, left], [, right]) => right - left);
  const topBackendEntry = activityEntries[0];
  const skillSelection = isRecord(metadata.skill_selection) ? metadata.skill_selection : null;
  const selectionMethod = skillSelection && typeof skillSelection.method === 'string' ? skillSelection.method : null;
  const executionDurationLabel = formatDurationSeconds(workflow.execution_time);
  const selectedSkillLabel = t('workflowDetail.skillsSelected', { count: workflow.selected_skills.length });
  const iterationsLabel = t('workflowDetail.iteration', { count: workflow.iterations });
  const totalStepLabel = t('workflowDetail.step', { count: workflow.total_steps });
  const actionCountLabel = t('workflowDetail.agentAction', { count: workflow.agent_action_count });
  const agentActionCount = timelineSummary.byType.agent_action ?? 0;
  const toolExecutionCount = timelineSummary.byType.tool_execution ?? 0;
  const latestEventLabel = timelineSummary.lastTimestamp ? formatTimeLabel(timelineSummary.lastTimestamp) : '—';
  const timelineEventLabel = t('workflowDetail.mergedEvent', { count: timelineSummary.total });
  const statusLabel = humanizeToken(workflow.status || 'unknown');
  const selectionMethodLabel = selectionMethod ? humanizeToken(selectionMethod) : t('workflowDetail.notRecorded');
  const successRateLabel = formatPercent(workflow.success_rate);

  return (
    <div className="workflow-detail-page p-6">
      <div className="mx-auto max-w-[1480px] space-y-6">
        <section className="workflow-hero p-6 lg:p-8 space-y-8">
          <div className="flex flex-col gap-6 xl:flex-row xl:items-start xl:justify-between">
            <div className="min-w-0 flex-1 space-y-5">
              <div className="flex flex-wrap items-center gap-3">
                <Link
                  to="/workflows"
                  className="workflow-chip text-sm transition-colors hover:border-[color:var(--color-border-dark)] hover:text-ink"
                >
                  {t('workflowDetail.backToWorkflows')}
                </Link>
                <WorkflowChip className={getStatusChipClasses(workflow.status)}>{statusLabel}</WorkflowChip>
                <WorkflowChip>{selectedSkillLabel}</WorkflowChip>
                <WorkflowChip>{timelineEventLabel}</WorkflowChip>
              </div>

              <div className="space-y-3">
                <div className="workflow-kicker">{t('workflowDetail.workflowDetail')}</div>
                <h1 className="max-w-5xl text-4xl font-semibold leading-[1.05] tracking-[-0.05em] text-ink lg:text-5xl xl:text-[3.6rem]">
                  {workflow.task_name}
                </h1>
                <p className="workflow-copy max-w-4xl text-lg leading-8 text-muted line-clamp-4">
                  {formatInstruction(workflow.instruction, 480, t('format.noInstruction'))}
                </p>
              </div>
            </div>

            <div className="workflow-soft-card w-full max-w-sm shrink-0 p-5 space-y-5">
              <p className="workflow-copy text-base leading-7 text-muted">
                {t('workflowDetail.runDescription', { status: statusLabel, iterations: iterationsLabel, actions: actionCountLabel })}
              </p>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1">
                  <div className="workflow-kicker">{t('workflowDetail.started')}</div>
                  <div className="text-base font-medium text-ink">{formatDate(workflow.start_time)}</div>
                </div>
                <div className="space-y-1">
                  <div className="workflow-kicker">{t('workflowDetail.duration')}</div>
                  <div className="text-base font-medium text-ink">{executionDurationLabel}</div>
                </div>
                <div className="space-y-1">
                  <div className="workflow-kicker">{t('workflowDetail.stepsLabel')}</div>
                  <div className="text-base font-medium text-ink">{totalStepLabel}</div>
                </div>
                <div className="space-y-1">
                  <div className="workflow-kicker">{t('workflowDetail.latestEvent')}</div>
                  <div className="text-base font-medium text-ink">{latestEventLabel}</div>
                </div>
              </div>
            </div>
          </div>

          <section className="workflow-metrics-row">
            <SummaryMetric
              label={t('workflowDetail.successRate')}
              value={successRateLabel}
              hint={t('workflowDetail.successRateHint', { count: workflow.success_count, iterations: iterationsLabel })}
            />
            <SummaryMetric
              label={t('workflowDetail.iterations')}
              value={workflow.iterations}
              hint={t('workflowDetail.totalRuntime', { duration: executionDurationLabel })}
            />
            <SummaryMetric
              label={t('workflowDetail.activeBackends')}
              value={activityEntries.length}
              hint={topBackendEntry ? t('workflowDetail.mostActive', { backend: humanizeToken(topBackendEntry[0]), count: topBackendEntry[1] }) : t('workflowDetail.noRecordedActivity')}
            />
            <SummaryMetric
              label={t('workflowDetail.timelineEvents')}
              value={timelineSummary.total}
              hint={t('workflowDetail.agentToolHint', { agentCount: agentActionCount, toolCount: toolExecutionCount })}
            />
          </section>
        </section>

        <section className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.72fr)]">
          <div className="workflow-panel p-5 space-y-4">
            {timeline.length === 0 ? (
              <QuietEmptyState
                title={t('workflowDetail.noTimelineData')}
                description={t('workflowDetail.noTimelineDesc')}
              />
            ) : (
              <div role="list" aria-label="Workflow timeline events">
                {timeline.map((event, index) => {
                  const isOpen = openIndex === index;
                  const presentation = describeTimelineEvent(event);
                  const markerClasses = getMarkerClasses(getEventTone(event));
                  const visibleIdentity = presentation.primaryFact?.value ?? null;
                  const visibleMeta = presentation.secondaryFact ?? null;

                  return (
                    <article key={`${event.timestamp}-${event.type}-${index}`} className="workflow-accordion-item flex gap-3" role="listitem">
                      <div className="flex w-10 shrink-0 flex-col items-center self-stretch">
                        <div className={`flex h-8 w-8 items-center justify-center rounded-full border text-[11px] font-semibold ${markerClasses}`}>
                          {index + 1}
                        </div>
                        {index < timeline.length - 1 ? <div className="mt-2.5 w-px flex-1 bg-[color:var(--color-border)]" /> : null}
                      </div>

                      <div className={`workflow-expand flex-1${isOpen ? ' is-opened' : ''}`}>
                        <button
                          type="button"
                          className="w-full cursor-pointer border-0 bg-transparent p-0 text-left font-inherit"
                          onClick={() => setOpenIndex(isOpen ? null : index)}
                          aria-expanded={isOpen}
                        >
                          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                            <div className="min-w-0 space-y-2">
                              <div className="flex flex-wrap items-center gap-2">
                                <div className="workflow-kicker">{humanizeToken(event.type)}</div>
                                {event.status ? (
                                  <WorkflowChip className={getStatusChipClasses(event.status)}>
                                    {humanizeToken(event.status)}
                                  </WorkflowChip>
                                ) : null}
                              </div>

                              <div className="space-y-1.5">
                                <h3 className="text-lg font-semibold leading-tight tracking-[-0.03em] text-ink">
                                  {presentation.title}
                                </h3>
                                {(visibleIdentity || visibleMeta) ? (
                                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm leading-6 text-muted">
                                    {visibleIdentity ? (
                                      <span className="font-medium text-ink">{visibleIdentity}</span>
                                    ) : null}
                                    {visibleMeta ? (
                                      <span>
                                        <span className="workflow-inline-label">{visibleMeta.label}</span>
                                        {visibleMeta.value}
                                      </span>
                                    ) : null}
                                  </div>
                                ) : null}
                              </div>
                            </div>

                            <div className="flex items-start gap-3">
                              <div className="shrink-0 text-left lg:text-right">
                                <div className="text-sm text-muted">{formatTimeLabel(event.timestamp)}</div>
                              </div>
                              <span className="workflow-expand-summary">
                                <span className="workflow-expand-icon" aria-hidden="true">
                                  <span className="workflow-toggle-line" />
                                  <span className="workflow-toggle-line is-2" />
                                </span>
                              </span>
                            </div>
                          </div>
                        </button>

                        <div className="workflow-accordion-content">
                          <div className="overflow-hidden">
                            <div className="workflow-expand-body mt-3 space-y-3 border-t border-[color:var(--color-border)] pt-3">
                              {presentation.summary ? (
                                <p className="workflow-copy text-[15px] leading-6 text-ink">{presentation.summary}</p>
                              ) : null}
                              {presentation.secondary ? (
                                <p className="text-sm leading-6 text-muted">{presentation.secondary}</p>
                              ) : null}

                              {presentation.facts.length > 0 ? (
                                <dl className="flex flex-wrap gap-x-5 gap-y-2">
                                  {presentation.facts.map((fact) => (
                                    <div key={`${fact.label}-${fact.value}`} className="flex min-w-0 items-baseline gap-2">
                                      <dt className="text-[11px] uppercase tracking-[0.16em] text-muted whitespace-nowrap">{fact.label}</dt>
                                      <dd className="break-words text-sm leading-6 text-ink">{fact.value}</dd>
                                    </div>
                                  ))}
                                </dl>
                              ) : null}

                              <div className="workflow-soft-card p-3.5">
                                <div className="text-[11px] uppercase tracking-[0.16em] text-muted">{t('workflowDetail.rawEventJson')}</div>
                                <pre className="workflow-json mt-3 whitespace-pre-wrap break-all text-xs leading-6 text-muted">
                                  {stringify(event.details)}
                                </pre>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </div>

          <aside className="space-y-4 xl:sticky xl:top-6">
            <section className="workflow-panel p-5 space-y-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="workflow-kicker">{t('workflowDetail.selection')}</div>
                  <h2 className="mt-2 text-2xl font-semibold tracking-[-0.03em] text-ink">{t('workflowDetail.selectedSkills')}</h2>
                </div>
                {workflow.selected_skills.length > 0 ? <WorkflowChip>{workflow.selected_skills.length}</WorkflowChip> : null}
              </div>

              {workflow.selected_skills.length > 0 ? (
                <div className="flex flex-wrap gap-2 text-xs">
                  {workflow.selected_skills.map((skillId, index) => (
                    <Link
                      key={`${skillId}-${index}`}
                      to={`/skills/${encodeURIComponent(skillId)}`}
                      title={skillId}
                      className="workflow-chip inline-flex max-w-full items-center transition-colors hover:border-[color:var(--color-border-dark)] hover:text-ink"
                    >
                      <span className="block max-w-[240px] truncate">{skillId}</span>
                    </Link>
                  ))}
                </div>
              ) : (
                <div className="space-y-2">
                  <div className="text-lg font-semibold tracking-[-0.02em] text-ink">{t('workflowDetail.noSelectedSkills')}</div>
                  <p className="workflow-copy text-sm leading-6 text-muted">{t('workflowDetail.noSelectedSkillsDesc')}</p>
                </div>
              )}
            </section>

            <section className="workflow-panel p-5 space-y-5">
              <div>
                <div className="workflow-kicker">{t('workflowDetail.session')}</div>
                <h2 className="mt-2 text-2xl font-semibold tracking-[-0.03em] text-ink">{t('workflowDetail.overview')}</h2>
              </div>

              <div className="space-y-5">
                <SidebarRow label={t('workflowDetail.taskId')}>
                  <div className="break-all">{workflow.task_id}</div>
                </SidebarRow>

                <SidebarRow label={t('workflowDetail.runtime')}>
                  <div>{executionDurationLabel}</div>
                  <div className="text-xs leading-6 text-muted">
                    {iterationsLabel} · {totalStepLabel} · {actionCountLabel}
                  </div>
                </SidebarRow>

                <SidebarRow label={t('workflowDetail.window')}>
                  <div>{formatDate(workflow.start_time)}</div>
                  <div className="text-xs leading-6 text-muted">{t('workflowDetail.ended', { date: formatDate(workflow.end_time) })}</div>
                </SidebarRow>

                <SidebarRow label={t('workflowDetail.selectionMethod')}>
                  <div>{selectionMethodLabel}</div>
                  <div className="text-xs leading-6 text-muted">{selectedSkillLabel}</div>
                </SidebarRow>

                {enabledBackends.length > 0 ? (
                  <SidebarRow label={t('workflowDetail.enabledBackends')}>
                    <div className="flex flex-wrap gap-2 text-xs">
                      {enabledBackends.map((backend) => (
                        <WorkflowChip key={backend}>{humanizeToken(backend)}</WorkflowChip>
                      ))}
                    </div>
                  </SidebarRow>
                ) : null}

                {activityEntries.length > 0 ? (
                  <SidebarRow label={t('workflowDetail.backendActivity')}>
                    <div className="flex flex-wrap gap-2 text-xs">
                      {activityEntries.map(([backend, count]) => (
                        <WorkflowChip key={backend}>{`${humanizeToken(backend)} ${count}`}</WorkflowChip>
                      ))}
                    </div>
                  </SidebarRow>
                ) : null}
              </div>

            </section>
          </aside>
        </section>
      </div>
    </div>
  );
}
