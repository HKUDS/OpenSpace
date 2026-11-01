import { createCircuitBreaker } from '@/utils/circuit-breaker';

export interface ProbeStatus {
  url: string;
  ok: boolean;
  latencyMs: number;
}

export interface SystemHealth {
  cpu: number;
  memoryUsedPercent: number;
  uptime: number;
  probes: ProbeStatus[];
}

const healthBreaker = createCircuitBreaker<SystemHealth>({
  name: 'SystemHealth',
  cacheTtlMs: 10_000, // 10 second cache
});

/**
 * Fetch system health metrics: CPU, memory, uptime, and URL probes.
 * Wrapped with circuit breaker for resilience.
 * @param probeUrls Optional array of URLs to probe (default: GitHub, Google, etc.)
 */
export async function fetchSystemHealth(probeUrls?: string[]): Promise<SystemHealth> {
  const probesParam = probeUrls?.join(',') || '';
  const queryString = probesParam ? `?probes=${encodeURIComponent(probesParam)}` : '';

  return healthBreaker.execute(async () => {
    const resp = await fetch(`/api/health${queryString}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    
    return {
      cpu: data.cpu ?? 0,
      memoryUsedPercent: data.memoryUsedPercent ?? 0,
      uptime: data.uptime ?? 0,
      probes: (data.probes || []).map((p: any) => ({
        url: p.url || '',
        ok: p.ok ?? false,
        latencyMs: p.latencyMs ?? 0,
      })),
    };
  }, {
    cpu: 0,
    memoryUsedPercent: 0,
    uptime: 0,
    probes: [],
  });
}
