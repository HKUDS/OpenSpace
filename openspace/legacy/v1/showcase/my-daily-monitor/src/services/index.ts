export { fetchStockQuotes } from './stock-market';
export type { StockQuote } from './stock-market';

export { fetchNews, searchNews } from './news';
export type { NewsArticle, ThreatLevel } from './news';

export { fetchEmails, fetchEmailResult } from './email';
export type { EmailMessage, EmailResult } from './email';

export { fetchCalendarEvents, fetchCalendarResult } from './schedule';
export type { CalendarEvent, CalendarResult } from './schedule';

export { fetchWorkflowRuns, fetchCodeStatusResult } from './code-status';
export type { WorkflowRun, CodeStatusResult } from './code-status';

export { fetchRecentDocuments, getDemoDocuments, getDocTypeIcon } from './office';
export type { OfficeDocument } from './office';

export { fetchSocialFeed, fetchSocialResult } from './social';
export type { SocialPost, SocialResult } from './social';

export { fetchDailyFinance, getDemoFinance } from './finance';
export type { FinanceTransaction, DailySummary } from './finance';

export { fetchFeishuMessages, fetchFeishuResult } from './feishu';
export type { FeishuMessage, FeishuResult } from './feishu';

export { RefreshScheduler } from './refresh-scheduler';
export type { RefreshRegistration } from './refresh-scheduler';

export {
  getSecret, setSecret, hasSecret, maskSecret,
  getPreferences, setPreferences,
  subscribeSettingsChange,
  getStockSymbols, getGithubRepos,
} from './settings-store';
