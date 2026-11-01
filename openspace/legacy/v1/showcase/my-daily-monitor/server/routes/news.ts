/**
 * News API proxy — RSS feed aggregation with images + threat classification.
 * Reference: worldmonitor/server/worldmonitor/news/v1/_feeds.ts
 */
import type { IncomingHttpHeaders } from 'node:http';
import { createHash } from 'node:crypto';

interface FeedConfig {
  name: string;
  url: string;
  category: string;
}

// Google News RSS helper (same trick as worldmonitor)
const gn = (q: string) =>
  `https://news.google.com/rss/search?q=${encodeURIComponent(q)}&hl=en-US&gl=US&ceid=US:en`;

const ALL_FEEDS: FeedConfig[] = [
  // Tech
  { name: 'Hacker News', url: 'https://hnrss.org/frontpage', category: 'tech' },
  { name: 'Ars Technica', url: 'https://feeds.arstechnica.com/arstechnica/technology-lab', category: 'tech' },
  { name: 'The Verge', url: 'https://www.theverge.com/rss/index.xml', category: 'tech' },
  { name: 'TechCrunch', url: gn('site:techcrunch.com when:1d'), category: 'tech' },
  { name: 'MIT Tech Review', url: 'https://www.technologyreview.com/feed/', category: 'tech' },
  // Finance
  { name: 'CNBC', url: 'https://www.cnbc.com/id/100003114/device/rss/rss.html', category: 'finance' },
  { name: 'Yahoo Finance', url: 'https://finance.yahoo.com/news/rssindex', category: 'finance' },
  { name: 'Reuters Business', url: gn('site:reuters.com business markets'), category: 'finance' },
  { name: 'Bloomberg', url: gn('site:bloomberg.com markets when:1d'), category: 'finance' },
  { name: 'Financial Times', url: 'https://www.ft.com/rss/home', category: 'finance' },
  // World
  { name: 'BBC World', url: 'https://feeds.bbci.co.uk/news/world/rss.xml', category: 'world' },
  { name: 'Reuters World', url: gn('site:reuters.com world'), category: 'world' },
  { name: 'AP News', url: gn('site:apnews.com'), category: 'world' },
  { name: 'Al Jazeera', url: 'https://www.aljazeera.com/xml/rss/all.xml', category: 'world' },
  { name: 'Guardian World', url: 'https://www.theguardian.com/world/rss', category: 'world' },
  { name: 'France 24', url: 'https://www.france24.com/en/rss', category: 'world' },
  // AI
  { name: 'AI News', url: gn('(OpenAI OR Anthropic OR "large language model" OR ChatGPT) when:2d'), category: 'ai' },
  { name: 'VentureBeat AI', url: 'https://venturebeat.com/category/ai/feed/', category: 'ai' },
  { name: 'The Verge AI', url: 'https://www.theverge.com/rss/ai-artificial-intelligence/index.xml', category: 'ai' },
  // China
  { name: 'SCMP', url: gn('site:scmp.com when:1d'), category: 'china' },
  { name: 'Caixin', url: gn('site:caixinglobal.com when:1d'), category: 'china' },
  // Science
  { name: 'Nature News', url: 'https://www.nature.com/nature.rss', category: 'science' },
  { name: 'Science Daily', url: 'https://www.sciencedaily.com/rss/all.xml', category: 'science' },
  // US
  { name: 'NPR News', url: 'https://feeds.npr.org/1001/rss.xml', category: 'us' },
  { name: 'PBS NewsHour', url: 'https://www.pbs.org/newshour/feeds/rss/headlines', category: 'us' },
  { name: 'Politico', url: 'https://rss.politico.com/politics-news.xml', category: 'us' },
  // Europe
  { name: 'EuroNews', url: 'https://www.euronews.com/rss?format=xml', category: 'europe' },
  { name: 'DW News', url: 'https://rss.dw.com/xml/rss-en-all', category: 'europe' },
];

const AVAILABLE_CATEGORIES = [...new Set(ALL_FEEDS.map(f => f.category))];

// Keyword threat classifier (same approach as worldmonitor _classifier.ts)
type ThreatLevel = 'critical' | 'high' | 'medium' | 'low' | 'info';

const THREAT_KEYWORDS: Record<ThreatLevel, string[]> = {
  critical: ['war', 'attack', 'explosion', 'crash', 'emergency', 'killed', 'dead', 'catastrophe', 'tsunami', 'earthquake'],
  high: ['sanctions', 'missile', 'nuclear', 'cyberattack', 'hack', 'breach', 'crisis', 'collapse', 'assassination'],
  medium: ['tensions', 'conflict', 'protest', 'strike', 'tariff', 'regulation', 'layoffs', 'recession', 'investigation'],
  low: ['election', 'summit', 'agreement', 'policy', 'earnings', 'merger', 'IPO', 'partnership', 'funding'],
  info: [],
};

function classifyThreat(title: string): ThreatLevel {
  const lower = title.toLowerCase();
  for (const [level, keywords] of Object.entries(THREAT_KEYWORDS) as [ThreatLevel, string[]][]) {
    if (keywords.some(kw => lower.includes(kw))) return level;
  }
  return 'info';
}

// ---- XML parsing with image extraction ----
function extractTag(xml: string, tag: string): string {
  const re = new RegExp(`<${tag}[^>]*><!\\[CDATA\\[(.+?)\\]\\]></${tag}>|<${tag}[^>]*>(.+?)</${tag}>`, 's');
  const m = xml.match(re);
  return (m?.[1] || m?.[2] || '').trim();
}

function extractImage(block: string): string | null {
  // 1. media:content url
  const mediaMatch = block.match(/<media:content[^>]+url="([^"]+)"/);
  if (mediaMatch) return mediaMatch[1];
  // 2. media:thumbnail url
  const thumbMatch = block.match(/<media:thumbnail[^>]+url="([^"]+)"/);
  if (thumbMatch) return thumbMatch[1];
  // 3. enclosure (type image)
  const enclosureMatch = block.match(/<enclosure[^>]+url="([^"]+)"[^>]+type="image/);
  if (enclosureMatch) return enclosureMatch[1];
  // 4. <image><url>...</url></image>
  const imgUrlMatch = block.match(/<image>[^]*?<url>([^<]+)<\/url>/);
  if (imgUrlMatch) return imgUrlMatch[1];
  // 5. img src in description/content
  const imgSrcMatch = block.match(/<img[^>]+src="([^"]+)"/);
  if (imgSrcMatch) return imgSrcMatch[1];
  return null;
}

interface ParsedItem {
  title: string;
  link: string;
  pubDate: string;
  description: string;
  image: string | null;
}

function extractItems(xml: string): ParsedItem[] {
  const items: ParsedItem[] = [];
  // RSS items
  const itemRegex = /<item[^>]*>([\s\S]*?)<\/item>/g;
  let match;
  while ((match = itemRegex.exec(xml)) !== null) {
    const block = match[1];
    items.push({
      title: extractTag(block, 'title'),
      link: extractTag(block, 'link'),
      pubDate: extractTag(block, 'pubDate'),
      description: extractTag(block, 'description').replace(/<[^>]*>/g, '').slice(0, 300),
      image: extractImage(block),
    });
  }
  // Atom entries
  const entryRegex = /<entry[^>]*>([\s\S]*?)<\/entry>/g;
  while ((match = entryRegex.exec(xml)) !== null) {
    const block = match[1];
    const linkMatch = block.match(/<link[^>]*href="([^"]+)"/);
    items.push({
      title: extractTag(block, 'title'),
      link: linkMatch?.[1] || extractTag(block, 'link'),
      pubDate: extractTag(block, 'published') || extractTag(block, 'updated'),
      description: extractTag(block, 'summary').replace(/<[^>]*>/g, '').slice(0, 300),
      image: extractImage(block),
    });
  }
  return items;
}

const newsCache = new Map<string, { data: unknown; ts: number }>();
const NEWS_CACHE_TTL = 5 * 60_000;

export async function handleNewsRequest(
  query: Record<string, string>,
  _body: string,
  _headers: IncomingHttpHeaders,
): Promise<unknown> {
  const categories = (query.category || 'tech,finance,world').split(',').map(s => s.trim());
  const userKeywords = (query.keywords || '').split(',').map(s => s.trim()).filter(Boolean);
  const sources = (query.sources || '').split(',').map(s => s.trim()).filter(Boolean);
  const cacheKey = `${categories.sort().join(',')}:${sources.sort().join(',')}`;

  // Return metadata about available sources if requested
  if (query.action === 'sources') {
    return {
      categories: AVAILABLE_CATEGORIES,
      feeds: ALL_FEEDS.map(f => ({ name: f.name, category: f.category })),
    };
  }

  const cached = newsCache.get(cacheKey);
  if (cached && Date.now() - cached.ts < NEWS_CACHE_TTL) return cached.data;

  // Filter feeds by category and optionally by source name
  let feeds = ALL_FEEDS.filter(f => categories.includes(f.category));
  if (sources.length > 0) {
    feeds = feeds.filter(f => sources.includes(f.name));
  }

  const seenHashes = new Set<string>();
  const articles: unknown[] = [];

  const results = await Promise.allSettled(
    feeds.map(async (feed) => {
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        const resp = await fetch(feed.url, {
          headers: {
            'User-Agent': 'Mozilla/5.0 (compatible; MyDailyMonitor/1.0)',
            Accept: 'application/rss+xml, application/xml, text/xml, */*',
          },
          signal: controller.signal,
        });
        clearTimeout(timeout);
        if (!resp.ok) return [];
        const xml = await resp.text();
        return extractItems(xml).slice(0, 5).map(item => ({
          ...item,
          source: feed.name,
          category: feed.category,
          threatLevel: classifyThreat(item.title),
          matchedKeywords: userKeywords.filter(kw => item.title.toLowerCase().includes(kw.toLowerCase())),
        }));
      } catch { return []; }
    })
  );

  for (const r of results) {
    if (r.status !== 'fulfilled') continue;
    for (const article of r.value) {
      const hash = createHash('sha256').update(article.title).digest('hex').slice(0, 12);
      if (seenHashes.has(hash)) continue;
      seenHashes.add(hash);
      articles.push({
        title: article.title,
        url: article.link,
        source: article.source,
        category: article.category,
        description: article.description,
        image: article.image,
        publishedAt: article.pubDate ? new Date(article.pubDate).toISOString() : new Date().toISOString(),
        threatLevel: article.threatLevel,
        matchedKeywords: article.matchedKeywords,
      });
    }
  }

  articles.sort((a: any, b: any) => new Date(b.publishedAt).getTime() - new Date(a.publishedAt).getTime());

  const response = {
    articles: articles.slice(0, 50),
    count: articles.length,
    categories: AVAILABLE_CATEGORIES,
    activeSources: feeds.map(f => f.name),
  };
  newsCache.set(cacheKey, { data: response, ts: Date.now() });
  return response;
}
