/**
 * GitHub API proxy — workflow runs, trending repos, user activity, repo stats.
 */
import type { IncomingHttpHeaders } from 'node:http';

const GITHUB_API = 'https://api.github.com';

// Cache
const cache = new Map<string, { data: unknown; ts: number }>();
const CACHE_TTL = 5 * 60_000;

function cached(key: string): unknown | null {
  const c = cache.get(key);
  return c && Date.now() - c.ts < CACHE_TTL ? c.data : null;
}

function setCache(key: string, data: unknown): void {
  cache.set(key, { data, ts: Date.now() });
}

async function ghFetch(path: string, token: string): Promise<any> {
  const resp = await fetch(`${GITHUB_API}${path}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'MyDailyMonitor/1.0' },
  });
  if (!resp.ok) throw new Error(`GitHub ${resp.status}: ${path}`);
  return resp.json();
}

export async function handleGithubRequest(
  query: Record<string, string>,
  _body: string,
  headers: IncomingHttpHeaders,
): Promise<unknown> {
  const token = (headers['x-github-token'] as string) || process.env.GITHUB_PAT || '';
  const action = query.action || 'runs';

  // ---- Trending repos (GitHub Search API, no auth needed) ----
  if (action === 'trending') {
    const lang = query.language || '';
    const searchQuery = query.q || '';
    const cacheKey = `trending:${lang}:${searchQuery}`;
    const c = cached(cacheKey);
    if (c) return c;

    try {
      const today = new Date();
      const weekAgo = new Date(today.getTime() - 7 * 86400000).toISOString().split('T')[0];
      const langFilter = lang ? `+language:${encodeURIComponent(lang)}` : '';
      // If user provides a search query (keywords like "agent", "claw"), use it
      const keywordFilter = searchQuery ? `+${encodeURIComponent(searchQuery)}+in:name,description,readme` : '';
      const minStars = searchQuery ? '10' : '500'; // lower threshold for keyword search
      const url = `${GITHUB_API}/search/repositories?q=stars:>${minStars}+pushed:>${weekAgo}${langFilter}${keywordFilter}&sort=stars&order=desc&per_page=25`;
      const resp = await fetch(url, {
        headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'MyDailyMonitor/1.0' },
        signal: AbortSignal.timeout(10000),
      });
      if (!resp.ok) return { repos: [], error: `GitHub Search: ${resp.status}` };
      const data = await resp.json() as any;
      const repos = (data.items || []).slice(0, 25).map((r: any) => ({
        fullName: r.full_name,
        description: r.description || '',
        language: r.language || '',
        stars: r.stargazers_count || 0,
        forks: r.forks_count || 0,
        url: r.html_url,
        avatar: r.owner?.avatar_url || '',
        topics: (r.topics || []).slice(0, 4),
        createdAt: r.created_at,
        updatedAt: r.pushed_at,
      }));
      const result = { repos, totalCount: data.total_count || 0 };
      setCache(cacheKey, result);
      return result;
    } catch (err: any) {
      return { repos: [], error: err.message };
    }
  }

  // ---- Star history for specific repos ----
  if (action === 'star-check') {
    const repoNames = (query.repos || '').split(',').map((s: string) => s.trim()).filter(Boolean);
    if (repoNames.length === 0) return { repos: [] };

    const authHeaders: Record<string, string> = { Accept: 'application/vnd.github+json', 'User-Agent': 'MyDailyMonitor/1.0' };
    if (token) authHeaders['Authorization'] = `Bearer ${token}`;

    const results = await Promise.allSettled(
      repoNames.slice(0, 10).map(async (name: string) => {
        const resp = await fetch(`${GITHUB_API}/repos/${name}`, { headers: authHeaders });
        if (!resp.ok) return null;
        const r = await resp.json() as any;
        return {
          fullName: r.full_name,
          description: r.description || '',
          stars: r.stargazers_count || 0,
          forks: r.forks_count || 0,
          openIssues: r.open_issues_count || 0,
          language: r.language || '',
          url: r.html_url,
          avatar: r.owner?.avatar_url || '',
          updatedAt: r.pushed_at,
        };
      })
    );
    return {
      repos: results.filter(r => r.status === 'fulfilled' && r.value).map(r => (r as any).value),
    };
  }

  // All other actions need token
  if (!token) return { error: 'GITHUB_PAT not configured', runs: [], events: [], repos: [] };

  // ---- My repos with stats ----
  if (action === 'my-repos') {
    const cacheKey = 'my-repos';
    const c = cached(cacheKey);
    if (c) return c;

    try {
      const data = await ghFetch('/user/repos?sort=pushed&per_page=20&affiliation=owner', token);
      const repos = (data || []).map((r: any) => ({
        fullName: r.full_name,
        description: r.description || '',
        language: r.language || '',
        stars: r.stargazers_count || 0,
        forks: r.forks_count || 0,
        openIssues: r.open_issues_count || 0,
        isPrivate: r.private,
        updatedAt: r.pushed_at || r.updated_at,
        url: r.html_url,
        defaultBranch: r.default_branch,
      }));
      const result = { repos };
      setCache(cacheKey, result);
      return result;
    } catch (err: any) {
      return { repos: [], error: err.message };
    }
  }

  // ---- Activity feed (events) ----
  if (action === 'activity') {
    const cacheKey = 'activity';
    const c = cached(cacheKey);
    if (c) return c;

    try {
      const data = await ghFetch('/users/' + (query.username || '') + '/received_events?per_page=30', token);
      // If no username, get authenticated user events
      const events = (Array.isArray(data) ? data : []).slice(0, 20).map((e: any) => ({
        id: e.id,
        type: e.type,
        repo: e.repo?.name || '',
        actor: e.actor?.login || '',
        actorAvatar: e.actor?.avatar_url || '',
        action: describeEvent(e),
        createdAt: e.created_at,
      }));
      const result = { events };
      setCache(cacheKey, result);
      return result;
    } catch (err: any) {
      return { events: [], error: err.message };
    }
  }

  // ---- Workflow runs (existing) ----
  const repos = (query.repos || '').split(',').map(s => s.trim()).filter(Boolean);
  if (repos.length === 0) return { runs: [], message: 'No repos configured' };

  const allRuns: unknown[] = [];
  const results = await Promise.allSettled(
    repos.map(async (repo) => {
      const data = await ghFetch(`/repos/${repo}/actions/runs?per_page=5`, token);
      return (data.workflow_runs || []).map((run: any) => ({
        id: run.id, repo, name: run.name, status: run.status, conclusion: run.conclusion,
        branch: run.head_branch, commit: run.head_sha?.slice(0, 7), url: run.html_url,
        createdAt: run.created_at, updatedAt: run.updated_at,
        durationSeconds: run.updated_at && run.created_at ? Math.round((new Date(run.updated_at).getTime() - new Date(run.created_at).getTime()) / 1000) : null,
      }));
    })
  );
  for (const r of results) { if (r.status === 'fulfilled') allRuns.push(...r.value); }
  allRuns.sort((a: any, b: any) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
  return { runs: allRuns.slice(0, 20) };
}

function describeEvent(e: any): string {
  switch (e.type) {
    case 'PushEvent': return `pushed ${e.payload?.commits?.length || 0} commit(s)`;
    case 'PullRequestEvent': return `${e.payload?.action} PR #${e.payload?.pull_request?.number}`;
    case 'IssuesEvent': return `${e.payload?.action} issue #${e.payload?.issue?.number}`;
    case 'WatchEvent': return 'starred';
    case 'ForkEvent': return 'forked';
    case 'CreateEvent': return `created ${e.payload?.ref_type} ${e.payload?.ref || ''}`;
    case 'DeleteEvent': return `deleted ${e.payload?.ref_type} ${e.payload?.ref || ''}`;
    case 'IssueCommentEvent': return `commented on #${e.payload?.issue?.number}`;
    case 'ReleaseEvent': return `released ${e.payload?.release?.tag_name || ''}`;
    default: return e.type.replace('Event', '').toLowerCase();
  }
}
