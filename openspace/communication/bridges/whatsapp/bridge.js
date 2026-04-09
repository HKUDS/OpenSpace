#!/usr/bin/env node
import {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  makeWASocket,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
} from 'fs';
import { randomBytes } from 'crypto';
import qrcode from 'qrcode-terminal';
import { WebSocketServer, WebSocket } from 'ws';

import {
  matchesAllowedUser,
  normalizeWhatsAppIdentifier,
  parseAllowedUsers,
} from './allowlist.js';

const args = process.argv.slice(2);

function getArg(name, defaultValue) {
  const index = args.indexOf(`--${name}`);
  return index !== -1 && args[index + 1] ? args[index + 1] : defaultValue;
}

const PORT = parseInt(getArg('port', '3000'), 10);
const HOST = getArg('host', '127.0.0.1');
const BIND_HOST = HOST === 'localhost' ? '127.0.0.1' : HOST;
const SESSION_DIR = path.resolve(
  getArg('session', path.join(process.env.HOME || '~', '.openspace', 'whatsapp', 'session'))
);
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat');
const BRIDGE_TOKEN = String(process.env.BRIDGE_TOKEN || '').trim();
const DEFAULT_REPLY_PREFIX = 'OpenSpace\n────────────\n';
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX === undefined
  ? DEFAULT_REPLY_PREFIX
  : process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n');
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');

const IMAGE_CACHE_DIR = path.join(SESSION_DIR, '..', 'image_cache');
const DOCUMENT_CACHE_DIR = path.join(SESSION_DIR, '..', 'document_cache');
const AUDIO_CACHE_DIR = path.join(SESSION_DIR, '..', 'audio_cache');

const MAX_RECENT_SENT = 50;
const MAX_RECENT_INBOUND = 512;
const AUTH_TIMEOUT_MS = 5000;

if (!BRIDGE_TOKEN) {
  console.error('BRIDGE_TOKEN is required');
  process.exit(1);
}
if (!['127.0.0.1', 'localhost'].includes(HOST)) {
  console.error(`Refusing to bind WhatsApp bridge to non-loopback host: ${HOST}`);
  process.exit(1);
}

mkdirSync(SESSION_DIR, { recursive: true });
mkdirSync(IMAGE_CACHE_DIR, { recursive: true });
mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
mkdirSync(AUDIO_CACHE_DIR, { recursive: true });

const logger = pino({ level: 'warn' });
const clients = new Set();
const recentlySentIds = new Set();
const recentInboundById = new Map();

let sock = null;
let connectionState = 'disconnected';
let reconnectTimer = null;

function broadcast(payload) {
  const encoded = JSON.stringify(payload);
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN && ws._authed) {
      ws.send(encoded);
    }
  }
}

function recordRecentOutbound(messageId) {
  if (!messageId) {
    return;
  }
  recentlySentIds.delete(messageId);
  recentlySentIds.add(messageId);
  while (recentlySentIds.size > MAX_RECENT_SENT) {
    recentlySentIds.delete(recentlySentIds.values().next().value);
  }
}

function recordRecentInbound(messageId, rawMessage) {
  if (!messageId || !rawMessage) {
    return;
  }
  recentInboundById.delete(messageId);
  recentInboundById.set(messageId, rawMessage);
  while (recentInboundById.size > MAX_RECENT_INBOUND) {
    const firstKey = recentInboundById.keys().next().value;
    recentInboundById.delete(firstKey);
  }
}

function currentStatusPayload() {
  return { type: 'status', status: connectionState };
}

function buildQuotedOptions(replyToMessageId) {
  if (!replyToMessageId) {
    return {};
  }
  const quoted = recentInboundById.get(String(replyToMessageId).trim());
  return quoted ? { quoted } : {};
}

function formatOutgoingMessage(message) {
  if (WHATSAPP_MODE !== 'self-chat') {
    return message;
  }
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function buildLidMap() {
  const mapping = {};
  try {
    for (const fileName of readdirSync(SESSION_DIR)) {
      const match = fileName.match(/^lid-mapping-(.+)\.json$/);
      if (!match) {
        continue;
      }
      const value = JSON.parse(readFileSync(path.join(SESSION_DIR, fileName), 'utf8'));
      const normalized = normalizeWhatsAppIdentifier(value);
      if (normalized) {
        mapping[normalized] = match[1];
        mapping[match[1]] = normalized;
      }
    }
  } catch {
    return {};
  }
  return mapping;
}

let lidToPhone = buildLidMap();

function normalizeId(value) {
  return normalizeWhatsAppIdentifier(value);
}

function getMyIdentifiers() {
  const ids = new Set();
  if (sock?.user?.id) {
    ids.add(normalizeId(sock.user.id));
  }
  if (sock?.user?.lid) {
    ids.add(normalizeId(sock.user.lid));
  }
  return ids;
}

function getMessageContainer(message) {
  return message?.message || {};
}

function extractContextInfo(message) {
  const container = getMessageContainer(message);
  return (
    container.extendedTextMessage?.contextInfo
    || container.imageMessage?.contextInfo
    || container.videoMessage?.contextInfo
    || container.documentMessage?.contextInfo
    || container.audioMessage?.contextInfo
    || container.conversation?.contextInfo
    || null
  );
}

async function cacheMedia(rawMessage, mediaMessage, targetDir, prefix, fallbackExt) {
  const buffer = await downloadMediaMessage(
    rawMessage,
    'buffer',
    {},
    { logger, reuploadRequest: sock.updateMediaMessage }
  );
  mkdirSync(targetDir, { recursive: true });
  const mime = mediaMessage.mimetype || '';
  const ext = mime.includes('/') ? `.${mime.split('/')[1].split(';')[0]}` : fallbackExt;
  const filePath = path.join(targetDir, `${prefix}_${randomBytes(6).toString('hex')}${ext || fallbackExt}`);
  writeFileSync(filePath, buffer);
  return filePath;
}

function resolveAllowedFilePath(filePath) {
  const mediaRootRaw = String(process.env.BRIDGE_MEDIA_ROOT || '').trim();
  if (!mediaRootRaw) {
    throw new Error('BRIDGE_MEDIA_ROOT is not configured');
  }
  const mediaRoot = realpathSync(mediaRootRaw);
  const resolvedPath = realpathSync(String(filePath || ''));
  const relative = path.relative(mediaRoot, resolvedPath);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`File path escapes bridge media root: ${filePath}`);
  }
  const stat = statSync(resolvedPath);
  if (!stat.isFile()) {
    throw new Error(`File is not a regular file: ${filePath}`);
  }
  return resolvedPath;
}

async function sendText(to, text, replyToMessageId) {
  if (!sock || connectionState !== 'connected') {
    throw new Error('Not connected to WhatsApp');
  }
  const sent = await sock.sendMessage(
    to,
    { text: formatOutgoingMessage(text) },
    buildQuotedOptions(replyToMessageId)
  );
  recordRecentOutbound(sent?.key?.id);
  return sent;
}

async function sendMedia(to, filePath, mimetype, caption, fileName, replyToMessageId) {
  if (!sock || connectionState !== 'connected') {
    throw new Error('Not connected to WhatsApp');
  }

  const resolvedPath = resolveAllowedFilePath(filePath);
  if (!existsSync(resolvedPath)) {
    throw new Error(`File not found: ${resolvedPath}`);
  }
  const buffer = readFileSync(resolvedPath);
  const normalizedMime = String(mimetype || '').toLowerCase();
  let payload;
  if (normalizedMime.startsWith('image/')) {
    payload = { image: buffer, caption: caption || undefined };
  } else if (normalizedMime.startsWith('video/')) {
    payload = { video: buffer, caption: caption || undefined };
  } else if (normalizedMime.startsWith('audio/')) {
    payload = {
      audio: buffer,
      mimetype: normalizedMime || 'audio/ogg; codecs=opus',
      ptt: normalizedMime.includes('ogg') || normalizedMime.includes('opus'),
    };
  } else {
    payload = {
      document: buffer,
      fileName: fileName || path.basename(resolvedPath),
      caption: caption || undefined,
    };
  }

  const sent = await sock.sendMessage(
    to,
    payload,
    buildQuotedOptions(replyToMessageId)
  );
  recordRecentOutbound(sent?.key?.id);
  return sent;
}

async function handleInboundMessage(rawMessage) {
  if (!rawMessage?.message) {
    return;
  }

  const chatId = rawMessage.key?.remoteJid || '';
  const senderId = rawMessage.key?.participant || chatId;
  const isGroup = chatId.endsWith('@g.us');
  const senderNumber = senderId.replace(/@.*/, '');

  if (rawMessage.key?.fromMe) {
    if (isGroup || chatId.includes('status')) {
      return;
    }
    if (WHATSAPP_MODE === 'bot') {
      return;
    }

    const myIds = getMyIdentifiers();
    const chatNumber = normalizeId(chatId);
    if (!myIds.has(chatNumber)) {
      return;
    }
  }

  if (!rawMessage.key?.fromMe && !matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
    return;
  }

  const container = getMessageContainer(rawMessage);
  const contextInfo = extractContextInfo(rawMessage);
  const mentionedIds = (contextInfo?.mentionedJid || []).map((value) => normalizeId(value));
  const mentionsBot = mentionedIds.some((value) => getMyIdentifiers().has(value));
  const replyToMessageId = contextInfo?.stanzaId || null;

  let body = '';
  let hasMedia = false;
  let mediaType = '';
  const mediaUrls = [];

  if (container.conversation) {
    body = container.conversation;
  } else if (container.extendedTextMessage?.text) {
    body = container.extendedTextMessage.text;
  } else if (container.imageMessage) {
    body = container.imageMessage.caption || '';
    hasMedia = true;
    mediaType = 'image';
    try {
      mediaUrls.push(await cacheMedia(rawMessage, container.imageMessage, IMAGE_CACHE_DIR, 'img', '.jpg'));
    } catch (error) {
      console.error('[bridge] Failed to download image:', error.message);
    }
  } else if (container.videoMessage) {
    body = container.videoMessage.caption || '';
    hasMedia = true;
    mediaType = 'video';
    try {
      mediaUrls.push(await cacheMedia(rawMessage, container.videoMessage, DOCUMENT_CACHE_DIR, 'vid', '.mp4'));
    } catch (error) {
      console.error('[bridge] Failed to download video:', error.message);
    }
  } else if (container.audioMessage || container.pttMessage) {
    hasMedia = true;
    mediaType = container.pttMessage ? 'ptt' : 'audio';
    try {
      const audioMessage = container.pttMessage || container.audioMessage;
      mediaUrls.push(await cacheMedia(rawMessage, audioMessage, AUDIO_CACHE_DIR, 'aud', '.ogg'));
    } catch (error) {
      console.error('[bridge] Failed to download audio:', error.message);
    }
  } else if (container.documentMessage) {
    body = container.documentMessage.caption || '';
    hasMedia = true;
    mediaType = 'document';
    try {
      mediaUrls.push(
        await cacheMedia(
          rawMessage,
          container.documentMessage,
          DOCUMENT_CACHE_DIR,
          'doc',
          path.extname(container.documentMessage.fileName || '') || '.bin'
        )
      );
    } catch (error) {
      console.error('[bridge] Failed to download document:', error.message);
    }
  }

  const messageId = rawMessage.key?.id;
  if (messageId && recentlySentIds.has(messageId)) {
    recentlySentIds.delete(messageId);
    return;
  }

  if (messageId) {
    recordRecentInbound(messageId, rawMessage);
  }

  const normalizedSenderId = lidToPhone[normalizeId(senderId)] || senderId;
  const event = {
    type: 'message',
    messageId,
    chatId,
    senderId: normalizedSenderId,
    senderName: rawMessage.pushName || senderNumber,
    chatName: isGroup ? chatId.split('@')[0] : (rawMessage.pushName || senderNumber),
    isGroup,
    body,
    hasMedia,
    mediaType,
    mediaUrls,
    replyToMessageId,
    mentionedIds,
    mentionsBot,
    timestamp: rawMessage.messageTimestamp,
  };
  broadcast(event);
}

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['OpenSpace', 'Chrome', '120.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    getMessage: async () => ({ conversation: '' }),
  });

  sock.ev.on('creds.update', () => {
    saveCreds();
    lidToPhone = buildLidMap();
  });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      console.log('\nScan this QR code with WhatsApp on your phone:\n');
      qrcode.generate(qr, { small: true });
      console.log('\nWaiting for scan...\n');
      broadcast({ type: 'qr', qr });
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';
      broadcast(currentStatusPayload());
      if (reason === DisconnectReason.loggedOut) {
        console.log('Logged out. Delete session and restart to re-authenticate.');
        process.exit(1);
      }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(
        () => startSocket().catch((error) => console.error('WhatsApp reconnect failed:', error)),
        reason === 515 ? 1000 : 3000
      );
    } else if (connection === 'open') {
      connectionState = 'connected';
      console.log('WhatsApp connected');
      broadcast(currentStatusPayload());
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' && type !== 'append') {
      return;
    }
    for (const message of messages) {
      try {
        await handleInboundMessage(message);
      } catch (error) {
        console.error('[bridge] Failed to normalize inbound message:', error);
      }
    }
  });
}

function startBridgeServer() {
  const wss = new WebSocketServer({ host: BIND_HOST, port: PORT });
  console.log(`OpenSpace WhatsApp bridge listening on ws://${BIND_HOST}:${PORT} (mode: ${WHATSAPP_MODE})`);

  wss.on('connection', (ws, request) => {
    if (request.headers.origin) {
      ws.close(4003, 'Origin header is not allowed');
      return;
    }

    ws._authed = false;
    clients.add(ws);
    const authTimeout = setTimeout(() => {
      if (!ws._authed) {
        ws.close(4001, 'Authentication timeout');
      }
    }, AUTH_TIMEOUT_MS);

    ws.once('message', (raw) => {
      try {
        const payload = JSON.parse(raw.toString());
        if (payload.type !== 'auth' || payload.token !== BRIDGE_TOKEN) {
          ws.close(4003, 'Invalid bridge token');
          return;
        }
        ws._authed = true;
        clearTimeout(authTimeout);
        ws.send(JSON.stringify({ type: 'auth_ok' }));
        ws.send(JSON.stringify(currentStatusPayload()));

        ws.on('message', async (commandRaw) => {
          try {
            const command = JSON.parse(commandRaw.toString());
            const requestId = command.requestId || null;
            let sent = null;
            if (command.type === 'send') {
              sent = await sendText(command.to, command.text || '', command.replyToMessageId);
            } else if (command.type === 'send_media') {
              sent = await sendMedia(
                command.to,
                command.filePath,
                command.mimetype,
                command.caption,
                command.fileName,
                command.replyToMessageId
              );
            } else {
              throw new Error(`Unsupported bridge command: ${command.type}`);
            }
            ws.send(JSON.stringify({
              type: 'ack',
              requestId,
              messageId: sent?.key?.id || null,
            }));
          } catch (error) {
            ws.send(JSON.stringify({
              type: 'error',
              requestId: (() => {
                try {
                  return JSON.parse(commandRaw.toString()).requestId || null;
                } catch {
                  return null;
                }
              })(),
              error: error?.message || String(error),
            }));
          }
        });
      } catch {
        ws.close(4003, 'Invalid auth payload');
      }
    });

    ws.on('close', () => {
      clearTimeout(authTimeout);
      clients.delete(ws);
    });
    ws.on('error', () => {
      clearTimeout(authTimeout);
      clients.delete(ws);
    });
  });

  return wss;
}

async function shutdown(server) {
  clearTimeout(reconnectTimer);
  for (const ws of clients) {
    try {
      ws.close();
    } catch {}
  }
  clients.clear();
  if (server) {
    await new Promise((resolve) => server.close(resolve));
  }
  if (sock) {
    try {
      sock.end(new Error('Bridge shutdown'));
    } catch {}
    sock = null;
  }
}

const server = startBridgeServer();
startSocket().catch((error) => {
  console.error('Failed to start WhatsApp socket:', error);
  process.exit(1);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, async () => {
    try {
      await shutdown(server);
    } finally {
      process.exit(0);
    }
  });
}
