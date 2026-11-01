/**
 * WorldClockPanel — shows major market timezones with open/close status.
 * Reference: worldmonitor WorldClockPanel.ts
 */
import { Panel } from './Panel';

interface CityEntry {
  city: string;
  label: string;
  timezone: string;
  marketOpen?: number;
  marketClose?: number;
}

const CITIES: CityEntry[] = [
  { city: 'New York', label: 'NYSE', timezone: 'America/New_York', marketOpen: 9, marketClose: 16 },
  { city: 'London', label: 'LSE', timezone: 'Europe/London', marketOpen: 8, marketClose: 16 },
  { city: 'Shanghai', label: 'SSE', timezone: 'Asia/Shanghai', marketOpen: 9, marketClose: 15 },
  { city: 'Hong Kong', label: 'HKEX', timezone: 'Asia/Hong_Kong', marketOpen: 9, marketClose: 16 },
  { city: 'Tokyo', label: 'TSE', timezone: 'Asia/Tokyo', marketOpen: 9, marketClose: 15 },
  { city: 'Singapore', label: 'SGX', timezone: 'Asia/Singapore', marketOpen: 9, marketClose: 17 },
  { city: 'Frankfurt', label: 'XETRA', timezone: 'Europe/Berlin', marketOpen: 9, marketClose: 17 },
  { city: 'Sydney', label: 'ASX', timezone: 'Australia/Sydney', marketOpen: 10, marketClose: 16 },
  { city: 'Mumbai', label: 'NSE', timezone: 'Asia/Kolkata', marketOpen: 9, marketClose: 15 },
  { city: 'Dubai', label: 'DFM', timezone: 'Asia/Dubai', marketOpen: 10, marketClose: 14 },
];

export class WorldClockPanel extends Panel {
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'world-clock', title: 'World Clock', showCount: false });
    this.render();
    this.timer = setInterval(() => this.render(), 10_000);
  }

  async refresh(): Promise<void> { this.render(); }

  private render(): void {
    const now = new Date();
    const rows = CITIES.map(c => {
      const timeStr = now.toLocaleTimeString('en-US', { timeZone: c.timezone, hour: '2-digit', minute: '2-digit', hour12: false });
      const hour = parseInt(now.toLocaleTimeString('en-US', { timeZone: c.timezone, hour: 'numeric', hour12: false }), 10);
      const isOpen = c.marketOpen != null && c.marketClose != null && hour >= c.marketOpen && hour < c.marketClose;
      const localDay = new Date(now.toLocaleString('en-US', { timeZone: c.timezone })).getDay();
      const isWeekend = localDay === 0 || localDay === 6;
      const status = isWeekend ? 'closed' : (isOpen ? 'open' : 'closed');
      const statusColor = status === 'open' ? 'var(--green)' : 'var(--text-muted)';
      const dotColor = status === 'open' ? 'var(--green)' : (hour >= 6 && hour < 20 ? 'var(--yellow)' : 'var(--text-ghost)');

      return `
        <div class="wclock-row">
          <span class="wclock-dot" style="background:${dotColor}"></span>
          <span class="wclock-city">${c.city}</span>
          <span class="wclock-label">${c.label}</span>
          <span class="wclock-time">${timeStr}</span>
          <span class="wclock-status" style="color:${statusColor}">${status.toUpperCase()}</span>
        </div>`;
    }).join('');

    this.setContent(`<div class="wclock-list">${rows}</div>`);
  }

  public destroy(): void {
    if (this.timer) clearInterval(this.timer);
    super.destroy();
  }
}

