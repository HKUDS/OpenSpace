/**
 * Email API proxy — Gmail API via OAuth refresh token.
 * Credentials come from request headers (set by frontend from localStorage).
 */
import type { IncomingHttpHeaders } from 'node:http';

const GMAIL_API = 'https://gmail.googleapis.com/gmail/v1';
const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';

async function getGmailAccessToken(clientId: string, clientSecret: string, refreshToken: string): Promise<string | null> {
  try {
    const resp = await fetch(GOOGLE_TOKEN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ client_id: clientId, client_secret: clientSecret, refresh_token: refreshToken, grant_type: 'refresh_token' }),
    });
    if (!resp.ok) return null;
    const data = await resp.json() as any;
    return data.access_token || null;
  } catch { return null; }
}

export async function handleEmailRequest(
  query: Record<string, string>,
  _body: string,
  headers: IncomingHttpHeaders,
): Promise<unknown> {
  // Read ALL credentials from request headers (frontend sends from localStorage)
  const clientId = (headers['x-gmail-client-id'] as string) || process.env.GMAIL_CLIENT_ID || '';
  const clientSecret = (headers['x-gmail-client-secret'] as string) || process.env.GMAIL_CLIENT_SECRET || '';
  const refreshToken = (headers['x-gmail-token'] as string) || process.env.GMAIL_REFRESH_TOKEN || '';

  if (!clientId || !clientSecret || !refreshToken) {
    return { emails: [], configured: false, message: 'Gmail OAuth not configured — need Client ID, Client Secret, and Refresh Token' };
  }

  const accessToken = await getGmailAccessToken(clientId, clientSecret, refreshToken);
  if (!accessToken) {
    return { emails: [], configured: true, error: 'Failed to refresh access token. Check Client ID, Secret, and Refresh Token.' };
  }

  const maxResults = query.maxResults || '15';
  const q = query.q || 'is:unread';

  try {
    const listResp = await fetch(
      `${GMAIL_API}/users/me/messages?maxResults=${maxResults}&q=${encodeURIComponent(q)}`,
      { headers: { Authorization: `Bearer ${accessToken}` } },
    );
    if (!listResp.ok) throw new Error(`Gmail list: ${listResp.status}`);
    const listData = await listResp.json() as any;
    const messageIds: string[] = (listData.messages || []).map((m: any) => m.id);

    const emails = await Promise.all(
      messageIds.slice(0, 15).map(async (id) => {
        const resp = await fetch(
          `${GMAIL_API}/users/me/messages/${id}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date`,
          { headers: { Authorization: `Bearer ${accessToken}` } },
        );
        if (!resp.ok) return null;
        const msg = await resp.json() as any;
        const getHeader = (name: string) =>
          msg.payload?.headers?.find((h: any) => h.name.toLowerCase() === name.toLowerCase())?.value || '';
        return {
          id: msg.id,
          from: getHeader('From').replace(/<.*>/, '').trim(),
          subject: getHeader('Subject'),
          snippet: msg.snippet || '',
          receivedAt: new Date(parseInt(msg.internalDate, 10)).toISOString(),
          unread: (msg.labelIds || []).includes('UNREAD'),
          labels: msg.labelIds || [],
        };
      })
    );

    return { emails: emails.filter(Boolean), configured: true, totalUnread: listData.resultSizeEstimate || 0 };
  } catch (err: any) {
    return { emails: [], configured: true, error: err.message };
  }
}
