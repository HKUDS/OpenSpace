/**
 * BreakingNewsBanner — top-of-screen alert banner for critical/high news.
 * Supports desktop notifications + auto-dismiss.
 * Reference: worldmonitor BreakingNewsBanner.ts
 */

export interface BreakingAlert {
  id: string;
  headline: string;
  source: string;
  level: 'critical' | 'high';
  url?: string;
  timestamp: Date;
}

const DISMISS_MS = { critical: 60_000, high: 30_000 };
const COOLDOWN_MS = 60_000;
const dismissed = new Set<string>();
let lastAlertMs = 0;

let container: HTMLElement | null = null;

function ensureContainer(): HTMLElement {
  if (!container) {
    container = document.createElement('div');
    container.className = 'breaking-news-container';
    document.body.appendChild(container);
  }
  return container;
}

export function pushBreakingAlert(alert: BreakingAlert): void {
  if (dismissed.has(alert.id)) return;
  if (Date.now() - lastAlertMs < COOLDOWN_MS) return;
  lastAlertMs = Date.now();
  dismissed.add(alert.id);

  const c = ensureContainer();
  const el = document.createElement('div');
  el.className = `breaking-alert breaking-${alert.level}`;
  el.innerHTML = `
    <span class="breaking-level">${alert.level.toUpperCase()}</span>
    <span class="breaking-headline">${alert.headline}</span>
    <span class="breaking-source">${alert.source}</span>
    ${alert.url ? `<a href="${alert.url}" target="_blank" class="breaking-link">→</a>` : ''}
    <button class="breaking-dismiss" aria-label="Dismiss">&times;</button>
  `;

  el.querySelector('.breaking-dismiss')?.addEventListener('click', () => el.remove());
  c.appendChild(el);

  // Auto-dismiss
  setTimeout(() => el.remove(), DISMISS_MS[alert.level]);

  // Desktop notification
  if (Notification.permission === 'granted') {
    new Notification(`[${alert.level.toUpperCase()}] ${alert.source}`, { body: alert.headline, icon: '/favicon.ico' });
  } else if (Notification.permission !== 'denied') {
    Notification.requestPermission();
  }
}

/** Scan news articles for breaking alerts */
export function scanForBreakingNews(articles: Array<{ title: string; source: string; url: string; threatLevel?: string }>): void {
  for (const a of articles) {
    if (a.threatLevel === 'critical' || a.threatLevel === 'high') {
      pushBreakingAlert({
        id: a.title.slice(0, 50),
        headline: a.title,
        source: a.source,
        level: a.threatLevel as 'critical' | 'high',
        url: a.url,
        timestamp: new Date(),
      });
    }
  }
}

