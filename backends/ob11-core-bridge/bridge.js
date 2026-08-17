'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const { WebSocket, WebSocketServer } = require('ws');

const HOST = process.env.AIPLUGIN4_BACKEND_HOST || '0.0.0.0';
const PORT = Number(process.env.AIPLUGIN4_BACKEND_PORT || 46880);
const CORE_PATH = process.env.AIPLUGIN4_BRIDGE_CORE_PATH || '/core';
const CORE_PATHS = new Set((process.env.AIPLUGIN4_BRIDGE_CORE_PATHS || `${CORE_PATH},${CORE_PATH.replace(/\/$/, '')}/ws`)
  .split(',').map(value => value.trim()).filter(Boolean));
const ONEBOT_PATH = process.env.AIPLUGIN4_BRIDGE_ONEBOT_PATH || '/onebot';
const CONTROL_PATH = process.env.AIPLUGIN4_BRIDGE_CONTROL_PATH || '/control';
const BRIDGE_TOKEN = process.env.AIPLUGIN4_BRIDGE_TOKEN || '';
const CORE_TOKEN = process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN ?? BRIDGE_TOKEN;
const PROTOCOL_TOKEN = process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN ?? BRIDGE_TOKEN;
const CORE_URL = process.env.AIPLUGIN4_BRIDGE_CORE_URL || '';
const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_SETTLE_MS = 400;
const MAX_MESSAGES = 50;
const ACTIONS = new Set(['send_msg', 'send_group_msg', 'send_private_msg', 'send_forward_msg', 'send_group_forward_msg']);

const log = (level, message, error) => {
  const suffix = error ? ` ${error instanceof Error ? error.stack || error.message : String(error)}` : '';
  console[level](`[ob11-core-bridge] ${message}${suffix}`);
};

function sendRaw(ws, data) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
}
function jsonSend(ws, value) {
  sendRaw(ws, JSON.stringify(value));
}
function parseMessage(data) {
  try { return JSON.parse(Buffer.isBuffer(data) ? data.toString('utf8') : String(data)); }
  catch (_) { return null; }
}
function idString(value) { return value === undefined || value === null ? '' : String(value); }
function laneKey(target) {
  const selfId = idString(target && (target.selfId ?? target.self_id));
  const messageType = target && (target.messageType ?? target.message_type) || 'group';
  const peer = messageType === 'private'
    ? idString(target && (target.userId ?? target.user_id))
    : idString(target && (target.groupId ?? target.group_id));
  return `${selfId}|${messageType}|${peer}`;
}
function actionTarget(action, params) {
  if (!params || !ACTIONS.has(action)) return null;
  let messageType = params.message_type;
  if (messageType !== 'private' && messageType !== 'group') {
    messageType = action === 'send_private_msg' || params.user_id !== undefined ? 'private' : 'group';
  }
  const peer = messageType === 'private' ? params.user_id : params.group_id;
  if (peer === undefined || peer === null) return null;
  return { selfId: '', messageType, [messageType === 'private' ? 'userId' : 'groupId']: peer };
}
function eventTarget(event) {
  if (!event || event.post_type !== 'message') return null;
  const messageType = event.message_type || (event.group_id !== undefined ? 'group' : 'private');
  return { selfId: event.self_id, messageType, userId: event.user_id, groupId: event.group_id };
}
function textOfSegments(segments) {
  if (!Array.isArray(segments)) return '';
  return segments.map(segment => {
    if (!segment || typeof segment !== 'object') return '';
    if (segment.type === 'text') return String(segment.data && segment.data.text || '');
    return `[${segment.type || 'segment'}]`;
  }).join('');
}
function extractText(params) {
  if (!params) return '';
  if (typeof params.message === 'string') return params.message;
  return textOfSegments(params.message);
}
function nowSafeId() {
  const timestamp = Date.now() * 1000;
  return timestamp + (Bridge.nextMessageId++ % 1000);
}

class Invocation {
  constructor(bridge, controlWs, request) {
    this.bridge = bridge;
    this.controlWs = controlWs;
    this.id = String(request.id || `invoke_${crypto.randomUUID()}`);
    this.target = request.target || {};
    this.lane = laneKey(this.target);
    this.capture = request.capture || {};
    this.mode = this.capture.mode || 'reply_only';
    this.forward = this.capture.forward === true;
    this.maxMessages = Math.max(1, Math.min(Number(this.capture.maxMessages || MAX_MESSAGES), MAX_MESSAGES));
    this.settleMs = Math.max(0, Math.min(Number(this.capture.settleMs ?? DEFAULT_SETTLE_MS), 10000));
    this.timeoutMs = Math.max(100, Math.min(Number(request.timeoutMs || DEFAULT_TIMEOUT_MS), 120000));
    this.messages = [];
    this.forwardedCount = 0;
    this.interceptedCount = 0;
    this.ambiguous = false;
    this.completed = false;
    this.completedBy = '';
    this.timer = null;
    this.settleTimer = null;
    this.coreWs = null;
  }
  start() {
    this.promise = new Promise(resolve => { this.resolve = resolve; });
    this.timer = setTimeout(() => this.finish('timeout'), this.timeoutMs);
    return this.promise;
  }
  matchesTarget(target, coreWs) {
    if (!target || (this.coreWs && this.coreWs !== coreWs)) return false;
    const expected = laneKey(this.target);
    const actual = laneKey({ ...target, selfId: target.selfId || this.target.selfId });
    return expected === actual;
  }
  acceptMessage(message, meta) {
    if (this.completed || this.messages.length >= this.maxMessages) return false;
    this.messages.push({ ...message, ...meta });
    jsonSend(this.controlWs, {
      type: 'command.message', id: this.id, message: this.messages[this.messages.length - 1],
      forwarded: meta.forwarded === true, intercepted: meta.intercepted === true
    });
    if (this.messages.length >= this.maxMessages) this.finish('max_messages');
    else {
      if (this.settleTimer) clearTimeout(this.settleTimer);
      this.settleTimer = setTimeout(() => this.finish('idle'), this.settleMs);
    }
    return true;
  }
  finish(reason) {
    if (this.completed) return;
    this.completed = true;
    this.completedBy = reason;
    if (this.timer) clearTimeout(this.timer);
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.resolve({
      type: 'command.result', id: this.id, ok: true, messages: this.messages,
      completedBy: this.completedBy, ambiguous: this.ambiguous,
      forwardedCount: this.forwardedCount, interceptedCount: this.interceptedCount
    });
  }
  fail(error) {
    if (this.completed) return;
    this.completed = true;
    if (this.timer) clearTimeout(this.timer);
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.resolve({
      type: 'command.result', id: this.id, ok: false,
      error: error instanceof Error ? error.message : String(error),
      messages: this.messages, completedBy: 'disconnect', ambiguous: this.ambiguous
    });
  }
}

class Bridge {
  static nextMessageId = 900000000000;
  constructor() {
    this.server = null;
    this.wss = new WebSocketServer({ noServer: true });
    this.coreClients = new Set();
    this.protocolClients = new Set();
    this.controlClients = new Set();
    this.invocations = new Map();
    this.lanes = new Map();
    this.echoTargets = new Map();
    this.stopping = false;
    this.coreConnector = null;
    this.coreReconnectTimer = null;
    this.coreGeneration = 0;
  }
  async start() {
    this.stopping = false;
    this.coreConnector = null;
    this.coreReconnectTimer = null;
    this.server = http.createServer((req, res) => {
      if (req.url === '/healthz') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true, coreConnected: this.isCoreConnected(), coreClients: this.coreClients.size, protocolClients: this.protocolClients.size }));
        return;
      }
      res.writeHead(404); res.end('not found');
    });
    this.server.on('upgrade', (req, socket, head) => {
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
      const kind = CORE_PATHS.has(url.pathname) ? 'core' : url.pathname === ONEBOT_PATH ? 'onebot' : url.pathname === CONTROL_PATH ? 'control' : '';
      if (!kind) { socket.destroy(); return; }
      const expectedToken = kind === 'core' ? CORE_TOKEN : kind === 'onebot' ? PROTOCOL_TOKEN : BRIDGE_TOKEN;
      if (expectedToken && url.searchParams.get('access_token') !== expectedToken && req.headers.authorization !== `Bearer ${expectedToken}`) { socket.destroy(); return; }
      this.wss.handleUpgrade(req, socket, head, ws => {
        ws._bridgeKind = kind;
        this.wss.emit('connection', ws, req);
      });
    });
    this.wss.on('connection', ws => this.attachClient(ws));
    await new Promise(resolve => this.server.listen(PORT, HOST, resolve));
    log('info', `listening ${HOST}:${PORT}, core=${[...CORE_PATHS].join(',')}, onebot=${ONEBOT_PATH}, control=${CONTROL_PATH}`);
    if (CORE_URL) this.connectOutboundCore();
    return this;
  }
  async stop() {
    this.stopping = true;
    this.coreGeneration++;
    if (this.coreReconnectTimer) clearTimeout(this.coreReconnectTimer);
    this.coreReconnectTimer = null;
    const outboundCore = this.coreConnector;
    this.coreConnector = null;
    for (const invocation of this.invocations.values()) invocation.fail(new Error('bridge stopped'));
    for (const ws of [...this.coreClients, ...this.protocolClients, ...this.controlClients]) ws.close();
    if (outboundCore && !this.coreClients.has(outboundCore)) outboundCore.close();
    await new Promise(resolve => this.server ? this.server.close(() => resolve()) : resolve());
  }
  isCoreConnected() { return this.coreClients.size > 0; }
  connectOutboundCore() {
    if (!CORE_URL || this.stopping || this.coreConnector) return;
    const generation = this.coreGeneration;
    const separator = CORE_URL.includes('?') ? '&' : '?';
    const url = CORE_TOKEN ? `${CORE_URL}${separator}access_token=${encodeURIComponent(CORE_TOKEN)}` : CORE_URL;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (error) {
      log('warn', `connect outbound core failed: ${url}`, error);
      this.scheduleCoreReconnect();
      return;
    }
    ws._bridgeKind = 'core';
    ws._bridgeOutbound = true;
    this.coreConnector = ws;
    this.attachClient(ws);
    ws.on('open', () => log('info', `connected outbound core: ${url.replace(/access_token=[^&]*/, 'access_token=***')}`));
    ws.on('close', () => {
      if (this.coreConnector === ws) this.coreConnector = null;
      if (!this.stopping && this.coreGeneration === generation) this.scheduleCoreReconnect();
    });
    ws.on('error', error => log('warn', 'outbound core websocket error', error));
  }
  scheduleCoreReconnect() {
    if (this.stopping || !CORE_URL || this.coreReconnectTimer) return;
    const generation = this.coreGeneration;
    this.coreReconnectTimer = setTimeout(() => {
      this.coreReconnectTimer = null;
      if (this.coreGeneration === generation) this.connectOutboundCore();
    }, 1000);
  }
  attachClient(ws) {
    if (ws._bridgeKind === 'core') {
      ws._bridgeCoreId = '';
      this.coreClients.add(ws);
      ws.on('message', data => this.handleCoreMessage(ws, data));
      ws.on('close', () => {
        this.coreClients.delete(ws);
        for (const [echo, target] of this.echoTargets) if (target === ws) this.echoTargets.delete(echo);
        for (const invocation of this.invocations.values()) if (invocation.coreWs === ws) invocation.fail(new Error('core websocket disconnected'));
      });
      ws.on('error', e => log('warn', 'core client error', e));
    } else if (ws._bridgeKind === 'onebot') {
      this.protocolClients.add(ws);
      ws.on('message', data => this.handleProtocolMessage(ws, data));
      ws.on('close', () => this.protocolClients.delete(ws));
      ws.on('error', e => log('warn', 'protocol client error', e));
    } else {
      this.controlClients.add(ws);
      ws.once('message', data => {
        const hello = parseMessage(data);
        if (!hello || hello.type !== 'hello' || hello.protocol !== 'aiplugin4-core-bridge' || (BRIDGE_TOKEN && hello.token !== BRIDGE_TOKEN)) {
          ws.close(1008, 'invalid hello'); return;
        }
        ws._bridgeHello = true;
        jsonSend(ws, { type: 'hello.ok', protocol: 'aiplugin4-core-bridge', version: 1, coreConnected: this.isCoreConnected() });
        ws.on('message', raw => this.handleControl(ws, raw));
      });
      ws.on('close', () => this.controlClients.delete(ws));
      ws.on('error', e => log('warn', 'control client error', e));
    }
  }
  coreForTarget(target) {
    const selfId = idString(target && (target.selfId ?? target.self_id));
    const exact = [...this.coreClients].find(ws => ws._bridgeCoreId && ws._bridgeCoreId === selfId);
    if (exact) return exact;
    if (this.coreClients.size === 1) return [...this.coreClients][0];
    return null;
  }
  coreForEvent(event) {
    const selfId = idString(event && event.self_id);
    return [...this.coreClients].find(ws => ws._bridgeCoreId && ws._bridgeCoreId === selfId) || (this.coreClients.size === 1 ? [...this.coreClients][0] : null);
  }
  ensureCore(target) {
    const core = this.coreForTarget(target);
    if (!core || core.readyState !== WebSocket.OPEN) throw new Error('SealDice 核心 WS 尚未连接到中转站');
    return core;
  }
  setCoreId(ws, value) {
    if (value !== undefined && value !== null && value !== '') ws._bridgeCoreId = idString(value);
  }
  broadcastProtocol(data, sourceCoreWs) {
    for (const ws of this.protocolClients) sendRaw(ws, data);
    return sourceCoreWs;
  }
  broadcastCore(data) {
    for (const ws of this.coreClients) sendRaw(ws, data);
  }
  handleCoreMessage(ws, data) {
    const packet = parseMessage(data);
    if (!packet) { this.broadcastProtocol(data, ws); return; }
    if (packet.self_id !== undefined) this.setCoreId(ws, packet.self_id);
    if (packet.data && packet.data.user_id !== undefined && packet.echo) this.setCoreId(ws, packet.data.user_id);
    if (packet.action && ACTIONS.has(packet.action)) {
      if (this.captureAction(ws, packet)) return;
      if (packet.echo) this.echoTargets.set(String(packet.echo), ws);
      this.broadcastProtocol(JSON.stringify(packet), ws);
      return;
    }
    if (packet.action && packet.echo) this.echoTargets.set(String(packet.echo), ws);
    if (packet.post_type === 'message' && this.captureEvent(ws, packet)) return;
    this.broadcastProtocol(JSON.stringify(packet), ws);
  }
  handleProtocolMessage(ws, data) {
    const packet = parseMessage(data);
    if (!packet) { this.broadcastCore(data); return; }
    if (packet.echo && this.echoTargets.has(String(packet.echo))) {
      const core = this.echoTargets.get(String(packet.echo));
      this.echoTargets.delete(String(packet.echo));
      if (packet.data && packet.data.user_id !== undefined) this.setCoreId(core, packet.data.user_id);
      sendRaw(core, JSON.stringify(packet));
      return;
    }
    const core = packet.self_id !== undefined ? this.coreForEvent(packet) : (this.coreClients.size === 1 ? [...this.coreClients][0] : null);
    if (core) sendRaw(core, JSON.stringify(packet));
    else this.broadcastCore(JSON.stringify(packet));
  }
  findInvocationForTarget(target, coreWs) {
    const matches = [...this.invocations.values()].filter(inv => !inv.completed && inv.matchesTarget(target, coreWs));
    if (matches.length > 1) matches.forEach(inv => { inv.ambiguous = true; });
    return matches.sort((a, b) => a.startedAt - b.startedAt)[0] || null;
  }
  captureEvent(coreWs, event) {
    const target = eventTarget(event);
    const invocation = this.findInvocationForTarget(target, coreWs);
    if (!invocation || invocation.mode === 'reply_only' && !this.hasReplyReference(event, invocation)) {
      if (invocation) invocation.ambiguous = true;
      return false;
    }
    const forwarded = invocation.forward;
    if (forwarded) {
      invocation.forwardedCount++;
      this.broadcastProtocol(JSON.stringify(event), coreWs);
    } else {
      invocation.interceptedCount++;
    }
    invocation.acceptMessage({ messageId: idString(event.message_id), segments: event.message || [], text: event.raw_message || textOfSegments(event.message) }, { source: 'event', forwarded, intercepted: !forwarded });
    return true;
  }
  hasReplyReference(event, invocation) {
    const segments = Array.isArray(event.message) ? event.message : [];
    return segments.some(segment => segment && segment.type === 'reply' && idString(segment.data && (segment.data.id || segment.data.message_id)) === idString(invocation.virtualMessageId));
  }
  captureAction(coreWs, packet) {
    const target = actionTarget(packet.action, packet.params || packet.data);
    const invocation = this.findInvocationForTarget(target, coreWs);
    if (!invocation) return false;
    const params = packet.params || packet.data || {};
    const segments = Array.isArray(params.message) ? params.message : [{ type: 'text', data: { text: extractText(params) } }];
    const messageId = idString(Bridge.nextMessageId++);
    const message = { messageId, action: packet.action, segments, text: extractText(params) };
    const forwarded = invocation.forward;
    if (forwarded) {
      invocation.forwardedCount++;
      if (packet.echo) this.echoTargets.set(String(packet.echo), coreWs);
      this.broadcastProtocol(JSON.stringify(packet), coreWs);
    } else {
      invocation.interceptedCount++;
      this.sendActionSuccess(coreWs, packet);
    }
    invocation.acceptMessage(message, { source: 'action', forwarded, intercepted: !forwarded });
    return true;
  }
  sendActionSuccess(coreWs, packet) {
    if (!packet.echo) return;
    jsonSend(coreWs, { status: 'ok', retcode: 0, data: { message_id: Bridge.nextMessageId++ }, echo: packet.echo });
  }
  async handleControl(ws, raw) {
    const request = parseMessage(raw);
    if (!request || request.type !== 'command.invoke' || !request.id || !request.command || !request.command.raw) { jsonSend(ws, { type: 'error', error: 'invalid command.invoke' }); return; }
    const lane = laneKey(request.target || {});
    const previous = this.lanes.get(lane) || Promise.resolve();
    const run = previous.catch(() => undefined).then(() => this.invoke(ws, request));
    const queued = run.finally(() => { if (this.lanes.get(lane) === queued) this.lanes.delete(lane); });
    this.lanes.set(lane, queued);
    await run.catch(e => jsonSend(ws, { type: 'command.result', id: String(request.id), ok: false, error: e.message }));
  }
  async invoke(ws, request) {
    const invocation = new Invocation(this, ws, request);
    invocation.startedAt = Date.now();
    invocation.virtualMessageId = nowSafeId();
    this.invocations.set(invocation.id, invocation);
    const resultPromise = invocation.start();
    try {
      const coreWs = this.ensureCore(request.target || {});
      invocation.coreWs = coreWs;
      const target = request.target || {};
      const messageType = target.messageType === 'private' ? 'private' : 'group';
      const peer = messageType === 'private' ? target.userId : target.groupId;
      if (peer === undefined || peer === null) throw new Error('target.groupId/userId is required');
      const raw = String(request.command.raw);
      const userId = Number(target.userId || 0);
      const fakeEvent = {
        time: Math.floor(Date.now() / 1000), self_id: Number(target.selfId || coreWs._bridgeCoreId || 0), post_type: 'message',
        message_type: messageType, sub_type: 'normal', message_id: invocation.virtualMessageId,
        user_id: userId, ...(messageType === 'group' ? { group_id: Number(peer) } : {}),
        message: [{ type: 'text', data: { text: raw } }], raw_message: raw, font: 0,
        sender: { user_id: userId, nickname: String(request.actor && request.actor.nickname || 'AI'), card: '', sex: 'unknown', age: 0, role: String(request.actor && request.actor.role || 'member'), title: '' }
      };
      sendRaw(coreWs, JSON.stringify(fakeEvent));
      jsonSend(ws, await resultPromise);
    } catch (e) {
      invocation.fail(e);
      jsonSend(ws, await resultPromise);
    } finally {
      this.invocations.delete(invocation.id);
    }
  }
}

if (require.main === module) {
  const bridge = new Bridge();
  bridge.start().catch(e => { log('error', 'failed to start', e); process.exitCode = 1; });
  process.on('SIGINT', () => bridge.stop().finally(() => process.exit(0)));
  process.on('SIGTERM', () => bridge.stop().finally(() => process.exit(0)));
}

module.exports = { Bridge, laneKey, textOfSegments };
