/**
 * Tech Community Panel — HN / Reddit / V2EX aggregator.
 * All free APIs, no keys needed.
 */
import { Panel } from './Panel';
import { fetchCommunityPosts, type CommunityPost, type CommunitySource } from '@/services/social';
import { formatTime, escapeHtml } from '@/utils';

const PLATFORM_META: Record<string, { label: string; color: string; icon: string }> = {
  hn:     { label: 'HN',     color: '#ff6600', icon: 'Y' },
  reddit: { label: 'Reddit', color: '#ff4500', icon: 'r/' },
  v2ex:   { label: 'V2EX',   color: '#333',    icon: 'V' },
};

const TABS: { id: CommunitySource; label: string }[] = [
  { id: 'all',    label: 'All' },
  { id: 'hn',     label: 'Hacker News' },
  { id: 'reddit', label: 'Reddit' },
];

export class SocialPanel extends Panel {
  private activeSource: CommunitySource = 'all';
  private allPosts: CommunityPost[] = [];
  private tabsEl: HTMLElement | null = null;
  private listEl: HTMLElement | null = null;

  constructor() {
    super({ id: 'social', title: 'Tech Community', showCount: true });
    this.content.style.padding = '0';
    this.buildLayout();
    this.refresh();
  }

  private buildLayout(): void {
    this.content.innerHTML = '';

    this.tabsEl = document.createElement('div');
    this.tabsEl.className = 'panel-tabs';
    this.renderTabs();
    this.content.appendChild(this.tabsEl);

    this.listEl = document.createElement('div');
    this.listEl.className = 'social-list';
    this.listEl.style.padding = '0 4px 4px';
    this.content.appendChild(this.listEl);
  }

  private renderTabs(): void {
    if (!this.tabsEl) return;
    this.tabsEl.innerHTML = '';
    for (const t of TABS) {
      const btn = document.createElement('button');
      btn.className = `panel-tab ${t.id === this.activeSource ? 'active' : ''}`;
      btn.textContent = t.label;
      btn.addEventListener('click', () => {
        this.activeSource = t.id;
        this.renderTabs();
        this.filterAndRender();
      });
      this.tabsEl.appendChild(btn);
    }
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      this.allPosts = await fetchCommunityPosts('all');
      if (!this.tabsEl?.isConnected || !this.listEl?.isConnected) {
        this.buildLayout();
      }
      this.filterAndRender();
      this.setDataBadge('live', `${this.allPosts.length} posts`);
    } catch {
      this.showError('Failed to load community feed', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private filterAndRender(): void {
    const posts = this.activeSource === 'all'
      ? this.allPosts
      : this.allPosts.filter(p => p.platform === this.activeSource);

    this.setCount(posts.length);
    this.renderPosts(posts);
  }

  private fmtScore(n: number): string {
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return n.toString();
  }

  private renderPosts(posts: CommunityPost[]): void {
    if (!this.listEl) return;

    if (posts.length === 0) {
      this.listEl.innerHTML = '<div class="panel-empty">No posts found</div>';
      return;
    }

    const rows = posts.map(p => {
      const meta = PLATFORM_META[p.platform] || PLATFORM_META.hn;
      const discussUrl = p.platform === 'hn'
        ? `https://news.ycombinator.com/item?id=${p.id.replace('hn-', '')}`
        : p.platform === 'reddit'
        ? p.url
        : p.url;

      return `
      <div class="community-item">
        <div class="community-score">
          <span class="community-score-num">${this.fmtScore(p.score)}</span>
          <span class="community-score-label">pts</span>
        </div>
        <div class="community-body">
          <a href="${p.url}" target="_blank" rel="noopener" class="community-title">${escapeHtml(p.title)}</a>
          <div class="community-meta">
            <span class="community-platform-badge" style="background:${meta.color}">${meta.label}</span>
            <span class="community-author">${escapeHtml(p.author)}</span>
            <a href="${discussUrl}" target="_blank" rel="noopener" class="community-comments">💬 ${p.comments}</a>
            <span class="community-time">${formatTime(new Date(p.timestamp))}</span>
          </div>
        </div>
      </div>`;
    }).join('');

    this.listEl.innerHTML = rows;
  }
}
