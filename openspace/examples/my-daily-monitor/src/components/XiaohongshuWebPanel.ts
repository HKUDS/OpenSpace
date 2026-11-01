import { WebPanel } from './WebPanel';

export class XiaohongshuWebPanel extends WebPanel {
  constructor() {
    super({
      id: 'xiaohongshu-web',
      title: 'Xiaohongshu',
      url: 'https://www.xiaohongshu.com/explore',
      showCount: false,
      className: 'panel-wide',
    });
  }
}

