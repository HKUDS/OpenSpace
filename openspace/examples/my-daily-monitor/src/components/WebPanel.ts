import { Panel, type PanelOptions } from './Panel';

export interface WebPanelOptions extends PanelOptions {
  url: string;
  allowFullscreen?: boolean;
}

/**
 * WebPanel — base class for iframe-embedded multimodal panels.
 * Supports: toolbar with URL, open-in-new-tab, expand/fullscreen, refresh.
 */
export class WebPanel extends Panel {
  protected iframe: HTMLIFrameElement;
  protected currentUrl: string;
  private toolbar: HTMLElement;
  private urlDisplay: HTMLElement;
  private fullscreenOverlay: HTMLElement | null = null;
  private allowFullscreen: boolean;

  constructor(options: WebPanelOptions) {
    super({ ...options, className: `web-panel ${options.className || ''}`.trim() });
    this.currentUrl = options.url;
    this.allowFullscreen = options.allowFullscreen !== false;

    // Build toolbar
    this.toolbar = document.createElement('div');
    this.toolbar.className = 'web-panel-toolbar';

    // Refresh button
    const refreshBtn = document.createElement('button');
    refreshBtn.textContent = '↻';
    refreshBtn.title = 'Refresh';
    refreshBtn.addEventListener('click', () => this.reloadFrame());
    this.toolbar.appendChild(refreshBtn);

    // URL display
    this.urlDisplay = document.createElement('span');
    this.urlDisplay.className = 'web-panel-url';
    this.urlDisplay.textContent = this.currentUrl;
    this.toolbar.appendChild(this.urlDisplay);

    // Expand button
    if (this.allowFullscreen) {
      const expandBtn = document.createElement('button');
      expandBtn.textContent = '⤢ Expand';
      expandBtn.title = 'Expand to fullscreen';
      expandBtn.addEventListener('click', () => this.toggleFullscreen());
      this.toolbar.appendChild(expandBtn);
    }

    // Open external button
    const openBtn = document.createElement('button');
    openBtn.textContent = '↗ Open';
    openBtn.title = 'Open in new tab';
    openBtn.addEventListener('click', () => window.open(this.currentUrl, '_blank'));
    this.toolbar.appendChild(openBtn);

    // iFrame
    this.iframe = document.createElement('iframe');
    this.iframe.className = 'web-panel-frame';
    this.iframe.src = this.currentUrl;
    this.iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups allow-forms');
    this.iframe.setAttribute('loading', 'lazy');
    this.iframe.setAttribute('referrerpolicy', 'no-referrer');

    // Replace loading with real content
    this.content.innerHTML = '';
    this.content.appendChild(this.toolbar);
    this.content.appendChild(this.iframe);

    this.setDataBadge('live');
  }

  public navigateTo(url: string): void {
    this.currentUrl = url;
    this.iframe.src = url;
    this.urlDisplay.textContent = url;
  }

  public reloadFrame(): void {
    this.iframe.src = this.currentUrl;
  }

  private toggleFullscreen(): void {
    if (this.fullscreenOverlay) {
      this.exitFullscreen();
    } else {
      this.enterFullscreen();
    }
  }

  private enterFullscreen(): void {
    this.fullscreenOverlay = document.createElement('div');
    this.fullscreenOverlay.className = 'web-panel-fullscreen-overlay';

    // Clone toolbar
    const fsToolbar = document.createElement('div');
    fsToolbar.className = 'web-panel-toolbar';

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕ Close';
    closeBtn.addEventListener('click', () => this.exitFullscreen());
    fsToolbar.appendChild(closeBtn);

    const urlLabel = document.createElement('span');
    urlLabel.className = 'web-panel-url';
    urlLabel.textContent = this.currentUrl;
    fsToolbar.appendChild(urlLabel);

    const openBtn = document.createElement('button');
    openBtn.textContent = '↗ Open in Tab';
    openBtn.addEventListener('click', () => window.open(this.currentUrl, '_blank'));
    fsToolbar.appendChild(openBtn);

    // Fullscreen iframe
    const fsIframe = document.createElement('iframe');
    fsIframe.className = 'web-panel-frame';
    fsIframe.src = this.currentUrl;
    fsIframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups allow-forms');

    this.fullscreenOverlay.appendChild(fsToolbar);
    this.fullscreenOverlay.appendChild(fsIframe);
    document.body.appendChild(this.fullscreenOverlay);

    // ESC to close
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { this.exitFullscreen(); document.removeEventListener('keydown', onKey); }
    };
    document.addEventListener('keydown', onKey);
  }

  private exitFullscreen(): void {
    if (this.fullscreenOverlay) {
      this.fullscreenOverlay.remove();
      this.fullscreenOverlay = null;
    }
  }

  public async refresh(): Promise<void> {
    this.reloadFrame();
  }

  public destroy(): void {
    this.exitFullscreen();
    super.destroy();
  }
}

