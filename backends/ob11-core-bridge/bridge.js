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
const MCP_PATH = process.env.AIPLUGIN4_BRIDGE_MCP_PATH || '/mcp';
const BRIDGE_TOKEN = process.env.AIPLUGIN4_BRIDGE_TOKEN || '';
const CORE_TOKEN = process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN ?? BRIDGE_TOKEN;
const PROTOCOL_URL = process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL || '';
const PROTOCOL_TOKEN = process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN || '';
const PROTOCOL_RECONNECT_MIN = 1000;
const PROTOCOL_RECONNECT_MAX = 30000;
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

const MCP_PUBLIC_INPUT_SCHEMA = z.object({
  action: z.enum(['list', 'call']).describe('list=返回可用模式说明；call=执行核心指令'),
  command: z.string().min(1).optional().describe('结构化模式的核心指令名，不含前缀'),
  args: z.array(z.string()).optional().describe('结构化模式的指令参数，按顺序填写'),
  raw_message: z.string().min(1).optional().describe('原始消息模式；会原样注入核心，不得与 command/args 同时使用'),
  maxMessages: z.number().int().min(1).max(MAX_MESSAGES).optional().describe('最多收集多少条消息'),
  settleMs: z.number().int().min(0).max(10000).optional().describe('收到消息后等待多久没有新消息才结束'),
  timeoutMs: z.number().int().min(100).max(120000).optional().describe('最长等待时间，单位毫秒'),
  captureMode: z.enum(['reply_only', 'lane']).optional().describe('消息捕获范围'),
  forward: z.boolean().optional().describe('是否把捕获到的消息继续转发给协议端'),
  triggerUserId: z.string().min(1).optional().describe('可选；注入消息的发送者/触发对象用户 ID'),
  atUserId: z.string().min(1).optional().describe('可选；注入消息中 @ 的用户 ID；群聊可用')
}).passthrough();

function validateMcpRequest(value) {
  const hasCommand = value.command !== undefined;
  const hasArgs = value.args !== undefined;
  const hasRawMessage = value.raw_message !== undefined;
  const hasExecutionOptions = value.maxMessages !== undefined
    || value.settleMs !== undefined
    || value.timeoutMs !== undefined
    || value.captureMode !== undefined
    || value.forward !== undefined
    || value.triggerUserId !== undefined
    || value.atUserId !== undefined;

  if (value.action === 'list') {
    if (hasCommand || hasArgs || hasRawMessage || hasExecutionOptions) {
      return 'action=list 不能携带执行参数';
    }
    return '';
  }

  if (hasRawMessage && (hasCommand || hasArgs)) {
    return 'raw_message 不能与 command/args 同时使用';
  } else if (!hasRawMessage && !hasCommand) {
    return 'action=call 必须提供 command 或 raw_message';
  }
  if (hasArgs && !hasCommand && !hasRawMessage) {
    return 'args 只能与 command 一起使用';
  }
  if (hasRawMessage && !String(value.raw_message).trim()) {
    return 'raw_message 不能为空';
  }
  return '';
}

function mcpResult(result) {
  return { content: [{ type: 'text', text: JSON.stringify(result) }] };
}

function normalizeMcpTarget(request) {
  const target = request && request.target;
  if (!target || typeof target !== 'object') throw new Error('target is required');
  const messageType = target.messageType === 'private' ? 'private' : target.messageType === 'group' ? 'group' : '';
  if (!messageType) throw new Error('target.messageType must be group or private');
  const selfId = idString(target.selfId);
  if (!selfId) throw new Error('target.selfId is required');
  const peer = messageType === 'private' ? target.userId : target.groupId;
  if (peer === undefined || peer === null || idString(peer) === '') {
    throw new Error(`target.${messageType === 'private' ? 'userId' : 'groupId'} is required`);
  }
  return {
    selfId,
    messageType,
    ...(messageType === 'private' ? { userId: idString(peer) } : { groupId: idString(peer) }),
    ...(target.userId !== undefined ? { userId: idString(target.userId) } : {})
  };
}

function normalizeMcpActor(request, target) {
  const actor = request && request.actor;
  if (!actor || typeof actor !== 'object') throw new Error('actor is required');
  return {
    userId: idString(request.triggerUserId || actor.userId || target.userId || target.selfId),
    nickname: idString(actor.nickname || 'AI'),
    role: idString(actor.role || 'member')
  };
}

function invocationFromMcp(request, id) {
  const target = normalizeMcpTarget(request);
  const actor = normalizeMcpActor(request, target);
  const forward = request.forward === true;
  const invocation = {
    target,
    actor,
    capture: {
      mode: request.captureMode || (forward ? 'lane' : 'reply_only'),
      forward,
      ...(request.maxMessages !== undefined ? { maxMessages: request.maxMessages } : {}),
      ...(request.settleMs !== undefined ? { settleMs: request.settleMs } : {})
    },
    ...(request.timeoutMs !== undefined ? { timeoutMs: request.timeoutMs } : {}),
    id,
    ...(request.triggerUserId !== undefined ? { triggerUserId: idString(request.triggerUserId) } : {}),
    ...(request.atUserId !== undefined ? { atUserId: idString(request.atUserId) } : {})
  };

  if (request.raw_message !== undefined) {
    return { ...invocation, raw_message: String(request.raw_message) };
  }

  const command = String(request.command).trim().replace(/^core\|/, '').trim();
  const args = Array.isArray(request.args) ? request.args.map(value => String(value)) : [];
  const prefix = request.__commandPrefix === undefined ? '.' : String(request.__commandPrefix);
  const raw = `${prefix}${command}${args.length ? ` ${args.join(' ')}` : ''}`.trim();
  return { ...invocation, command: { raw, name: command, args } };
}

function createMcpServer(bridge) {
  const server = new McpServer({ name: 'ob11-core-bridge', version: '1.0.6' });
  const call = async (request) => {
    try {
      const validationError = validateMcpRequest(request);
      if (validationError) return mcpResult({ ok: false, error: validationError, messages: [] });
      if (request.action === 'list') return mcpResult({ ok: true, action: 'list', modes: ['structured', 'raw_message'] });
      const result = await bridge.queueInvoke(invocationFromMcp(request, `core_${crypto.randomUUID()}`));
      return mcpResult(result);
    } catch (error) {
      return mcpResult({ ok: false, error: error instanceof Error ? error.message : String(error), messages: [] });
    }
  };
  server.registerTool('run_core_command', { inputSchema: MCP_PUBLIC_INPUT_SCHEMA }, call);
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
    this.protocolTimer = null;
    this.protocolReconnectMs = PROTOCOL_RECONNECT_MIN;
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
      const kind = CORE_PATHS.has(url.pathname) ? 'core' : '';
      if (!kind) { socket.destroy(); return; }
      const expectedToken = CORE_TOKEN;
      if (expectedToken && url.searchParams.get('access_token') !== expectedToken && req.headers.authorization !== `Bearer ${expectedToken}`) { socket.destroy(); return; }
      this.wss.handleUpgrade(req, socket, head, ws => {
        ws._bridgeKind = kind;
        this.wss.emit('connection', ws, req);
      });
    });
    this.wss.on('connection', (ws, req) => this.attachClient(ws, req));
    await new Promise(resolve => this.server.listen(PORT, HOST, resolve));
    log('info', `listening ${HOST}:${PORT}, core=${[...CORE_PATHS].join(',')}, protocol=${PROTOCOL_URL || '未配置'}, mcp=${MCP_PATH}`);
    this.startProtocolClient();
    return this;
  }
  async stop() {
    this.stopping = true;
    if (this.protocolTimer) { clearTimeout(this.protocolTimer); this.protocolTimer = null; }
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
  }
  attachProtocolClient(ws) {
    this.protocolClients.add(ws);
    ws.on('message', data => this.handleProtocolMessage(ws, data));
    ws.on('error', e => log('warn', 'protocol client error', e));
  }
  startProtocolClient() {
    if (!PROTOCOL_URL) {
      log('warn', '未配置协议端地址 AIPLUGIN4_BRIDGE_PROTOCOL_URL，中间件不会主动连接协议端');
      return;
    }
    this.protocolReconnectMs = PROTOCOL_RECONNECT_MIN;
    this.connectProtocolClient();
  }
  connectProtocolClient() {
    if (this.stopping) return;
    let url;
    try { url = new URL(PROTOCOL_URL); } catch (e) {
      log('error', `协议端地址无效: ${PROTOCOL_URL}`, e);
      return;
    }
    if (PROTOCOL_TOKEN && !url.searchParams.has('access_token')) url.searchParams.set('access_token', PROTOCOL_TOKEN);
    const ws = new WebSocket(url.toString(), PROTOCOL_TOKEN ? { headers: { Authorization: `Bearer ${PROTOCOL_TOKEN}` } } : undefined);
    ws.on('open', () => {
      if (this.stopping) { try { ws.close(); } catch (_) { /* ignore */ } return; }
      this.protocolReconnectMs = PROTOCOL_RECONNECT_MIN;
      log('info', `协议端已连接: ${PROTOCOL_URL}`);
      this.attachProtocolClient(ws);
    });
    ws.on('close', () => {
      this.protocolClients.delete(ws);
      if (this.stopping) return;
      log('warn', `协议端连接断开，${this.protocolReconnectMs / 1000}s 后重连: ${PROTOCOL_URL}`);
      this.scheduleProtocolReconnect();
    });
    ws.on('error', () => { /* 连接失败/中途错误随后触发 close，统一在 close 中处理重连 */ });
    ws.on('unexpected-response', (_req, res) => {
      log('error', `协议端握手失败 HTTP ${res.statusCode}（token 可能不正确）: ${PROTOCOL_URL}`);
    });
  }
  scheduleProtocolReconnect() {
    if (this.stopping || this.protocolTimer) return;
    this.protocolTimer = setTimeout(() => {
      this.protocolTimer = null;
      this.protocolReconnectMs = Math.min(this.protocolReconnectMs * 2, PROTOCOL_RECONNECT_MAX);
      this.connectProtocolClient();
    }, this.protocolReconnectMs);
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
      const raw = request.raw_message !== undefined
        ? String(request.raw_message)
        : String(request.command && request.command.raw || '');
      if (!raw) throw new Error('command.raw or raw_message is required');
      const triggerUserId = idString(request.triggerUserId || request.actor && request.actor.userId || target.userId || target.selfId);
      const userIdNumber = Number(triggerUserId);
      const userId = Number.isFinite(userIdNumber) && userIdNumber > 0 ? userIdNumber : triggerUserId;
      const atUserId = messageType === 'group' ? idString(request.atUserId) : '';
      const atSegment = atUserId ? [{ type: 'at', data: { qq: atUserId } }] : [];
      const atRaw = atUserId ? `[CQ:at,qq=${atUserId}] ` : '';
      const fakeEvent = {
        time: Math.floor(Date.now() / 1000), self_id: Number(target.selfId || coreWs._bridgeCoreId || 0), post_type: 'message',
        message_type: messageType, sub_type: 'normal', message_id: invocation.virtualMessageId,
        user_id: userId, ...(messageType === 'group' ? { group_id: Number(peer) } : {}),
        message: [...atSegment, { type: 'text', data: { text: raw } }], raw_message: `${atRaw}${raw}`, font: 0,
        sender: { user_id: userId, nickname: String(request.actor && request.actor.nickname || `用户${triggerUserId}`), card: '', sex: 'unknown', age: 0, role: String(request.actor && request.actor.role || 'member'), title: '' }
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
