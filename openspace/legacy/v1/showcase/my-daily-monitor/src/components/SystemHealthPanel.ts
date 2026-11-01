/**
 * SystemHealthPanel — Real-time system health monitoring
 * Shows CPU, memory as progress bars with color-coded thresholds
 * Displays probe status as colored dots with latency info
 */
import { Panel } from './Panel';
import { fetchSystemHealth, type SystemHealth } from '@/services/system-health';

export class SystemHealthPanel extends Panel {
  private probeUrls: string[] = [
    'https://www.google.com',
    'https://github.com',
    'https://api.github.com',
  ];

  constructor() {
    super({ id: 'system-health', title: 'System Health', showCount: false });
    this.refresh();
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const health = await fetchSystemHealth(this.probeUrls);
      this.render(health);
      this.setDataBadge('live');
    } catch {
      this.showError('Failed to load system health', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private render(health: SystemHealth): void {
    this.content.innerHTML = `
      <div class="health-metrics">
        ${this.renderMetric('CPU', health.cpu, '%')}
        ${this.renderMetric('Memory', health.memoryUsedPercent, '%')}
        ${this.renderUptime(health.uptime)}
      </div>
      <div class="health-probes">
        <div class="health-probes-title">Network Probes</div>
        ${health.probes.map(p => this.renderProbe(p)).join('')}
      </div>
    `;
  }

  private renderMetric(label: string, value: number, unit: string): string {
    const color = this.getMetricColor(value);
    const barColor = this.getBarColor(value);
    
    return `
      <div class="health-metric">
        <div class="health-metric-header">
          <span class="health-metric-label">${label}</span>
          <span class="health-metric-value" style="color: ${color}">${value}${unit}</span>
        </div>
        <div class="health-bar">
          <div class="health-bar-fill" style="width: ${value}%; background: ${barColor}"></div>
        </div>
      </div>
    `;
  }

  private renderUptime(seconds: number): string {
    const formatted = this.formatUptime(seconds);
    return `
      <div class="health-uptime">
        <span class="health-uptime-label">Uptime</span>
        <span class="health-uptime-value">${formatted}</span>
      </div>
    `;
  }

  private renderProbe(probe: { url: string; ok: boolean; latencyMs: number }): string {
    const statusColor = probe.ok ? 'var(--green)' : 'var(--red)';
    const statusText = probe.ok ? 'OK' : 'FAIL';
    const displayUrl = this.formatUrl(probe.url);
    
    return `
      <div class="health-probe">
        <div class="health-probe-dot" style="background: ${statusColor}"></div>
        <div class="health-probe-info">
          <div class="health-probe-url">${displayUrl}</div>
          <div class="health-probe-meta">
            <span class="health-probe-status" style="color: ${statusColor}">${statusText}</span>
            <span class="health-probe-latency">${probe.latencyMs}ms</span>
          </div>
        </div>
      </div>
    `;
  }

  /**
   * Get color for metric value based on thresholds
   * Green: 0-60%, Yellow: 60-80%, Red: 80-100%
   */
  private getMetricColor(value: number): string {
    if (value < 60) return 'var(--green)';
    if (value < 80) return 'var(--yellow)';
    return 'var(--red)';
  }

  /**
   * Get bar fill color based on thresholds
   */
  private getBarColor(value: number): string {
    if (value < 60) return 'linear-gradient(90deg, var(--green), rgba(68, 255, 136, 0.6))';
    if (value < 80) return 'linear-gradient(90deg, var(--yellow), rgba(255, 170, 0, 0.6))';
    return 'linear-gradient(90deg, var(--red), rgba(255, 68, 68, 0.6))';
  }

  /**
   * Format uptime seconds into human-readable string
   */
  private formatUptime(seconds: number): string {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    if (days > 0) {
      return `${days}d ${hours}h ${minutes}m`;
    } else if (hours > 0) {
      return `${hours}h ${minutes}m`;
    } else {
      return `${minutes}m`;
    }
  }

  /**
   * Format URL for display (remove protocol, truncate if too long)
   */
  private formatUrl(url: string): string {
    let display = url.replace(/^https?:\/\//, '').replace(/^www\./, '');
    if (display.length > 30) {
      display = display.substring(0, 27) + '...';
    }
    return display;
  }
}
