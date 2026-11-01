/**
 * QuickLinksPanel — fast-access buttons for external apps.
 * Replaces broken iframe panels (Twitter/小红书 block iframes).
 */
import { Panel } from './Panel';

interface QuickLink {
  name: string;
  url: string;
  icon: string;
  color: string;
  description: string;
}

const LINKS: QuickLink[] = [
  { name: 'Twitter / X', url: 'https://x.com/home', icon: '𝕏', color: '#1da1f2', description: 'Timeline' },
  { name: 'Xiaohongshu', url: 'https://www.xiaohongshu.com/explore', icon: '📕', color: '#ff2d55', description: 'Explore' },
  { name: 'GitHub', url: 'https://github.com', icon: '🐙', color: '#8b5cf6', description: 'Repos & PRs' },
  { name: 'Gmail', url: 'https://mail.google.com', icon: '📧', color: '#ea4335', description: 'Inbox' },
  { name: 'Google Calendar', url: 'https://calendar.google.com', icon: '📅', color: '#4285f4', description: 'Events' },
  { name: 'Feishu', url: 'https://www.feishu.cn', icon: '💬', color: '#3370ff', description: 'Messages' },
  { name: 'Notion', url: 'https://notion.so', icon: '📝', color: '#fff', description: 'Notes & Docs' },
  { name: 'ChatGPT', url: 'https://chat.openai.com', icon: '🤖', color: '#10a37f', description: 'AI Chat' },
];

export class QuickLinksPanel extends Panel {
  constructor() {
    super({ id: 'quick-links', title: 'Quick Links', showCount: false });
    this.render();
  }

  private render(): void {
    const grid = LINKS.map(l => `
      <a href="${l.url}" target="_blank" rel="noopener" class="ql-item" title="${l.description}">
        <span class="ql-icon" style="background:${l.color}20;color:${l.color}">${l.icon}</span>
        <span class="ql-name">${l.name}</span>
      </a>
    `).join('');

    this.setContent(`<div class="ql-grid">${grid}</div>`);
  }

  async refresh(): Promise<void> {}
}

