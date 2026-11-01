---
name: personal-monitor-domain
description: Domain specification for a personal daily monitoring dashboard. Defines all target panels (email, Feishu, Office, social, news, Twitter, code status, schedule, stock market, daily finance), their data sources, APIs, and priority ordering for incremental development.
---

# Personal Monitor Domain Specification

This skill defines WHAT the my-daily-monitor dashboard should do — the panels, data sources, APIs, and features that make up a personal daily monitoring system. Use this as a reference when creating new panels or deciding what to build next.

## When to Use

- You are planning which panel to build next
- You need API details for a specific data source
- You are composing multiple panels into an integrated dashboard
- You need to understand the priority ordering for incremental development

## Target Panels Overview

| # | Panel | Data Source | Priority | Status |
|---|-------|-------------|----------|--------|
| 1 | Schedule / Calendar | Google Calendar API | P0 — Core | Implemented |
| 2 | Email Inbox | Gmail API (OAuth) | P0 — Core | Implemented |
| 3 | Feishu Messages | Feishu/Lark Open API | P0 — Core | Implemented |
| 4 | Stock Market | Finnhub API | P0 — Core | Implemented |
| 5 | News Feed | GNews / HackerNews | P0 — Core | Implemented |
| 6 | Code Status (CI/CD) | GitHub Actions API | P1 — Important | Implemented |
| 7 | Office Documents | Microsoft Graph API | P1 — Important | Implemented |
| 8 | Social Feed (Twitter) | Twitter API v2 | P1 — Important | Implemented |
| 9 | Daily Finance | Manual / Spreadsheet | P1 — Important | Implemented |
| 10 | Weather | OpenWeatherMap / wttr.in | P1 — Important | Implemented |
| 11 | World Clock | Built-in (no API) | P2 — Nice to Have | Implemented |
| 12 | Map | MapLibre GL | P2 — Nice to Have | Implemented |
| 13 | AI Insights | LLM API (Groq/Ollama) | P2 — Nice to Have | Implemented |
| 14 | Xiaohongshu | Web scraping / API | P2 — Nice to Have | Partial |
| 15 | DevOps | Server probes | P2 — Nice to Have | Implemented |
| 16 | Quick Links | Static config | P3 — Extra | Implemented |
| 17 | Live News | YouTube embed | P3 — Extra | Implemented |
| 18 | My Monitors | Custom keywords | P3 — Extra | Implemented |

## Panel Details

### 1. Schedule / Calendar

**Purpose**: Show today's meetings, upcoming events, and agenda

**API**: Google Calendar API v3
- Auth: OAuth 2.0 (reuses Gmail credentials)
- Endpoint: `GET /api/calendar?calendarId=primary`
- Headers: `X-Gmail-Client-Id`, `X-Gmail-Client-Secret`, `X-Gmail-Token`

**Data shape**:
```typescript
interface CalendarEvent {
  id: string;
  title: string;
  startTime: string;      // ISO 8601
  endTime: string;
  location?: string;
  isAllDay?: boolean;
  color?: string;
  meetingLink?: string;    // Zoom/Meet URL
}
```

**Refresh interval**: 5 minutes

### 2. Email Inbox

**Purpose**: Show unread emails, sender, subject, snippet

**API**: Gmail API v1
- Auth: OAuth 2.0 (Client ID + Client Secret + Refresh Token)
- Endpoint: `GET /api/emails?maxResults=15&q=is:unread`
- Headers: `X-Gmail-Client-Id`, `X-Gmail-Client-Secret`, `X-Gmail-Token`
- Server-side: Exchange refresh token for access token, call Gmail API

**Data shape**:
```typescript
interface EmailMessage {
  id: string;
  from: string;
  subject: string;
  snippet: string;
  receivedAt: string;
  unread: boolean;
  labels?: string[];
}
```

**Refresh interval**: 2 minutes

### 3. Feishu Messages

**Purpose**: Show recent Feishu/Lark chat messages across all conversations

**API**: Feishu Open Platform API
- Auth: App ID + App Secret → tenant access token
- Endpoint: `GET /api/feishu`
- Headers: `X-Feishu-App-Id`, `X-Feishu-App-Secret`
- Server-side: Get tenant_access_token, fetch chat list, fetch recent messages

**Data shape**:
```typescript
interface FeishuMessage {
  id: string;
  chatName: string;
  chatType: string;        // 'group' | 'p2p'
  avatar: string;
  senderName: string;
  content: string;
  imageUrl: string;
  timestamp: string;
  unread: boolean;
  chatId: string;
  recentMessages: FeishuRecentMsg[];
}
```

**Refresh interval**: 1 minute

### 4. Stock Market

**Purpose**: Track personal stock watchlist with prices, changes, sparklines

**API**: Finnhub Stock API
- Auth: Query param `token=API_KEY`
- Endpoint: `GET /api/stocks?symbols=AAPL,MSFT,...`
- Free tier: 60 requests/minute
- Server-side: Batch fetch quotes, return unified format

**Data shape**:
```typescript
interface StockQuote {
  symbol: string;
  name: string;
  price: number | null;
  change: number | null;
  changePercent: number | null;
  high: number | null;
  low: number | null;
  sparkline?: number[];
}
```

**Default watchlist**: AAPL, MSFT, GOOGL, AMZN, TSLA, NVDA, META

**Refresh interval**: 1 minute

### 5. News Feed

**Purpose**: Aggregated headlines from multiple sources with threat classification

**APIs**:
- GNews (primary): `https://gnews.io/api/v4` — 100 req/day free
- HackerNews (tech): `https://hacker-news.firebaseio.com/v0/` — unlimited, no key
- RSS feeds (fallback): Direct XML parsing

**Data shape**:
```typescript
interface NewsArticle {
  title: string;
  description: string;
  url: string;
  source: string;
  publishedAt: string;
  image?: string;
  threatLevel?: 'critical' | 'high' | 'medium' | 'low';
}
```

**Refresh interval**: 5 minutes

### 6. Code Status (CI/CD)

**Purpose**: Show GitHub Actions workflow runs — passed, failed, in-progress

**API**: GitHub REST API v3
- Auth: Personal Access Token (PAT) in `GITHUB_TOKEN`
- Endpoint: `GET /api/github?action=runs&owner=...&repo=...`
- Server-side: Fetch `/repos/{owner}/{repo}/actions/runs`

**Data shape**:
```typescript
interface WorkflowRun {
  id: number;
  name: string;
  status: string;
  conclusion: string | null;   // 'success' | 'failure' | 'cancelled'
  branch: string;
  createdAt: string;
  url: string;
}
```

**Refresh interval**: 2 minutes

### 7. Office Documents

**Purpose**: Recent Office document activity (PPT, DOC, XLSX, PDF)

**API**: Microsoft Graph API
- Auth: OAuth 2.0 (Azure AD app registration)
- Endpoint: `GET /api/office`
- Server-side: Call Graph API `/me/drive/recent`

**Data shape**:
```typescript
interface OfficeDocument {
  id: string;
  name: string;
  type: 'doc' | 'xlsx' | 'ppt' | 'pdf';
  modifiedAt: string;
  modifiedBy: string;
  url?: string;
}
```

**Refresh interval**: 5 minutes

**Note**: Currently uses demo data. Full implementation requires Azure AD app registration with `Files.Read` permission.

### 8. Social Feed

**Purpose**: Twitter timeline + Xiaohongshu content

**Twitter API v2**:
- Auth: Bearer Token
- Endpoint: `GET /api/social?action=search&q=...` or `action=list&listId=...`
- Server-side: Forward to Twitter search/list endpoint

**Xiaohongshu**:
- No official API; requires web scraping or reverse-engineered endpoints
- Potential approach: headless browser scraping via server-side proxy

**Data shape**:
```typescript
interface SocialPost {
  id: string;
  platform: 'twitter' | 'xiaohongshu';
  author: string;
  username?: string;
  content: string;
  timestamp: string;
  likes?: number;
  retweets?: number;
  comments?: number;
  url?: string;
}
```

**Refresh interval**: 3 minutes

### 9. Daily Finance

**Purpose**: Track daily income and expenses, show balance

**Data source options**:
- Google Sheets API (personal budget spreadsheet)
- YNAB API (budgeting app)
- Local JSON file / localStorage
- Custom bookkeeping endpoint

**Data shape**:
```typescript
interface DailySummary {
  totalIncome: number;
  totalExpense: number;
  balance: number;
  transactions: FinanceTransaction[];
}

interface FinanceTransaction {
  id: string;
  description: string;
  amount: number;
  type: 'income' | 'expense';
  category: string;
  date: string;
}
```

**Refresh interval**: 10 minutes

## Composition: Today Focus Sidebar

The sidebar aggregates highlights from multiple panels into a single glanceable view:

```typescript
interface TodayFocusData {
  nextEvent?: { title: string; startTime: string; minutesUntil: number };
  unreadEmails: number;
  unreadFeishu: number;
  stockAlerts: Array<{ symbol: string; changePercent: number }>;
  ciFailures: number;
  dailyBriefing?: string;   // AI-generated summary
}
```

**Dependencies**: schedule, email, feishu, stock-market, code-status, ai-summary

## Composition: AI Daily Briefing

Uses an LLM to synthesize a morning briefing from all data sources:

**Input**: Unread emails, today's events, top news headlines, stock movers
**Output**: 2-3 paragraph natural language summary
**API**: Groq (free, fast) or local Ollama

## API Key Configuration

All API keys are stored in browser localStorage via a Settings modal:

| Key | Required For | How to Get |
|-----|-------------|-----------|
| `FINNHUB_API_KEY` | Stock Market | https://finnhub.io/ (free) |
| `GNEWS_API_KEY` | News | https://gnews.io/ (free, 100/day) |
| `GMAIL_CLIENT_ID` | Email + Calendar | Google Cloud Console OAuth |
| `GMAIL_CLIENT_SECRET` | Email + Calendar | Google Cloud Console OAuth |
| `GMAIL_REFRESH_TOKEN` | Email + Calendar | OAuth flow |
| `FEISHU_APP_ID` | Feishu | Feishu Open Platform |
| `FEISHU_APP_SECRET` | Feishu | Feishu Open Platform |
| `GITHUB_TOKEN` | Code Status | GitHub → Settings → PAT |
| `TWITTER_BEARER_TOKEN` | Social Feed | Twitter Developer Portal |
| `GROQ_API_KEY` | AI Insights | https://console.groq.com/ |

Keys are passed to the server via request headers (never in query params), and the server-side proxy uses them to call external APIs.

## Personal Data Sensitivity

This dashboard handles personal data. Key considerations:

1. **API keys** — stored in localStorage, never sent to third parties
2. **Email content** — proxied through your own server, not cached
3. **Calendar events** — may contain sensitive meeting details
4. **Finance data** — keep local, never expose to external APIs
5. **Social accounts** — use read-only tokens, never write access
6. **No analytics** — unlike WorldMonitor, no Sentry/Vercel Analytics needed
7. **Self-hosted** — run on localhost or personal server, not public deployment

## Development Priority

**Phase 1** (MVP): Schedule + Email + Feishu + Stock + News — the daily essentials
**Phase 2** (Complete): Code Status + Office + Social + Finance + Weather
**Phase 3** (Enhanced): Map + AI Insights + World Clock + Xiaohongshu
**Phase 4** (Polish): DevOps + Live News + Quick Links + My Monitors + Command Palette

