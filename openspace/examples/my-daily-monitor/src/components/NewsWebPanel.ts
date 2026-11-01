import { WebPanel } from './WebPanel';

const NEWS_CHANNELS = [
  { name: 'Reuters', url: 'https://www.reuters.com/' },
  { name: 'BBC', url: 'https://www.bbc.com/news' },
  { name: 'CNN', url: 'https://www.cnn.com/' },
  { name: 'Bloomberg', url: 'https://www.bloomberg.com/' },
  { name: 'Al Jazeera', url: 'https://www.aljazeera.com/' },
];

export class NewsWebPanel extends WebPanel {
  private channelTabs: HTMLElement;
  private activeChannel = 0;

  constructor() {
    super({
      id: 'news-web',
      title: 'Live News',
      url: NEWS_CHANNELS[0].url,
      showCount: false,
      className: 'panel-wide',
    });

    // Insert channel tabs between toolbar and iframe
    this.channelTabs = document.createElement('div');
    this.channelTabs.className = 'panel-tabs';

    NEWS_CHANNELS.forEach((ch, idx) => {
      const tab = document.createElement('button');
      tab.className = `panel-tab ${idx === 0 ? 'active' : ''}`;
      tab.textContent = ch.name;
      tab.addEventListener('click', () => this.switchChannel(idx));
      this.channelTabs.appendChild(tab);
    });

    // Insert before iframe
    this.content.insertBefore(this.channelTabs, this.iframe);
  }

  private switchChannel(idx: number): void {
    this.activeChannel = idx;
    this.navigateTo(NEWS_CHANNELS[idx].url);

    // Update tab active state
    const tabs = this.channelTabs.querySelectorAll('.panel-tab');
    tabs.forEach((t, i) => t.classList.toggle('active', i === idx));
  }
}

