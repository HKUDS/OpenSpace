/**
 * CodingHub — tabbed panel: Trending | CI/CD | My Repos | Activity | Tracked
 * Trending includes search + keyword quick-filters (agent, claw, evolve, etc.)
 */
import { Panel } from './Panel';
import { getSecret, getPreferences } from '@/services/settings-store';
import { formatTime, escapeHtml } from '@/utils';

type CodingTab = 'trending' | 'ci' | 'repos' | 'activity' | 'tracked';

const QUICK_KEYWORDS = ['agent', 'claw', 'evolve', 'mcp', 'llm', 'rag', 'cursor', 'vscode'];

export class CodeStatusPanel extends Panel {
  private activeTab: CodingTab = 'trending';
  private tabsEl: HTMLElement | null = null;
  private bodyEl: HTMLElement | null = null;
  private searchQuery = '';

  constructor() {
    super({ id: 'code-status', title: 'Coding Hub', showCount: true, className: 'panel-wide' });
    this.content.style.padding = '0';
    this.content.style.display = 'flex';
    this.content.style.flexDirection = 'column';
    this.buildLayout();
    this.refresh();
  }

  private buildLayout(): void {
    this.content.innerHTML = '';
    this.tabsEl = document.createElement('div');
    this.tabsEl.className = 'panel-tabs';
    this.renderTabs();
    this.content.appendChild(this.tabsEl);

    this.bodyEl = document.createElement('div');
    this.bodyEl.style.cssText = 'padding:0;overflow-y:auto;flex:1;min-height:0;';
    this.content.appendChild(this.bodyEl);
  }

  private renderTabs(): void {
    if (!this.tabsEl) return;
    const tabs: { id: CodingTab; label: string }[] = [
      { id: 'trending', label: '🔥 Trending' },
      { id: 'tracked', label: '⭐ Tracked' },
      { id: 'ci', label: '⚙ CI/CD' },
      { id: 'repos', label: '📦 My Repos' },
      { id: 'activity', label: '📡 Activity' },
    ];
    this.tabsEl.innerHTML = '';
    for (const t of tabs) {
      const btn = document.createElement('button');
      btn.className = `panel-tab ${t.id === this.activeTab ? 'active' : ''}`;
      btn.textContent = t.label;
      btn.addEventListener('click', () => { this.activeTab = t.id; this.searchQuery = ''; this.renderTabs(); this.refresh(); });
      this.tabsEl.appendChild(btn);
    }
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    if (this.bodyEl) this.bodyEl.innerHTML = '<div class="panel-loading" style="padding:24px"><div class="panel-loading-radar"><div class="panel-radar-sweep"></div><div class="panel-radar-dot"></div></div></div>';

    try {
      if (this.activeTab === 'trending') await this.loadTrending();
      else if (this.activeTab === 'tracked') await this.loadTracked();
      else if (this.activeTab === 'ci') await this.loadCI();
      else if (this.activeTab === 'repos') await this.loadMyRepos();
      else if (this.activeTab === 'activity') await this.loadActivity();
      this.setDataBadge('live');
    } catch {
      if (this.bodyEl) this.bodyEl.innerHTML = '<div class="panel-empty">Failed to load</div>';
    } finally {
      this.setFetching(false);
    }
  }

  // ---- Trending tab with search + keyword filters ----
  private async loadTrending(): Promise<void> {
    if (!this.bodyEl) return;

    // Search bar + keyword quick filters
    const searchBar = `
      <div class="ch-search-bar">
        <input class="ch-search-input" placeholder="Search repos..." value="${escapeHtml(this.searchQuery)}" id="chSearchInput" />
        <div class="ch-quick-keywords">
          ${QUICK_KEYWORDS.map(kw => `<button class="ch-kw-btn ${this.searchQuery === kw ? 'active' : ''}" data-kw="${kw}">${kw}</button>`).join('')}
        </div>
      </div>
    `;

    const resp = await fetch(`/api/github?action=trending&q=${encodeURIComponent(this.searchQuery)}`);
    const data = await resp.json();
    if (data.error) { this.bodyEl.innerHTML = `${searchBar}<div class="panel-empty">${data.error}</div>`; this.wireSearch(); return; }

    const repos = data.repos || [];
    this.setCount(repos.length);

    const rows = repos.map((r: any, i: number) => `
      <a href="${r.url}" target="_blank" class="ch-trending-row" rel="noopener">
        <span class="ch-rank">${i + 1}</span>
        <img src="${r.avatar}" class="ch-trending-avatar" onerror="this.style.display='none'" />
        <div class="ch-trending-info">
          <div class="ch-trending-name">${escapeHtml(r.fullName)}</div>
          <div class="ch-trending-desc">${escapeHtml((r.description || '').slice(0, 80))}</div>
          <div class="ch-trending-meta">
            ${r.language ? `<span class="ch-lang">${escapeHtml(r.language)}</span>` : ''}
            <span>⭐ ${this.fmtNum(r.stars)}</span>
            <span>🍴 ${this.fmtNum(r.forks)}</span>
            ${(r.topics || []).map((t: string) => `<span class="ch-topic">${escapeHtml(t)}</span>`).join('')}
          </div>
        </div>
      </a>
    `).join('');

    const countInfo = data.totalCount ? `<div class="ch-result-count">${data.totalCount.toLocaleString()} repos found</div>` : '';

    this.bodyEl.innerHTML = `${searchBar}${countInfo}<div class="ch-list" style="padding:0 8px 8px">${rows || '<div class="panel-empty">No repos found</div>'}</div>`;
    this.wireSearch();
  }

  private wireSearch(): void {
    const input = this.bodyEl?.querySelector<HTMLInputElement>('#chSearchInput');
    if (input) {
      input.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') { this.searchQuery = input.value.trim(); this.loadTrending(); }
      });
    }
    this.bodyEl?.querySelectorAll<HTMLButtonElement>('.ch-kw-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const kw = btn.dataset.kw || '';
        this.searchQuery = this.searchQuery === kw ? '' : kw;
        this.loadTrending();
      });
    });
  }

  // ---- Tracked repos (star-history style) ----
  private async loadTracked(): Promise<void> {
    if (!this.bodyEl) return;
    const tracked = getPreferences().githubRepos;
    if (tracked.length === 0) {
      this.bodyEl.innerHTML = '<div class="panel-empty">No tracked repos. Go to Settings → Preferences → GitHub Repos to add repos to track.</div>';
      return;
    }

    const token = getSecret('GITHUB_PAT');
    const headers: Record<string, string> = {};
    if (token) headers['X-Github-Token'] = token;

    const resp = await fetch(`/api/github?action=star-check&repos=${tracked.join(',')}`, { headers });
    const data = await resp.json();
    const repos = data.repos || [];
    this.setCount(repos.length);

    if (repos.length === 0) { this.bodyEl.innerHTML = '<div class="panel-empty">Could not fetch repo data</div>'; return; }

    // Sort by stars descending
    repos.sort((a: any, b: any) => b.stars - a.stars);

    const rows = repos.map((r: any, i: number) => `
      <a href="${r.url}" target="_blank" class="ch-trending-row" rel="noopener">
        <span class="ch-rank">${i + 1}</span>
        <img src="${r.avatar}" class="ch-trending-avatar" onerror="this.style.display='none'" />
        <div class="ch-trending-info">
          <div class="ch-trending-name">${escapeHtml(r.fullName)}</div>
          <div class="ch-trending-desc">${escapeHtml((r.description || '').slice(0, 80))}</div>
          <div class="ch-trending-meta">
            ${r.language ? `<span class="ch-lang">${escapeHtml(r.language)}</span>` : ''}
            <span>⭐ ${this.fmtNum(r.stars)}</span>
            <span>🍴 ${this.fmtNum(r.forks)}</span>
            ${r.openIssues > 0 ? `<span style="color:var(--yellow)">⚠ ${r.openIssues} issues</span>` : ''}
          </div>
        </div>
        <span class="ch-ci-time">${formatTime(new Date(r.updatedAt))}</span>
      </a>
    `).join('');

    const historyLink = tracked.length > 0
      ? `<div style="padding:6px 8px"><a href="https://star-history.com/#${tracked.join('&')}&Date" target="_blank" style="font-size:10px;color:var(--blue)">📈 View star history chart on star-history.com →</a></div>`
      : '';

    this.bodyEl.innerHTML = `${historyLink}<div class="ch-list" style="padding:0 8px 8px">${rows}</div>`;
  }

  // ---- CI/CD tab ----
  private async loadCI(): Promise<void> {
    if (!this.bodyEl) return;
    const token = getSecret('GITHUB_PAT');
    const repos = getPreferences().githubRepos;
    if (!token) { this.bodyEl.innerHTML = '<div class="panel-empty">GitHub PAT not configured. Go to Settings.</div>'; return; }
    if (repos.length === 0) { this.bodyEl.innerHTML = '<div class="panel-empty">No repos configured.</div>'; return; }

    const resp = await fetch(`/api/github?action=runs&repos=${repos.join(',')}`, { headers: { 'X-Github-Token': token } });
    const data = await resp.json();
    const runs = data.runs || [];
    this.setCount(runs.length);
    if (runs.length === 0) { this.bodyEl.innerHTML = '<div class="panel-empty">No workflow runs</div>'; return; }

    const rows = runs.map((r: any) => {
      const icon = r.conclusion === 'success' ? '✅' : r.conclusion === 'failure' ? '❌' : '🔄';
      return `
      <a href="${r.url}" target="_blank" class="ch-ci-row" rel="noopener">
        <span class="ch-ci-icon">${icon}</span>
        <div class="ch-ci-info">
          <div class="ch-ci-repo">${escapeHtml(r.repo)}</div>
          <div class="ch-ci-detail">${escapeHtml(r.name)} · ${escapeHtml(r.branch)} · ${r.commit}</div>
        </div>
        <span class="ch-ci-time">${formatTime(new Date(r.updatedAt))}</span>
      </a>`;
    }).join('');
    this.bodyEl.innerHTML = `<div class="ch-list" style="padding:0 8px 8px">${rows}</div>`;
  }

  // ---- My Repos tab ----
  private async loadMyRepos(): Promise<void> {
    if (!this.bodyEl) return;
    const token = getSecret('GITHUB_PAT');
    if (!token) { this.bodyEl.innerHTML = '<div class="panel-empty">GitHub PAT not configured.</div>'; return; }

    const resp = await fetch('/api/github?action=my-repos', { headers: { 'X-Github-Token': token } });
    const data = await resp.json();
    const repos = data.repos || [];
    this.setCount(repos.length);
    if (repos.length === 0) { this.bodyEl.innerHTML = '<div class="panel-empty">No repos</div>'; return; }

    const rows = repos.map((r: any) => `
      <a href="${r.url}" target="_blank" class="ch-repo-row" rel="noopener">
        <div class="ch-repo-info">
          <div class="ch-repo-name">${r.isPrivate ? '🔒' : '📂'} ${escapeHtml(r.fullName)}</div>
          ${r.description ? `<div class="ch-repo-desc">${escapeHtml(r.description).slice(0, 60)}</div>` : ''}
        </div>
        <div class="ch-repo-stats">
          ${r.language ? `<span class="ch-lang">${escapeHtml(r.language)}</span>` : ''}
          <span>⭐${r.stars}</span>
          ${r.openIssues > 0 ? `<span style="color:var(--yellow)">⚠${r.openIssues}</span>` : ''}
        </div>
        <span class="ch-ci-time">${formatTime(new Date(r.updatedAt))}</span>
      </a>
    `).join('');
    this.bodyEl.innerHTML = `<div class="ch-list" style="padding:0 8px 8px">${rows}</div>`;
  }

  // ---- Activity tab ----
  private async loadActivity(): Promise<void> {
    if (!this.bodyEl) return;
    const token = getSecret('GITHUB_PAT');
    if (!token) { this.bodyEl.innerHTML = '<div class="panel-empty">GitHub PAT not configured.</div>'; return; }

    try {
      const meResp = await fetch('https://api.github.com/user', { headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'MDM/1.0' } });
      const me = await meResp.json() as any;
      if (!me.login) { this.bodyEl.innerHTML = '<div class="panel-empty">Could not get username</div>'; return; }

      const resp = await fetch(`/api/github?action=activity&username=${me.login}`, { headers: { 'X-Github-Token': token } });
      const data = await resp.json();
      const events = data.events || [];
      this.setCount(events.length);
      if (events.length === 0) { this.bodyEl.innerHTML = '<div class="panel-empty">No recent activity</div>'; return; }

      const rows = events.map((e: any) => `
        <div class="ch-activity-row">
          <img src="${e.actorAvatar}" class="ch-activity-avatar" onerror="this.style.display='none'" />
          <div class="ch-activity-info">
            <span class="ch-activity-actor">${escapeHtml(e.actor)}</span>
            <span class="ch-activity-action">${escapeHtml(e.action)}</span>
            <span class="ch-activity-repo">${escapeHtml(e.repo)}</span>
          </div>
          <span class="ch-ci-time">${formatTime(new Date(e.createdAt))}</span>
        </div>
      `).join('');
      this.bodyEl.innerHTML = `<div class="ch-list" style="padding:0 8px 8px">${rows}</div>`;
    } catch (err: any) {
      this.bodyEl.innerHTML = `<div class="panel-empty">${err.message}</div>`;
  }
}

  private fmtNum(n: number): string {
    return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n.toString();
  }
}
