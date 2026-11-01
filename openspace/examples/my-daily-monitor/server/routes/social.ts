/**
 * Tech Community API — aggregates free APIs from HN, Reddit, V2EX.
 * All endpoints are free and require NO API keys.
 */
import type { IncomingHttpHeaders } from 'node:http';

const FETCH_TIMEOUT = 8000;

interface CommunityPost {
  id: string;
  title: string;
  url: string;
  score: number;
  comments: number;
  author: string;
  platform: string;
  timestamp: string;
}

// ---- Cache ----
const cache = new Map<string, { data: unknown; ts: number }>();
const CACHE_TTL = 3 * 60_000; // 3 min

function cached<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const entry = cache.get(key);
  if (entry && Date.now() - entry.ts < CACHE_TTL) return Promise.resolve(entry.data as T);
  return fn().then(data => { cache.set(key, { data, ts: Date.now() }); return data; });
}

export async function handleSocialRequest(
  query: Record<string, string>,
  _body: string,
  _headers: IncomingHttpHeaders,
): Promise<unknown> {
  const source = query.source || 'all';

  try {
    if (source === 'hn') return { posts: await fetchHN(), source: 'hn' };
    if (source === 'reddit') return { posts: await fetchReddit(query.sub || 'programming'), source: 'reddit' };
    // V2EX disabled — content quality issues
    // if (source === 'v2ex') return { posts: await fetchV2EX(), source: 'v2ex' };

    // Fetch HN + Reddit in parallel
    const [hn, reddit] = await Promise.allSettled([
      fetchHN(),
      fetchReddit(query.sub || 'programming'),
    ]);

    const posts: CommunityPost[] = [];
    if (hn.status === 'fulfilled') posts.push(...hn.value);
    if (reddit.status === 'fulfilled') posts.push(...reddit.value);

    // Sort by score descending
    posts.sort((a, b) => b.score - a.score);

    return { posts: posts.slice(0, 40), source: 'all' };
  } catch (err: any) {
    return { posts: [], error: err.message };
  }
}

// ============================================================
//  Hacker News — https://github.com/HackerNews/API
// ============================================================
async function fetchHN(): Promise<CommunityPost[]> {
  return cached('hn', async () => {
    const resp = await fetch('https://hacker-news.firebaseio.com/v0/topstories.json', {
      signal: AbortSignal.timeout(FETCH_TIMEOUT),
    });
    if (!resp.ok) throw new Error(`HN ${resp.status}`);
    const ids: number[] = await resp.json() as number[];

    // Fetch top 15 items in parallel
    const items = await Promise.allSettled(
      ids.slice(0, 15).map(async (id) => {
        const r = await fetch(`https://hacker-news.firebaseio.com/v0/item/${id}.json`, {
          signal: AbortSignal.timeout(FETCH_TIMEOUT),
        });
        return r.json() as Promise<any>;
      })
    );

    return items
      .filter((r): r is PromiseFulfilledResult<any> => r.status === 'fulfilled' && r.value)
      .map(r => r.value)
      .filter(item => item.type === 'story' && item.title)
      .map(item => ({
        id: `hn-${item.id}`,
        title: item.title,
        url: item.url || `https://news.ycombinator.com/item?id=${item.id}`,
        score: item.score || 0,
        comments: item.descendants || 0,
        author: item.by || '',
        platform: 'hn',
        timestamp: new Date((item.time || 0) * 1000).toISOString(),
      }));
  });
}

// ============================================================
//  Reddit — public JSON endpoint (no auth)
// ============================================================
async function fetchReddit(subreddit: string): Promise<CommunityPost[]> {
  return cached(`reddit-${subreddit}`, async () => {
    const resp = await fetch(`https://www.reddit.com/r/${subreddit}/hot.json?limit=15&raw_json=1`, {
      headers: { 'User-Agent': 'MyDailyMonitor/1.0' },
      signal: AbortSignal.timeout(FETCH_TIMEOUT),
    });
    if (!resp.ok) throw new Error(`Reddit ${resp.status}`);
    const data = await resp.json() as any;

    return (data.data?.children || [])
      .filter((c: any) => c.data && !c.data.stickied)
      .slice(0, 15)
      .map((c: any) => {
        const d = c.data;
        return {
          id: `reddit-${d.id}`,
          title: d.title,
          url: d.url?.startsWith('http') ? d.url : `https://reddit.com${d.permalink}`,
          score: d.score || 0,
          comments: d.num_comments || 0,
          author: d.author || '',
          platform: 'reddit',
          timestamp: new Date((d.created_utc || 0) * 1000).toISOString(),
        };
      });
  });
}

// ============================================================
//  V2EX — free hot topics API
// ============================================================
async function fetchV2EX(): Promise<CommunityPost[]> {
  return cached('v2ex', async () => {
    // V2EX v1 API is free, no auth needed. v2 requires token.
    const resp = await fetch('https://www.v2ex.com/api/topics/hot.json', {
      headers: { 'User-Agent': 'MyDailyMonitor/1.0' },
      signal: AbortSignal.timeout(FETCH_TIMEOUT),
    });
    if (!resp.ok) throw new Error(`V2EX ${resp.status}`);
    const data = await resp.json() as any;

    // v1 API returns array directly (not wrapped in { result })
    const topics = Array.isArray(data) ? data : (data.result || data);
    return topics.slice(0, 15).map((t: any) => ({
      id: `v2ex-${t.id}`,
      title: t.title,
      url: `https://www.v2ex.com/t/${t.id}`,
      score: t.votes || 0,
      comments: t.replies || 0,
      author: t.member?.username || '',
      platform: 'v2ex',
      timestamp: t.last_modified ? new Date(t.last_modified * 1000).toISOString()
        : t.created ? new Date(t.created * 1000).toISOString()
        : new Date().toISOString(),
    }));
  });
}
