/**
 * StockPanel — tabbed: Stocks / Crypto / Commodities.
 * All powered by Yahoo Finance (no extra API key for crypto/commodities).
 */
import { Panel } from './Panel';
import { fetchStockQuotes, type StockQuote } from '@/services/stock-market';
import { formatPrice, formatChange, getChangeClass, miniSparkline } from '@/utils';

type StockTab = 'stocks' | 'crypto' | 'commodities';

const CRYPTO_SYMBOLS = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'BNB-USD', 'XRP-USD'];
const COMMODITY_SYMBOLS = ['GC=F', 'SI=F', 'CL=F', 'NG=F', 'HG=F'];
const COMMODITY_NAMES: Record<string, string> = {
  'GC=F': 'Gold', 'SI=F': 'Silver', 'CL=F': 'Crude Oil', 'NG=F': 'Natural Gas', 'HG=F': 'Copper',
};
const CRYPTO_NAMES: Record<string, string> = {
  'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum', 'SOL-USD': 'Solana', 'BNB-USD': 'BNB', 'XRP-USD': 'XRP',
};

export class StockPanel extends Panel {
  private activeTab: StockTab = 'stocks';
  private tabsEl: HTMLElement | null = null;
  private listEl: HTMLElement | null = null;

  constructor() {
    super({ id: 'stocks', title: 'Markets', showCount: true });
    this.buildLayout();
    this.refresh();
  }

  private buildLayout(): void {
    this.content.innerHTML = '';
    this.content.style.padding = '0';

    this.tabsEl = document.createElement('div');
    this.tabsEl.className = 'panel-tabs';
    this.renderTabs();
    this.content.appendChild(this.tabsEl);

    this.listEl = document.createElement('div');
    this.listEl.className = 'stock-list';
    this.listEl.style.padding = '0 4px 4px';
    this.content.appendChild(this.listEl);
  }

  private renderTabs(): void {
    if (!this.tabsEl) return;
    this.tabsEl.innerHTML = '';
    for (const tab of ['stocks', 'crypto', 'commodities'] as StockTab[]) {
      const btn = document.createElement('button');
      btn.className = `panel-tab ${tab === this.activeTab ? 'active' : ''}`;
      btn.textContent = tab === 'stocks' ? '📈 Stocks' : tab === 'crypto' ? '₿ Crypto' : '🛢 Commodities';
      btn.addEventListener('click', () => { this.activeTab = tab; this.renderTabs(); this.refresh(); });
      this.tabsEl.appendChild(btn);
    }
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      let symbols: string[] | undefined;
      let nameMap: Record<string, string> = {};

      if (this.activeTab === 'crypto') {
        symbols = CRYPTO_SYMBOLS;
        nameMap = CRYPTO_NAMES;
      } else if (this.activeTab === 'commodities') {
        symbols = COMMODITY_SYMBOLS;
        nameMap = COMMODITY_NAMES;
      }

      const quotes = await fetchStockQuotes(symbols);
      // Apply friendly names for crypto/commodities
      if (Object.keys(nameMap).length > 0) {
        for (const q of quotes) {
          if (nameMap[q.symbol]) q.name = nameMap[q.symbol];
        }
      }
      this.render(quotes);
      this.setCount(quotes.length);
      this.setDataBadge('live');
    } catch {
      this.showError('Failed to load market data', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private render(quotes: StockQuote[]): void {
    if (!this.listEl) return;
    const rows = quotes.map(q => {
      const changeClass = getChangeClass(q.changePercent);
      const spark = miniSparkline(q.sparkline, q.changePercent);
      return `
        <div class="stock-row">
          <span class="stock-symbol">${q.symbol.replace('-USD', '').replace('=F', '')}</span>
          <span class="stock-name">${q.name}</span>
          <span class="stock-price num">${q.price != null ? formatPrice(q.price) : '\u2014'}</span>
          <span class="stock-change num ${changeClass}">${formatChange(q.changePercent)}</span>
          <span class="stock-sparkline">${spark}</span>
        </div>`;
    }).join('');
    this.listEl.innerHTML = rows;
  }
}
