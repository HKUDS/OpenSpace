import { WebPanel } from './WebPanel';

export class TwitterWebPanel extends WebPanel {
  constructor() {
    super({
      id: 'twitter-web',
      title: 'Twitter / X',
      url: 'https://x.com/home',
      showCount: false,
      className: 'panel-wide',
    });
  }
}

