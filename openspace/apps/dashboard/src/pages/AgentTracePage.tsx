import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { workflowsApi, type WorkflowDetail, type WorkflowSummary, type WorkflowTraceDatum, type WorkflowTraceEvent } from '../api';
import { formatDate, formatInstruction } from '../utils/format';

type TraceMetricProps = {
  label: string;
  value: ReactNode;
  hint: string;
};

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function collapseWhitespace(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function truncate(value: string, max = 220): string {
  const compact = collapseWhitespace(value);
  if (compact.length <= max) {
    return compact;
  }
  return `${compact.slice(0, max).trimEnd()}...`;
}

function humanize(value: string): string {
  const normalized = value.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!normalized) {
    return 'Unknown';
  }
  return normalized.replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatTime(value?: string | null): string {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (!Number.isNaN(date.getTime())) {
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(date);
  }
  return value;
}

function metricHint(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function TraceMetric({ label, value, hint }: TraceMetricProps) {
  return (
    <div className="trace-metric">
      <div className="trace-kicker">{label}</div>
      <div className="trace-metric-value">{value}</div>
      <div className="trace-metric-hint">{hint}</div>
    </div>
  );
}

function TraceChip({ children, tone = 'default' }: { children: ReactNode; tone?: string }) {
  return <span className={`trace-chip trace-chip-${tone}`}>{children}</span>;
}

function datumText(item: WorkflowTraceDatum): string {
  if (typeof item.value === 'string') {
    return item.value;
  }
  if (typeof item.value === 'number' || typeof item.value === 'boolean' || item.value === null) {
    return String(item.value);
  }
  return stringify(item.value);
}

function comparableText(value: string): string {
  return collapseWhitespace(value).toLowerCase();
}

function TraceDatumCard({ item }: { item: WorkflowTraceDatum }) {
  const { t } = useTranslation();
  const value = datumText(item);
  const isStructured = item.kind === 'json' || typeof item.value === 'object';
  const preview = item.preview?.trim() ?? '';
  const hasPreview = preview.length > 0;
  const valueLabel = isStructured ? 'JSON' : item.kind;
  const shouldCollapseValue = hasPreview && comparableText(preview) !== comparableText(value);
  const shouldShowValueOnly = !hasPreview || comparableText(preview) === comparableText(value);

  return (
    <div className="trace-io-card">
      <div className="flex items-center justify-between gap-2">
        <div className="trace-io-label">{item.label}</div>
        <TraceChip>{valueLabel}</TraceChip>
      </div>
      {hasPreview ? (
        <div className="trace-io-preview mt-2 text-sm leading-6 text-ink">{preview}</div>
      ) : null}
      {shouldShowValueOnly ? (
        isStructured ? (
          <pre className="trace-io-value mt-2 whitespace-pre-wrap break-words text-xs leading-6 text-muted">
            {value}
          </pre>
        ) : (
          <div className="trace-io-value mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-muted">
            {value.length > 0 ? value : '-'}
          </div>
        )
      ) : null}
      {shouldCollapseValue ? (
        <details className="trace-inline-details mt-3">
          <summary>{isStructured ? t('agentTrace.showFullJson') : t('agentTrace.showFullValue')}</summary>
          {isStructured ? (
            <pre className="trace-io-value mt-2 whitespace-pre-wrap break-words text-xs leading-6 text-muted">
              {value}
            </pre>
          ) : (
            <div className="trace-io-value mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-muted">
              {value.length > 0 ? value : '-'}
            </div>
          )}
        </details>
      ) : null}
    </div>
  );
}

function TraceFact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="trace-fact">
      <div className="trace-kicker">{label}</div>
      <div className="mt-1 min-w-0 break-words text-sm leading-6 text-ink">{value || '-'}</div>
    </div>
  );
}

function DetailMetaLine({ event }: { event: WorkflowTraceEvent }) {
  const parts = [
    `#${event.sequence}`,
    formatTime(event.timestamp),
    humanize(event.harness),
    event.status ? humanize(event.status) : null,
  ].filter(Boolean);
  return <div className="trace-meta-line">{parts.join(' · ')}</div>;
}

function DetailFactGrid({ event }: { event: WorkflowTraceEvent }) {
  const { t } = useTranslation();
  const facts = [
    { label: t('agentTrace.iteration'), value: event.iteration ? String(event.iteration) : null },
    { label: t('agentTrace.source'), value: event.source },
    { label: t('agentTrace.agent'), value: event.agent_name },
    { label: t('agentTrace.tool'), value: event.tool_name },
    { label: t('agentTrace.backend'), value: event.backend ? humanize(event.backend) : null },
  ].filter((item): item is { label: string; value: string } => Boolean(item.value));

  if (facts.length === 0) {
    return null;
  }

  return (
    <section className="trace-debug-summary">
      {facts.map((fact) => (
        <TraceFact key={fact.label} label={fact.label} value={fact.value} />
      ))}
    </section>
  );
}

function TraceDataList({
  title,
  items,
}: {
  title: string;
  items: WorkflowTraceDatum[];
}) {
  if (items.length === 0) {
    return null;
  }
  return (
    <section className="trace-explain-block trace-io-section">
      <div className="flex items-center justify-between gap-3">
        <div className="trace-kicker">{title}</div>
        <TraceChip>{items.length}</TraceChip>
      </div>
      <div className="mt-3 space-y-3">
        {items.map((item, index) => (
          <TraceDatumCard key={`${item.label}-${index}`} item={item} />
        ))}
      </div>
    </section>
  );
}

function HarnessDot({ harness }: { harness: string }) {
  return <span className={`trace-dot trace-dot-${harness}`} aria-hidden="true" />;
}

function eventMatches(event: WorkflowTraceEvent, query: string): boolean {
  if (!query) {
    return true;
  }
  const haystack = [
    event.harness,
    event.source,
    event.title,
    event.summary,
    event.decision,
    event.impact,
    event.agent_name,
    event.tool_name,
    event.backend,
    event.status,
    ...event.based_on,
    ...(event.inputs ?? []).map((item) => `${item.label}\n${item.preview}\n${datumText(item)}`),
    ...(event.outputs ?? []).map((item) => `${item.label}\n${item.preview}\n${datumText(item)}`),
  ].join('\n').toLowerCase();
  return haystack.includes(query);
}

function basisItems(event: WorkflowTraceEvent): string[] {
  return event.based_on.length > 0 ? event.based_on : ['No explicit basis was recorded for this event.'];
}

function eventSubtitle(event: WorkflowTraceEvent): string {
  const parts = [
    event.agent_name,
    event.tool_name,
    event.backend ? humanize(event.backend) : null,
    event.iteration ? `Iteration ${event.iteration}` : null,
  ].filter(Boolean);
  return parts.join(' · ');
}

function eventSummary(event: WorkflowTraceEvent): string {
  const summary = event.summary?.trim() ?? '';
  if (!summary || comparableText(summary) === comparableText(event.title)) {
    return '';
  }
  return summary;
}

function workflowLogFolderKey(workflow: WorkflowSummary): string {
  return workflow.log_folder || workflow.log_root || 'unknown';
}

function workflowLogFolderLabel(workflow: WorkflowSummary): string {
  return workflow.log_folder_label || workflow.log_root_label || 'Unknown log folder';
}

function workflowRunLabel(workflow: WorkflowSummary): string {
  const started = formatDate(workflow.start_time);
  const path = workflow.log_relative_path || workflow.path;
  return `${workflow.task_name} · ${started} · ${path}`;
}

export default function AgentTracePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { workflowId = '' } = useParams();
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [activeHarness, setActiveHarness] = useState('all');
  const [activeLogFolder, setActiveLogFolder] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const items = await workflowsApi.listWorkflows();
        const targetWorkflowId = workflowId === 'latest' ? items[0]?.id : workflowId;
        if (!targetWorkflowId) {
          throw new Error(t('agentTrace.noLogRuns'));
        }
        const detail = await workflowsApi.getWorkflow(targetWorkflowId);
        if (!cancelled) {
          setWorkflows(items);
          setWorkflow(detail);
          setSelectedId(detail.trace?.events[0]?.event_id ?? null);
          setActiveLogFolder((current) => current ?? workflowLogFolderKey(detail));
          if (workflowId === 'latest') {
            navigate(`/workflows/${encodeURIComponent(detail.id)}/trace`, { replace: true });
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t('agentTrace.failedToLoad'));
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
  }, [navigate, workflowId, t]);

  const trace = workflow?.trace;
  const events = trace?.events ?? [];
  const logFolderOptions = useMemo(() => {
    const folders = new Map<string, { id: string; label: string; count: number }>();
    for (const item of workflows) {
      const id = workflowLogFolderKey(item);
      const existing = folders.get(id);
      if (existing) {
        existing.count += 1;
      } else {
        folders.set(id, { id, label: workflowLogFolderLabel(item), count: 1 });
      }
    }
    return Array.from(folders.values());
  }, [workflows]);
  const resolvedActiveLogFolder = activeLogFolder ?? (workflow ? workflowLogFolderKey(workflow) : 'all');
  const visibleWorkflowOptions = useMemo(() => (
    resolvedActiveLogFolder === 'all'
      ? workflows
      : workflows.filter((item) => workflowLogFolderKey(item) === resolvedActiveLogFolder)
  ), [resolvedActiveLogFolder, workflows]);
  const runWorkflowOptions = useMemo(() => {
    if (!workflow || visibleWorkflowOptions.some((item) => item.id === workflow.id)) {
      return visibleWorkflowOptions;
    }
    return [workflow, ...visibleWorkflowOptions];
  }, [visibleWorkflowOptions, workflow]);
  const harnessEntries = useMemo(
    () => Object.entries(trace?.summary.harness_counts ?? {}).sort(([, left], [, right]) => right - left),
    [trace],
  );
  const filteredEvents = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return events.filter((event) => (
      (activeHarness === 'all' || event.harness === activeHarness) &&
      eventMatches(event, normalizedQuery)
    ));
  }, [activeHarness, events, query]);
  const selectedEvent = useMemo(() => {
    if (filteredEvents.length === 0) {
      return null;
    }
    return filteredEvents.find((event) => event.event_id === selectedId) ?? filteredEvents[0];
  }, [filteredEvents, selectedId]);

  const latestWorkflow = workflows[0] ?? workflow;

  const openWorkflowTrace = (nextWorkflowId: string) => {
    navigate(`/workflows/${encodeURIComponent(nextWorkflowId)}/trace`);
  };

  const handleLogFolderChange = (nextFolder: string) => {
    setActiveLogFolder(nextFolder);
    const candidates = nextFolder === 'all'
      ? workflows
      : workflows.filter((item) => workflowLogFolderKey(item) === nextFolder);
    const nextWorkflow = candidates[0];
    if (nextWorkflow && nextWorkflow.id !== workflow?.id) {
      openWorkflowTrace(nextWorkflow.id);
    }
  };

  if (loading) {
    return (
      <div className="trace-page p-6">
        <div className="trace-panel p-6 text-sm text-muted">{t('agentTrace.loading')}</div>
      </div>
    );
  }

  if (error || !workflow || !trace) {
    return (
      <div className="trace-page p-6">
        <div className="trace-panel p-6 text-sm text-danger">{error ?? t('agentTrace.notFound')}</div>
      </div>
    );
  }

  return (
    <div className="trace-page p-6">
      <div className="mx-auto max-w-[1560px] space-y-5">
        <section className="trace-hero space-y-5">
          <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div className="min-w-0 space-y-3">
              <div className="flex flex-wrap gap-2">
                <Link className="trace-chip" to={`/workflows/${encodeURIComponent(workflow.id)}`}>
                  {t('agentTrace.backToWorkflow')}
                </Link>
              </div>
              <div>
                <div className="trace-kicker">{workflow.task_id}</div>
                <h1 className="mt-2 max-w-5xl text-4xl font-semibold leading-[1.05] text-ink lg:text-5xl">
                  {workflow.task_name}
                </h1>
                <p className="mt-3 max-w-5xl text-base leading-7 text-muted">
                  {formatInstruction(workflow.instruction, 520, t('format.noInstruction'))}
                </p>
              </div>
            </div>
            <div className="trace-panel w-full max-w-md p-4">
              <div className="grid grid-cols-2 gap-4">
                <TraceMetric label={t('agentTrace.started')} value={formatDate(workflow.start_time)} hint={formatDate(workflow.end_time)} />
                <TraceMetric label={t('agentTrace.status')} value={humanize(workflow.status)} hint={metricHint(workflow.total_steps, 'tool step')} />
                <TraceMetric label={t('agentTrace.events')} value={trace.summary.total_events} hint={metricHint(trace.summary.iterations.length, 'iteration')} />
                <TraceMetric label={t('agentTrace.tools')} value={trace.summary.tools.length} hint={metricHint(trace.summary.agents.length, 'agent')} />
              </div>
            </div>
          </div>

          <div className="trace-panel p-4">
            <div className="trace-log-grid">
              <label className="trace-control">
                <span className="trace-kicker">{t('agentTrace.logFolder')}</span>
                <select
                  value={resolvedActiveLogFolder}
                  onChange={(event) => handleLogFolderChange(event.target.value)}
                  className="trace-select"
                >
                  <option value="all">{t('agentTrace.allLogFolders', { count: workflows.length })}</option>
                  {logFolderOptions.map((folder) => (
                    <option key={folder.id} value={folder.id}>
                      {folder.label} · {folder.count}
                    </option>
                  ))}
                </select>
              </label>
              <label className="trace-control trace-run-control">
                <span className="trace-kicker">{t('agentTrace.runLog')}</span>
                <select
                  value={workflow.id}
                  onChange={(event) => openWorkflowTrace(event.target.value)}
                  className="trace-select"
                >
                  {runWorkflowOptions.map((item) => (
                    <option key={item.id} value={item.id}>
                      {workflowRunLabel(item)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="trace-filter trace-latest-button"
                disabled={!latestWorkflow}
                onClick={() => latestWorkflow ? openWorkflowTrace(latestWorkflow.id) : undefined}
              >
                {t('agentTrace.latestRun')}
              </button>
            </div>
            <div className="trace-log-path mt-3">
              <span className="trace-kicker">{t('agentTrace.currentLogPath')}</span>
              <code>{workflow.log_relative_path || workflow.path}</code>
            </div>
          </div>

          <div className="trace-panel p-3">
            <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className={`trace-filter ${activeHarness === 'all' ? 'is-active' : ''}`}
                  onClick={() => setActiveHarness('all')}
                >
                  {t('agentTrace.allHarnesses')} · {events.length}
                </button>
                {harnessEntries.map(([harness, count]) => (
                  <button
                    type="button"
                    key={harness}
                    className={`trace-filter ${activeHarness === harness ? 'is-active' : ''}`}
                    onClick={() => setActiveHarness(harness)}
                  >
                    <HarnessDot harness={harness} />
                    {humanize(harness)} · {count}
                  </button>
                ))}
              </div>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t('agentTrace.searchPlaceholder')}
                className="trace-search min-w-[320px] px-3 py-2"
              />
            </div>
          </div>
        </section>

        <section className="trace-debug-layout">
          <div className="trace-panel trace-event-panel min-h-0 overflow-hidden">
            <div className="border-b border-[color:var(--color-border)] px-4 py-3">
              <div className="trace-kicker">{t('agentTrace.eventStream')}</div>
              <div className="mt-1 text-sm text-muted">{t('agentTrace.visibleEvents', { count: filteredEvents.length })}</div>
            </div>
            <div className="trace-event-list overflow-auto p-2">
              {filteredEvents.length === 0 ? (
                <div className="p-5 text-sm leading-6 text-muted">{t('agentTrace.noEvents')}</div>
              ) : filteredEvents.map((event) => {
                const isSelected = selectedEvent?.event_id === event.event_id;
                const summary = eventSummary(event);
                return (
                  <button
                    type="button"
                    key={event.event_id}
                    className={`trace-event-row ${isSelected ? 'is-selected' : ''}`}
                    onClick={() => setSelectedId(event.event_id)}
                  >
                    <div className="flex items-start gap-3">
                      <div className="trace-sequence">{event.sequence}</div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <HarnessDot harness={event.harness} />
                          <span className="trace-kicker">{humanize(event.harness)}</span>
                          {event.status ? <TraceChip tone={event.status === 'error' ? 'danger' : 'success'}>{humanize(event.status)}</TraceChip> : null}
                        </div>
                        <div className="mt-2 truncate text-base font-semibold text-ink">{event.title}</div>
                        <div className="mt-1 text-sm leading-6 text-muted">{eventSubtitle(event) || event.source}</div>
                        {summary ? (
                          <div className="mt-2 text-left text-sm leading-6 text-muted">{truncate(summary, 190)}</div>
                        ) : null}
                        <div className="mt-3 flex flex-wrap gap-2">
                          <TraceChip>{t('agentTrace.ioCount', { inputs: event.inputs?.length ?? 0, outputs: event.outputs?.length ?? 0 })}</TraceChip>
                        </div>
                      </div>
                      <div className="shrink-0 text-xs text-muted">{formatTime(event.timestamp)}</div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="trace-panel min-h-0 overflow-hidden">
            {selectedEvent ? (
              <div className="flex flex-col">
                <div className="border-b border-[color:var(--color-border)] p-5">
                  <DetailMetaLine event={selectedEvent} />
                  <h2 className="mt-3 text-3xl font-semibold leading-tight text-ink">{selectedEvent.title}</h2>
                  {eventSummary(selectedEvent) ? (
                    <p className="mt-3 text-base leading-7 text-muted">{eventSummary(selectedEvent)}</p>
                  ) : null}
                </div>

                <div className="p-5 space-y-5">
                  <DetailFactGrid event={selectedEvent} />

                  <div className="space-y-5">
                    <TraceDataList
                      title={t('agentTrace.inputs')}
                      items={selectedEvent.inputs ?? []}
                    />
                    <TraceDataList
                      title={t('agentTrace.outputs')}
                      items={selectedEvent.outputs ?? []}
                    />
                  </div>

                  <div className="trace-reason-grid">
                    <section className="trace-explain-block">
                      <div className="trace-kicker">{t('agentTrace.basedOn')}</div>
                      <ul className="mt-3 space-y-2 text-sm leading-6 text-muted">
                        {basisItems(selectedEvent).map((item, index) => (
                          <li key={`${item}-${index}`}>{item}</li>
                        ))}
                      </ul>
                    </section>
                    <section className="trace-explain-block">
                      <div className="trace-kicker">{t('agentTrace.decision')}</div>
                      <p className="mt-3 text-sm leading-6 text-muted">{selectedEvent.decision || t('agentTrace.noDecision')}</p>
                    </section>
                    <section className="trace-explain-block">
                      <div className="trace-kicker">{t('agentTrace.impact')}</div>
                      <p className="mt-3 text-sm leading-6 text-muted">{selectedEvent.impact || t('agentTrace.noImpact')}</p>
                    </section>
                  </div>

                  <details className="trace-details-block">
                    <summary>{t('agentTrace.metadata')}</summary>
                    <pre className="trace-json mt-3 whitespace-pre-wrap break-words text-xs leading-6 text-muted">
                      {stringify(selectedEvent.metadata)}
                    </pre>
                  </details>

                  <details className="trace-details-block">
                    <summary>{t('agentTrace.raw')}</summary>
                    <pre className="trace-json mt-3 whitespace-pre-wrap break-words text-xs leading-6 text-muted">
                      {stringify(selectedEvent.raw)}
                    </pre>
                  </details>
                </div>
              </div>
            ) : (
              <div className="p-5 text-sm text-muted">{t('agentTrace.noEventSelected')}</div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
