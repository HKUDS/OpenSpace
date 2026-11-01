import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  evolutionApi,
  type EvidenceRefPreview,
  type EvolutionAction,
  type EvolutionCandidate,
  type EvolutionJob,
  type EvolutionReviewItem,
  type QualitySignalAuditRow,
} from '../api';
import EmptyState from '../components/EmptyState';
import MetricCard from '../components/MetricCard';
import { formatDate, truncate } from '../utils/format';

type JsonRecord = Record<string, unknown>;

const JOB_STATUSES = ['all', 'pending', 'running', 'failed_retryable', 'failed', 'completed'];
const CANDIDATE_STATUSES = ['pending', 'all', 'promoted', 'rejected', 'superseded'];

function statusTone(status: string) {
  if (['completed', 'committed', 'committed_reconciled', 'promoted'].includes(status)) {
    return 'text-accent';
  }
  if (['failed', 'failed_needs_review', 'rejected'].includes(status)) {
    return 'text-danger';
  }
  if (['failed_retryable', 'running', 'committing'].includes(status)) {
    return 'text-primary';
  }
  return 'text-muted';
}

function JsonPreview({ value }: { value: unknown }) {
  return (
    <pre className="field-surface max-h-[320px] overflow-auto p-3 text-xs whitespace-pre-wrap">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export default function EvolutionPage() {
  const { t } = useTranslation();
  const [jobs, setJobs] = useState<EvolutionJob[]>([]);
  const [candidates, setCandidates] = useState<EvolutionCandidate[]>([]);
  const [reviewItems, setReviewItems] = useState<EvolutionReviewItem[]>([]);
  const [qualitySignals, setQualitySignals] = useState<QualitySignalAuditRow[]>([]);
  const [jobStatus, setJobStatus] = useState('all');
  const [candidateStatus, setCandidateStatus] = useState('pending');
  const [selectedCandidate, setSelectedCandidate] = useState<EvolutionCandidate | null>(null);
  const [selectedReviewItem, setSelectedReviewItem] = useState<EvolutionReviewItem | null>(null);
  const [selectedJob, setSelectedJob] = useState<EvolutionJob | null>(null);
  const [selectedQualitySignal, setSelectedQualitySignal] = useState<QualitySignalAuditRow | null>(null);
  const [selectedDecision, setSelectedDecision] = useState<JsonRecord | null>(null);
  const [selectedAction, setSelectedAction] = useState<EvolutionAction | null>(null);
  const [selectedRef, setSelectedRef] = useState<EvidenceRefPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busyCandidateId, setBusyCandidateId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [qualitySignalError, setQualitySignalError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const reload = async () => {
    setLoading(true);
    setError(null);
    setQualitySignalError(null);
    try {
      const [nextJobs, nextCandidates, nextReviewItems] = await Promise.all([
        evolutionApi.listJobs({ status: jobStatus, limit: 100 }),
        evolutionApi.listCandidates({ status: candidateStatus, limit: 100 }),
        evolutionApi.listReviewItems({ limit: 100 }),
      ]);
      setJobs(nextJobs);
      setCandidates(nextCandidates);
      setReviewItems(nextReviewItems);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('evolution.failedToLoad'));
    } finally {
      setLoading(false);
    }
    try {
      setQualitySignals(await evolutionApi.listQualitySignals({ limit: 100 }));
    } catch (err) {
      setQualitySignals([]);
      setQualitySignalError(err instanceof Error ? err.message : t('evolution.failedToLoadSignals'));
    }
  };

  useEffect(() => {
    void reload();
  }, [candidateStatus, jobStatus]);

  const summary = useMemo(() => {
    const openJobs = jobs.filter((job) => ['pending', 'running', 'failed_retryable'].includes(job.status)).length;
    const failedJobs = jobs.filter((job) => job.status.startsWith('failed')).length;
    const pendingCandidates = candidates.filter((candidate) => candidate.status === 'pending').length;
    const pendingReviews = reviewItems.length;
    return { openJobs, failedJobs, pendingCandidates, pendingReviews };
  }, [candidates, jobs, reviewItems]);

  const loadCandidate = async (candidate: EvolutionCandidate) => {
    setDetailLoading(true);
    setDetailError(null);
    setSelectedReviewItem(null);
    setSelectedJob(null);
    setSelectedQualitySignal(null);
    setSelectedRef(null);
    setSelectedAction(null);
    try {
      const [detail, decision] = await Promise.all([
        evolutionApi.getCandidate(candidate.candidate_id),
        evolutionApi.getDecision(candidate.decision_id),
      ]);
      setSelectedCandidate(detail);
      setSelectedDecision(decision);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('evolution.failedToLoadDetails'));
    } finally {
      setDetailLoading(false);
    }
  };

  const loadAction = async (actionId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    setSelectedReviewItem(null);
    setSelectedJob(null);
    setSelectedQualitySignal(null);
    setSelectedCandidate(null);
    setSelectedDecision(null);
    setSelectedRef(null);
    try {
      setSelectedAction(await evolutionApi.getAction(actionId));
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('evolution.failedToLoadDetails'));
    } finally {
      setDetailLoading(false);
    }
  };

  const loadRefPreview = async (refId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      setSelectedRef(await evolutionApi.previewEvidenceRef(refId, 2000));
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('evolution.failedToLoadRef'));
    } finally {
      setDetailLoading(false);
    }
  };

  const loadJob = async (job: EvolutionJob) => {
    setDetailLoading(true);
    setDetailError(null);
    setSelectedCandidate(null);
    setSelectedReviewItem(null);
    setSelectedQualitySignal(null);
    setSelectedAction(null);
    setSelectedRef(null);
    try {
      const detail = await evolutionApi.getJob(job.job_id);
      setSelectedJob(detail);
      setSelectedDecision(detail.decision_ids[0] ? await evolutionApi.getDecision(detail.decision_ids[0]) : null);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('evolution.failedToLoadDetails'));
    } finally {
      setDetailLoading(false);
    }
  };

  const loadReviewItem = async (item: EvolutionReviewItem) => {
    if (item.item_type === 'candidate' && item.candidate_id) {
      const candidate = candidates.find((candidateItem) => candidateItem.candidate_id === item.candidate_id);
      if (candidate) {
        await loadCandidate(candidate);
        setSelectedReviewItem(item);
        return;
      }
    }
    setDetailLoading(true);
    setDetailError(null);
    setSelectedCandidate(null);
    setSelectedJob(null);
    setSelectedQualitySignal(null);
    setSelectedAction(null);
    setSelectedRef(null);
    setSelectedReviewItem(item);
    try {
      setSelectedDecision(item.decision_id ? await evolutionApi.getDecision(item.decision_id) : null);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('evolution.failedToLoadDetails'));
    } finally {
      setDetailLoading(false);
    }
  };

  const selectQualitySignal = (signal: QualitySignalAuditRow) => {
    setDetailLoading(false);
    setDetailError(null);
    setSelectedCandidate(null);
    setSelectedReviewItem(null);
    setSelectedJob(null);
    setSelectedDecision(null);
    setSelectedAction(null);
    setSelectedRef(null);
    setSelectedQualitySignal(signal);
  };

  const rejectCandidate = async (candidate: EvolutionCandidate) => {
    setBusyCandidateId(candidate.candidate_id);
    setDetailError(null);
    try {
      const updated = await evolutionApi.rejectCandidate(candidate.candidate_id, 'manual reject from dashboard');
      setSelectedCandidate(updated);
      setSelectedReviewItem(null);
      await reload();
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('evolution.failedToUpdateCandidate'));
    } finally {
      setBusyCandidateId(null);
    }
  };

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold font-serif">{t('evolution.title')}</h1>
        </div>
        <button type="button" className="btn-outline-ink text-sm" onClick={() => void reload()}>
          {t('evolution.refresh')}
        </button>
      </div>

      <section className="metrics-row">
        <MetricCard label={t('evolution.openJobs')} value={summary.openJobs} hint={t('evolution.visibleJobs', { count: jobs.length })} />
        <MetricCard label={t('evolution.failedJobs')} value={summary.failedJobs} hint={t('evolution.jobFilter', { status: jobStatus })} />
        <MetricCard label={t('evolution.pendingReviewItems')} value={summary.pendingReviews} hint={t('evolution.reviewQueueHint')} />
        <MetricCard label={t('evolution.pendingCandidates')} value={summary.pendingCandidates} hint={t('evolution.candidateFilter', { status: candidateStatus })} />
      </section>

      <section className="grid grid-cols-[1.1fr_0.9fr] gap-6">
        <div className="space-y-6">
          <div className="panel-surface p-5 space-y-4">
            <div>
              <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('evolution.qualitySignalAudit')}</div>
              <h2 className="text-2xl font-bold font-serif mt-1">{t('evolution.qualitySignals')}</h2>
            </div>
            {qualitySignalError ? <div className="text-sm text-danger">{qualitySignalError}</div> : null}
            {!loading && !error && !qualitySignalError && qualitySignals.length === 0 ? (
              <EmptyState title={t('evolution.noQualitySignals')} description={t('evolution.noQualitySignalsDesc')} />
            ) : null}
            <div className="space-y-3">
              {qualitySignals.map((signal) => (
                <article key={signal.signal_ref || signal.job_id || signal.merge_key} className="record-card p-4 space-y-3">
                  <div className="flex items-start justify-between gap-4">
                    <button
                      type="button"
                      className="min-w-0 flex-1 bg-transparent p-0 text-left"
                      onClick={() => selectQualitySignal(signal)}
                    >
                      <div className="font-bold truncate">
                        {signal.signal_type || signal.actionability || t('evolution.qualitySignalLabel')}
                      </div>
                      <div className="text-xs text-muted font-mono break-all">
                        {signal.signal_ref || signal.job_id || signal.merge_key || t('common.none')}
                      </div>
                    </button>
                    <div className={`text-sm font-bold shrink-0 ${statusTone(signal.job_status || signal.admission_status)}`}>
                      {signal.job_status || signal.admission_status || t('evolution.signalOnly')}
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                    <div className="break-all">
                      {signal.subject_type || t('common.unknown')}: {signal.subject_id || signal.tool_key || signal.skill_id || t('common.none')}
                    </div>
                    <div className="break-all">
                      {signal.not_triggerable_reason || t('evolution.triggerable')}
                    </div>
                    <div className="break-all">
                      {t('evolution.admissionShort')} {signal.admission_status || t('common.none')}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn-outline-ink text-xs"
                    onClick={() => selectQualitySignal(signal)}
                  >
                    {t('evolution.inspectSignal')}
                  </button>
                </article>
              ))}
            </div>
          </div>

          <div className="panel-surface p-5 space-y-4">
            <div>
              <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('evolution.reviewEntrance')}</div>
              <h2 className="text-2xl font-bold font-serif mt-1">{t('evolution.reviewQueue')}</h2>
            </div>
            {!loading && !error && reviewItems.length === 0 ? (
              <EmptyState title={t('evolution.noReviewItems')} description={t('evolution.noReviewItemsDesc')} />
            ) : null}
            <div className="space-y-3">
              {reviewItems.map((item) => (
                <article key={item.item_id} className="record-card p-4 space-y-3">
                  <div className="flex items-start justify-between gap-4">
                    <button
                      type="button"
                      className="min-w-0 flex-1 bg-transparent p-0 text-left"
                      onClick={() => void loadReviewItem(item)}
                    >
                      <div className="font-bold truncate">{item.title}</div>
                      <div className="text-xs text-muted font-mono break-all">{item.item_id}</div>
                    </button>
                    <div className={`text-sm font-bold shrink-0 ${statusTone(item.status)}`}>{item.status}</div>
                  </div>
                  <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                    <div>{item.item_type}</div>
                    <div>{item.summary || item.review_note || t('common.none')}</div>
                    <div>{formatDate(item.updated_at || item.created_at)}</div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className="btn-outline-ink text-xs"
                      onClick={() => void loadReviewItem(item)}
                    >
                      {t('evolution.inspectReview')}
                    </button>
                    <span className="tag px-2 py-1 text-xs text-muted">
                      {t('evolution.inspectOnly')}
                    </span>
                  </div>
                </article>
              ))}
            </div>
          </div>

          <div className="panel-surface p-5 space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('evolution.auditJobs')}</div>
                <h2 className="text-2xl font-bold font-serif mt-1">{t('evolution.triggerJobs')}</h2>
              </div>
              <select value={jobStatus} onChange={(event) => setJobStatus(event.target.value)} className="px-3 py-2 text-sm">
                {JOB_STATUSES.map((status) => (
                  <option key={status} value={status}>{status}</option>
                ))}
              </select>
            </div>
            {loading ? <div className="text-sm text-muted">{t('common.loading')}</div> : null}
            {error ? <div className="text-sm text-danger">{error}</div> : null}
            {!loading && !error && jobs.length === 0 ? (
              <EmptyState title={t('evolution.noJobs')} description={t('evolution.noJobsDesc')} />
            ) : null}
            <div className="space-y-3">
              {jobs.map((job) => (
                <article key={job.job_id} className="record-card p-4 space-y-3">
                  <div className="flex items-start justify-between gap-4">
                    <button
                      type="button"
                      className="min-w-0 flex-1 bg-transparent p-0 text-left"
                      onClick={() => void loadJob(job)}
                    >
                      <div className="font-bold truncate">{job.trigger_type} - {job.reason}</div>
                      <div className="text-xs text-muted font-mono break-all">{job.job_id}</div>
                    </button>
                    <div className={`text-sm font-bold shrink-0 ${statusTone(job.status)}`}>{job.status}</div>
                  </div>
                  <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                    <div>{formatDate(job.created_at)}</div>
                    <div>{t('evolution.packets', { count: job.packet_ids.length })}</div>
                    <div>{t('evolution.decisions', { count: job.decision_ids.length })}</div>
                  </div>
                  {job.action_ids.length > 0 ? (
                    <div className="flex flex-wrap gap-2 text-xs">
                      {job.action_ids.map((actionId) => (
                        <button
                          key={actionId}
                          type="button"
                          className="tag px-2 py-1 hover:border-[color:var(--color-border-dark)]"
                          onClick={() => void loadAction(actionId)}
                        >
                          {truncate(actionId, 32)}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <button
                    type="button"
                    className="btn-outline-ink text-xs"
                    onClick={() => void loadJob(job)}
                  >
                    {t('evolution.inspectJob')}
                  </button>
                </article>
              ))}
            </div>
          </div>

          <div className="panel-surface p-5 space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('evolution.candidates')}</div>
                <h2 className="text-2xl font-bold font-serif mt-1">{t('evolution.admissionQueue')}</h2>
              </div>
              <select value={candidateStatus} onChange={(event) => setCandidateStatus(event.target.value)} className="px-3 py-2 text-sm">
                {CANDIDATE_STATUSES.map((status) => (
                  <option key={status} value={status}>{status}</option>
                ))}
              </select>
            </div>
            {!loading && !error && candidates.length === 0 ? (
              <EmptyState title={t('evolution.noCandidates')} description={t('evolution.noCandidatesDesc')} />
            ) : null}
            <div className="space-y-3">
              {candidates.map((candidate) => (
                <article key={candidate.candidate_id} className="record-card p-4 space-y-3">
                  <div className="flex items-start justify-between gap-4">
                    <button
                      type="button"
                      className="min-w-0 flex-1 bg-transparent p-0 text-left"
                      onClick={() => void loadCandidate(candidate)}
                    >
                      <div className="font-bold truncate">{candidate.proposed_action}</div>
                      <div className="text-xs text-muted font-mono break-all">{candidate.candidate_id}</div>
                    </button>
                    <div className={`text-sm font-bold shrink-0 ${statusTone(candidate.status)}`}>{candidate.status}</div>
                  </div>
                  <div className="grid grid-cols-3 gap-3 text-xs text-muted">
                    <div>{t('evolution.recurrence', { count: candidate.recurrence_count })}</div>
                    <div>{t('evolution.targets', { count: candidate.target_skill_ids.length })}</div>
                    <div>{formatDate(candidate.updated_at)}</div>
                  </div>
                  {candidate.status === 'pending' ? (
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="btn-outline-ink text-xs"
                        disabled={busyCandidateId === candidate.candidate_id}
                        onClick={() => void rejectCandidate(candidate)}
                      >
                        {t('evolution.reject')}
                      </button>
                    </div>
                  ) : null}
                </article>
              ))}
            </div>
          </div>
        </div>

        <aside className="panel-surface p-5 space-y-5 self-start sticky top-6">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-muted">{t('evolution.details')}</div>
            <h2 className="text-2xl font-bold font-serif mt-1">{t('evolution.auditDetail')}</h2>
          </div>
          {detailLoading ? <div className="text-sm text-muted">{t('common.loading')}</div> : null}
          {detailError ? <div className="text-sm text-danger">{detailError}</div> : null}
          {!selectedCandidate && !selectedAction && !selectedReviewItem && !selectedJob && !selectedQualitySignal ? (
            <EmptyState title={t('evolution.noSelection')} description={t('evolution.noSelectionDesc')} />
          ) : null}

          {selectedQualitySignal ? (
            <section className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-bold">{t('evolution.selectedQualitySignal')}</h3>
                <span className={statusTone(selectedQualitySignal.job_status || selectedQualitySignal.admission_status)}>
                  {selectedQualitySignal.job_status || selectedQualitySignal.admission_status || t('evolution.signalOnly')}
                </span>
              </div>
              <div className="font-mono text-xs break-all">
                {selectedQualitySignal.signal_ref || selectedQualitySignal.job_id || t('common.none')}
              </div>
              <div className="space-y-2">
                <div><strong>{t('evolution.signalType')}</strong> {selectedQualitySignal.signal_type || t('common.none')}</div>
                <div><strong>{t('evolution.subject')}</strong> {selectedQualitySignal.subject_type || t('common.unknown')} / <span className="font-mono break-all">{selectedQualitySignal.subject_id || t('common.none')}</span></div>
                <div><strong>{t('evolution.toolKey')}</strong> <span className="font-mono break-all">{selectedQualitySignal.tool_key || t('common.none')}</span></div>
                <div><strong>{t('evolution.skillId')}</strong> <span className="font-mono break-all">{selectedQualitySignal.skill_id || t('common.none')}</span></div>
                <div><strong>{t('evolution.actionability')}</strong> {selectedQualitySignal.actionability || t('common.none')} / {selectedQualitySignal.evidence_status || t('common.none')}</div>
                <div><strong>{t('evolution.notTriggerableReason')}</strong> {selectedQualitySignal.not_triggerable_reason || t('evolution.triggerable')}</div>
                <div><strong>{t('evolution.job')}</strong> <span className="font-mono break-all">{selectedQualitySignal.job_id || t('common.none')}</span> / {selectedQualitySignal.job_status || t('common.none')}</div>
                <div><strong>{t('evolution.admission')}</strong> {selectedQualitySignal.admission_status || t('common.none')}</div>
                <div><strong>{t('evolution.hardFailures')}</strong> {selectedQualitySignal.admission_hard_failures.length ? selectedQualitySignal.admission_hard_failures.join(', ') : t('common.none')}</div>
                <div><strong>{t('evolution.warnings')}</strong> {selectedQualitySignal.admission_warnings.length ? selectedQualitySignal.admission_warnings.join(', ') : t('common.none')}</div>
                <div><strong>{t('evolution.rawBackrefs')}</strong> {selectedQualitySignal.raw_backref_count}</div>
                <div><strong>{t('evolution.mergeKey')}</strong> <span className="font-mono break-all">{selectedQualitySignal.merge_key || t('common.none')}</span></div>
              </div>
            </section>
          ) : null}

          {selectedJob ? (
            <section className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-bold">{t('evolution.selectedJob')}</h3>
                <span className={statusTone(selectedJob.status)}>{selectedJob.status}</span>
              </div>
              <div className="font-mono text-xs break-all">{selectedJob.job_id}</div>
              <div className="space-y-2">
                <div><strong>{t('evolution.trigger')}</strong> {selectedJob.trigger_type} / {selectedJob.reason}</div>
                <div><strong>{t('evolution.profile')}</strong> {selectedJob.evidence_profile} / {selectedJob.subprofile}</div>
                <div><strong>{t('evolution.watermark')}</strong> {selectedJob.manifest_watermark ?? t('common.none')}</div>
                <div><strong>{t('evolution.error')}</strong> {selectedJob.error || t('common.none')}</div>
              </div>
              <div className="space-y-2">
                <div className="font-bold">{t('evolution.linkedRecords')}</div>
                <div className="flex flex-wrap gap-2">
                  {selectedJob.packet_ids.map((packetId) => (
                    <button
                      key={packetId}
                      type="button"
                      className="tag max-w-full px-2 py-1 text-xs"
                      onClick={() => {
                        void (async () => {
                          setDetailLoading(true);
                          try {
                            setSelectedDecision(await evolutionApi.getPacket(packetId));
                          } finally {
                            setDetailLoading(false);
                          }
                        })();
                      }}
                    >
                      {truncate(packetId, 32)}
                    </button>
                  ))}
                  {selectedJob.decision_ids.map((decisionId) => (
                    <button
                      key={decisionId}
                      type="button"
                      className="tag max-w-full px-2 py-1 text-xs"
                      onClick={() => {
                        void (async () => {
                          setDetailLoading(true);
                          try {
                            setSelectedDecision(await evolutionApi.getDecision(decisionId));
                          } finally {
                            setDetailLoading(false);
                          }
                        })();
                      }}
                    >
                      {truncate(decisionId, 32)}
                    </button>
                  ))}
                  {selectedJob.action_ids.map((actionId) => (
                    <button
                      key={actionId}
                      type="button"
                      className="tag max-w-full px-2 py-1 text-xs"
                      onClick={() => void loadAction(actionId)}
                    >
                      {truncate(actionId, 32)}
                    </button>
                  ))}
                </div>
              </div>
              {selectedDecision ? (
                <div className="space-y-2">
                  <div className="font-bold">{t('evolution.selectedPayload')}</div>
                  <JsonPreview value={selectedDecision} />
                </div>
              ) : null}
              <JsonPreview value={selectedJob} />
            </section>
          ) : null}

          {selectedReviewItem && !selectedCandidate ? (
            <section className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-bold">{t('evolution.selectedReviewItem')}</h3>
                <span className={statusTone(selectedReviewItem.status)}>{selectedReviewItem.status}</span>
              </div>
              <div className="font-mono text-xs break-all">{selectedReviewItem.item_id}</div>
              <div className="space-y-2">
                <div><strong>{t('evolution.reviewType')}</strong> {selectedReviewItem.item_type}</div>
                <div><strong>{t('evolution.reviewSummary')}</strong> {selectedReviewItem.summary || t('common.none')}</div>
                <div><strong>{t('evolution.approvalState')}</strong> {selectedReviewItem.approval_available ? t('evolution.approvalAvailable') : t('evolution.inspectOnly')}</div>
                <div><strong>{t('evolution.reviewNote')}</strong> {selectedReviewItem.review_note || t('common.none')}</div>
                <div><strong>{t('evolution.decision')}</strong> <span className="font-mono break-all">{selectedReviewItem.decision_id || t('common.none')}</span></div>
              </div>
              {selectedDecision ? (
                <div className="space-y-2">
                  <div className="font-bold">{t('evolution.decisionPayload')}</div>
                  <JsonPreview value={selectedDecision} />
                </div>
              ) : null}
            </section>
          ) : null}

          {selectedCandidate ? (
            <section className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-bold">{t('evolution.selectedCandidate')}</h3>
                <span className={statusTone(selectedCandidate.status)}>{selectedCandidate.status}</span>
              </div>
              <div className="font-mono text-xs break-all">{selectedCandidate.candidate_id}</div>
              <div className="space-y-2">
                <div><strong>{t('evolution.decision')}</strong> <span className="font-mono break-all">{selectedCandidate.decision_id}</span></div>
                <div><strong>{t('evolution.action')}</strong> {selectedCandidate.proposed_action}</div>
                <div><strong>{t('evolution.targetsLabel')}</strong> {selectedCandidate.target_skill_ids.length ? selectedCandidate.target_skill_ids.join(', ') : t('common.none')}</div>
                <div><strong>{t('evolution.blockedReason')}</strong> {selectedCandidate.blocked_reason || t('common.none')}</div>
                <div><strong>{t('evolution.neededEvidence')}</strong> {selectedCandidate.needed_evidence?.length ? selectedCandidate.needed_evidence.join(', ') : t('common.none')}</div>
              </div>
              {selectedCandidate.evidence_refs.length > 0 ? (
                <div className="space-y-2">
                  <div className="font-bold">{t('evolution.evidenceRefs')}</div>
                  <div className="flex flex-wrap gap-2">
                    {selectedCandidate.evidence_refs.map((refId) => (
                      <button
                        key={refId}
                        type="button"
                        className="tag max-w-full px-2 py-1 text-xs"
                        onClick={() => void loadRefPreview(refId)}
                      >
                        <span className="block max-w-[300px] truncate">{refId}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}
              {selectedDecision ? (
                <div className="space-y-2">
                  <div className="font-bold">{t('evolution.decisionPayload')}</div>
                  <JsonPreview value={selectedDecision} />
                </div>
              ) : null}
              {selectedCandidate.last_recheck_result ? (
                <div className="space-y-2">
                  <div className="font-bold">{t('evolution.lastRecheckResult')}</div>
                  <JsonPreview value={selectedCandidate.last_recheck_result} />
                </div>
              ) : null}
            </section>
          ) : null}

          {selectedAction ? (
            <section className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-bold">{t('evolution.selectedAction')}</h3>
                <span className={statusTone(selectedAction.commit_status)}>{selectedAction.commit_status}</span>
              </div>
              <div className="font-mono text-xs break-all">{selectedAction.action_id}</div>
              <div className="space-y-2">
                <div><strong>{t('evolution.skillId')}</strong> <span className="font-mono break-all">{selectedAction.skill_id || t('common.none')}</span></div>
                <div><strong>{t('evolution.targetDir')}</strong> <span className="break-all">{selectedAction.active_target_dir}</span></div>
                <div><strong>{t('evolution.stagingDir')}</strong> <span className="break-all">{selectedAction.staging_dir}</span></div>
                <div><strong>{t('evolution.changedFiles')}</strong> {selectedAction.changed_files.length ? selectedAction.changed_files.join(', ') : t('common.none')}</div>
                {selectedAction.failure_reason ? (
                  <div className="text-danger"><strong>{t('evolution.failure')}</strong> {selectedAction.failure_reason}</div>
                ) : null}
              </div>
              <JsonPreview value={selectedAction} />
            </section>
          ) : null}

          {selectedRef ? (
            <section className="space-y-2 text-sm">
              <div className="font-bold">{t('evolution.refPreview')}</div>
              <div className="font-mono text-xs break-all">{selectedRef.ref_id}</div>
              <pre className="field-surface max-h-[260px] overflow-auto p-3 text-xs whitespace-pre-wrap">{selectedRef.content}</pre>
            </section>
          ) : null}
        </aside>
      </section>
    </div>
  );
}
