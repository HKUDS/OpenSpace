/**
 * AI Summary service — uses OpenRouter API for LLM-based summarization.
 * Works with any OpenRouter-compatible model (default: free Llama 3.1 8B).
 */

import { getSecret, getPreferences } from '@/services/settings-store';

const OPENROUTER_API = 'https://openrouter.ai/api/v1/chat/completions';
const DEFAULT_MODEL = 'minimax/minimax-m2.5';

async function callLLM(systemPrompt: string, userContent: string, maxTokens = 400): Promise<string | null> {
  const apiKey = getSecret('OPENROUTER_API_KEY');
  if (!apiKey) return null;

  const model = getSecret('OPENROUTER_MODEL') || DEFAULT_MODEL;

  try {
    const resp = await fetch(OPENROUTER_API, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
        'HTTP-Referer': 'https://my-daily-monitor.local',
        'X-Title': 'My Daily Monitor',
      },
      body: JSON.stringify({
        model,
        max_tokens: maxTokens,
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: userContent },
        ],
      }),
    });

    if (!resp.ok) return null;
    const data = await resp.json() as any;
    return data.choices?.[0]?.message?.content || null;
  } catch {
    return null;
  }
}

/**
 * Summarize a list of email subjects/snippets into bullet points.
 */
export async function summarizeEmails(emails: Array<{ from: string; subject: string; snippet: string }>): Promise<string | null> {
  if (!getPreferences().aiSummaryEnabled) return null;
  if (emails.length === 0) return null;

  const content = emails.map((e, i) => `${i + 1}. From: ${e.from}\n   Subject: ${e.subject}\n   Preview: ${e.snippet}`).join('\n\n');

  return callLLM(
    'You are a concise email assistant. Summarize the following emails into 3-5 bullet points, highlighting action items and urgency. Use plain text, no markdown.',
    content,
  );
}

/**
 * Summarize news headlines into a brief overview.
 */
export async function summarizeNews(articles: Array<{ title: string; source: string }>): Promise<string | null> {
  if (!getPreferences().aiSummaryEnabled) return null;
  if (articles.length === 0) return null;

  const content = articles.map((a, i) => `${i + 1}. [${a.source}] ${a.title}`).join('\n');

  return callLLM(
    'You are a news briefing assistant. Summarize the top stories into a concise 2-3 sentence morning brief. Focus on the most impactful items. Use plain text.',
    content,
    300,
  );
}

/**
 * Summarize Feishu meeting minutes / messages.
 */
export async function summarizeFeishuMessages(messages: Array<{ chatName: string; senderName: string; content: string }>): Promise<string | null> {
  if (!getPreferences().aiSummaryEnabled) return null;
  if (messages.length === 0) return null;

  const content = messages.map((m, i) => `${i + 1}. [${m.chatName}] ${m.senderName}: ${m.content}`).join('\n');

  return callLLM(
    'You are a workplace communication assistant. Summarize the following Feishu messages into key action items and highlights. Use plain text bullet points.',
    content,
  );
}

/**
 * Analyze a process that just exited — what happened, success/failure, root cause.
 */
export async function analyzeProcessExit(info: {
  label: string;
  command: string;
  duration: string;
  cpu: number;
  mem: number;
  tailOutput?: string;
}): Promise<string | null> {
  const apiKey = getSecret('OPENROUTER_API_KEY');
  if (!apiKey) return null;

  const model = getSecret('OPENROUTER_MODEL') || DEFAULT_MODEL;

  const parts = [
    `Process: ${info.label}`,
    `Command: ${info.command}`,
    `Duration: ${info.duration}`,
    `Last CPU: ${info.cpu.toFixed(1)}%  Last MEM: ${info.mem.toFixed(1)}%`,
  ];
  if (info.tailOutput) {
    parts.push(`Last output (tail):\n${info.tailOutput}`);
  }

  try {
    const resp = await fetch(OPENROUTER_API, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
        'HTTP-Referer': 'https://my-daily-monitor.local',
        'X-Title': 'My Daily Monitor',
      },
      body: JSON.stringify({
        model,
        max_tokens: 200,
        messages: [
          { role: 'system', content: 'You are a DevOps assistant. A monitored process just exited. Analyze it in 2-3 concise sentences: was it likely successful or did it crash? Any notable observations from the metrics or output? Use plain text, no markdown.' },
          { role: 'user', content: parts.join('\n') },
        ],
      }),
    });
    if (!resp.ok) return null;
    const data = await resp.json() as any;
    return data.choices?.[0]?.message?.content || null;
  } catch {
    return null;
  }
}

/**
 * Generate a comprehensive morning daily briefing from all data sources.
 */
export async function generateDailyBriefing(data: {
  emails: Array<{ from: string; subject: string }>;
  events: Array<{ title: string; startTime: string }>;
  news: Array<{ title: string; source: string }>;
  stocks: Array<{ symbol: string; changePercent: number | null }>;
}): Promise<string | null> {
  if (!getPreferences().aiSummaryEnabled) return null;

  const sections: string[] = [];

  if (data.events.length > 0) {
    sections.push('SCHEDULE:\n' + data.events.map(e => `- ${e.title} at ${new Date(e.startTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`).join('\n'));
  }

  if (data.emails.length > 0) {
    sections.push('UNREAD EMAILS:\n' + data.emails.slice(0, 5).map(e => `- ${e.from}: ${e.subject}`).join('\n'));
  }

  if (data.stocks.length > 0) {
    const movers = data.stocks.filter(s => s.changePercent != null && Math.abs(s.changePercent) > 1);
    if (movers.length > 0) {
      sections.push('MARKET MOVERS:\n' + movers.map(s => `- ${s.symbol}: ${s.changePercent! >= 0 ? '+' : ''}${s.changePercent!.toFixed(1)}%`).join('\n'));
    }
  }

  if (data.news.length > 0) {
    sections.push('TOP NEWS:\n' + data.news.slice(0, 5).map(n => `- [${n.source}] ${n.title}`).join('\n'));
  }

  const content = sections.join('\n\n');

  return callLLM(
    'You are a personal morning briefing assistant. Based on the data below, write a concise 3-4 sentence morning briefing that helps the user plan their day. Mention the most important meetings, urgent emails, significant market moves, and breaking news. Use plain text.',
    content,
    400,
  );
}

