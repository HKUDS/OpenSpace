import { getSecret } from '@/services/settings-store';

export interface FeishuRecentMsg {
  content: string;
  senderName: string;
  msgType: string;
  createTime: string;
}

export interface FeishuMessage {
  id: string;
  chatName: string;
  chatType: string;
  avatar: string;
  description: string;
  memberCount: number;
  senderName: string;
  content: string;
  imageUrl: string;
  timestamp: string;
  unread: boolean;
  chatId: string;
  recentMessages: FeishuRecentMsg[];
}

export interface FeishuResult {
  messages: FeishuMessage[];
  configured: boolean;
  error?: string;
  hint?: string;
}

export async function fetchFeishuMessages(): Promise<FeishuMessage[]> {
  return (await fetchFeishuResult()).messages;
}

export async function fetchFeishuResult(): Promise<FeishuResult> {
  const appId = getSecret('FEISHU_APP_ID');
  const appSecret = getSecret('FEISHU_APP_SECRET');
  if (!appId || !appSecret) {
    return { messages: [], configured: false, error: 'Feishu not configured. Go to Settings → API Keys → add Feishu App ID and Secret.' };
  }

  try {
    const resp = await fetch('/api/feishu?action=overview', {
      headers: { 'X-Feishu-App-Id': appId, 'X-Feishu-App-Secret': appSecret },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) return { messages: [], configured: true, error: data.error };
    if (data.hint) return { messages: [], configured: true, hint: data.hint, error: data.hint };

    const chatMessages = (data.chats || data.messages || []) as any[];
    const messages: FeishuMessage[] = chatMessages.map((cm: any) => ({
      id: cm.chatId || '',
      chatName: cm.chatName || 'Unknown',
      chatType: cm.chatType || 'group',
      avatar: cm.avatar || '',
      description: cm.description || '',
      memberCount: cm.memberCount || 0,
      senderName: cm.latestSenderName || '',
      content: cm.latestMessage || '(no messages)',
      imageUrl: cm.latestImageUrl || '',
      timestamp: cm.latestTime ? new Date(parseInt(cm.latestTime, 10)).toISOString() : new Date().toISOString(),
      unread: cm.hasUnread ?? true,
      chatId: cm.chatId || '',
      recentMessages: (cm.recentMessages || []).map((rm: any) => ({
        content: rm.content || '',
        senderName: rm.senderName || '',
        msgType: rm.msgType || '',
        createTime: rm.createTime ? new Date(parseInt(rm.createTime, 10)).toISOString() : '',
      })),
    }));

    return { messages, configured: true };
  } catch (err: any) {
    return { messages: [], configured: true, error: err.message };
}
}
