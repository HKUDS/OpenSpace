/**
 * Today's Focus sidebar — persistent left sidebar showing:
 * - Top 3 priority tasks
 * - Countdown to next meeting
 * - Stock alerts
 * - Unread counts
 * - AI daily briefing
 */

export interface FocusData {
  nextEvent?: { title: string; startTime: string; minutesUntil: number };
  unreadEmails: number;
  unreadFeishu: number;
  stockAlerts: Array<{ symbol: string; changePercent: number }>;
  ciFailures: number;
  dailyBriefing?: string;
}

let sidebarEl: HTMLElement | null = null;

export function createTodayFocusSidebar(): HTMLElement {
  sidebarEl = document.createElement('aside');
  sidebarEl.className = 'today-focus-sidebar';
  sidebarEl.id = 'todayFocus';
  sidebarEl.innerHTML = `
    <div class="focus-header">
      <span class="focus-title">TODAY'S FOCUS</span>
      <button class="focus-toggle" id="focusToggle" title="Toggle sidebar">◀</button>
    </div>
    <div class="focus-content" id="focusContent">
      <div class="focus-section">
        <div class="focus-loading">Gathering data...</div>
      </div>
    </div>
  `;

  sidebarEl.querySelector('#focusToggle')?.addEventListener('click', () => {
    sidebarEl?.classList.toggle('collapsed');
    const btn = sidebarEl?.querySelector('#focusToggle');
    if (btn) btn.textContent = sidebarEl?.classList.contains('collapsed') ? '▶' : '◀';
  });

  return sidebarEl;
}

export function updateTodayFocus(data: FocusData): void {
  const content = document.getElementById('focusContent');
  if (!content) return;

  const sections: string[] = [];

  // Next event countdown
  if (data.nextEvent) {
    const mins = data.nextEvent.minutesUntil;
    const timeStr = mins <= 0 ? 'NOW' : mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${mins % 60}m`;
    const urgency = mins <= 5 ? 'critical' : mins <= 15 ? 'warn' : '';
    sections.push(`
      <div class="focus-section">
        <div class="focus-section-title">NEXT EVENT</div>
        <div class="focus-event ${urgency}">
          <span class="focus-event-time">${timeStr}</span>
          <span class="focus-event-name">${data.nextEvent.title}</span>
        </div>
      </div>
    `);
  }

  // Unread counts
  const badges: string[] = [];
  if (data.unreadEmails > 0) badges.push(`<span class="focus-badge badge-info">📧 ${data.unreadEmails}</span>`);
  if (data.unreadFeishu > 0) badges.push(`<span class="focus-badge badge-info">💬 ${data.unreadFeishu}</span>`);
  if (data.ciFailures > 0) badges.push(`<span class="focus-badge badge-error">❌ ${data.ciFailures} CI</span>`);

  if (badges.length > 0) {
    sections.push(`
      <div class="focus-section">
        <div class="focus-section-title">UNREAD</div>
        <div class="focus-badges">${badges.join('')}</div>
      </div>
    `);
  }

  // Stock alerts (>3% move)
  if (data.stockAlerts.length > 0) {
    const alerts = data.stockAlerts.map(s => {
      const cls = s.changePercent >= 0 ? 'positive' : 'negative';
      return `<span class="focus-stock ${cls}">${s.symbol} ${s.changePercent >= 0 ? '+' : ''}${s.changePercent.toFixed(1)}%</span>`;
    }).join('');
    sections.push(`
      <div class="focus-section">
        <div class="focus-section-title">STOCK ALERTS</div>
        <div class="focus-stocks">${alerts}</div>
      </div>
    `);
  }

  // AI briefing
  if (data.dailyBriefing) {
    sections.push(`
      <div class="focus-section">
        <div class="focus-section-title">AI BRIEFING</div>
        <div class="focus-briefing">${data.dailyBriefing}</div>
      </div>
    `);
  }

  content.innerHTML = sections.length > 0 ? sections.join('') : '<div class="focus-section"><div class="focus-empty">All clear — enjoy your day!</div></div>';
}

