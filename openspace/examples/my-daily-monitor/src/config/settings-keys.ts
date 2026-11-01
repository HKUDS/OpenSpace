/** All configurable secret keys for external API integrations. */
export type SecretKey =
  | 'FINNHUB_API_KEY'
  | 'NEWSAPI_KEY'
  | 'GITHUB_PAT'
  | 'GMAIL_CLIENT_ID'
  | 'GMAIL_CLIENT_SECRET'
  | 'GMAIL_REFRESH_TOKEN'
  | 'GOOGLE_CALENDAR_ENABLED'
  | 'OUTLOOK_CLIENT_ID'
  | 'OUTLOOK_REFRESH_TOKEN'
  | 'FEISHU_APP_ID'
  | 'FEISHU_APP_SECRET'
  | 'TWITTER_BEARER_TOKEN'
  | 'OPENROUTER_API_KEY'
  | 'OPENROUTER_MODEL';

export interface SecretMeta {
  key: SecretKey;
  label: string;
  placeholder: string;
  group: string;
  required?: boolean;
  type?: 'text' | 'password' | 'toggle';
  hint?: string;
}

export const SECRET_REGISTRY: SecretMeta[] = [
  // Stock
  { key: 'FINNHUB_API_KEY', label: 'Finnhub API Key', placeholder: 'c1234...', group: 'Stocks', required: true, hint: 'Free at finnhub.io' },
  // News
  { key: 'NEWSAPI_KEY', label: 'NewsAPI Key (optional)', placeholder: '', group: 'News', hint: 'Optional — RSS feeds work without it' },
  // GitHub
  { key: 'GITHUB_PAT', label: 'GitHub Personal Access Token', placeholder: 'ghp_...', group: 'Code', required: true, hint: 'Needs repo + actions:read scope' },
  // Gmail
  { key: 'GMAIL_CLIENT_ID', label: 'Gmail OAuth Client ID', placeholder: '...apps.googleusercontent.com', group: 'Email' },
  { key: 'GMAIL_CLIENT_SECRET', label: 'Gmail OAuth Client Secret', placeholder: 'GOCSPX-...', group: 'Email', type: 'password' },
  { key: 'GMAIL_REFRESH_TOKEN', label: 'Gmail Refresh Token', placeholder: '1//0...', group: 'Email', type: 'password' },
  { key: 'GOOGLE_CALENDAR_ENABLED', label: 'Enable Google Calendar', placeholder: '', group: 'Schedule', type: 'toggle', hint: 'Uses same Gmail OAuth credentials' },
  // Outlook
  { key: 'OUTLOOK_CLIENT_ID', label: 'Outlook Client ID', placeholder: '', group: 'Email', hint: 'Microsoft Graph API' },
  { key: 'OUTLOOK_REFRESH_TOKEN', label: 'Outlook Refresh Token', placeholder: '', group: 'Email', type: 'password' },
  // Feishu
  { key: 'FEISHU_APP_ID', label: 'Feishu App ID', placeholder: 'cli_...', group: 'Feishu', required: true },
  { key: 'FEISHU_APP_SECRET', label: 'Feishu App Secret', placeholder: '', group: 'Feishu', type: 'password' },
  // Twitter
  { key: 'TWITTER_BEARER_TOKEN', label: 'Twitter Bearer Token', placeholder: 'AAAA...', group: 'Social', type: 'password' },
  // AI
  { key: 'OPENROUTER_API_KEY', label: 'OpenRouter API Key', placeholder: 'sk-or-...', group: 'AI', type: 'password', hint: 'For AI summaries' },
  { key: 'OPENROUTER_MODEL', label: 'AI Model', placeholder: 'minimax/minimax-m2.5', group: 'AI', hint: 'OpenRouter model ID' },
];

export const SECRET_GROUPS = [...new Set(SECRET_REGISTRY.map(s => s.group))];

