/**
 * Tech Community service — HN, Reddit, V2EX.
 * All free, no API keys needed.
 */
import { createCircuitBreaker } from '@/utils/circuit-breaker';

export interface CommunityPost {
  id: string;
  title: string;
  url: string;
  score: number;
  comments: number;
  author: string;
  platform: 'hn' | 'reddit' | 'v2ex';
  timestamp: string;
}

const communityBreaker = createCircuitBreaker<CommunityPost[]>({
  name: 'Community',
  cacheTtlMs: 3 * 60_000,
});

export type CommunitySource = 'all' | 'hn' | 'reddit' | 'v2ex';

export async function fetchCommunityPosts(source: CommunitySource = 'all'): Promise<CommunityPost[]> {
  return communityBreaker.execute(async () => {
    const resp = await fetch(`/api/social?source=${source}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return (data.posts || []) as CommunityPost[];
  }, []);
}

// Keep old exports for backward compatibility (unused now but safe)
export type SocialPost = CommunityPost;
export interface SocialResult {
  posts: CommunityPost[];
  configured: boolean;
  error?: string;
}
export async function fetchSocialFeed(): Promise<CommunityPost[]> {
  return fetchCommunityPosts('all');
}
export async function fetchSocialResult(): Promise<SocialResult> {
  const posts = await fetchCommunityPosts('all');
  return { posts, configured: true };
}
