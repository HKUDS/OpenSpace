/**
 * System Health API — CPU, memory, uptime, and configurable URL probes.
 * Uses Node.js os module for system metrics and fetch for health checks.
 */
import os from 'node:os';
import type { IncomingHttpHeaders } from 'node:http';

interface ProbeResult {
  url: string;
  ok: boolean;
  latencyMs: number;
}

interface HealthResponse {
  cpu: number;
  memoryUsedPercent: number;
  uptime: number;
  probes: ProbeResult[];
}

// Default probe URLs if none provided
const DEFAULT_PROBES = [
  'https://www.google.com',
  'https://github.com',
  'https://api.github.com',
];

/**
 * Ping a URL with timeout and measure latency
 */
async function probeUrl(url: string, timeoutMs = 3000): Promise<ProbeResult> {
  const startTime = Date.now();
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);

    const response = await fetch(url, {
      method: 'HEAD',
      signal: controller.signal,
      headers: { 'User-Agent': 'SystemHealthMonitor/1.0' },
    });

    clearTimeout(timeout);
    const latencyMs = Date.now() - startTime;

    return {
      url,
      ok: response.ok,
      latencyMs,
    };
  } catch (error) {
    const latencyMs = Date.now() - startTime;
    return {
      url,
      ok: false,
      latencyMs,
    };
  }
}

/**
 * Calculate CPU load average as percentage (0-100)
 * Uses 1-minute load average normalized by CPU count
 */
function getCpuLoad(): number {
  const loadAvg = os.loadavg()[0]; // 1-minute average
  const cpuCount = os.cpus().length;
  const loadPercent = (loadAvg / cpuCount) * 100;
  return Math.min(Math.round(loadPercent), 100);
}

/**
 * Calculate memory usage percentage
 */
function getMemoryUsage(): number {
  const total = os.totalmem();
  const free = os.freemem();
  const used = total - free;
  const usedPercent = (used / total) * 100;
  return Math.round(usedPercent);
}

/**
 * Get system uptime in seconds
 */
function getSystemUptime(): number {
  return Math.round(os.uptime());
}

export async function handleHealthRequest(
  query: Record<string, string>,
  _body: string,
  _headers: IncomingHttpHeaders,
): Promise<HealthResponse> {
  // Parse probe URLs from query params (comma-separated)
  const probeUrlsRaw = query.probes || DEFAULT_PROBES.join(',');
  const probeUrls = probeUrlsRaw
    .split(',')
    .map(u => u.trim())
    .filter(Boolean);

  // Collect system metrics
  const cpu = getCpuLoad();
  const memoryUsedPercent = getMemoryUsage();
  const uptime = getSystemUptime();

  // Run all probes in parallel
  const probeResults = await Promise.all(
    probeUrls.map(url => probeUrl(url, 3000))
  );

  return {
    cpu,
    memoryUsedPercent,
    uptime,
    probes: probeResults,
  };
}
