import { Panel } from './Panel';
import { fetchCalendarResult, type CalendarEvent } from '@/services/schedule';

export class SchedulePanel extends Panel {
  constructor() {
    super({ id: 'schedule', title: 'Schedule', showCount: true });
    this.refresh();
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const result = await fetchCalendarResult();
      if (!result.configured) {
        this.setContent(`<div class="panel-empty">${result.error || 'Google Calendar not configured.'}<br><br><small>Click <b>⚙ Settings</b> in the header to configure.</small></div>`);
        this.setDataBadge('unavailable');
        return;
      }
      if (result.error) {
        this.showError(result.error, () => this.refresh());
        return;
      }
      if (result.events.length === 0) {
        this.setContent('<div class="panel-empty">No events today</div>');
        this.setDataBadge('live');
        this.setCount(0);
        return;
      }
      this.render(result.events);
      this.setCount(result.events.length);
      this.setDataBadge('live');
    } catch {
      this.showError('Failed to load schedule', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private render(events: CalendarEvent[]): void {
    const now = new Date();
    const rows = events.map(ev => {
      const start = new Date(ev.startTime);
      const end = new Date(ev.endTime);
      const isNow = now >= start && now <= end;
      const timeStr = ev.isAllDay ? 'All day' : `${start.getHours().toString().padStart(2, '0')}:${start.getMinutes().toString().padStart(2, '0')}`;
      return `
        <div class="schedule-item ${isNow ? 'schedule-now' : ''}">
          <span class="schedule-time">${timeStr}</span>
          <div class="schedule-info">
            <div class="schedule-title">${ev.title}</div>
            ${ev.location ? `<div class="schedule-location">${ev.location}</div>` : ''}
            ${ev.meetingLink ? `<a href="${ev.meetingLink}" target="_blank" class="schedule-link">Join meeting →</a>` : ''}
          </div>
        </div>`;
    }).join('');
    this.setContent(`<div class="schedule-list">${rows}</div>`);
  }
}
