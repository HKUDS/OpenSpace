import { Panel } from './Panel';
import { fetchNews, type NewsArticle, type ThreatLevel } from '@/services/news';
import { formatTime, escapeHtml } from '@/utils';
import { getPreferences, setPreferences } from '@/services/settings-store';

const CATEGORY_LABELS: Record<string, string> = {
  tech: 'Tech',
  finance: 'Finance',
  world: 'World',
  ai: 'AI',
  china: 'China',
  science: 'Science',
  us: 'US',
  europe: 'Europe',
};

const ALL_CATEGORIES = Object.keys(CATEGORY_LABELS);

const THREAT_COLORS: Record<ThreatLevel, string> = {
  critical: 'var(--red)',
  high: 'var(--semantic-high)',
  medium: 'var(--yellow)',
  low: 'var(--blue)',
  info: 'var(--text-muted)',
};

const SOURCE_COLORS: Record<string, string> = {
  'BBC': '#3b82f6',
  'Reuters': '#ff8800',
  'AP': '#44aa44',
  'Al Jazeera': '#9333ea',
};

export class NewsPanel extends Panel {
  private activeCategories: string[];
  private tabsEl: HTMLElement | null = null;
  private listEl: HTMLElement | null = null;
  private allArticles: NewsArticle[] = [];

  constructor() {
    super({ id: 'news', title: 'News', showCount: true, className: 'panel-wide' });
    this.activeCategories = getPreferences().newsCategories;
    this.buildLayout();
    this.refresh();
  }

  private buildLayout(): void {
    this.content.innerHTML = '';
    this.content.style.padding = '0';

    // Category tabs
    this.tabsEl = document.createElement('div');
    this.tabsEl.className = 'panel-tabs';
    this.renderTabs();
    this.content.appendChild(this.tabsEl);

    // News list
    this.listEl = document.createElement('div');
    this.listEl.className = 'news-list';
    this.listEl.style.padding = '0 8px 8px';
    this.content.appendChild(this.listEl);
  }

  private renderTabs(): void {
    if (!this.tabsEl) return;
    this.tabsEl.innerHTML = '';

    for (const cat of ALL_CATEGORIES) {
      const btn = document.createElement('button');
      btn.className = `panel-tab ${this.activeCategories.includes(cat) ? 'active' : ''}`;
      btn.textContent = CATEGORY_LABELS[cat] || cat;
      btn.addEventListener('click', () => this.toggleCategory(cat));
      this.tabsEl.appendChild(btn);
    }
  }

  private toggleCategory(cat: string): void {
    if (this.activeCategories.includes(cat)) {
      if (this.activeCategories.length <= 1) return; // keep at least one
      this.activeCategories = this.activeCategories.filter(c => c !== cat);
    } else {
      this.activeCategories = [...this.activeCategories, cat];
    }
    // Persist preference
    setPreferences({ newsCategories: this.activeCategories });
    this.renderTabs();
    // Re-filter cached articles instantly (no network request needed)
    this.filterAndRender();
  }

  private filterAndRender(): void {
    const filtered = this.allArticles.filter(
      a => !a.category || this.activeCategories.includes(a.category)
    );
    this.renderArticles(filtered);
    this.setCount(filtered.length);
    this.setDataBadge('live', `${filtered.length} articles`);
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      this.allArticles = await fetchNews();
      // Rebuild layout in case showError/showLoading overwrote content
      if (!this.tabsEl?.isConnected || !this.listEl?.isConnected) {
        this.buildLayout();
      }
      this.filterAndRender();
    } catch {
      this.showError('Failed to load news', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private getSourceColor(source: string): string {
    return SOURCE_COLORS[source] || '#666';
  }

  private renderArticles(articles: NewsArticle[]): void {
    if (!this.listEl) return;

    // Sort articles by publishedAt descending (newest first)
    const sortedArticles = [...articles].sort((a, b) => {
      const dateA = new Date(a.publishedAt).getTime();
      const dateB = new Date(b.publishedAt).getTime();
      return dateB - dateA;
    });

    const rows = sortedArticles.map(a => {
      const threatColor = THREAT_COLORS[(a.threatLevel as ThreatLevel) || 'info'];
      const hasImage = a.image && !a.image.includes('data:');
      const sourceColor = this.getSourceColor(a.source);
      const matchBadges = (a.matchedKeywords || []).map(
        kw => `<span class="news-keyword-badge">${escapeHtml(kw)}</span>`
      ).join('');

      const thumbHtml = hasImage 
        ? `<div class="news-thumb">
             <img src="${a.image}" alt="" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'news-thumb-fallback\\'>📰</div>'" />
           </div>` 
        : `<div class="news-thumb">
             <div class="news-thumb-fallback">📰</div>
           </div>`;

      return `
        <div class="news-item-rich">
          <div class="news-item-threat" style="background:${threatColor}"></div>
          ${thumbHtml}
          <div class="news-item-body">
            <div class="news-item-header">
              <a href="${a.url}" target="_blank" rel="noopener" class="news-title">${escapeHtml(a.title)}</a>
            </div>
            ${a.description ? `<div class="news-description">${escapeHtml(a.description)}</div>` : ''}
            <div class="news-meta">
              <span class="source-badge" style="background-color:${sourceColor}">${escapeHtml(a.source)}</span>
              <span class="news-time">${formatTime(new Date(a.publishedAt))}</span>
              ${a.threatLevel && a.threatLevel !== 'info' ? `<span class="news-threat-label" style="color:${threatColor}">${a.threatLevel.toUpperCase()}</span>` : ''}
              ${matchBadges}
            </div>
          </div>
        </div>`;
    }).join('');

    this.listEl.innerHTML = rows;
  }
}
