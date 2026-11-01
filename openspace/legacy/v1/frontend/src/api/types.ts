export interface OverviewResponse {
  health: {
    status: string;
    db_path: string;
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
  lineage_graph?: SkillLineage;
  critical_tools?: string[];
  tool_dependencies?: string[];
}

export interface SkillDetail extends Skill {
  recent_analyses: ExecutionAnalysis[];
  source: SkillSource;
  lineage_graph: SkillLineage;
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

export interface WorkflowSummary {
  id: string;
  path: string;
  task_id: string;
  task_name: string;
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
  artifacts: {
    init_screenshot_url: string | null;
    screenshots: WorkflowArtifact[];
    video_url: string | null;
  };
}
