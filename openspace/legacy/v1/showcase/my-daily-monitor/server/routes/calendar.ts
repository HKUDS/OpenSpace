/**
 * Calendar API proxy — Google Calendar API v3.
 * Credentials come from request headers (frontend sends from localStorage).
 */
import type { IncomingHttpHeaders } from 'node:http';

const GCAL_API = 'https://www.googleapis.com/calendar/v3';
const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';

async function getAccessToken(clientId: string, clientSecret: string, refreshToken: string): Promise<string | null> {
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

export async function handleCalendarRequest(
  query: Record<string, string>,
  _body: string,
  headers: IncomingHttpHeaders,
): Promise<unknown> {
  // Read credentials from headers (frontend → localStorage → headers)
  const clientId = (headers['x-gmail-client-id'] as string) || process.env.GMAIL_CLIENT_ID || '';
  const clientSecret = (headers['x-gmail-client-secret'] as string) || process.env.GMAIL_CLIENT_SECRET || '';
  const refreshToken = (headers['x-gmail-token'] as string) || process.env.GMAIL_REFRESH_TOKEN || '';

  if (!clientId || !clientSecret || !refreshToken) {
    return { events: [], configured: false, message: 'Google Calendar not configured (needs Gmail OAuth credentials)' };
  }

  const accessToken = await getAccessToken(clientId, clientSecret, refreshToken);
  if (!accessToken) {
    return { events: [], configured: true, error: 'Failed to refresh access token' };
  }

  const calendarId = query.calendarId || 'primary';
  const now = new Date();
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
  const dayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1).toISOString();

  try {
    const resp = await fetch(
      `${GCAL_API}/calendars/${encodeURIComponent(calendarId)}/events?timeMin=${dayStart}&timeMax=${dayEnd}&singleEvents=true&orderBy=startTime&maxResults=20`,
      { headers: { Authorization: `Bearer ${accessToken}` } },
    );
    if (!resp.ok) throw new Error(`Calendar API: ${resp.status}`);
    const data = await resp.json() as any;
    const events = (data.items || []).map((ev: any) => ({
      id: ev.id,
      title: ev.summary || '(No title)',
      startTime: ev.start?.dateTime || ev.start?.date || '',
      endTime: ev.end?.dateTime || ev.end?.date || '',
      location: ev.location || '',
      isAllDay: !!ev.start?.date,
      meetingLink: ev.hangoutLink || ev.conferenceData?.entryPoints?.[0]?.uri || '',
    }));
    return { events, configured: true };
  } catch (err: any) {
    return { events: [], configured: true, error: err.message };
  }
}
