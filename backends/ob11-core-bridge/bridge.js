'use strict';

// ob11 核心桥：两种模式共用同一个 WS 端点。
//
//   relay       —— 1.1.1 的行为，逐字保留：桥在中间做 API 中转 + 假消息注入/捕获，
//                  并主动出站连接真实 OB11 协议端。老用户 protocol_url 非空即自动进这个模式。
//   standalone  —— 本改造的主形态：桥自己就是一个 OB11 协议端。海豹正向 WS 连进来，
//                  action 全在本地应答（responder.js），事件只在虚拟世界里产生。
//                  不再拦截、不再向任何真实协议端注入，也不建任何出站连接。
//
// 模式判别（00 §8.1）：
//     AIPLUGIN4_BRIDGE_MODE 取值 relay/standalone → 听它
//     否则 PROTOCOL_URL 非空 → relay；为空 → standalone
//
// 依赖方向：bridge.js → responder.js → virtual-world.js（单向、无环）。

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { WebSocket, WebSocketServer } = require('ws');

const { VirtualWorld } = require('./virtual-world');
const { Responder, NAMED_ACTIONS, textOfSegments } = require('./responder');

const HOST = process.env.AIPLUGIN4_BACKEND_HOST || '0.0.0.0';
const PORT = Number(process.env.AIPLUGIN4_BACKEND_PORT || 46880);
// 端点路由（REQ-D1-14/15）：核心端**不占用任何路径**。
//
// 历史实现要求核心侧连 `/core`（或 `/core/ws`），不对就 socket.destroy() ——
// 结果是用户在「目标地址」里填 `ws://host:port` 永远连不上，得靠文档教他补后缀。
// 但桥根本不需要靠路径区分来客：来客只有两类，`/plugin` 是插件（自己要握手 hello），
// 其余全部是核心。所以这里只把 `/plugin` 挑出来，剩下的统统当核心端，
// 用户填 `ws://host:port`、`ws://host:port/`、`ws://host:port/core`、随便什么路径都通。
const PLUGIN_PATH = process.env.AIPLUGIN4_BRIDGE_PLUGIN_PATH || '/plugin';
const CORE_TOKEN = process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN || '';
const PLUGIN_TOKEN = process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN || '';
// relay 专用（standalone 下不会被读取）
const PROTOCOL_URL = process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL || '';
const PROTOCOL_TOKEN = process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN || '';
const PROTOCOL_RECONNECT_MIN = 1000;
const PROTOCOL_RECONNECT_MAX = 30000;

// 平台名与虚拟世界：全部用户可填，默认值只是开箱即用（REQ-D2-13/16）。
//
// 注意这里**刻意不用 `||`**：`||` 会把"用户清空了输入框"（空字符串）当成"没填"从而静默回退默认值，
// 而 REQ-D2-17 把空值列为 5 类非法配置之一 —— 必须让空字符串原样传给 validate() 报错。
// 只有 `undefined`（真的没设置这个环境变量）才走默认值。
const envOr = (name, fallback) => (process.env[name] === undefined ? fallback : process.env[name]);
const PLATFORM = (envOr('AIPLUGIN4_BRIDGE_PLATFORM', 'QQ') || '').trim() || 'QQ';
const MODE_RAW = envOr('AIPLUGIN4_BRIDGE_MODE', '');
const mode = MODE_RAW === 'relay' || MODE_RAW === 'standalone'
  ? MODE_RAW
  : (PROTOCOL_URL ? 'relay' : 'standalone');
const STATE_DIR = envOr('AIPLUGIN4_BRIDGE_STATE_DIR', '.state');
const VIRTUAL = {
  selfId: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID', '10001'),
  nickname: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME', 'AI骰娘'),
  selfRole: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ROLE', 'admin'),
  groupId: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID', '20002'),
  groupName: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_NAME', 'AI 测试群'),
  peerId: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID', '30002'),
  maxMemberCount: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT', '200')
};

const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_SETTLE_MS = 400;
const MAX_MESSAGES = 50;
// 传输层保活：每 30s ping，连续 2 次无 pong（≈90s）才判死（REQ-D1-09）。
// 之所以不能按"最近收到核心帧"计时：核心没有应用层心跳，空闲但健康的连接上没有任何上行帧。
const KEEPALIVE_MS = Number(process.env.AIPLUGIN4_BRIDGE_KEEPALIVE_MS || 30000);
const KEEPALIVE_MISS_LIMIT = 2;
// 全部需要记账/本地应答的 action（REQ-D3-03：语义从"要拦截转发"改为"要记账"）
const ACTIONS = new Set(NAMED_ACTIONS);

const log = (level, message, error) => {
  const suffix = error ? ` ${error instanceof Error ? error.stack || error.message : String(error)}` : '';
  console[level](`[ob11-core-bridge] ${message}${suffix}`);
};

/** 读 backend.json 的 version —— get_version_info 与 hello_result 的唯一对外事实源。 */
function readBridgeVersion() {
  try {
    return String(JSON.parse(fs.readFileSync(path.join(__dirname, 'backend.json'), 'utf8')).version || '');
  } catch (_) {
    try { return String(JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8')).version || ''); }
    catch (_) { return ''; }
  }
}
const BRIDGE_VERSION = readBridgeVersion() || '2.0.0';

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
function extractText(params) {
  if (!params) return '';
  if (typeof params.message === 'string') return params.message;
  return textOfSegments(params.message);
}

class Invocation {
  constructor(bridge, request) {
    this.bridge = bridge;
    this.id = String(request.id || `invoke_${crypto.randomUUID()}`);
    this.target = request.target || {};
    this.lane = laneKey(this.target);
    this.capture = request.capture || {};
    this.mode = this.capture.mode || 'reply_only';
    this.forward = this.capture.forward !== false;
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

/**
 * `raw_message` 与 `command.raw` 互斥等既有校验（REQ-D6-05）。
 * standalone 下允许用 `source` 代替 `target`（真实 ID 只作投影输入，REQ-D7-02）。
 */
function validateWsRequest(value) {
  if (!value || typeof value !== 'object') return 'payload is required';
  const hasSource = value.source && typeof value.source === 'object';
  if (!hasSource) {
    const target = value.target;
    if (!target || typeof target !== 'object') return 'target is required';
  }
  const hasRaw = value.raw_message !== undefined;
  const command = value.command || {};
  const hasCommandRaw = command.raw !== undefined;
  if (hasRaw && hasCommandRaw) return 'raw_message cannot be combined with command.raw';
  if (!hasRaw && !hasCommandRaw) return 'command.raw or raw_message is required';
  if (hasRaw && !String(value.raw_message).trim()) return 'raw_message cannot be empty';
  if (hasCommandRaw && !String(command.raw).trim()) return 'command.raw cannot be empty';
  return '';
}

function normalizeTarget(request) {
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

function normalizeActor(request, target) {
  const actor = request && request.actor;
  if (!actor || typeof actor !== 'object') throw new Error('actor is required');
  return {
    userId: idString(request.trigger || actor.userId || target.userId || target.selfId),
    nickname: idString(actor.nickname || 'AI'),
    role: idString(actor.role || 'member')
  };
}

function invocationFromWs(request, id) {
  const target = normalizeTarget(request);
  const actor = normalizeActor(request, target);
  const capture = request.capture || {};
  const forward = capture.forward !== false;
  const invocation = {
    target,
    actor,
    capture: {
      mode: capture.mode || (forward ? 'lane' : 'reply_only'),
      forward,
      ...(capture.maxMessages !== undefined ? { maxMessages: Number(capture.maxMessages) } : {}),
      ...(capture.settleMs !== undefined ? { settleMs: Number(capture.settleMs) } : {})
    },
    ...(request.timeoutMs !== undefined ? { timeoutMs: Number(request.timeoutMs) } : {}),
    id,
    ...(request.trigger !== undefined ? { trigger: idString(request.trigger) } : {}),
    ...(Array.isArray(request.at) ? { at: request.at.map(idString).filter(Boolean) } : {})
  };

  if (request.raw_message !== undefined) {
    return { ...invocation, raw_message: String(request.raw_message) };
  }

  const command = request.command || {};
  const raw = command.raw !== undefined ? String(command.raw) : '';
  const name = command.name !== undefined ? String(command.name) : '';
  const args = Array.isArray(command.args) ? command.args.map(value => String(value)) : [];
  return { ...invocation, command: { raw, name, args } };
}

/** token 宽松比较（REQ-D1-04）：裸值或 `Bearer <token>` 都认，双来源独立判定。 */
const stripBearer = value => String(value || '').trim().replace(/^Bearer\s+/i, '');
function tokenMatches(expected, req, url) {
  if (!expected) return true; // 未配置 token → 不校验（现状语义）
  const want = stripBearer(expected);
  const fromQuery = url.searchParams.get('access_token');
  const fromHeader = stripBearer(req.headers.authorization);
  // 任一来源匹配即通过。注意不能写成 `query || header`，那是行为收缩：
  // "query 带旧错值 + header 带正确值"今天能过，`||` 合并后会被拒（海豹走 header、插件走 query）。
  return [fromQuery, fromHeader].some(value => value && stripBearer(value) === want);
}

class Bridge {
  constructor() {
    this.server = null;
    this.wss = new WebSocketServer({ noServer: true });
    this.coreClients = new Set();
    this.pluginClients = new Set();
    // relay 专用字段：standalone 下保留属性名但取 null（不是空 Set —— /healthz 要输出 null，
    // 且 deepEqual 用例不接受 0/false/[]，见 REQ-D10-01）
    this.protocolClients = mode === 'relay' ? new Set() : null;
    this.echoTargets = mode === 'relay' ? new Map() : null;
    this.droppedProtocolEvents = mode === 'relay' ? 0 : null;
    this.invocations = new Map();
    this.lanes = new Map();
    this.stopping = false;

    this.protocolTimer = null;
    this.protocolReconnectMs = PROTOCOL_RECONNECT_MIN;
    this.keepaliveTimer = null;

    // 虚拟世界与应答机：standalone 的主战场。relay 下也建（无害），
    // 但只有 standalone 分支会用到它们。
    this.mode = mode;
    this.platform = PLATFORM;
    this.corePort = PORT;
    this.world = new VirtualWorld({
      stateDir: STATE_DIR,
      platform: PLATFORM,
      selfId: VIRTUAL.selfId,
      nickname: VIRTUAL.nickname,
      selfRole: VIRTUAL.selfRole,
      groupId: VIRTUAL.groupId,
      groupName: VIRTUAL.groupName,
      peerId: VIRTUAL.peerId,
      maxMemberCount: VIRTUAL.maxMemberCount
    });
    if (this.mode === 'standalone') this.world.load();
    this.responder = new Responder(this.world, { version: BRIDGE_VERSION });
    // 老插件（payload 里没有 source）会走"真实 ID 直接用"的降级路径，必须能被外部看见
    this.legacyTarget = false;
    // 老插件降级提示只打一次，避免刷屏
    this._legacyWarned = false;
  }

  async start() {
    // 自检必须早于 createServer：非法配置时 this.server === null，测试才能断言这一点
    if (this.mode === 'standalone') {
      const problems = this.world.validate();
      if (problems.length > 0) {
        for (const problem of problems) log('error', `虚拟世界配置非法：${problem}`);
        throw new Error('虚拟世界配置非法');
      }
      if (this.platform !== 'QQ') {
        log('warn', `platform=${this.platform} 仅影响桥侧标识（日志//healthz/hello_result），海豹侧仍显示 QQ；如需改变海豹侧平台请改用对应协议的适配器`);
      }
      log('info', `standalone 虚拟账号：${this.world.displayId('user', this.world.selfId)}（${this.world.nickname}）`);
      log('info', `DiceMasters 建议添加：QQ:${this.world.selfId}（否则 MasterCheck 永不命中，PrivilegeLevel 上不到 100）`);
    }

    this.stopping = false;
    this.server = http.createServer((req, res) => {
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
      if (url.pathname === '/healthz') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify(this.health()));
        return;
      }
      res.writeHead(404); res.end('not found');
    });
    this.server.on('upgrade', (req, socket, head) => {
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
      // 只有 /plugin 需要认领；其余路径（含无路径、`/`、`/core`、`/core/ws`、任意自定义）一律是核心端。
      // 不在路径上拒绝连接：核心端是本进程的主要来客，拒绝只会让用户看到"连不上"却不知为何。
      const kind = url.pathname === PLUGIN_PATH ? 'plugin' : 'core';
      const expectedToken = kind === 'plugin' ? PLUGIN_TOKEN : CORE_TOKEN;
      if (!tokenMatches(expectedToken, req, url)) {
        // 静默 destroy 是历史行为，用户只能看到"连不上"；这里留痕但不回显 token 明文（REQ-D1-10）
        log('warn', `${kind} 端鉴权失败（token 不匹配），已拒绝连接：${url.pathname}${req.socket && req.socket.remoteAddress ? ` from ${req.socket.remoteAddress}` : ''}`);
        socket.destroy();
        return;
      }
      this.wss.handleUpgrade(req, socket, head, ws => {
        ws._bridgeKind = kind;
        this.wss.emit('connection', ws, req);
      });
    });
    this.wss.on('connection', (ws, req) => this.attachClient(ws, req));
    // listen 失败必须变成 reject，而不是 Node 默认的 'error' → uncaughtException（整个进程带走）。
    // CI 上真踩过：两个测试进程抢同一个端口，EADDRINUSE 直接把测试跑成 uncaughtException。
    try {
      await new Promise((resolve, reject) => {
        const onError = err => { this.server.removeListener('listening', onListening); reject(err); };
        const onListening = () => { this.server.removeListener('error', onError); resolve(); };
        this.server.once('error', onError);
        this.server.once('listening', onListening);
        this.server.listen(PORT, HOST);
      });
    } catch (err) {
      this.server = null;
      throw err;
    }
    log('info', `listening ${HOST}:${PORT}, mode=${this.mode}, core=<任意路径/无路径>, plugin=${PLUGIN_PATH}, protocol=${PROTOCOL_URL || '未配置'}`);
    if (this.mode === 'relay') this.startProtocolClient();
    else this.startKeepalive();
    return this;
  }

  /** /healthz：16 字段（8 新增 + 5 既有 + 3 遗留置 null），REQ-D10-01。 */
  health() {
    const counters = this.responder.counters();
    const standalone = this.mode === 'standalone';
    return {
      ok: true,
      coreConnected: this.isCoreConnected(),
      coreClients: this.coreClients.size,
      pluginConnected: this.pluginClients.size > 0,
      pluginClients: this.pluginClients.size,
      mode: this.mode,
      platform: this.platform,
      corePort: this.corePort,
      virtualSelfId: this.world.selfId || null,
      actionCount: counters.actionCount,
      eventCount: counters.eventCount,
      ledgerSize: counters.ledgerSize,
      legacyTarget: !!this.legacyTarget,
      // 三个 relay 遗留字段：standalone 下一律 null（不是 0/false），字段名不许消失
      protocolConnected: standalone ? null : this.protocolClients.size > 0,
      protocolClients: standalone ? null : this.protocolClients.size,
      droppedProtocolEvents: standalone ? null : this.droppedProtocolEvents
    };
  }

  async stop() {
    this.stopping = true;
    if (this.protocolTimer) { clearTimeout(this.protocolTimer); this.protocolTimer = null; }
    if (this.keepaliveTimer) { clearInterval(this.keepaliveTimer); this.keepaliveTimer = null; }
    for (const invocation of this.invocations.values()) invocation.fail(new Error('bridge stopped'));

    const sockets = [...this.coreClients, ...this.pluginClients, ...(this.protocolClients || [])];
    for (const ws of sockets) ws.close();
    await new Promise(resolve => this.server ? this.server.close(() => resolve()) : resolve());
    // standalone 下把虚拟世界落盘（映射表必须跨重启稳定，REQ-D2-02/03）
    if (this.mode === 'standalone') this.world.save();
  }
  isCoreConnected() { return this.coreClients.size > 0; }

  /**
   * 传输层保活（REQ-D1-09）。
   * 每 KEEPALIVE_MS 对每条 core 连接 ping 一次；收到 pong 就把 miss 计数清零；
   * 连续 KEEPALIVE_MISS_LIMIT 次没 pong 才 close（默认 30s × 2 ≈ 90s）。
   * 核心是 gorilla 客户端、默认自动回 pong，因此无需改核心。
   */
  startKeepalive() {
    if (!(KEEPALIVE_MS > 0)) return;
    this.keepaliveTimer = setInterval(() => {
      for (const ws of this.coreClients) {
        if (ws.readyState !== WebSocket.OPEN) continue;
        if ((ws._bridgePongMiss || 0) >= KEEPALIVE_MISS_LIMIT) {
          log('warn', `核心连接连续 ${KEEPALIVE_MISS_LIMIT} 次未回 pong（约 ${Math.round(KEEPALIVE_MS * (KEEPALIVE_MISS_LIMIT + 1) / 1000)}s），判定半开并关闭`);
          try { ws.close(); } catch (_) { /* ignore */ }
          continue;
        }
        ws._bridgePongMiss = (ws._bridgePongMiss || 0) + 1;
        try { ws.ping(); } catch (_) { /* ignore */ }
      }
    }, KEEPALIVE_MS);
    // 不让保活定时器吊住进程退出
    if (typeof this.keepaliveTimer.unref === 'function') this.keepaliveTimer.unref();
  }

  attachPluginClient(ws, req, path_, ip) {
    this.pluginClients.add(ws);
    log('info', `插件已连接: ${path_}${ip ? ` (${ip})` : ''}${PLUGIN_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
    ws.on('message', data => this.handlePluginMessage(ws, data));
    ws.on('close', () => {
      this.pluginClients.delete(ws);
      log('info', `插件已断开: ${path_}${ip ? ` (${ip})` : ''}`);
    });
    ws.on('error', e => log('warn', 'plugin client error', e));
  }

  handlePluginMessage(ws, data) {
    const packet = parseMessage(data);
    if (!packet) return;
    if (packet.type === 'ping') {
      jsonSend(ws, { type: 'pong' });
      return;
    }
    // hello 不参与 requestId/pending 那套请求-响应配对，必须在那一整套逻辑之前分支掉：
    // 放在后面会被"取不到 pending 就丢弃"的逻辑静默吞掉（REQ-D6-01 / REQ-D7-09 ①）
    if (packet.type === 'hello') {
      jsonSend(ws, {
        type: 'hello_result',
        requestId: idString(packet.requestId),
        payload: {
          mode: this.mode,
          platform: this.platform,
          corePort: this.corePort,
          virtualSelfId: this.world.selfId || null,
          virtualNickname: this.world.nickname,
          protocolVersion: 'v11',
          capabilities: this.mode === 'standalone'
            ? ['virtual-world', 'stable-ids', 'lane-capture', 'local-actions']
            : ['relay', 'lane-capture'],
          bridgeVersion: BRIDGE_VERSION
        }
      });
      return;
    }
    if (packet.type !== 'core_command') {
      log('debug', `插件报文类型未知，已忽略：${String(packet.type || '')}`);
      return;
    }

    const requestId = idString(packet.requestId);
    if (!requestId) {
      jsonSend(ws, { type: 'core_command_error', requestId: '', error: 'requestId is required' });
      return;
    }
    let request = packet.payload || {};
    const sendError = error => jsonSend(ws, {
      type: 'core_command_error',
      requestId,
      error: error instanceof Error ? error.message : String(error)
    });
    const validationError = validateWsRequest(request);
    if (validationError) {
      sendError(validationError);
      return;
    }
    try {
      request = this.projectRequest(request);
    } catch (e) {
      sendError(e);
      return;
    }
    this.queueInvoke(invocationFromWs(request, `core_${crypto.randomUUID()}`))
      .then(result => jsonSend(ws, { type: 'core_command_result', requestId, result }))
      .catch(sendError);
  }

  /**
   * 把插件报文投影成"虚拟世界里的目标"。
   *
   * standalone + `source`：真实 ID → 虚拟 ID（投影失败即抛，由调用方转 core_command_error，
   *   绝不允许把非数字真实 ID 送进核心，REQ-D2-10）。
   * standalone 无 `source`：老插件降级，直接用 `target` 里的真实 ID，并置 legacyTarget。
   * relay：原样返回（relay 语义一字不动）。
   */
  projectRequest(request) {
    if (this.mode !== 'standalone') return request;
    const source = request.source && typeof request.source === 'object' ? request.source : null;
    if (!source) {
      if (!this._legacyWarned) {
        this._legacyWarned = true;
        log('warn', '收到不带 source 的 core_command（老版本插件）：虚拟 ID 无法投影，本次直接沿用真实 ID。请升级插件到 4.32.0+');
      }
      this.legacyTarget = true;
      return request;
    }
    const projected = this.world.project(source);
    const peerTarget = projected.messageType === 'group'
      ? { groupId: String(projected.groupId) }
      : { userId: String(projected.userId) };
    const at = Array.isArray(request.at)
      ? request.at.map(value => {
        try { return String(this.world.virtualUserId(value)); } catch (_) { return null; }
      }).filter(Boolean)
      : undefined;
    return {
      ...request,
      target: {
        selfId: String(projected.selfId),
        messageType: projected.messageType,
        userId: String(projected.userId),
        ...peerTarget
      },
      trigger: String(projected.userId),
      ...(at ? { at } : {})
    };
  }

  attachClient(ws, req) {
    const path_ = req ? String(req.url || '').split('?')[0] : '';
    const ip = req && req.socket ? req.socket.remoteAddress : '';
    if (ws._bridgeKind === 'plugin') {
      this.attachPluginClient(ws, req, path_, ip);
      return;
    }

    // standalone 单核心门禁（REQ-D1-05）：第二条连接直接 1013 拒掉，不进 Set、不挂监听
    if (this.mode === 'standalone' && this.coreClients.size >= 1) {
      log('warn', `standalone 模式仅支持单个 SealDice 核心，已拒绝第 ${this.coreClients.size + 1} 条连接${ip ? ` (${ip})` : ''}`);
      try {
        jsonSend(ws, { status: 'failed', retcode: 1013, data: { wording: 'standalone 模式仅支持单个 SealDice 核心' }, echo: undefined });
        ws.close(1013, 'standalone 模式仅支持单个 SealDice 核心');
      } catch (_) { /* ignore */ }
      return;
    }

    ws._bridgeCoreId = '';
    ws._bridgePongMiss = 0;
    this.coreClients.add(ws);
    log('info', `SealDice 核心已连接: ${path_}${ip ? ` (${ip})` : ''}${CORE_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
    ws.on('message', data => this.handleCoreMessage(ws, data));
    ws.on('pong', () => { ws._bridgePongMiss = 0; });
    ws.on('close', () => {
      this.coreClients.delete(ws);
      if (this.echoTargets) {
        for (const [echo, target] of this.echoTargets) if (target === ws) this.echoTargets.delete(echo);
      }
      for (const invocation of this.invocations.values()) if (invocation.coreWs === ws) invocation.fail(new Error('core websocket disconnected'));
      log('info', `SealDice 核心已断开: ${path_}${ip ? ` (${ip})` : ''}${CORE_TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
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
    if (!packet) {
      if (this.mode === 'relay') { this.broadcastProtocol(data, ws); return; }
      // standalone：裸帧/非 JSON 一律丢弃并 warn（REQ-D4-08），不得 crash
      log('warn', '核心发来非 JSON 帧，已丢弃');
      return;
    }
    if (this.mode === 'relay') return this.handleCoreMessageRelay(ws, packet);
    // ── standalone ──
    // 核心作为客户端时不会向端点推事件；非 action 帧一律忽略（不再 broadcast）
    if (!packet.action) {
      log('debug', `standalone 忽略非 action 帧（post_type=${String(packet.post_type || '')}）`);
      return;
    }
    this.handleCoreAction(ws, packet);
  }

  /** standalone 的 action 分支：本地应答 + 记账 + lane 通知。 */
  handleCoreAction(ws, packet) {
    let outcome;
    try {
      outcome = this.responder.handle(packet);
    } catch (e) {
      log('error', `本地应答异常：${packet.action}`, e);
      if (packet.echo !== undefined) {
        jsonSend(ws, { status: 'failed', retcode: 1500, data: { wording: '桥内部异常' }, echo: packet.echo });
      }
      return;
    }
    if (outcome.reply !== undefined) jsonSend(ws, outcome.reply);
    if (outcome.messageId === undefined) return;
    // 记账成功后：把消息投给同 lane 的 Invocation（Invocation 是 Bridge 的字段，
    // 所以由这里通知，responder.js 只负责返回结论 —— 见 01 §3.2 的单向依赖约束）
    this.deliverToLane(outcome);
  }

  deliverToLane(outcome) {
    const target = outcome.peer;
    if (!target) return;
    const coreWs = this.coreClients.size === 1 ? [...this.coreClients][0] : null;
    const invocation = this.findInvocationForTarget(target, coreWs);
    if (!invocation) return;
    // 不累加 interceptedCount：standalone 下"拦截"这个概念不存在 —— 消息由本端全权受理，
    // 没有"从协议端嘴里抢下来"这回事（REQ-D6-04 要求该字段恒 0，真实条数看 deliveredCount）。
    const accepted = invocation.acceptMessage({
      messageId: idString(outcome.messageId),
      segments: outcome.segments || [],
      text: outcome.text || ''
    }, { source: 'action', forwarded: false, intercepted: false });
    // eventCount 的口径是"真正投递给调用方的消息条数"（/healthz 字段，REQ-D10-01），
    // 不是"调用次数"：调用被 timeout/队列丢弃时不该计数。
    if (accepted) this.responder.eventCount += 1;
  }

  /** relay 分支：1.1.1 的 handleCoreMessage 主体，逐字保留。 */
  handleCoreMessageRelay(ws, packet) {
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
    // 实时事件（message/notice/request/meta_event）一律不转发给核心：
    // 海豹需另接一条直连协议端的 WS 作为消息入口，桥只负责 API 中转与假消息注入/捕获，
    // 避免同一条事件经桥再进一次海豹导致双份处理（双连接冗余模式）。
    if (packet.post_type) {
      this.droppedProtocolEvents++;
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
    // 与 sendActionSuccess 共用同一个号（REQ-D5-02）：先发号，再记账，再回包
    const messageId = String(this.world.allocateMessageId());
    const message = { messageId, action: packet.action, segments, text: extractText(params) };
    const forwarded = invocation.forward;
    if (forwarded) {
      invocation.forwardedCount++;
      if (packet.echo) this.echoTargets.set(String(packet.echo), coreWs);
      this.broadcastProtocol(JSON.stringify(packet), coreWs);
    } else {
      invocation.interceptedCount++;
      this.sendActionSuccess(coreWs, packet, messageId);
    }
    invocation.acceptMessage(message, { source: 'action', forwarded, intercepted: !forwarded });
    return true;
  }
  sendActionSuccess(coreWs, packet, messageId) {
    if (!packet.echo) return;
    jsonSend(coreWs, { status: 'ok', retcode: 0, data: { message_id: Number(messageId) }, echo: packet.echo });
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
    // 唯一发号器（REQ-D5-01/05/08）：事件号与下行号同源、同号段、全部 JS 安全整数
    invocation.virtualMessageId = this.world.allocateMessageId();
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
      const trigger = idString(request.trigger || request.actor && request.actor.userId || target.userId || target.selfId);
      const standalone = this.mode === 'standalone';
      let userId;
      if (standalone) {
        // 投影表保证 userId 恒为正整数（REQ-D2-09/10）；这里再兜一次，不允许字符串进核心
        const numeric = Number(trigger);
        if (!Number.isSafeInteger(numeric) || numeric <= 0) throw new Error(`standalone 模式下 trigger 必须是正整数虚拟 ID：${trigger}`);
        userId = numeric;
      } else {
        const userIdNumber = Number(trigger);
        userId = Number.isFinite(userIdNumber) && userIdNumber > 0 ? userIdNumber : trigger;
      }
      const selfId = standalone ? this.world.selfId : Number(target.selfId || coreWs._bridgeCoreId || 0);
      const atUserIds = messageType === 'group' && Array.isArray(request.at) ? request.at.map(idString).filter(Boolean) : [];
      const atSegment = atUserIds.map(qq => ({ type: 'at', data: { qq } }));
      const atRaw = atUserIds.map(qq => `[CQ:at,qq=${qq}]`).join(' ');
      const atPrefix = atRaw ? `${atRaw} ` : '';
      const fakeEvent = {
        time: Math.floor(Date.now() / 1000), self_id: selfId, post_type: 'message',
        message_type: messageType, sub_type: 'normal', message_id: invocation.virtualMessageId,
        user_id: userId, ...(messageType === 'group' ? { group_id: Number(peer) } : {}),
        message: [...atSegment, { type: 'text', data: { text: raw } }], raw_message: `${atPrefix}${raw}`, font: 0,
        sender: { user_id: userId, nickname: String(request.actor && request.actor.nickname || `用户${trigger}`), card: '', sex: 'unknown', age: 0, role: String(request.actor && request.actor.role || 'member'), title: '' }
      };
      sendRaw(coreWs, JSON.stringify(fakeEvent));
      const result = await resultPromise;
      if (standalone) {
        // 虚拟世界在 standalone 下是本端事实源：投影产生的新映射要落盘（REQ-D2-02/03）。
        // eventCount 不在这里加 —— 它由 deliverToLane() 按"真正投递成功的消息条数"累加。
        this.world.save();
        return this.decorateResult(result, request);
      }
      return result;
    } catch (e) {
      invocation.fail(e);
      return await resultPromise;
    } finally {
      this.invocations.delete(invocation.id);
    }
  }

  /** standalone 结果补充 mode / virtualTarget / deliveredCount（REQ-D6-04，只增不改）。 */
  decorateResult(result, request) {
    const target = request.target || {};
    const delivered = Array.isArray(result.messages) ? result.messages.length : 0;
    return {
      ...result,
      mode: this.mode,
      deliveredCount: delivered,
      virtualTarget: {
        selfId: this.world.selfId,
        messageType: target.messageType === 'private' ? 'private' : 'group',
        ...(target.messageType === 'private'
          ? { userId: Number(target.userId) }
          : { groupId: Number(target.groupId) })
      },
      // 这三个字段在 standalone 下语义不存在，"收下几条"由 deliveredCount 表达（REQ-D6-04）：
      //   ambiguous        —— 歧义来自"无法从 OB11 action 区分本次响应与外部并发发送"，
      //                       standalone 只有一个来源（本桥自己的账本），不可能歧义；
      //   forwardedCount   —— 没有真实协议端，恒 0；
      //   interceptedCount —— "拦截"这个概念同样消失（全部消息都由本端受理），恒 0。
      // 之所以不暴露真实计数：老插件会拿 interceptedCount 判断"是否被拦截"，
      // 在 standalone 下给出 0 才能让它走"消息已收下"的新分支；真实条数看 deliveredCount。
      ambiguous: false,
      forwardedCount: 0,
      interceptedCount: 0
    };
  }
}

if (require.main === module) {
  const bridge = new Bridge();
  bridge.start().catch(e => { log('error', 'failed to start', e); process.exitCode = 1; });
  process.on('SIGINT', () => bridge.stop().finally(() => process.exit(0)));
  process.on('SIGTERM', () => bridge.stop().finally(() => process.exit(0)));
}

module.exports = { Bridge, laneKey, textOfSegments, mode, platform: PLATFORM, VirtualWorld, Responder, BRIDGE_VERSION };
