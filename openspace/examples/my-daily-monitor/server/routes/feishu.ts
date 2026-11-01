/**
 * Feishu Open API proxy — messages, meetings, docs.
 * Auth: app_id + app_secret → tenant_access_token
 *
 * Key APIs:
 *   GET /im/v1/messages     — read message history (needs container_id_type + container_id)
 *   GET /im/v1/chats        — list group chats bot belongs to
 *   GET /vc/v1/meetings     — list meetings
 *   GET /drive/v1/files     — list recent files
 *
 * IMPORTANT: The bot must be added to group chats to read messages.
 * In Feishu admin, create a Custom App → enable im:message, im:chat scopes → add bot to groups.
 */
import type { IncomingHttpHeaders } from 'node:http';

const FEISHU_BASE = 'https://open.feishu.cn/open-apis';

// Token cache
let tokenCache: { token: string; expiresAt: number } | null = null;

async function getTenantToken(appId: string, appSecret: string): Promise<string | null> {
  if (tokenCache && Date.now() < tokenCache.expiresAt) return tokenCache.token;
  try {
    const resp = await fetch(`${FEISHU_BASE}/auth/v3/tenant_access_token/internal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app_id: appId, app_secret: appSecret }),
    });
    if (!resp.ok) return null;
    const data = await resp.json() as any;
    if (data.code !== 0) return null;
    tokenCache = {
      token: data.tenant_access_token,
      expiresAt: Date.now() + (data.expire - 300) * 1000,
    };
    return tokenCache.token;
  } catch { return null; }
}

async function feishuGet(path: string, token: string, params: Record<string, string> = {}): Promise<any> {
  const qs = new URLSearchParams(params).toString();
  const url = `${FEISHU_BASE}${path}${qs ? '?' + qs : ''}`;
  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const errText = await resp.text().catch(() => '');
    throw new Error(`Feishu API ${resp.status}: ${errText.slice(0, 200)}`);
  }
  return resp.json();
}

// Extract text + image_key from Feishu message body
interface MsgContent {
  text: string;
  imageKey?: string;
}

function extractMessageContent(msg: any): MsgContent {
  try {
    const msgType = msg.msg_type;
    const body = msg.body?.content;
    if (!body) return { text: `[${msgType || 'message'}]` };

    if (msgType === 'text') {
      const parsed = JSON.parse(body);
      return { text: parsed.text || body };
    }
    if (msgType === 'post') {
      const parsed = JSON.parse(body);
      const title = parsed.title || '';
      const content = parsed.content?.[0]?.map((seg: any) => seg.text || '').join('') || '';
      return { text: title ? `${title}: ${content}` : content || '[rich text]' };
    }
    if (msgType === 'image') {
      try {
        const parsed = JSON.parse(body);
        return { text: '[Image]', imageKey: parsed.image_key };
      } catch {
        return { text: '[Image]' };
      }
    }
    if (msgType === 'file') return { text: '[File]' };
    if (msgType === 'audio') return { text: '[Audio]' };
    if (msgType === 'sticker') {
      try {
        const parsed = JSON.parse(body);
        return { text: '[Sticker]', imageKey: parsed.file_key };
      } catch {
        return { text: '[Sticker]' };
      }
    }
    if (msgType === 'interactive') return { text: '[Interactive card]' };
    if (msgType === 'share_chat') return { text: '[Shared chat]' };
    if (msgType === 'share_user') return { text: '[Shared contact]' };
    return { text: `[${msgType || 'message'}]` };
  } catch {
    return { text: '[message]' };
  }
}

// Build image download URL (Feishu requires auth to download)
function buildImageUrl(messageId: string, imageKey: string): string {
  // This returns the API path; frontend will call /api/feishu?action=image&messageId=...&imageKey=...
  return `/api/feishu?action=image&messageId=${messageId}&imageKey=${imageKey}`;
}

export async function handleFeishuRequest(
  query: Record<string, string>,
  _body: string,
  headers: IncomingHttpHeaders,
): Promise<unknown> {
  const appId = (headers['x-feishu-app-id'] as string) || process.env.FEISHU_APP_ID || '';
  const appSecret = (headers['x-feishu-app-secret'] as string) || process.env.FEISHU_APP_SECRET || '';

  if (!appId || !appSecret) {
    return { configured: false, error: 'Feishu App credentials not configured', chats: [], messages: [] };
  }

  const token = await getTenantToken(appId, appSecret);
  if (!token) {
    return { configured: true, error: 'Failed to get tenant_access_token. Check app_id and app_secret.', chats: [], messages: [] };
  }

  const action = query.action || 'overview';

  try {
    // ---- List chats the bot belongs to ----
    if (action === 'chats') {
      const data = await feishuGet('/im/v1/chats', token, { page_size: '50' });
      return { chats: data.data?.items || [], configured: true };
    }

    // ---- Download image from a message ----
    if (action === 'image' && query.messageId && query.imageKey) {
      try {
        const imgResp = await fetch(
          `${FEISHU_BASE}/im/v1/messages/${query.messageId}/resources/${query.imageKey}?type=image`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!imgResp.ok) return { error: `Image download failed: ${imgResp.status}` };
        const buf = await imgResp.arrayBuffer();
        const base64 = Buffer.from(buf).toString('base64');
        const contentType = imgResp.headers.get('content-type') || 'image/png';
        return { imageData: `data:${contentType};base64,${base64}`, configured: true };
      } catch (err: any) {
        return { error: err.message };
      }
    }

    // ---- Get messages from a specific chat ----
    if (action === 'messages' && query.chatId) {
      const data = await feishuGet('/im/v1/messages', token, {
        container_id_type: 'chat',
        container_id: query.chatId,
        page_size: query.limit || '20',
        sort_type: 'ByCreateTimeDesc',
      });
      const items = data.data?.items || [];
      const messages = items.map((msg: any) => {
        const mc = extractMessageContent(msg);
        return {
          id: msg.message_id,
          senderId: msg.sender?.id,
          senderType: msg.sender?.sender_type,
          content: mc.text,
          imageUrl: mc.imageKey ? buildImageUrl(msg.message_id, mc.imageKey) : undefined,
          msgType: msg.msg_type,
          createTime: msg.create_time,
          updateTime: msg.update_time,
        };
      });
      return { messages, configured: true };
    }

    // ---- List recent files ----
    if (action === 'files') {
      const data = await feishuGet('/drive/v1/files', token, {
        page_size: '20',
        order_by: 'EditedTime',
        direction: 'DESC',
      });
      return { files: data.data?.files || [], configured: true };
    }

    // ---- Overview: chats + latest message from each chat ----
    const chatsResp = await feishuGet('/im/v1/chats', token, { page_size: '20' });
    const chats = chatsResp.data?.items || [];

    if (chats.length === 0) {
      return {
        configured: true,
        chats: [],
        messages: [],
        hint: 'Bot is not a member of any group chats. Add the bot to group chats in Feishu admin.',
      };
    }

    // Collect all unique sender IDs to batch-resolve names
    const senderNameCache = new Map<string, string>();

    // Fetch latest 3 messages from each chat (parallel, max 10 chats)
    const chatMessagesResults = await Promise.allSettled(
      chats.slice(0, 10).map(async (chat: any) => {
        try {
          const msgResp = await feishuGet('/im/v1/messages', token, {
            container_id_type: 'chat',
            container_id: chat.chat_id,
            page_size: '3',
            sort_type: 'ByCreateTimeDesc',
          });
          const items = msgResp.data?.items || [];

          // Resolve sender names for each message
          const messagesWithSender = [];
          for (const msg of items) {
            const senderId = msg.sender?.id || '';
            const senderType = msg.sender?.sender_type || '';
            let senderName = senderNameCache.get(senderId) || '';

            if (!senderName && senderId && senderType === 'user') {
              // Try contact API first (needs contact:user.base:readonly)
              try {
                const userResp = await feishuGet(`/contact/v3/users/${senderId}`, token, { user_id_type: 'open_id' });
                senderName = userResp.data?.user?.name || '';
              } catch { /* permission may not be granted */ }

              // Fallback: try chat members list (needs im:chat:member:readonly)
              if (!senderName) {
                try {
                  const membersResp = await feishuGet(`/im/v1/chats/${chat.chat_id}/members`, token, { member_id_type: 'open_id', page_size: '50' });
                  const members = membersResp.data?.items || [];
                  const found = members.find((m: any) => m.member_id === senderId);
                  if (found?.name) senderName = found.name;
                } catch { /* also may fail */ }
              }

              if (!senderName) senderName = senderId.slice(0, 8);
              senderNameCache.set(senderId, senderName);
            } else if (senderType === 'app') {
              senderName = 'Bot';
            }

            const mc = extractMessageContent(msg);
            messagesWithSender.push({
              content: mc.text,
              imageUrl: mc.imageKey ? buildImageUrl(msg.message_id, mc.imageKey) : undefined,
              senderName,
              senderId,
              msgType: msg.msg_type,
              createTime: msg.create_time,
            });
          }

          const latest = messagesWithSender[0];
          return {
            chatId: chat.chat_id,
            chatName: chat.name || 'Unknown',
            chatType: chat.chat_type || 'group',
            avatar: chat.avatar || '',
            description: chat.description || '',
            memberCount: chat.user_count || 0,
            latestMessage: latest?.content || '',
            latestImageUrl: latest?.imageUrl || '',
            latestSenderName: latest?.senderName || '',
            latestTime: latest?.createTime || '',
            hasUnread: true,
            recentMessages: messagesWithSender,
          };
        } catch {
          return {
            chatId: chat.chat_id,
            chatName: chat.name || 'Unknown',
            chatType: chat.chat_type || 'group',
            avatar: '',
            description: '',
            memberCount: 0,
            latestMessage: '(failed to load)',
            latestSenderName: '',
            latestTime: '',
            hasUnread: false,
            recentMessages: [],
          };
        }
      })
    );

    const chatMessages = chatMessagesResults
      .filter(r => r.status === 'fulfilled')
      .map(r => (r as PromiseFulfilledResult<any>).value)
      .sort((a, b) => {
        // Sort by latest message time descending
        const ta = parseInt(a.latestTime || '0', 10);
        const tb = parseInt(b.latestTime || '0', 10);
        return tb - ta;
      });

    return { configured: true, chats: chatMessages, messages: chatMessages };
  } catch (err: any) {
    return { configured: true, error: err.message, chats: [], messages: [] };
  }
}
