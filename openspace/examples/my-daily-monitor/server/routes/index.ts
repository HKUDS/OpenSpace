import type { IncomingHttpHeaders } from 'node:http';
import { handleCalendarRequest } from './calendar';
import { handleEmailRequest } from './email';
import { handleFeishuRequest } from './feishu';
import { handleGithubRequest } from './github';
import { handleHealthRequest } from './health';
import { handleNewsRequest } from './news';
import { handleOfficeRequest } from './office';
import { handleSocialRequest } from './social';
import { handleStockRequest } from './stock';
import { handleSystemRequest } from './system';

export type RouteHandler = (
  query: Record<string, string>,
  body: string,
  headers: IncomingHttpHeaders,
) => Promise<unknown>;

export const apiRoutes: Record<string, RouteHandler> = {
  '/api/stocks': handleStockRequest,
  '/api/news': handleNewsRequest,
  '/api/github': handleGithubRequest,
  '/api/emails': handleEmailRequest,
  '/api/calendar': handleCalendarRequest,
  '/api/feishu': handleFeishuRequest,
  '/api/social': handleSocialRequest,
  '/api/system': handleSystemRequest,
  '/api/office': handleOfficeRequest,
  '/api/health': handleHealthRequest,
};
