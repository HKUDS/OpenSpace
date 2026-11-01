import { Panel } from './Panel';
import { escapeHtml, formatTime } from '@/utils';

/**
 * OfficePanel — shows "not configured" instead of fake demo data.
 * Will support real Microsoft Graph / Google Drive when credentials are set up.
 */
export class OfficePanel extends Panel {
  constructor() {
    super({ id: 'office', title: 'Office Docs', showCount: true });
    this.refresh();
  }

  async refresh(): Promise<void> {
    this.setContent(`
      <div class="panel-empty">
        📄 Office documents not configured.<br><br>
        <small style="color:var(--text-muted)">
          To connect real documents, configure one of:<br>
          • Microsoft Graph API (Azure AD credentials)<br>
          • Google Drive API (OAuth2 credentials)<br>
          in Settings → API Keys.
        </small>
      </div>
    `);
    this.setDataBadge('unavailable');
    this.setCount(0);
  }
}
