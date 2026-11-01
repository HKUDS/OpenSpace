import './styles/main.css';
import './styles/happy-theme.css';
import { applyStoredTheme, toggleTheme } from './utils/theme-manager';

// Apply stored theme ASAP (safety net — inline script in index.html does the flash-free path)
applyStoredTheme();

import {
  StockPanel,
  NewsPanel,
  EmailPanel,
  SchedulePanel,
  CodeStatusPanel,
  SocialPanel,
  FinancePanel,
  FeishuPanel,
  LiveNewsPanel,
  QuickLinksPanel,
  DevOpsPanel,
  MapPanel,
  WorldClockPanel,
  InsightsPanel,
  WeatherPanel,
  MyMonitorsPanel,
  openSettings,
  registerCommands,
  createTodayFocusSidebar,
  updateTodayFocus,
  scanForBreakingNews,
} from './components';
import { Panel } from './components/Panel';
import { RefreshScheduler } from './services/refresh-scheduler';
import { formatDate } from './utils';
import { generateDailyBriefing } from './services/ai-summary';

// ============================================================
//  Panel instances — organized by section
// ============================================================

// Overview
const mapPanel = new MapPanel();
const insightsPanel = new InsightsPanel();
const weatherPanel = new WeatherPanel();
const worldClockPanel = new WorldClockPanel();
const quickLinksPanel = new QuickLinksPanel();

// Productivity
const schedulePanel = new SchedulePanel();
const emailPanel = new EmailPanel();
const feishuPanel = new FeishuPanel();

// Markets & Finance
const stockPanel = new StockPanel();
const financePanel = new FinancePanel();

// Information
const newsPanel = new NewsPanel();
const socialPanel = new SocialPanel();
const liveNewsPanel = new LiveNewsPanel();
const monitorsPanel = new MyMonitorsPanel();

// DevOps & System
const devOpsPanel = new DevOpsPanel();
const codeStatusPanel = new CodeStatusPanel();

// ============================================================
//  Mount panels to single dense grid (grouped by affinity)
// ============================================================
const sidebarMount = document.getElementById('sidebarMount')!;
sidebarMount.appendChild(createTodayFocusSidebar());

const grid = document.getElementById('panelsGrid')!;
const allPanels: Panel[] = [
  // Row 1: map + AI agent (both wide)
  mapPanel, insightsPanel,
  // Row 2: daily essentials
  schedulePanel, weatherPanel, emailPanel, feishuPanel,
  // Row 3: markets
  stockPanel, financePanel,
  // Row 4: news + community (news is wide)
  newsPanel, socialPanel, worldClockPanel,
  // Row 6: devops + code (devops is wide)
  devOpsPanel, codeStatusPanel,
  // Row 7: media + extras
  liveNewsPanel, monitorsPanel, quickLinksPanel,
];

for (const p of allPanels) grid.appendChild(p.getElement());

// ============================================================
//  Custom Panel System — import via URL/iframe or user config
// ============================================================
const CUSTOM_PANELS_KEY = 'mdm-custom-panels';

interface CustomPanelConfig {
  id: string;
  title: string;
  url: string;
  type: 'iframe' | 'api';
  width?: 'normal' | 'wide';
}

function loadCustomPanels(): CustomPanelConfig[] {
  try { return JSON.parse(localStorage.getItem(CUSTOM_PANELS_KEY) || '[]'); } catch { return []; }
}

function saveCustomPanels(panels: CustomPanelConfig[]): void {
  localStorage.setItem(CUSTOM_PANELS_KEY, JSON.stringify(panels));
}

function createCustomPanel(config: CustomPanelConfig): HTMLElement {
  const panel = document.createElement('div');
  panel.className = `panel ${config.width === 'wide' ? 'panel-wide' : ''}`;
  panel.dataset.panel = config.id;

  panel.innerHTML = `
    <div class="panel-header">
      <div class="panel-header-left">
        <span class="panel-title">${config.title}</span>
      </div>
      <button class="custom-panel-remove" data-cpid="${config.id}" title="Remove panel" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:14px;">✕</button>
    </div>
    <div class="panel-content" style="padding:0;overflow:hidden;">
      <iframe src="${config.url}" style="width:100%;height:100%;border:none;background:var(--bg);" loading="lazy" sandbox="allow-scripts allow-same-origin allow-popups"></iframe>
    </div>
  `;

  return panel;
}

function renderCustomPanels(): void {
  const configs = loadCustomPanels();
  const panelGrid = document.getElementById('panelsGrid');
  if (!panelGrid) return;

  // Remove existing custom panels
  panelGrid.querySelectorAll('[data-panel^="custom-"]').forEach(el => el.remove());

  for (const cfg of configs) {
    panelGrid.appendChild(createCustomPanel(cfg));
  }

  // Wire remove buttons
  panelGrid.querySelectorAll<HTMLButtonElement>('.custom-panel-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.cpid;
      const updated = loadCustomPanels().filter(p => p.id !== id);
      saveCustomPanels(updated);
      renderCustomPanels();
    });
  });
}

function showAddCustomPanelDialog(): void {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay active';
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.innerHTML = `
    <div class="modal-header">
      <span class="modal-title">Add Custom Panel</span>
      <button class="modal-close">&times;</button>
    </div>
    <div style="padding:16px;">
      <div class="settings-row">
        <label class="settings-label">Panel Title</label>
        <input class="settings-input" id="cpTitle" placeholder="e.g. Grafana Dashboard" />
      </div>
      <div class="settings-row">
        <label class="settings-label">URL (iframe embed)</label>
        <input class="settings-input" id="cpUrl" placeholder="https://grafana.example.com/d/xxx?orgId=1&kiosk" />
        <div class="settings-hint">Any URL that supports iframe embedding. Works with: Grafana, Kibana, Datadog, Notion, Google Sheets, etc.</div>
      </div>
      <div class="settings-row">
        <label class="settings-label">Width</label>
        <select class="settings-input" id="cpWidth" style="padding:6px 8px;">
          <option value="normal">Normal (1 column)</option>
          <option value="wide">Wide (2 columns)</option>
        </select>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;">
        <button class="settings-cancel-btn" id="cpCancel">Cancel</button>
        <button class="settings-save-btn" id="cpSave">Add Panel</button>
      </div>
    </div>
  `;

  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  modal.querySelector('.modal-close')!.addEventListener('click', () => overlay.remove());
  modal.querySelector('#cpCancel')!.addEventListener('click', () => overlay.remove());
  modal.querySelector('#cpSave')!.addEventListener('click', () => {
    const title = (modal.querySelector('#cpTitle') as HTMLInputElement).value.trim();
    const url = (modal.querySelector('#cpUrl') as HTMLInputElement).value.trim();
    const width = (modal.querySelector('#cpWidth') as HTMLSelectElement).value as 'normal' | 'wide';
    if (!title || !url) return;

    const configs = loadCustomPanels();
    configs.push({ id: `custom-${Date.now()}`, title, url, type: 'iframe', width });
    saveCustomPanels(configs);
    renderCustomPanels();
    overlay.remove();
  });
}

// Mount custom panels (appended to main grid)
renderCustomPanels();

// ============================================================
//  Header clock
// ============================================================
function updateClock(): void {
  const clockEl = document.getElementById('headerClock');
  const dateEl = document.getElementById('headerDate');
  const now = new Date();
  if (clockEl) clockEl.textContent = now.toLocaleTimeString();
  if (dateEl) dateEl.textContent = formatDate(now);
}
updateClock();
setInterval(updateClock, 1000);

// ============================================================
//  Refresh scheduler
// ============================================================
const scheduler = new RefreshScheduler();
scheduler.registerAll([
  { name: 'stocks', fn: () => stockPanel.refresh(), intervalMs: 60_000 },
  { name: 'news', fn: async () => {
    await newsPanel.refresh();
    try {
      const { fetchNews } = await import('./services/news');
      const articles = await fetchNews();
      scanForBreakingNews(articles as any);
    } catch {}
  }, intervalMs: 5 * 60_000 },
  { name: 'map-data', fn: () => refreshMapMarkers(), intervalMs: 10 * 60_000 },
  { name: 'email', fn: () => emailPanel.refresh(), intervalMs: 2 * 60_000 },
  { name: 'feishu', fn: () => feishuPanel.refresh(), intervalMs: 60_000 },
  { name: 'social', fn: () => socialPanel.refresh(), intervalMs: 3 * 60_000 },
  { name: 'code-status', fn: () => codeStatusPanel.refresh(), intervalMs: 2 * 60_000 },
  { name: 'schedule', fn: () => schedulePanel.refresh(), intervalMs: 5 * 60_000 },
  { name: 'finance', fn: () => financePanel.refresh(), intervalMs: 10 * 60_000 },
  { name: 'insights', fn: () => insightsPanel.refresh(), intervalMs: 15 * 60_000 },
  { name: 'weather', fn: () => weatherPanel.refresh(), intervalMs: 30 * 60_000 },
  { name: 'monitors', fn: () => monitorsPanel.refresh(), intervalMs: 5 * 60_000 },
]);

// ============================================================
//  Settings + Command Palette
// ============================================================
document.getElementById('settingsBtn')?.addEventListener('click', openSettings);
document.getElementById('themeToggleBtn')?.addEventListener('click', () => {
  const next = toggleTheme();
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = next === 'dark' ? '◐' : '◑';
});
// Set initial icon
{
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = document.documentElement.dataset.theme === 'dark' ? '◐' : '◑';
}

registerCommands([
  { label: 'Settings', description: 'Open settings modal', action: openSettings, keywords: ['config', 'api', 'key'] },
  { label: 'Add Custom Panel', description: 'Import an external panel via URL', action: showAddCustomPanelDialog, keywords: ['custom', 'import', 'iframe', 'grafana'] },
  { label: 'AI Agent', description: 'Jump to AI agent', action: () => scrollToPanel('insights'), keywords: ['summary', 'briefing', 'agent'] },
  { label: 'Stock Market', description: 'Jump to markets panel', action: () => scrollToPanel('stocks'), keywords: ['market', 'crypto', 'commodity'] },
  { label: 'News', description: 'Jump to news panel', action: () => scrollToPanel('news'), keywords: ['headlines', 'rss'] },
  { label: 'Email', description: 'Jump to email panel', action: () => scrollToPanel('email'), keywords: ['gmail', 'inbox'] },
  { label: 'Schedule', description: 'Jump to schedule panel', action: () => scrollToPanel('schedule'), keywords: ['calendar', 'events'] },
  { label: 'Feishu', description: 'Jump to Feishu panel', action: () => scrollToPanel('feishu'), keywords: ['lark', 'chat'] },
  { label: 'Code Status', description: 'Jump to CI/CD panel', action: () => scrollToPanel('code-status'), keywords: ['github', 'ci'] },
  { label: 'Social', description: 'Jump to community feed', action: () => scrollToPanel('social'), keywords: ['hn', 'reddit'] },
  { label: 'Finance', description: 'Jump to daily finance', action: () => scrollToPanel('finance'), keywords: ['expenses'] },
  { label: 'Map', description: 'Jump to global map', action: () => scrollToPanel('map'), keywords: ['globe', 'world'] },
  { label: 'Weather', description: 'Jump to weather panel', action: () => scrollToPanel('weather'), keywords: ['forecast', 'temperature'] },
  { label: 'Refresh All', description: 'Trigger all panel refreshes', action: () => {
    for (const name of ['stocks', 'news', 'email', 'feishu', 'social', 'code-status', 'schedule', 'finance', 'insights', 'weather', 'monitors']) {
      scheduler.trigger(name);
    }
  }, keywords: ['reload', 'update'] },
]);

function scrollToPanel(id: string): void {
  document.querySelector(`[data-panel="${id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ============================================================
//  Today's Focus sidebar
// ============================================================
async function refreshFocusSidebar(): Promise<void> {
  try {
    const [
      { fetchCalendarEvents },
      { fetchEmails },
      { fetchFeishuMessages },
      { fetchStockQuotes },
      { fetchWorkflowRuns },
      { fetchNews },
    ] = await Promise.all([
      import('./services/schedule'),
      import('./services/email'),
      import('./services/feishu'),
      import('./services/stock-market'),
      import('./services/code-status'),
      import('./services/news'),
    ]);

    const [events, emails, feishuMsgs, stocks, runs, news] = await Promise.all([
      fetchCalendarEvents(), fetchEmails(), fetchFeishuMessages(),
      fetchStockQuotes(), fetchWorkflowRuns(), fetchNews(),
    ]);

    const now = new Date();
    const upcoming = events
      .map(e => ({ ...e, minutesUntil: Math.round((new Date(e.startTime).getTime() - now.getTime()) / 60_000) }))
      .filter(e => e.minutesUntil > -15)
      .sort((a, b) => a.minutesUntil - b.minutesUntil);

    let dailyBriefing: string | undefined;
    try {
      const b = await generateDailyBriefing({
        emails: emails.filter(e => e.unread).map(e => ({ from: e.from, subject: e.subject })),
        events: events.map(e => ({ title: e.title, startTime: e.startTime })),
        news: news.slice(0, 10).map(n => ({ title: n.title, source: n.source })),
        stocks: stocks.map(s => ({ symbol: s.symbol, changePercent: s.changePercent })),
      });
      if (b) dailyBriefing = b;
    } catch {}

    updateTodayFocus({
      nextEvent: upcoming[0] ? { title: upcoming[0].title, startTime: upcoming[0].startTime, minutesUntil: upcoming[0].minutesUntil } : undefined,
      unreadEmails: emails.filter(e => e.unread).length,
      unreadFeishu: feishuMsgs.filter(m => m.unread).length,
      stockAlerts: stocks.filter(s => s.changePercent != null && Math.abs(s.changePercent!) > 2).map(s => ({ symbol: s.symbol, changePercent: s.changePercent! })),
      ciFailures: runs.filter(r => r.conclusion === 'failure').length,
      dailyBriefing,
    });
  } catch (err) {
    console.warn('[Focus] Sidebar update failed:', err);
  }
}

refreshFocusSidebar();
setInterval(refreshFocusSidebar, 60_000);

// ============================================================
//  Map markers from real data
// ============================================================
const SOURCE_COORDS: Record<string, [number, number]> = {
  'BBC World': [-0.12, 51.51], 'BBC': [-0.12, 51.51], 'Reuters': [-73.98, 40.75], 'Reuters World': [-73.98, 40.75],
  'Reuters Business': [-73.98, 40.75], 'AP News': [-73.98, 40.75], 'Al Jazeera': [51.53, 25.29],
  'CNBC': [-74.0, 40.71], 'Bloomberg': [-73.99, 40.72], 'CNN': [-84.39, 33.75],
  'Financial Times': [-0.10, 51.52], 'France 24': [2.35, 48.86], 'DW News': [13.38, 52.52],
  'Hacker News': [-122.42, 37.77], 'TechCrunch': [-122.42, 37.77], 'The Verge': [-73.99, 40.73],
  'Ars Technica': [-73.99, 40.73], 'VentureBeat AI': [-122.42, 37.77], 'Yahoo Finance': [-122.42, 37.77],
  'EuroNews': [4.85, 45.76], 'Guardian World': [-0.12, 51.51], 'SCMP': [114.17, 22.28],
  'Caixin': [121.47, 31.23], 'Nature News': [-0.13, 51.53], 'NPR News': [-77.01, 38.90],
  'Politico': [-77.04, 38.91],
};

async function geolocateUrl(url: string): Promise<[number, number] | null> {
  try {
    const hostname = new URL(url).hostname;
    const resp = await fetch(`https://ipapi.co/${hostname}/json/`);
    if (!resp.ok) return null;
    const data = await resp.json() as any;
    if (data.latitude && data.longitude) return [data.longitude, data.latitude];
  } catch {}
  return null;
}

async function refreshMapMarkers(): Promise<void> {
  const markers: Array<{ id: string; lat: number; lng: number; title: string; type: 'news' | 'schedule' | 'alert' | 'activity'; description?: string; url?: string; color?: string }> = [];
  try {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(pos => {
        mapPanel.addMarker({ id: 'my-location', lat: pos.coords.latitude, lng: pos.coords.longitude, title: 'You are here', type: 'activity', color: '#44ff88', description: 'Current location' });
      }, () => {}, { timeout: 3000 });
    }
    try {
      const { fetchCalendarEvents } = await import('./services/schedule');
      const events = await fetchCalendarEvents();
      for (const ev of events) {
        if (!ev.location || ev.location.includes('http') || ev.location.includes('Meeting')) continue;
        try {
          const geoResp = await fetch(`https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(ev.location)}&format=json&limit=1`, { headers: { 'User-Agent': 'MyDailyMonitor/1.0' } });
          const geoData = await geoResp.json() as any[];
          if (geoData[0]) markers.push({ id: `event-${ev.id}`, lat: parseFloat(geoData[0].lat), lng: parseFloat(geoData[0].lon), title: ev.title, type: 'schedule', description: ev.location, color: '#44ff88' });
        } catch {}
      }
    } catch {}
    try {
      const { fetchNews } = await import('./services/news');
      const articles = await fetchNews();
      const usedSources = new Set<string>();
      for (const a of articles.slice(0, 20)) {
        const coords = SOURCE_COORDS[a.source];
        if (!coords || usedSources.has(a.source)) continue;
        usedSources.add(a.source);
        const isAlert = a.threatLevel === 'critical' || a.threatLevel === 'high';
        markers.push({ id: `news-${a.source}`, lat: coords[1], lng: coords[0], title: `${a.source}: ${a.title}`, type: isAlert ? 'alert' : 'news', description: a.source, url: a.url });
      }
    } catch {}
    try {
      const probes = JSON.parse(localStorage.getItem('mdm-server-probes') || '[]') as string[];
      if (probes.length > 0) {
        const probeResp = await fetch(`/api/system?action=probe&urls=${probes.slice(0, 5).join(',')}`);
        const probeData = await probeResp.json() as any;
        for (const r of (probeData.probes || [])) {
          if (!r.url) continue;
          const coords = await geolocateUrl(r.url);
          if (coords) markers.push({ id: `server-${r.url}`, lat: coords[1], lng: coords[0], title: `${new URL(r.url).hostname} — ${r.ok ? 'UP' : 'DOWN'}`, type: r.ok ? ('server-up' as any) : ('server-down' as any), description: r.ok ? `${r.status} OK · ${r.latencyMs}ms` : (r.error || 'Failed'), url: r.url });
        }
      }
    } catch {}
    mapPanel.setMarkers(markers);
  } catch (err) {
    console.warn('[Map] marker refresh failed:', err);
  }
}

setTimeout(refreshMapMarkers, 5000);

// ============================================================
//  Cleanup
// ============================================================
window.addEventListener('beforeunload', () => {
  scheduler.destroy();
  for (const p of allPanels) p.destroy();
});

console.log(`[MyDailyMonitor] v0.4.0 — ${allPanels.length} panels, 5 sections, custom panel support`);
