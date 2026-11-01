/**
 * MyMonitorsPanel — custom keyword monitoring.
 * User adds keywords, panel highlights matching news articles.
 * Reference: worldmonitor MonitorPanel.ts
 */
import { Panel } from './Panel';
import { fetchNews, type NewsArticle } from '@/services/news';
import { formatTime, escapeHtml } from '@/utils';

const STORAGE_KEY = 'mdm-monitors-v1';

interface Monitor {
  id: string;
  keywords: string;
  color: string;
}

const COLORS = ['#3b82f6', '#ef4444', '#f59e0b', '#10b981', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];

function loadMonitors(): Monitor[] {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); } catch { return []; }
}
function saveMonitors(m: Monitor[]): void { localStorage.setItem(STORAGE_KEY, JSON.stringify(m)); }

export class MyMonitorsPanel extends Panel {
  private monitors: Monitor[] = [];
  private matches: Map<string, NewsArticle[]> = new Map();

  constructor() {
    super({ id: 'monitors', title: 'My Monitors', showCount: true });
    this.monitors = loadMonitors();
    this.buildUI();
    this.refresh();
  }

  private buildUI(): void {
    this.content.innerHTML = '';
    this.content.style.padding = '0';

    // Input bar
    const bar = document.createElement('div');
    bar.className = 'monitor-input-bar';
    bar.innerHTML = `
      <input class="monitor-input" placeholder="Add keyword to watch..." id="monitorInput" />
      <button class="monitor-add-btn" id="monitorAddBtn">+ Add</button>
    `;
    this.content.appendChild(bar);

    // Monitor list
    const list = document.createElement('div');
    list.className = 'monitor-list';
    list.id = 'monitorList';
    this.content.appendChild(list);

    // Results
    const results = document.createElement('div');
    results.className = 'monitor-results';
    results.id = 'monitorResults';
    this.content.appendChild(results);

    // Events
    bar.querySelector('#monitorAddBtn')?.addEventListener('click', () => this.addMonitor());
    bar.querySelector('#monitorInput')?.addEventListener('keypress', (e) => {
      if ((e as KeyboardEvent).key === 'Enter') this.addMonitor();
    });

    this.renderMonitors();
  }

  private addMonitor(): void {
    const input = this.content.querySelector<HTMLInputElement>('#monitorInput');
    if (!input) return;
    const kw = input.value.trim();
    if (!kw) return;
    const color = COLORS[this.monitors.length % COLORS.length];
    this.monitors.push({ id: Date.now().toString(), keywords: kw, color });
    saveMonitors(this.monitors);
    input.value = '';
    this.renderMonitors();
    this.refresh();
  }

  private removeMonitor(id: string): void {
    this.monitors = this.monitors.filter(m => m.id !== id);
    saveMonitors(this.monitors);
    this.renderMonitors();
    this.refresh();
  }

  private renderMonitors(): void {
    const list = this.content.querySelector('#monitorList');
    if (!list) return;
    list.innerHTML = this.monitors.map(m => `
      <div class="monitor-tag" style="border-color:${m.color}">
        <span class="monitor-dot" style="background:${m.color}"></span>
        <span>${escapeHtml(m.keywords)}</span>
        <button class="monitor-remove" data-id="${m.id}">&times;</button>
      </div>
    `).join('');

    list.querySelectorAll<HTMLButtonElement>('.monitor-remove').forEach(btn => {
      btn.addEventListener('click', () => this.removeMonitor(btn.dataset.id!));
    });
  }

  async refresh(): Promise<void> {
    if (this.isFetching || this.monitors.length === 0) {
      if (this.monitors.length === 0) this.renderResults();
      return;
    }
    this.setFetching(true);
    try {
      const articles = await fetchNews();
      this.matches.clear();
      for (const m of this.monitors) {
        const kws = m.keywords.toLowerCase().split(/[,\s]+/).filter(Boolean);
        const matched = articles.filter(a => kws.some(kw => a.title.toLowerCase().includes(kw)));
        if (matched.length > 0) this.matches.set(m.id, matched);
      }
      const total = [...this.matches.values()].reduce((s, a) => s + a.length, 0);
      this.setCount(total);
      this.renderResults();
      this.setDataBadge(total > 0 ? 'live' : 'cached');
    } catch {
      this.showError('Failed to scan news', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private renderResults(): void {
    const results = this.content.querySelector('#monitorResults');
    if (!results) return;

    if (this.monitors.length === 0) {
      results.innerHTML = '<div class="panel-empty">Add keywords above to start monitoring</div>';
      return;
    }

    let html = '';
    for (const m of this.monitors) {
      const matched = this.matches.get(m.id) || [];
      if (matched.length === 0) continue;
      html += `<div class="monitor-group">
        <div class="monitor-group-title" style="color:${m.color}">● ${escapeHtml(m.keywords)} <span style="color:var(--text-dim)">(${matched.length})</span></div>
        ${matched.slice(0, 3).map(a => `
          <a href="${a.url}" target="_blank" class="monitor-match">
            <span class="monitor-match-title">${escapeHtml(a.title)}</span>
            <span class="monitor-match-meta">${escapeHtml(a.source)} · ${formatTime(new Date(a.publishedAt))}</span>
          </a>
        `).join('')}
      </div>`;
    }

    results.innerHTML = html || '<div class="panel-empty">No matches found — keywords will be checked against news feeds</div>';
  }
}

