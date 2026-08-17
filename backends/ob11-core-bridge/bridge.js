'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const { WebSocket, WebSocketServer } = require('ws');
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { z } = require('zod');

const HOST = process.env.AIPLUGIN4_BACKEND_HOST || '0.0.0.0';
const PORT = Number(process.env.AIPLUGIN4_BACKEND_PORT || 46880);
const CORE_PATH = process.env.AIPLUGIN4_BRIDGE_CORE_PATH || '/core';
const CORE_PATHS = new Set((process.env.AIPLUGIN4_BRIDGE_CORE_PATHS || `${CORE_PATH},${CORE_PATH.replace(/\/$/, '')}/ws`)
  .split(',').map(value => value.trim()).filter(Boolean));
const ONEBOT_PATH = process.env.AIPLUGIN4_BRIDGE_ONEBOT_PATH || '/onebot';
const MCP_PATH = process.env.AIPLUGIN4_BRIDGE_MCP_PATH || '/mcp';
const BRIDGE_TOKEN = process.env.AIPLUGIN4_BRIDGE_TOKEN || '';
const CORE_TOKEN = process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN ?? BRIDGE_TOKEN;
const PROTOCOL_TOKEN = process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN ?? BRIDGE_TOKEN;
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
  constructor(bridge, request) {
    this.bridge = bridge;
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

const MCP_TARGET_SCHEMA = z.object({
  selfId: z.string(),
  messageType: z.enum(['group', 'private']),
  groupId: z.string().optional(),
  userId: z.string().optional()
});
const MCP_ACTOR_SCHEMA = z.object({
  userId: z.string(),
  nickname: z.string(),
  role: z.string()
});
const MCP_COMMAND_SCHEMA = z.object({
  raw: z.string(),
  name: z.string(),
  args: z.array(z.string()).default([])
});
const MCP_CAPTURE_SCHEMA = z.object({
  mode: z.enum(['reply_only', 'lane']).optional(),
  forward: z.boolean().optional(),
  maxMessages: z.number().int().min(1).max(MAX_MESSAGES).optional(),
  settleMs: z.number().int().min(0).max(10000).optional()
}).optional();

function mcpResult(result) {
  return { content: [{ type: 'text', text: JSON.stringify(result) }] };
}

function createMcpServer(bridge) {
  const server = new McpServer({ name: 'ob11-core-bridge', version: '1.0.2' });
  const input = {
    target: MCP_TARGET_SCHEMA.describe('注入假消息的目标群/私聊'),
    actor: MCP_ACTOR_SCHEMA.describe('假消息发送者'),
    command: MCP_COMMAND_SCHEMA.describe('要注入核心的消息'),
    capture: MCP_CAPTURE_SCHEMA.describe('响应捕获与转发选项'),
    timeoutMs: z.number().int().min(100).max(120000).optional().describe('最长等待时间，单位毫秒')
  };
  const call = (kind) => async (request) => {
    try {
      const result = await bridge.queueInvoke({ ...request, id: `${kind}_${crypto.randomUUID()}` });
      return mcpResult(result);
    } catch (error) {
      return mcpResult({ ok: false, error: error instanceof Error ? error.message : String(error), messages: [] });
    }
  };
  server.tool('run_ext_command', input, call('ext'));
  server.tool('run_core_command', input, call('core'));
  return server;
}

class Bridge {
  static nextMessageId = 900000000000;
  constructor() {
    this.server = null;
    this.wss = new WebSocketServer({ noServer: true });
    this.coreClients = new Set();
    this.protocolClients = new Set();
    this.invocations = new Map();
    this.lanes = new Map();
    this.echoTargets = new Map();
    this.stopping = false;
    this.mcpTransports = new Map();
  }
  async start() {
    this.stopping = false;
    this.server = http.createServer((req, res) => {
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
      if (url.pathname === '/healthz') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true, coreConnected: this.isCoreConnected(), protocolConnected: this.protocolClients.size > 0, coreClients: this.coreClients.size, protocolClients: this.protocolClients.size }));
        return;
      }
      if (url.pathname === MCP_PATH) {
        if (!this.authorizeHttp(req, url)) {
          res.writeHead(401, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'unauthorized' }));
          return;
        }
        this.handleMcp(req, res).catch(error => {
          log('warn', 'MCP request failed', error);
          if (!res.headersSent) {
            res.writeHead(500, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ error: 'MCP request failed' }));
          }
        });
        return;
      }
      res.writeHead(404); res.end('not found');
    });
    this.server.on('upgrade', (req, socket, head) => {
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
      const kind = CORE_PATHS.has(url.pathname) ? 'core' : url.pathname === ONEBOT_PATH ? 'onebot' : '';
      if (!kind) { socket.destroy(); return; }
      const expectedToken = kind === 'core' ? CORE_TOKEN : PROTOCOL_TOKEN;
      if (expectedToken && url.searchParams.get('access_token') !== expectedToken && req.headers.authorization !== `Bearer ${expectedToken}`) { socket.destroy(); return; }
      this.wss.handleUpgrade(req, socket, head, ws => {
        ws._bridgeKind = kind;
        this.wss.emit('connection', ws, req);
      });
    });
    this.wss.on('connection', (ws, req) => this.attachClient(ws, req));
    await new Promise(resolve => this.server.listen(PORT, HOST, resolve));
    log('info', `listening ${HOST}:${PORT}, core=${[...CORE_PATHS].join(',')}, onebot=${ONEBOT_PATH}, mcp=${MCP_PATH}`);
    return this;
  }
  async stop() {
    this.stopping = true;
    for (const invocation of this.invocations.values()) invocation.fail(new Error('bridge stopped'));
    for (const transport of this.mcpTransports.values()) { try { await transport.close(); } catch (_) { /* ignore */ } }
    this.mcpTransports.clear();
    for (const ws of [...this.coreClients, ...this.protocolClients]) ws.close();
    await new Promise(resolve => this.server ? this.server.close(() => resolve()) : resolve());
  }
  authorizeHttp(req, url) {
    if (!BRIDGE_TOKEN) return true;
    const auth = req.headers.authorization || '';
    return auth === `Bearer ${BRIDGE_TOKEN}`
      || req.headers['x-token'] === BRIDGE_TOKEN
      || url.searchParams.get('access_token') === BRIDGE_TOKEN;
  }
  async handleMcp(req, res) {
    const requestedSessionId = req.headers['mcp-session-id'];
    let transport = requestedSessionId ? this.mcpTransports.get(String(requestedSessionId)) : null;
    if (!transport) {
      if (req.method !== 'POST') {
        res.writeHead(400, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'MCP session is required' }));
        return;
      }
      let sessionId = '';
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => crypto.randomUUID(),
        onsessioninitialized: id => { sessionId = id; this.mcpTransports.set(id, transport); }
      });
      transport.onclose = () => { if (sessionId) this.mcpTransports.delete(sessionId); };
      await createMcpServer(this).connect(transport);
    }
    await transport.handleRequest(req, res);
  }
  isCoreConnected() { return this.coreClients.size > 0; }
  attachClient(ws, req) {
    const path = req ? String(req.url || '').split('?')[0] : '';
    const ip = req && req.socket ? req.socket.remoteAddress : '';
    if (ws._bridgeKind === 'core') {
      ws._bridgeCoreId = '';
      this.coreClients.add(ws);
      log('info', `SealDice 核心已连接: ${path}${ip ? ` (${ip})` : ''}${CORE_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
      ws.on('message', data => this.handleCoreMessage(ws, data));
      ws.on('close', () => {
        this.coreClients.delete(ws);
        for (const [echo, target] of this.echoTargets) if (target === ws) this.echoTargets.delete(echo);
        for (const invocation of this.invocations.values()) if (invocation.coreWs === ws) invocation.fail(new Error('core websocket disconnected'));
        log('info', `SealDice 核心已断开: ${path}${ip ? ` (${ip})` : ''}${CORE_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
      });
      ws.on('error', e => log('warn', 'core client error', e));
    } else if (ws._bridgeKind === 'onebot') {
      this.protocolClients.add(ws);
      log('info', `协议端已连接: ${path}${ip ? ` (${ip})` : ''}${PROTOCOL_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
      ws.on('message', data => this.handleProtocolMessage(ws, data));
      ws.on('close', () => {
        this.protocolClients.delete(ws);
        log('info', `协议端已断开: ${path}${ip ? ` (${ip})` : ''}${PROTOCOL_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
      });
      ws.on('error', e => log('warn', 'protocol client error', e));
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
      if (this.protocolClients.size === 0) { this.failAction(ws, packet); return; }
      if (packet.echo) this.echoTargets.set(String(packet.echo), ws);
      this.broadcastProtocol(JSON.stringify(packet), ws);
      return;
    }
    if (packet.action && packet.echo) {
      if (this.protocolClients.size === 0) { this.failAction(ws, packet); return; }
      this.echoTargets.set(String(packet.echo), ws);
    }
    if (packet.post_type === 'message' && this.captureEvent(ws, packet)) return;
    this.broadcastProtocol(JSON.stringify(packet), ws);
  }
  failAction(ws, packet) {
    if (!packet.echo) return;
    log('warn', `API 请求 ${packet.action} 失败：无 OB11 协议端连接，已直接返回 failed`);
    jsonSend(ws, { status: 'failed', retcode: 100, data: null, echo: packet.echo });
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
  queueInvoke(request) {
    const lane = laneKey(request.target || {});
    const previous = this.lanes.get(lane) || Promise.resolve();
    const run = previous.catch(() => undefined).then(() => this.invoke(request));
    const queued = run.finally(() => { if (this.lanes.get(lane) === queued) this.lanes.delete(lane); });
    this.lanes.set(lane, queued);
    return run;
  }
  async invoke(request) {
    const invocation = new Invocation(this, request);
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
      return await resultPromise;
    } catch (e) {
      invocation.fail(e);
      return await resultPromise;
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
