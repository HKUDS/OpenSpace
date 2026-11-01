import { Panel } from './Panel';
import { fetchFeishuResult, type FeishuMessage } from '@/services/feishu';
import { escapeHtml } from '@/utils';

/** Format like Feishu: today → "15:13", yesterday → "Yesterday", older → "Mar 9" */
function feishuTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.floor((today.getTime() - msgDay.getTime()) / 86400000);

  if (diffDays === 0) return d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false });
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return d.toLocaleDateString('en', { weekday: 'short' });
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
}

/** Show name — if it's a raw open_id, show "User" instead of hiding */
function displayName(raw: string): string {
  if (!raw) return '';
  if (/^ou_[a-zA-Z0-9]{4,}/.test(raw)) return 'User';
  if (raw === 'Bot') return 'Bot';
  return raw;
}

export class FeishuPanel extends Panel {
  constructor() {
    super({ id: 'feishu', title: 'Feishu', showCount: true });
    this.refresh();
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const result = await fetchFeishuResult();
      if (!result.configured) {
        this.setContent(`<div class="panel-empty">${result.error}<br><br><small>Click <b>⚙ Settings</b> to configure.</small></div>`);
        this.setDataBadge('unavailable');
        return;
      }
      if (result.error) {
        this.setContent(`<div class="panel-empty" style="color:var(--yellow)">${escapeHtml(result.error)}</div>`);
        this.setDataBadge('unavailable');
        return;
      }
      if (result.messages.length === 0) {
        this.setContent('<div class="panel-empty">No chats. Add the bot to groups.</div>');
        return;
      }
      this.render(result.messages);
      this.setCount(result.messages.filter(m => m.unread).length);
      this.setDataBadge('live', `${result.messages.length}`);
    } catch {
      this.showError('Failed to load Feishu', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private render(messages: FeishuMessage[]): void {
    const rows = messages.map(m => {
      // Avatar: use image if available, else initials on blue bg
      const avatarHtml = m.avatar
        ? `<img src="${m.avatar}" class="fs-av-img" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" /><div class="fs-av-init" style="display:none">${this.initials(m.chatName)}</div>`
        : `<div class="fs-av-init">${this.initials(m.chatName)}</div>`;

      // Build preview: "Sender: message" like Feishu
      const sender = displayName(m.senderName);
      const isImage = m.content === '[Image]' && m.imageUrl;
      const previewText = sender ? `${sender}: ${m.content}` : m.content;

      // If message is an image, load it async via API
      const imageThumbHtml = isImage
        ? `<div class="fs-img-thumb" data-img-url="${m.imageUrl}"><span class="fs-img-loading">📷</span></div>`
        : '';

      return `
      <div class="fs-row ${m.unread ? 'fs-unread' : ''}">
        <div class="fs-av">${avatarHtml}</div>
        <div class="fs-mid">
          <div class="fs-name">${escapeHtml(m.chatName)}${m.memberCount > 1 ? ` <span class="fs-cnt">(${m.memberCount})</span>` : ''}</div>
          ${isImage ? imageThumbHtml : `<div class="fs-msg">${escapeHtml(previewText)}</div>`}
        </div>
        <div class="fs-right">
          <span class="fs-time">${feishuTime(m.timestamp)}</span>
          ${m.unread ? '<span class="fs-dot"></span>' : ''}
        </div>
      </div>`;
    }).join('');

    this.setContent(`<div class="fs-list">${rows}</div>`);

    // Async load image thumbnails
    this.loadImageThumbs();
  }

  private async loadImageThumbs(): Promise<void> {
    const thumbs = this.content.querySelectorAll<HTMLElement>('.fs-img-thumb[data-img-url]');
    for (const el of thumbs) {
      const url = el.dataset.imgUrl;
      if (!url) continue;
      try {
        const appId = (await import('@/services/settings-store')).getSecret('FEISHU_APP_ID');
        const appSecret = (await import('@/services/settings-store')).getSecret('FEISHU_APP_SECRET');
        const resp = await fetch(url, {
          headers: { 'X-Feishu-App-Id': appId, 'X-Feishu-App-Secret': appSecret },
        });
        if (!resp.ok) continue;
        const data = await resp.json();
        if (data.imageData) {
          el.innerHTML = `<img src="${data.imageData}" class="fs-img-preview" />`;
        }
      } catch { /* keep placeholder */ }
    }
  }

  private initials(name: string): string {
    return name.slice(0, 2).toUpperCase();
  }
}
