import { Panel } from './Panel';
import { fetchEmailResult, type EmailMessage } from '@/services/email';
import { formatTime, escapeHtml } from '@/utils';

export class EmailPanel extends Panel {
  constructor() {
    super({ id: 'email', title: 'Email', showCount: true });
    this.refresh();
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const result = await fetchEmailResult();
      if (!result.configured) {
        this.setContent(`<div class="panel-empty">${result.error || 'Gmail not configured.'}<br><br><small>Click <b>⚙ Settings</b> to configure.</small></div>`);
        this.setDataBadge('unavailable');
        return;
      }
      if (result.error) {
        this.showError(result.error, () => this.refresh());
        return;
      }
      this.render(result.emails);
      this.setCount(result.emails.filter(e => e.unread).length);
      this.setDataBadge('live');
    } catch {
      this.showError('Failed to load emails', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private render(emails: EmailMessage[]): void {
    if (emails.length === 0) {
      this.setContent('<div class="panel-empty">Inbox zero! 🎉</div>');
      return;
    }
    const rows = emails.map(e => `
      <div class="email-item ${e.unread ? 'unread' : ''}">
        <div class="email-subject">${escapeHtml(e.subject)}</div>
        <div class="email-meta">
          <span class="email-from">${escapeHtml(e.from)}</span>
          <span class="email-time">${formatTime(new Date(e.receivedAt))}</span>
        </div>
      </div>`).join('');
    this.setContent(`<div class="email-list">${rows}</div>`);
  }
}
