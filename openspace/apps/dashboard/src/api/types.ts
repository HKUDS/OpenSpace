export interface OverviewResponse {
  health: {
    status: string;
    db_path: string;
    evidence_db_path: string;
    workflow_count: number;
    frontend_dist_exists: boolean;
  };
  pipeline: PipelineStage[];
  skills: {
    summary: SkillStats;
    average_score: number;
    top: Skill[];
    recent: Skill[];
  };
  workflows: {
    total: number;
    average_success_rate: number;
    recent: WorkflowSummary[];
  };
}

export interface PipelineStage {
  id: string;
  title: string;
  description: string;
}

export interface ExecutionAnalysis {
  task_id: string;
  timestamp: string;
  task_completed: boolean;
  execution_note: string;
  tool_issues: string[];
  evolution_suggestions: Array<Record<string, unknown>>;
  analyzed_by: string;
  analyzed_at: string;
}

export interface SkillLineageMeta {
  origin: string;
  generation: number;
  parent_skill_ids: string[];
  change_summary: string;
  content_diff?: string;
  content_snapshot?: Record<string, string>;
  evolution_action_id?: string | null;
  provenance_refs?: string[];
  source_task_id?: string | null;
  created_at: string;
  created_by: string;
}

export interface SkillLineageNode {
  skill_id: string;
  name: string;
  description: string;
  origin: string;
  generation: number;
  created_at: string;
  visibility: string;
  is_active: boolean;
  tags: string[];
  score: number;
  effective_rate: number;
  total_selections: number;
}

export interface SkillLineageEdge {
  source: string;
  target: string;
}

export interface SkillLineage {
  skill_id: string;
  nodes: SkillLineageNode[];
  edges: SkillLineageEdge[];
  total_nodes: number;
}

export interface SkillSource {
  exists: boolean;
  path: string;
  content: string | null;
}

export interface Skill {
  skill_id: string;
  name: string;
  description: string;
  path: string;
  skill_dir: string;
  is_active: boolean;
  category: string;
  tags: string[];
  visibility: string;
  creator_id: string;
  lineage: SkillLineageMeta;
  origin: string;
  generation: number;
  parent_skill_ids: string[];
  total_selections: number;
  total_applied: number;
  total_completions: number;
  total_fallbacks: number;
  applied_rate: number;
  completion_rate: number;
  effective_rate: number;
  fallback_rate: number;
  score: number;
  first_seen: string;
  last_updated: string;
  recent_analyses?: ExecutionAnalysis[];
  source?: SkillSource;
  critical_tools?: string[];
  tool_dependencies?: string[];
  latest_evolution_action_id?: string | null;
  evolution_provenance_refs?: string[];
}

export interface SkillDetail extends Skill {
  recent_analyses: ExecutionAnalysis[];
  source: SkillSource;
}

export interface SkillStats {
  total_skills: number;
  total_skills_all: number;
  by_category: Record<string, number>;
  by_origin: Record<string, number>;
  total_analyses: number;
  evolution_candidates: number;
  total_selections: number;
  total_applied: number;
  total_completions: number;
  total_fallbacks: number;
  average_score: number;
  skills_with_activity: number;
  skills_with_recent_analysis: number;
  top_by_effective_rate: Skill[];
}

export interface EvolutionJob {
  job_id: string;
  trigger_type: string;
  status: string;
  reason: string;
  reason_tags: string[];
  scope: Record<string, unknown>;
  idempotency_key: string;
  evidence_profile: string;
  subprofile: string;
  manifest_watermark?: number;
  attempts?: number;
  locked_at?: string | null;
  completed_at?: string | null;
  result_ref?: string | null;
  error?: string | null;
  created_at: string;
  updated_at?: string | null;
  packet_ids: string[];
  decision_ids: string[];
  admission_ids: string[];
  candidate_ids: string[];
  validation_ids: string[];
  action_ids: string[];
}

export interface EvolutionCandidate {
  candidate_id: string;
  proposed_action: string;
  status: string;
  admission_id: string;
  source_task_ids: string[];
  target_skill_ids: string[];
  decision_id: string;
  decision_snapshot: Record<string, unknown>;
  evidence_refs: string[];
  similar_skill_ids: string[];
  recurrence: string;
  recurrence_count: number;
  merge_key: string;
  created_at: string;
  updated_at: string;
  promoted_action_id?: string | null;
  rejection_reason?: string | null;
  last_recheck_result?: Record<string, unknown> | null;
  blocked_reason?: string | null;
  needed_evidence?: string[];
}

export interface EvolutionReviewItem {
  item_id: string;
  item_type: 'candidate' | 'admission' | 'validation';
  status: string;
  title: string;
  summary: string;
  created_at: string;
  updated_at: string;
  candidate_id?: string;
  decision_id?: string;
  admission_id?: string;
  packet_id?: string;
  validation_id?: string;
  action_kind: 'inspect';
  approval_available: boolean;
  blocking_stage?: string;
  review_note?: string;
}

export interface QualitySignalAuditRow {
  signal_ref: string;
  signal_type: string;
  subject_type: string;
  subject_id: string;
  tool_key: string;
  skill_id: string;
  actionability: string;
  evidence_status: string;
  merge_key: string;
  raw_backref_count: number;
  job_id: string;
  job_status: string;
  admission_status: string;
  admission_hard_failures: string[];
  admission_warnings: string[];
  not_triggerable_reason: string;
}

export interface EvolutionAction {
  action_id: string;
  decision_id: string;
  trigger_job_id: string;
  authoring_id: string;
  validation_id: string;
  action_type: string;
  commit_status: string;
  skill_id?: string | null;
  parent_skill_ids: string[];
  changed_files: string[];
  evidence_refs: string[];
  staging_dir: string;
  active_target_dir: string;
  backup_dir?: string | null;
  failure_reason?: string | null;
  created_at: string;
  committed_at?: string | null;
  validation?: Record<string, unknown> | null;
  decision?: Record<string, unknown> | null;
  failures?: Array<Record<string, unknown>>;
}

export interface EvidenceRef {
  ref_id: string;
  ref_type: string;
  uri: string;
  session_id?: string | null;
  task_id?: string | null;
  producer: string;
  created_at: string;
  reliability: string;
  role: string;
  preview: string;
  metadata: Record<string, unknown>;
  contains_secret?: boolean;
}

export interface EvidenceRefPreview {
  ref_id: string;
  content: string;
  truncated: boolean;
  max_chars: number;
}

export interface WorkflowSummary {
  id: string;
  path: string;
  log_root?: string | null;
  log_root_label?: string | null;
  log_folder?: string | null;
  log_folder_label?: string | null;
  log_relative_path?: string | null;
  task_id: string;
  task_name: string;
  recording_task_id?: string | null;
  benchmark_task_id?: string | null;
  benchmark_task_run_id?: string | null;
  benchmark_run_name?: string | null;
  instruction_source?: string | null;
  instruction: string;
  status: string;
  iterations: number;
  execution_time: number;
  start_time: string | null;
  end_time: string | null;
  total_steps: number;
  success_count: number;
  success_rate: number;
  backend_counts: Record<string, number>;
  tool_counts: Record<string, number>;
  agent_action_count: number;
  has_video: boolean;
  video_url: string | null;
  screenshot_count: number;
  selected_skills: string[];
}

export interface WorkflowArtifact {
  name: string;
  path: string;
  url: string;
}

export interface WorkflowTimelineEvent {
  timestamp: string;
  type: 'agent_action' | 'tool_execution';
  step?: number;
  label: string;
  agent_name?: string;
  agent_type?: string;
  backend?: string;
  status?: string;
  details: Record<string, unknown>;
}

export interface WorkflowTraceDatum {
  label: string;
  kind: string;
  preview: string;
  value: unknown;
}

export interface WorkflowTraceEvent {
  event_id: string;
  sequence: number;
  timestamp: string;
  iteration?: number | null;
  harness: string;
  source: string;
  title: string;
  summary: string;
  based_on: string[];
  decision: string;
  impact: string;
  status?: string | null;
  agent_name?: string | null;
  tool_name?: string | null;
  backend?: string | null;
  inputs: WorkflowTraceDatum[];
  outputs: WorkflowTraceDatum[];
  metadata: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface WorkflowTraceSummary {
  total_events: number;
  harness_counts: Record<string, number>;
  agents: string[];
  tools: string[];
  iterations: number[];
  has_conversation_log: boolean;
  has_agent_actions: boolean;
  has_tool_trajectory: boolean;
  source_files: Record<string, string>;
  workflow_id: string;
}

export interface WorkflowTrace {
  summary: WorkflowTraceSummary;
  events: WorkflowTraceEvent[];
}

export interface WorkflowDetail extends WorkflowSummary {
  metadata: Record<string, unknown>;
  statistics: {
    total_steps: number;
    success_count: number;
    success_rate: number;
    backends: Record<string, number>;
    tools: Record<string, number>;
  };
  trajectory: Array<Record<string, unknown>>;
  plans: Array<Record<string, unknown>>;
  decisions: string[];
  agent_actions: Array<Record<string, unknown>>;
  agent_statistics: {
    total_actions: number;
    by_agent: Record<string, number>;
    by_type: Record<string, number>;
  };
  timeline: WorkflowTimelineEvent[];
  trace?: WorkflowTrace;
  artifacts: {
    init_screenshot_url: string | null;
    screenshots: WorkflowArtifact[];
    video_url: string | null;
  };
}
