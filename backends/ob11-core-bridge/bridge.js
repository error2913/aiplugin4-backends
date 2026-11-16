'use strict';

// ob11 核心桥：**只做一件事** —— 自己就是一个 OB11 协议端。
//
// 海豹（SealDice）用一条正向 WS 连进来，桥把每个 action 在本地答掉（responder.js），
// 事件只在本进程的虚拟世界里产生。不出站、不中转、不拦截、不注入。
//
// 端点路由：`/plugin` 是插件控制面，`/healthz` 是 HTTP 探活，**其余路径（含无路径）一律是核心端**。
// 之所以不在路径上拒绝：核心端是本进程的主要来客，拒绝只会让用户看到"连不上"却不知为什么。
//
// 鉴权：`AIPLUGIN4_BACKEND_TOKEN`（启动器统一注入的那一个 token）同时管核心端与插件端。
// 留空 = 不校验。海豹走 `Authorization: <裸 token>`，插件走 `?access_token=`，两种都认。
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
const PLUGIN_PATH = process.env.AIPLUGIN4_BRIDGE_PLUGIN_PATH || '/plugin';
// 核心端与插件端共用启动器的 token（其他后端也是这一个变量），不再各自配一个。
const TOKEN = process.env.AIPLUGIN4_BACKEND_TOKEN || '';

// 平台名与虚拟账号：只有这三项是用户可填的（默认值只是开箱即用）。
//
// 注意这里**刻意不用 `||`**：`||` 会把"用户清空了输入框"（空字符串）当成"没填"从而静默回退默认值，
// 而空值是被显式列为非法的配置之一 —— 必须让空字符串原样传给 validate() 报错。
// 只有 `undefined`（真的没设置这个环境变量）才走默认值。
const envOr = (name, fallback) => (process.env[name] === undefined ? fallback : process.env[name]);
const PLATFORM = (envOr('AIPLUGIN4_BRIDGE_PLATFORM', 'QQ') || '').trim() || 'QQ';
const STATE_DIR = envOr('AIPLUGIN4_BRIDGE_STATE_DIR', '.state');
const VIRTUAL = {
  selfId: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID', '10001'),
  nickname: envOr('AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME', 'AI骰娘')
};

const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_SETTLE_MS = 400;
const MAX_MESSAGES = 50;
// 传输层保活：每 30s ping，连续 2 次无 pong（≈90s）才判死。
// 之所以不能按"最近收到核心帧"计时：核心没有应用层心跳，空闲但健康的连接上没有任何上行帧。
const KEEPALIVE_MS = Number(process.env.AIPLUGIN4_BRIDGE_KEEPALIVE_MS || 30000);
const KEEPALIVE_MISS_LIMIT = 2;
// 全部需要记账/本地应答的 action
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
 * `raw_message` 与 `command.raw` 互斥等既有校验。
 * 允许用 `source` 代替 `target`（真实 ID 只作投影输入）。
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

/** token 宽松比较：裸值或 `Bearer <token>` 都认，query 与 header 双来源独立判定。 */
const stripBearer = value => String(value || '').trim().replace(/^Bearer\s+/i, '');
function tokenMatches(expected, req, url) {
  if (!expected) return true; // 未配置 token → 不校验
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
    this.invocations = new Map();
    this.lanes = new Map();
    this.stopping = false;
    this.keepaliveTimer = null;

    // 对外协议里仍然带 `mode: 'standalone'`：插件 4.32.0+ 靠它决定"走 source 投影"
    // （老桥不回这个字段 → 插件按 relay 语义发真实 target）。它是报文常量，不是配置项。
    this.mode = 'standalone';
    this.platform = PLATFORM;
    this.corePort = PORT;
    this.world = new VirtualWorld({
      stateDir: STATE_DIR,
      platform: PLATFORM,
      selfId: VIRTUAL.selfId,
      nickname: VIRTUAL.nickname
    });
    this.world.load();
    this.responder = new Responder(this.world, { version: BRIDGE_VERSION });
    // 老插件（payload 里没有 source）会走"真实 ID 直接用"的降级路径，必须能被外部看见
    this.legacyTarget = false;
    // 老插件降级提示只打一次，避免刷屏
    this._legacyWarned = false;
  }

  async start() {
    // 自检必须早于 createServer：非法配置时 this.server === null，测试才能断言这一点
    const problems = this.world.validate();
    if (problems.length > 0) {
      for (const problem of problems) log('error', `虚拟世界配置非法：${problem}`);
      throw new Error('虚拟世界配置非法');
    }
    if (this.platform !== 'QQ') {
      log('warn', `platform=${this.platform} 仅影响桥侧标识（日志//healthz/hello_result），海豹侧仍显示 QQ；如需改变海豹侧平台请改用对应协议的适配器`);
    }
    log('info', `${this.world.displayId('user', this.world.selfId)}（${this.world.nickname}）已就绪`);
    log('info', `DiceMasters 建议添加：${this.world.displayId('user', this.world.selfId)}（否则 MasterCheck 永不命中，PrivilegeLevel 上不到 100）`);

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
      // 只有 /plugin 需要认领；其余路径（含无路径、`/`、`/core`、任意自定义）一律是核心端。
      const kind = url.pathname === PLUGIN_PATH ? 'plugin' : 'core';
      if (!tokenMatches(TOKEN, req, url)) {
        // 静默 destroy 是历史行为，用户只能看到"连不上"；这里留痕但不回显 token 明文
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
    log('info', `listening ${HOST}:${PORT}, core=<任意路径/无路径>, plugin=${PLUGIN_PATH}, token=${TOKEN ? '已启用' : '未配置'}`);
    this.startKeepalive();
    return this;
  }

  /** /healthz：本进程的全部可观测事实（不含任何 relay 遗留字段）。 */
  health() {
    const counters = this.responder.counters();
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
      virtualNickname: this.world.nickname,
      actionCount: counters.actionCount,
      eventCount: counters.eventCount,
      ledgerSize: counters.ledgerSize,
      legacyTarget: !!this.legacyTarget,
      tokenRequired: !!TOKEN
    };
  }

  async stop() {
    this.stopping = true;
    if (this.keepaliveTimer) { clearInterval(this.keepaliveTimer); this.keepaliveTimer = null; }
    for (const invocation of this.invocations.values()) invocation.fail(new Error('bridge stopped'));

    const sockets = [...this.coreClients, ...this.pluginClients];
    for (const ws of sockets) ws.close();
    await new Promise(resolve => this.server ? this.server.close(() => resolve()) : resolve());
    // 虚拟世界落盘（映射表必须跨重启稳定）
    this.world.save();
  }
  isCoreConnected() { return this.coreClients.size > 0; }

  /**
   * 传输层保活。
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
    log('info', `插件已连接: ${path_}${ip ? ` (${ip})` : ''}${TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
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
    // 放在后面会被"取不到 pending 就丢弃"的逻辑静默吞掉
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
          capabilities: ['virtual-world', 'stable-ids', 'lane-capture', 'local-actions'],
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
   * 带 `source`：真实 ID → 虚拟 ID（投影失败即抛，由调用方转 core_command_error，
   *   绝不允许把非数字真实 ID 送进核心）。
   * 无 `source`：老插件降级，直接用 `target` 里的 ID，并置 legacyTarget。
   */
  projectRequest(request) {
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

    // 单核心门禁：第二条连接直接 1013 拒掉，不进 Set、不挂监听
    if (this.coreClients.size >= 1) {
      log('warn', `仅支持单个 SealDice 核心，已拒绝第 ${this.coreClients.size + 1} 条连接${ip ? ` (${ip})` : ''}`);
      try {
        jsonSend(ws, { status: 'failed', retcode: 1013, data: { wording: '仅支持单个 SealDice 核心' }, echo: undefined });
        ws.close(1013, '仅支持单个 SealDice 核心');
      } catch (_) { /* ignore */ }
      return;
    }

    ws._bridgePongMiss = 0;
    this.coreClients.add(ws);
    log('info', `SealDice 核心已连接: ${path_}${ip ? ` (${ip})` : ''}${TOKEN ? ' [token 校验通过]' : ' [未配置 token]'}`);
    ws.on('message', data => this.handleCoreMessage(ws, data));
    ws.on('pong', () => { ws._bridgePongMiss = 0; });
    ws.on('close', () => {
      this.coreClients.delete(ws);
      for (const invocation of this.invocations.values()) if (invocation.coreWs === ws) invocation.fail(new Error('core websocket disconnected'));
      log('info', `SealDice 核心已断开: ${path_}${ip ? ` (${ip})` : ''}`);
    });
    ws.on('error', e => log('warn', 'core client error', e));
  }

  coreForTarget() {
    if (this.coreClients.size === 1) return [...this.coreClients][0];
    return null;
  }
  ensureCore() {
    const core = this.coreForTarget();
    if (!core || core.readyState !== WebSocket.OPEN) throw new Error('SealDice 核心 WS 尚未连接');
    return core;
  }

  handleCoreMessage(ws, data) {
    const packet = parseMessage(data);
    if (!packet) {
      // 裸帧/非 JSON 一律丢弃并 warn，不得 crash
      log('warn', '核心发来非 JSON 帧，已丢弃');
      return;
    }
    // 核心作为客户端时不会向端点推事件；非 action 帧一律忽略
    if (!packet.action) {
      log('debug', `忽略非 action 帧（post_type=${String(packet.post_type || '')}）`);
      return;
    }
    this.handleCoreAction(ws, packet);
  }

  /** action 分支：本地应答 + 记账 + lane 通知。 */
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
    // 所以由这里通知，responder.js 只负责返回结论 —— 单向往依赖约束）
    this.deliverToLane(outcome);
  }

  deliverToLane(outcome) {
    const target = outcome.peer;
    if (!target) return;
    const coreWs = this.coreClients.size === 1 ? [...this.coreClients][0] : null;
    const invocation = this.findInvocationForTarget(target, coreWs);
    if (!invocation) return;
    // 不累加 interceptedCount：本端全权受理，没有"从协议端嘴里抢下来"这回事
    // （该字段恒 0，真实条数看 deliveredCount）。
    const accepted = invocation.acceptMessage({
      messageId: idString(outcome.messageId),
      segments: outcome.segments || [],
      text: outcome.text || ''
    }, { source: 'action', forwarded: false, intercepted: false });
    // eventCount 的口径是"真正投递给调用方的消息条数"（/healthz 字段），
    // 不是"调用次数"：调用被 timeout/队列丢弃时不该计数。
    if (accepted) this.responder.eventCount += 1;
  }

  findInvocationForTarget(target, coreWs) {
    const matches = [...this.invocations.values()].filter(inv => !inv.completed && inv.matchesTarget(target, coreWs));
    if (matches.length > 1) matches.forEach(inv => { inv.ambiguous = true; });
    return matches.sort((a, b) => a.startedAt - b.startedAt)[0] || null;
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
    // 唯一发号器：事件号与下行号同源、同号段、全部 JS 安全整数
    invocation.virtualMessageId = this.world.allocateMessageId();
    this.invocations.set(invocation.id, invocation);
    const resultPromise = invocation.start();
    try {
      const coreWs = this.ensureCore();
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
      // 投影表保证 ID 恒为正整数；这里再兜一次，不允许字符串进核心
      const userId = Number(trigger);
      if (!Number.isSafeInteger(userId) || userId <= 0) throw new Error(`trigger 必须是正整数虚拟 ID：${trigger}`);
      const atUserIds = messageType === 'group' && Array.isArray(request.at) ? request.at.map(idString).filter(Boolean) : [];
      const atSegment = atUserIds.map(qq => ({ type: 'at', data: { qq } }));
      const atRaw = atUserIds.map(qq => `[CQ:at,qq=${qq}]`).join(' ');
      const atPrefix = atRaw ? `${atRaw} ` : '';
      const fakeEvent = {
        time: Math.floor(Date.now() / 1000), self_id: this.world.selfId, post_type: 'message',
        message_type: messageType, sub_type: 'normal', message_id: invocation.virtualMessageId,
        user_id: userId, ...(messageType === 'group' ? { group_id: Number(peer) } : {}),
        message: [...atSegment, { type: 'text', data: { text: raw } }], raw_message: `${atPrefix}${raw}`, font: 0,
        sender: { user_id: userId, nickname: String(request.actor && request.actor.nickname || `用户${trigger}`), card: '', sex: 'unknown', age: 0, role: String(request.actor && request.actor.role || 'member'), title: '' }
      };
      sendRaw(coreWs, JSON.stringify(fakeEvent));
      const result = await resultPromise;
      // 虚拟世界是本端事实源：投影产生的新映射要落盘。
      // eventCount 不在这里加 —— 它由 deliverToLane() 按"真正投递成功的消息条数"累加。
      this.world.save();
      return this.decorateResult(result, request);
    } catch (e) {
      invocation.fail(e);
      return await resultPromise;
    } finally {
      this.invocations.delete(invocation.id);
    }
  }

  /** 结果补充 mode / virtualTarget / deliveredCount（只增不改）。 */
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
      // 这三个字段语义上不存在，"收下几条"由 deliveredCount 表达：
      //   ambiguous        —— 只有一个消息来源（本桥自己的账本），不可能歧义；
      //   forwardedCount   —— 没有真实协议端，恒 0；
      //   interceptedCount —— "拦截"这个概念不存在（全部消息都由本端受理），恒 0。
      // 之所以不暴露真实计数：老插件会拿 interceptedCount 判断"是否被拦截"，
      // 给出 0 才能让它走"消息已收下"的新分支；真实条数看 deliveredCount。
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

module.exports = { Bridge, laneKey, textOfSegments, mode: 'standalone', platform: PLATFORM, VirtualWorld, Responder, BRIDGE_VERSION };
