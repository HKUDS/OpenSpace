import { getSecret } from '@/services/settings-store';

export interface CalendarEvent {
  id: string;
  title: string;
  startTime: string;
  endTime: string;
  location?: string;
  isAllDay?: boolean;
  color?: string;
  meetingLink?: string;
}

export interface CalendarResult {
  events: CalendarEvent[];
  configured: boolean;
  error?: string;
}

export async function fetchCalendarEvents(): Promise<CalendarEvent[]> {
  return (await fetchCalendarResult()).events;
}

export async function fetchCalendarResult(): Promise<CalendarResult> {
  const enabled = getSecret('GOOGLE_CALENDAR_ENABLED');
  const clientId = getSecret('GMAIL_CLIENT_ID');
  const clientSecret = getSecret('GMAIL_CLIENT_SECRET');
  const refreshToken = getSecret('GMAIL_REFRESH_TOKEN');
  if (!enabled || !clientId || !clientSecret || !refreshToken) {
    return { events: [], configured: false, error: 'Google Calendar not configured. Go to Settings → enable Calendar toggle and add Gmail OAuth credentials.' };
  }

  try {
    const resp = await fetch('/api/calendar?calendarId=primary', {
      headers: {
        'X-Gmail-Client-Id': clientId,
        'X-Gmail-Client-Secret': clientSecret,
        'X-Gmail-Token': refreshToken,
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) return { events: [], configured: true, error: data.error };
    return { events: (data.events || []) as CalendarEvent[], configured: true };
  } catch (err: any) {
    return { events: [], configured: true, error: err.message };
}
}
