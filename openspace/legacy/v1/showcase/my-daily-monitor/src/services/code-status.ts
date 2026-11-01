import { getSecret, getPreferences } from '@/services/settings-store';

export interface WorkflowRun {
  id: number;
  repo: string;
  name: string;
  status: string;
  conclusion: string | null;
  branch: string;
  commit: string;
  url: string;
  createdAt: string;
  updatedAt: string;
  durationSeconds: number | null;
}

export interface CodeStatusResult {
  runs: WorkflowRun[];
  configured: boolean;
  error?: string;
}

/**
 * Fetch GitHub Actions workflow runs via API proxy.
 * NEVER returns fake data.
 */
export async function fetchWorkflowRuns(): Promise<WorkflowRun[]> {
  const result = await fetchCodeStatusResult();
  return result.runs;
}

export async function fetchCodeStatusResult(): Promise<CodeStatusResult> {
  const token = getSecret('GITHUB_PAT');
  const repos = getPreferences().githubRepos;
  if (!token) {
    return { runs: [], configured: false, error: 'GitHub not configured. Go to Settings → API Keys → add GitHub Personal Access Token.' };
  }
  if (repos.length === 0) {
    return { runs: [], configured: true, error: 'No repos configured. Go to Settings → Preferences → GitHub Repos.' };
  }

  try {
    const resp = await fetch(`/api/github?repos=${repos.join(',')}`, {
      headers: { 'X-Github-Token': token },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) return { runs: [], configured: true, error: data.error };
    return { runs: (data.runs || []) as WorkflowRun[], configured: true };
  } catch (err: any) {
    return { runs: [], configured: true, error: err.message };
  }
}
