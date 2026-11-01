---
name: news-api-integration
description: Integrate with news APIs (GNews, NewsAPI, etc.) for aggregated news feeds. Covers headline fetching, search, and category filtering.
---

# News API Integration

Multiple news APIs available. GNews is recommended for free tier usability.

## Recommended: GNews

- **Docs**: https://gnews.io/docs/v4
- **Base URL**: `https://gnews.io/api/v4`
- **Auth**: Query param `token=API_KEY`
- **Free tier**: 100 requests/day, 10 articles per request
- **CORS**: Yes

### Top Headlines

```
GET /top-headlines?category=general&lang=en&max=10&token=API_KEY
```

Categories: `general`, `world`, `nation`, `business`, `technology`, `entertainment`, `sports`, `science`, `health`

### Search

```
GET /search?q=artificial+intelligence&lang=en&max=10&token=API_KEY
```

### Response Shape

```json
{
  "totalArticles": 1234,
  "articles": [
    {
      "title": "Article Title",
      "description": "Short description...",
      "content": "Full article content (truncated)...",
      "url": "https://example.com/article",
      "image": "https://example.com/image.jpg",
      "publishedAt": "2024-01-15T12:00:00Z",
      "source": {
        "name": "CNN",
        "url": "https://cnn.com"
      }
    }
  ]
}
```

## Alternative: NewsAPI.org

- **Docs**: https://newsapi.org/docs
- **Free tier**: 100 requests/day, for development only
- **Note**: Free plan does NOT support production use (requires paid plan for CORS from browser)

```
GET /v2/top-headlines?country=us&apiKey=API_KEY
GET /v2/everything?q=bitcoin&apiKey=API_KEY
```

## Other Alternatives (from public-apis)

| API | Free Tier | Best For |
|-----|-----------|----------|
| **GNews** | 100 req/day | General news, good free tier |
| **Currents API** | 600 req/day | Real-time news, good rate |
| **NewsData.io** | 200 req/day | News + crypto news |
| **The Guardian** | Unlimited (rate limited) | UK/international news |
| **New York Times** | 500 req/day | US news, archives |
| **Mediastack** | 500 req/month | Multi-source aggregation |
| **TheNews API** | 100 req/day | Simple aggregation |
| **MarketAux** | 100 req/day | Financial news with sentiment |
| **HackerNews** | No limit | Tech/startup news (no key needed) |

## Frontend Service Implementation

```typescript
// src/services/news.ts
import { createCircuitBreaker } from '@/utils/circuit-breaker';

export interface NewsArticle {
  title: string;
  description: string;
  url: string;
  source: string;
  publishedAt: string;
  image?: string;
}

const newsBreaker = createCircuitBreaker<NewsArticle[]>({
  name: 'News',
  cacheTtlMs: 5 * 60_000, // 5 minutes
});

export async function fetchNews(category = 'general'): Promise<NewsArticle[]> {
  return newsBreaker.execute(async () => {
    const resp = await fetch(`/api/news?category=${category}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return data.articles || [];
  }, []);
}

export async function searchNews(query: string): Promise<NewsArticle[]> {
  const resp = await fetch(`/api/news?q=${encodeURIComponent(query)}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  return data.articles || [];
}
```

## HackerNews (No API Key Needed)

For tech news, HackerNews API is free and unlimited:

```typescript
// Fetch top 30 stories
const topIds = await fetch('https://hacker-news.firebaseio.com/v0/topstories.json').then(r => r.json());
const stories = await Promise.all(
  topIds.slice(0, 30).map((id: number) =>
    fetch(`https://hacker-news.firebaseio.com/v0/item/${id}.json`).then(r => r.json())
  )
);
// Each story: { title, url, score, by, time, descendants }
```

## Panel Rendering Pattern

```typescript
private render(articles: NewsArticle[]): void {
  const rows = articles.map(a => `
    <div class="news-item">
      <a href="${a.url}" target="_blank" rel="noopener" class="news-title">${escapeHtml(a.title)}</a>
      <div class="news-meta">
        <span class="news-source">${escapeHtml(a.source)}</span>
        <span class="news-time">${formatTime(new Date(a.publishedAt))}</span>
      </div>
    </div>
  `).join('');
  this.setContent(`<div class="news-list">${rows}</div>`);
}
```

Always escape user-controlled strings with `escapeHtml()` to prevent XSS.

