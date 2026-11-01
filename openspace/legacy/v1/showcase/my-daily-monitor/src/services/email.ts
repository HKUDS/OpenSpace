import { getSecret } from '@/services/settings-store';

export interface EmailMessage {
  id: string;
  from: string;
  subject: string;
  snippet: string;
  receivedAt: string;
  unread: boolean;
  labels?: string[];
}

export interface EmailResult {
  emails: EmailMessage[];
  configured: boolean;
  error?: string;
}

export async function fetchEmails(): Promise<EmailMessage[]> {
  return (await fetchEmailResult()).emails;
}

export async function fetchEmailResult(): Promise<EmailResult> {
  const clientId = getSecret('GMAIL_CLIENT_ID');
  const clientSecret = getSecret('GMAIL_CLIENT_SECRET');
  const refreshToken = getSecret('GMAIL_REFRESH_TOKEN');
  if (!clientId || !clientSecret || !refreshToken) {
    return { emails: [], configured: false, error: 'Gmail not configured. Go to Settings → API Keys → add Gmail OAuth Client ID, Secret, and Refresh Token.' };
  }

  try {
    const resp = await fetch('/api/emails?maxResults=15&q=is:unread', {
      headers: {
        'X-Gmail-Client-Id': clientId,
        'X-Gmail-Client-Secret': clientSecret,
        'X-Gmail-Token': refreshToken,
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) return { emails: [], configured: true, error: data.error };
    return { emails: (data.emails || []) as EmailMessage[], configured: true };
  } catch (err: any) {
    return { emails: [], configured: true, error: err.message };
  }
}
