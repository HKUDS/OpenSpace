/**
 * LiveNewsPanel — YouTube 24/7 news live streams.
 * Same approach as worldmonitor: embed YouTube IFrame Player.
 */
import { Panel } from './Panel';
import { escapeHtml } from '@/utils';

interface LiveChannel {
  id: string;
  name: string;
  videoId: string;
}

const CHANNELS: LiveChannel[] = [
  { id: 'bloomberg', name: 'Bloomberg', videoId: 'iEpJwprxDdk' },
  { id: 'sky', name: 'Sky News', videoId: 'uvviIF4725I' },
  { id: 'euronews', name: 'Euronews', videoId: 'pykpO5kQJ98' },
  { id: 'dw', name: 'DW News', videoId: 'LuKwFajn37U' },
  { id: 'cnbc', name: 'CNBC', videoId: '9NyxcX3rhQs' },
  { id: 'france24', name: 'France 24', videoId: 'u9foWyMSETk' },
  { id: 'aljazeera', name: 'Al Jazeera', videoId: 'gCNeDWCI0vo' },
  { id: 'cna', name: 'CNA Asia', videoId: 'XWq5kBlakcQ' },
];

export class LiveNewsPanel extends Panel {
  private activeChannel = 0;
  private iframeEl: HTMLIFrameElement | null = null;

  constructor() {
    super({ id: 'live-news', title: 'Live News', className: 'panel-wide', showCount: false });
    this.content.style.padding = '0';
    this.content.style.display = 'flex';
    this.content.style.flexDirection = 'column';
    this.content.style.overflow = 'hidden';
    this.buildUI();
  }

  private buildUI(): void {
    this.content.innerHTML = '';

    // Channel tabs
    const tabs = document.createElement('div');
    tabs.className = 'panel-tabs';
    CHANNELS.forEach((ch, i) => {
      const btn = document.createElement('button');
      btn.className = `panel-tab ${i === 0 ? 'active' : ''}`;
      btn.textContent = ch.name;
      btn.addEventListener('click', () => this.switchChannel(i, tabs));
      tabs.appendChild(btn);
    });
    this.content.appendChild(tabs);

    // YouTube embed
    this.iframeEl = document.createElement('iframe');
    this.iframeEl.style.cssText = 'flex:1;border:none;width:100%;min-height:0;background:#000;';
    this.iframeEl.setAttribute('allow', 'autoplay; encrypted-media');
    this.iframeEl.setAttribute('allowfullscreen', '');
    this.iframeEl.src = this.embedUrl(CHANNELS[0].videoId);
    this.content.appendChild(this.iframeEl);

    this.setDataBadge('live');
  }

  private switchChannel(idx: number, tabs: HTMLElement): void {
    this.activeChannel = idx;
    tabs.querySelectorAll('.panel-tab').forEach((t, i) => t.classList.toggle('active', i === idx));
    if (this.iframeEl) {
      this.iframeEl.src = this.embedUrl(CHANNELS[idx].videoId);
    }
  }

  private embedUrl(videoId: string): string {
    return `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=0&mute=1&rel=0`;
  }

  async refresh(): Promise<void> {}
}

