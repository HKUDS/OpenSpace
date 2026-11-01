import { createCircuitBreaker } from '@/utils/circuit-breaker';
import { getPreferences } from '@/services/settings-store';

export type ThreatLevel = 'critical' | 'high' | 'medium' | 'low' | 'info';

export interface NewsArticle {
  title: string;
  description: string;
  url: string;
  source: string;
  publishedAt: string;
  image?: string;
  category?: string;
  threatLevel?: ThreatLevel;
  matchedKeywords?: string[];
}

const newsBreaker = createCircuitBreaker<NewsArticle[]>({
  name: 'News',
  cacheTtlMs: 5 * 60_000,
});

/**
 * Fetch news via API proxy → RSS feed aggregation.
 * NO API KEY NEEDED — RSS feeds are free. This always works when the dev server is running.
 * NEVER returns fake data.
 */
export async function fetchNews(): Promise<NewsArticle[]> {
  const prefs = getPreferences();
  const keywords = prefs.newsKeywords.join(',');

  // Always fetch all categories so the cache stays universal.
  // Client-side filtering by active categories is done in NewsPanel.
  const allCategories = 'tech,finance,world,ai,china,science,us,europe';

  return newsBreaker.execute(async () => {
    const resp = await fetch(`/api/news?category=${allCategories}&keywords=${encodeURIComponent(keywords)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return (data.articles || []) as NewsArticle[];
  }, []);
}

export async function searchNews(query: string): Promise<NewsArticle[]> {
  const resp = await fetch(`/api/news?category=world&keywords=${encodeURIComponent(query)}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  return data.articles || [];
}
