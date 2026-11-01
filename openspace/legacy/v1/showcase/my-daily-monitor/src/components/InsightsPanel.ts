/**
 * AI Agent Panel — Smart AI coworker with tool routing.
 *
 * Key improvements over previous version:
 *  1. Smart context routing — only fetches data relevant to the question
 *  2. ALL data sources covered (weather, schedule, email, stocks, news, etc.)
 *  3. Real web search via news API
 *  4. Better task/content generation with structured output
 */
import { Panel } from './Panel';
import { escapeHtml } from '@/utils';
import { getSecret } from '@/services/settings-store';

// ---- Types ----
interface AgentMessage {
  role: 'user' | 'agent' | 'system' | 'tool';
  content: string;
  timestamp: number;
  toolName?: string;
}

type TaskStatus = 'pending' | 'done' | 'failed';
interface AgentTask {
  id: string;
  title: string;
  content: string;
  status: TaskStatus;
  createdAt: string;
}

// ---- Constants ----
const OPENROUTER_API = 'https://openrouter.ai/api/v1/chat/completions';
const DEFAULT_MODEL = 'minimax/minimax-m2.5';
const HISTORY_KEY = 'mdm-agent-history';
const TASKS_KEY = 'mdm-agent-tasks';
const MAX_HISTORY = 20;

const QUICK_ACTIONS = [
  { label: '📊 Daily Briefing',   prompt: 'Give me a full daily briefing covering weather, schedule, news highlights, stock market, and anything urgent.' },
  { label: '🌤 Weather',          prompt: 'What\'s the weather like today and the next few days?' },
  { label: '📈 Stock Analysis',   prompt: 'Analyze my stock watchlist — biggest movers, trends, risks.' },
  { label: '🌍 News Summary',     prompt: 'Summarize the top news stories and trending tech community posts.' },
  { label: '🖥 System Check',     prompt: 'Check my running processes, terminal sessions, server health, and system CPU/memory.' },
  { label: '📋 View Tasks',       prompt: '/tasks' },
];

const SYSTEM_PROMPT = `You are an AI coworker agent inside a personal monitoring dashboard. You have access to REAL data from the user's dashboard provided as context.

CAPABILITIES:
- Answer questions using real dashboard data (weather, stocks, news, schedule, processes, servers, etc.)
- Execute Python code on the server to CREATE FILES (PPT, reports, data analysis)
- Search the web via news feeds for current events
- Monitor system status (CPU, memory, processes, terminals)

TASK EXECUTION:
When the user asks you to create something (PPT, report, document, analysis), you MUST generate executable Python code.
Wrap your code in a \`\`\`python code block. The code will be executed on the server.

Available Python libraries: json, csv, os, datetime, math, statistics, collections.
For PPTs: use python-pptx (from pptx import Presentation).
For Excel: use openpyxl.
For PDFs: use reportlab or just write HTML.

The code should:
1. Create the file in /tmp/agent-output/
2. Print the output file path at the end

Example for PPT:
\`\`\`python
from pptx import Presentation
from pptx.util import Inches, Pt
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Title"
slide.placeholders[1].text = "Content"
prs.save("/tmp/agent-output/report.pptx")
print("/tmp/agent-output/report.pptx")
\`\`\`

RULES:
1. ONLY use data from the provided context — never make up numbers or facts
2. Be concise but actionable. Use bullet points and emoji for clarity.
3. Mark urgency: 🔴 critical, 🟡 important, 🟢 normal
4. When asked to CREATE something, always generate executable Python code
5. Reply in the SAME language the user uses
6. If data is missing, say so honestly
7. When the context says "not configured", tell the user what to set up in Settings`;

// ---- Tool definitions for smart routing ----
interface ToolDef {
  name: string;
  keywords: string[];
  fetch: () => Promise<string>;
}

async function fetchTool(url: string): Promise<any> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

const TOOLS: ToolDef[] = [
  {
    name: 'weather',
    keywords: ['weather', 'temperature', 'rain', 'forecast', 'wind', '天气', '温度', '下雨', '预报', 'briefing', '简报'],
    fetch: async () => {
      try {
        // Open-Meteo with browser geolocation (fallback to Beijing)
        const pos = await new Promise<GeolocationPosition>((resolve, reject) => {
          navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 3000 });
        }).catch(() => null);
        const lat = pos?.coords.latitude ?? 39.9;
        const lng = pos?.coords.longitude ?? 116.4;
        const resp = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lng}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code,is_day&daily=temperature_2m_max,temperature_2m_min,weather_code&timezone=auto&forecast_days=4`);
        const data = await resp.json() as any;
        const c = data.current;
        const daily = data.daily;
        let result = `WEATHER (${lat.toFixed(1)}, ${lng.toFixed(1)}):\n`;
        result += `Now: ${c.temperature_2m}°C, humidity ${c.relative_humidity_2m}%, wind ${c.wind_speed_10m}km/h\n`;
        if (daily) {
          result += 'Forecast:\n' + daily.time.map((d: string, i: number) =>
            `- ${d}: ${daily.temperature_2m_min[i]}~${daily.temperature_2m_max[i]}°C`
          ).join('\n');
        }
        return result;
      } catch { return 'WEATHER: Unable to fetch (location access denied or API error)'; }
    },
  },
  {
    name: 'news',
    keywords: ['news', 'headline', 'article', 'world', 'tech', 'finance', 'AI', '新闻', '头条', '资讯', 'briefing', '简报', 'summary', '总结'],
    fetch: async () => {
      try {
        const { fetchNews } = await import('@/services/news');
        const news = await fetchNews();
        if (news.length === 0) return 'NEWS: No articles available';
        const critical = news.filter(n => n.threatLevel === 'critical' || n.threatLevel === 'high');
        return `NEWS (${news.length} articles${critical.length > 0 ? `, 🔴 ${critical.length} critical` : ''}):\n` +
          news.slice(0, 12).map(n =>
            `- [${n.source}] ${n.title}${n.threatLevel && n.threatLevel !== 'info' ? ` ⚠${n.threatLevel.toUpperCase()}` : ''}`
          ).join('\n');
      } catch { return 'NEWS: Failed to fetch'; }
    },
  },
  {
    name: 'stocks',
    keywords: ['stock', 'market', 'price', 'share', 'nasdaq', 'sp500', '股', '股票', '市场', 'portfolio', 'briefing', '简报'],
    fetch: async () => {
      try {
        const { fetchStockQuotes } = await import('@/services/stock-market');
        const stocks = await fetchStockQuotes();
        if (stocks.length === 0) return 'STOCKS: No watchlist configured (add in Settings)';
        return 'STOCKS:\n' + stocks.map(s =>
          `- ${s.symbol}${s.name ? ` (${s.name})` : ''}: $${s.price?.toFixed(2) || '?'} ${(s.changePercent ?? 0) >= 0 ? '📈' : '📉'} ${(s.changePercent ?? 0) >= 0 ? '+' : ''}${(s.changePercent ?? 0).toFixed(2)}%`
        ).join('\n');
      } catch { return 'STOCKS: Failed to fetch'; }
    },
  },
  {
    name: 'community',
    keywords: ['hn', 'reddit', 'hacker news', 'community', 'trending', 'hot', '社区', '热帖', 'briefing'],
    fetch: async () => {
      try {
        const { fetchCommunityPosts } = await import('@/services/social');
        const posts = await fetchCommunityPosts('all');
        if (posts.length === 0) return 'COMMUNITY: No posts available';
        return 'TECH COMMUNITY:\n' + posts.slice(0, 10).map(p =>
          `- [${p.platform.toUpperCase()}] ${p.title} (${p.score}pts, ${p.comments}💬)`
        ).join('\n');
      } catch { return 'COMMUNITY: Failed to fetch'; }
    },
  },
  {
    name: 'schedule',
    keywords: ['schedule', 'calendar', 'meeting', 'event', 'today', '日程', '会议', '日历', 'briefing', '简报'],
    fetch: async () => {
      try {
        const { fetchCalendarResult } = await import('@/services/schedule');
        const result = await fetchCalendarResult();
        if (!result.configured) return 'SCHEDULE: Google Calendar not configured (enable in Settings → API Keys)';
        if (result.events.length === 0) return 'SCHEDULE: No events today';
        return 'TODAY\'S SCHEDULE:\n' + result.events.map(e =>
          `- ${new Date(e.startTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} ${e.title}${e.location ? ` @ ${e.location}` : ''}`
        ).join('\n');
      } catch { return 'SCHEDULE: Failed to fetch'; }
    },
  },
  {
    name: 'email',
    keywords: ['email', 'mail', 'inbox', 'unread', '邮件', '收件箱', 'briefing', '简报'],
    fetch: async () => {
      try {
        const { fetchEmailResult } = await import('@/services/email');
        const result = await fetchEmailResult();
        if (!result.configured) return 'EMAIL: Not configured (add Gmail/Outlook credentials in Settings)';
        const emails = result.emails;
        const unread = emails.filter(e => e.unread);
        return `EMAIL (${unread.length} unread / ${emails.length} total):\n` +
          emails.slice(0, 8).map(e =>
            `- ${e.unread ? '🔵' : '⚪'} ${e.from}: ${e.subject}`
          ).join('\n');
      } catch { return 'EMAIL: Failed to fetch'; }
    },
  },
  {
    name: 'processes',
    keywords: ['process', 'cpu', 'memory', 'running', 'terminal', 'system', 'server', '进程', '终端', '系统', '服务器', 'health'],
    fetch: async () => {
      const parts: string[] = [];
      // System health
      try {
        const { fetchSystemHealth } = await import('@/services/system-health');
        const health = await fetchSystemHealth();
        parts.push(`SYSTEM: CPU ${health.cpu.toFixed(1)}%, MEM ${health.memoryUsedPercent.toFixed(1)}%, uptime ${Math.floor(health.uptime / 3600)}h`);
      } catch {}
      // Running processes
      try {
        const data = await fetchTool('/api/system?action=jobs');
        const jobs = data.jobs || [];
        if (jobs.length > 0) {
          parts.push(`PROCESSES (${jobs.length}):\n` + jobs.slice(0, 8).map((j: any) =>
            `- ${j.label}: CPU ${j.cpu.toFixed(1)}%, MEM ${j.mem.toFixed(1)}%, ${j.duration || j.elapsed}`
          ).join('\n'));
        }
      } catch {}
      // Terminals
      try {
        const data = await fetchTool('/api/system?action=terminals&lines=3');
        const terms = data.terminals || [];
        const active = terms.filter((t: any) => t.isActive);
        if (terms.length > 0) {
          parts.push(`TERMINALS (${active.length} active / ${terms.length}):\n` + terms.slice(0, 6).map((t: any) =>
            `- #${t.termId} [${t.isActive ? 'ACTIVE' : 'IDLE'}] ${t.activeCommand || t.lastCommand || '(empty)'}`
          ).join('\n'));
        }
      } catch {}
      // Server probes
      try {
        const probes = JSON.parse(localStorage.getItem('mdm-server-probes') || '[]') as string[];
        if (probes.length > 0) {
          const data = await fetchTool(`/api/system?action=probe&urls=${probes.join(',')}`);
          parts.push('SERVERS:\n' + (data.probes || []).map((r: any) =>
            `- ${r.url}: ${r.ok ? `✅ UP (${r.latencyMs}ms)` : '❌ DOWN'}`
          ).join('\n'));
        }
      } catch {}
      return parts.join('\n\n') || 'SYSTEM: No data available';
    },
  },
  {
    name: 'github',
    keywords: ['github', 'repo', 'ci', 'build', 'deploy', 'workflow', 'code', '代码', '部署'],
    fetch: async () => {
      try {
        const { fetchCodeStatusResult } = await import('@/services/code-status');
        const result = await fetchCodeStatusResult();
        if (!result.configured) return 'GITHUB: Not configured (add GitHub PAT in Settings)';
        const runs = result.runs;
        if (runs.length === 0) return 'GITHUB: No recent workflow runs';
        return 'GITHUB CI/CD:\n' + runs.slice(0, 8).map(r =>
          `- ${r.repo}: ${r.status === 'completed' ? (r.conclusion === 'success' ? '✅' : '❌') : '🔄'} ${r.name} (${r.conclusion || r.status})`
        ).join('\n');
      } catch { return 'GITHUB: Failed to fetch'; }
    },
  },
  {
    name: 'search',
    keywords: ['search', 'find', 'look up', 'what is', '搜索', '查找', '什么是', 'latest', '最新'],
    fetch: async () => 'WEB_SEARCH: ready (will use query from user message)',
  },
];

// ---- Persistence ----
function loadHistory(): AgentMessage[] {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; }
}
function saveHistory(msgs: AgentMessage[]): void {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(msgs.slice(-MAX_HISTORY)));
}
function loadTasks(): AgentTask[] {
  try { return JSON.parse(localStorage.getItem(TASKS_KEY) || '[]'); } catch { return []; }
}
function saveTasks(tasks: AgentTask[]): void {
  localStorage.setItem(TASKS_KEY, JSON.stringify(tasks.slice(-30)));
}

// ---- Panel ----
export class InsightsPanel extends Panel {
  private messages: AgentMessage[] = [];
  private tasks: AgentTask[] = [];
  private chatEl: HTMLElement | null = null;
  private inputEl: HTMLInputElement | null = null;
  private isProcessing = false;

  constructor() {
    super({ id: 'insights', title: 'AI Agent', showCount: false, className: 'panel-wide' });
    this.content.style.padding = '0';
    this.content.style.display = 'flex';
    this.content.style.flexDirection = 'column';
    this.messages = loadHistory();
    this.tasks = loadTasks();
    this.buildUI();
  }

  private buildUI(): void {
    this.content.innerHTML = '';
    this.chatEl = document.createElement('div');
    this.chatEl.className = 'agent-chat';
    this.content.appendChild(this.chatEl);

    const inputBar = document.createElement('div');
    inputBar.className = 'agent-input-bar';
    this.inputEl = document.createElement('input');
    this.inputEl.className = 'agent-input';
    this.inputEl.placeholder = 'Ask anything — weather, stocks, news, or /task to create content...';
    this.inputEl.addEventListener('keypress', e => { if (e.key === 'Enter') this.handleSend(); });
    const sendBtn = document.createElement('button');
    sendBtn.className = 'agent-send-btn';
    sendBtn.textContent = '→';
    sendBtn.addEventListener('click', () => this.handleSend());
    inputBar.appendChild(this.inputEl);
    inputBar.appendChild(sendBtn);
    this.content.appendChild(inputBar);
    this.renderChat();
  }

  private renderChat(): void {
    if (!this.chatEl) return;
    if (this.messages.length === 0) {
      this.chatEl.innerHTML = `
        <div class="agent-welcome">
          <div class="agent-welcome-title">🤖 AI Agent</div>
          <div class="agent-welcome-desc">
            Your personal command center — ask me anything.<br>
            <span style="color:var(--text-muted);font-size:11px;">Stocks · News · Weather · Email · Schedule · GitHub · Servers — all at your fingertips.</span>
            ${!getSecret('OPENROUTER_API_KEY') ? '<br><span style="color:var(--yellow);font-size:11px;">⚠ Add OpenRouter API key in Settings to unlock AI.</span>' : ''}
          </div>
          <div class="agent-quick-actions">
            ${QUICK_ACTIONS.map((a, i) => `<button class="agent-quick-btn" data-qidx="${i}">${a.label}</button>`).join('')}
          </div>
        </div>`;
      this.wireQuickActions();
      return;
    }
    const msgs = this.messages.slice(-MAX_HISTORY);
    const html = msgs.map(m => {
      if (m.role === 'user') return `<div class="agent-msg agent-msg-user"><div class="agent-msg-content">${escapeHtml(m.content)}</div></div>`;
      if (m.role === 'tool') return `<div class="agent-msg agent-msg-tool"><span class="agent-tool-label">🔧 ${escapeHtml(m.toolName || 'tool')}</span><div class="agent-msg-content">${escapeHtml(m.content).slice(0, 300)}${m.content.length > 300 ? '...' : ''}</div></div>`;
      if (m.role === 'system') return `<div class="agent-msg agent-msg-system"><div class="agent-msg-content">${escapeHtml(m.content)}</div></div>`;
      return `<div class="agent-msg agent-msg-agent"><span class="agent-avatar">🤖</span><div class="agent-msg-content">${escapeHtml(m.content)}</div></div>`;
    }).join('');
    const quickHtml = `<div class="agent-quick-actions" style="margin-top:8px">${QUICK_ACTIONS.map((a, i) => `<button class="agent-quick-btn" data-qidx="${i}">${a.label}</button>`).join('')}</div>`;
    this.chatEl.innerHTML = html + quickHtml;
    this.wireQuickActions();
    this.chatEl.scrollTop = this.chatEl.scrollHeight;
  }

  private wireQuickActions(): void {
    this.chatEl?.querySelectorAll<HTMLButtonElement>('[data-qidx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.qidx!, 10);
        if (QUICK_ACTIONS[idx]) this.sendMessage(QUICK_ACTIONS[idx].prompt);
      });
    });
  }

  private handleSend(): void {
    if (!this.inputEl || !this.inputEl.value.trim() || this.isProcessing) return;
    const text = this.inputEl.value.trim();
    this.inputEl.value = '';
    this.sendMessage(text);
  }

  // ======================== Smart tool routing ========================
  private selectTools(query: string): ToolDef[] {
    const q = query.toLowerCase();

    // If it's a general briefing, select core tools
    if (q.includes('briefing') || q.includes('简报') || q.includes('daily')) {
      return TOOLS.filter(t => ['weather', 'schedule', 'news', 'stocks', 'email'].includes(t.name));
    }

    // Match tools by keywords
    const matched = TOOLS.filter(t => t.keywords.some(kw => q.includes(kw)));

    // If nothing matched, select a minimal set
    if (matched.length === 0) {
      // For task/content creation, just give news + stocks for context
      if (q.startsWith('/task') || q.includes('create') || q.includes('make') || q.includes('做') || q.includes('写') || q.includes('生成')) {
        return TOOLS.filter(t => ['news', 'stocks'].includes(t.name));
      }
      // For general questions, give a broad context
      return TOOLS.filter(t => ['news', 'stocks', 'weather'].includes(t.name));
    }

    return matched;
  }

  // ======================== Message handling ========================
  private async sendMessage(text: string): Promise<void> {
    if (this.isProcessing) return;
    this.isProcessing = true;

    // Commands
    if (text === '/tasks') {
      this.messages.push({ role: 'user', content: text, timestamp: Date.now() });
      this.showTaskList();
      this.isProcessing = false;
      return;
    }
    if (text === '/clear') {
      this.messages = [];
      saveHistory(this.messages);
      this.isProcessing = false;
      this.renderChat();
      return;
    }

    this.messages.push({ role: 'user', content: text, timestamp: Date.now() });

    // Step 1: Select relevant tools
    const tools = this.selectTools(text);
    const toolNames = tools.map(t => t.name).join(', ');
    this.messages.push({ role: 'system', content: `⏳ Fetching: ${toolNames}...`, timestamp: Date.now() });
    this.renderChat();

    try {
      // Step 2: Execute selected tools in parallel
      const results = await Promise.allSettled(tools.map(async t => {
        const data = await t.fetch();
        return { name: t.name, data };
      }));
      this.messages.pop(); // remove "fetching..." message

      const contextParts: string[] = [];
      for (const r of results) {
        if (r.status === 'fulfilled' && r.value.data) {
          contextParts.push(r.value.data);
        }
      }

      // Web search if needed
      const q = text.toLowerCase();
      if (q.includes('search') || q.includes('搜索') || q.includes('latest') || q.includes('最新') || q.includes('find')) {
        try {
          const searchQuery = text.replace(/^.*?(search|搜索|find|查找)\s*/i, '').trim() || text;
          const { searchNews } = await import('@/services/news');
          const results = await searchNews(searchQuery);
          if (results.length > 0) {
            contextParts.push('WEB SEARCH:\n' + results.slice(0, 5).map(a => `- [${a.source}] ${a.title} (${a.url})`).join('\n'));
          }
        } catch {}
      }

      const context = contextParts.join('\n\n');

      // Show tool results
      if (context) {
        this.messages.push({ role: 'tool', content: context, toolName: toolNames, timestamp: Date.now() });
        this.renderChat();
      }

      // Step 3: Call LLM
      const apiKey = getSecret('OPENROUTER_API_KEY');
      if (!apiKey) {
        this.messages.push({
          role: 'agent',
          content: context
            ? `Here's the data I gathered:\n\n${context}\n\n💡 Add an OpenRouter API key in Settings → API Keys → AI section to get intelligent analysis.`
            : '⚠ No API key configured and no relevant data found. Go to Settings → API Keys → AI to add an OpenRouter key.',
          timestamp: Date.now(),
        });
      } else {
        const model = getSecret('OPENROUTER_MODEL') || DEFAULT_MODEL;
        const isTask = text.startsWith('/task');
        const taskPrompt = isTask
          ? '\n\nThe user wants you to CREATE CONTENT. Produce complete, usable output (not just an outline). If it\'s a PPT, write full slide content with titles, bullet points, and speaker notes.'
          : '';

        const resp = await fetch(OPENROUTER_API, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${apiKey}`,
            'HTTP-Referer': 'https://my-daily-monitor.local',
            'X-Title': 'My Daily Monitor Agent',
          },
          body: JSON.stringify({
            model,
            max_tokens: isTask ? 1500 : 800,
            messages: [
              { role: 'system', content: SYSTEM_PROMPT + taskPrompt },
              { role: 'user', content: `Context data:\n${context || 'No data available.'}\n\nUser: ${text.replace(/^\/task\s*/i, '')}` },
            ],
          }),
        });

        if (!resp.ok) throw new Error(`LLM API ${resp.status}`);
        const data = await resp.json() as any;
        const reply = data.choices?.[0]?.message?.content || 'No response from AI.';

        // Detect Python code blocks and execute them
        const codeMatch = reply.match(/```python\n([\s\S]*?)```/);
        if (codeMatch) {
          this.messages.push({ role: 'agent', content: reply, timestamp: Date.now() });
          this.renderChat();

          // Execute the code on the server
          const code = codeMatch[1];
          this.messages.push({ role: 'system', content: '⚙️ Executing Python code...', timestamp: Date.now() });
          this.renderChat();

          try {
            const execResp = await fetch('/api/system?action=exec', {
              method: 'POST',
              headers: { 'Content-Type': 'text/plain' },
              body: code,
            });
            const execResult = await execResp.json() as any;
            this.messages.pop(); // remove "executing..."

            if (execResult.success) {
              const output = execResult.stdout || '';
              const filePath = output.trim().split('\n').pop() || '';
              this.messages.push({
                role: 'tool',
                content: `✅ Code executed successfully.\n${output}${execResult.stderr ? `\nWarnings: ${execResult.stderr}` : ''}`,
                toolName: 'execute_python',
                timestamp: Date.now(),
              });
              // Save as task
              const taskTitle = isTask ? text.replace(/^\/task\s*/i, '').slice(0, 60) : 'Code execution';
              this.tasks.unshift({ id: `t-${Date.now()}`, title: taskTitle, content: `Output: ${filePath || output}`, status: 'done', createdAt: new Date().toISOString() });
              saveTasks(this.tasks);
            } else {
              this.messages.push({
                role: 'tool',
                content: `❌ Execution failed:\n${execResult.stderr || execResult.error || 'Unknown error'}`,
                toolName: 'execute_python',
                timestamp: Date.now(),
              });
              if (isTask) {
                this.tasks.unshift({ id: `t-${Date.now()}`, title: text.slice(0, 60), content: `Failed: ${execResult.error || 'execution error'}`, status: 'failed', createdAt: new Date().toISOString() });
                saveTasks(this.tasks);
              }
            }
          } catch (execErr: any) {
            this.messages.pop();
            this.messages.push({ role: 'tool', content: `❌ Could not execute: ${execErr.message}`, toolName: 'execute_python', timestamp: Date.now() });
          }
        } else {
          // Normal text response (no code)
          if (isTask) {
            const taskTitle = text.replace(/^\/task\s*/i, '').slice(0, 60);
            this.tasks.unshift({ id: `t-${Date.now()}`, title: taskTitle, content: reply, status: 'done', createdAt: new Date().toISOString() });
            saveTasks(this.tasks);
          }
          this.messages.push({ role: 'agent', content: reply, timestamp: Date.now() });
        }
      }
    } catch (err: any) {
      if (this.messages[this.messages.length - 1]?.role === 'system') this.messages.pop();
      this.messages.push({ role: 'agent', content: `⚠️ Error: ${err.message}`, timestamp: Date.now() });
    }

    saveHistory(this.messages);
    this.isProcessing = false;
    this.renderChat();
  }

  private showTaskList(): void {
    if (this.tasks.length === 0) {
      this.messages.push({ role: 'agent', content: '📋 No tasks yet. Try: /task Create a summary of today\'s news', timestamp: Date.now() });
    } else {
      const icons: Record<TaskStatus, string> = { pending: '⏳', done: '✅', failed: '❌' };
      const list = this.tasks.slice(0, 10).map((t, i) =>
        `${i + 1}. ${icons[t.status]} ${t.title}\n   ${new Date(t.createdAt).toLocaleString()}\n   ${t.content.slice(0, 80)}${t.content.length > 80 ? '...' : ''}`
      ).join('\n\n');
      this.messages.push({ role: 'agent', content: `📋 Tasks (${this.tasks.length}):\n\n${list}`, timestamp: Date.now() });
    }
    saveHistory(this.messages);
    this.renderChat();
  }

  async refresh(): Promise<void> { /* Interactive panel, no auto-refresh */ }
}
