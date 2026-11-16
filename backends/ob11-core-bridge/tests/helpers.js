'use strict';

// 共享测试脚手架：bridge.test.js / relay.test.js 都从这里取 connection 与断言工具。
//
// 为什么单独成文件：`bridge.test.js` 原本把 setup/teardown/连接工具全部写成文件局部函数
// 且没有 module.exports，第二个测试文件想复用只能 `require('./bridge.test.js')`，
// 那会把该文件的全部 test() 一并注册进当前进程，"21 pass"的计数口径当场失效。
//
// 三个额外职责（S0 定下，后面所有切片依赖）：
//   1. state 隔离 —— 每个用例一个 mkdtemp 目录，避免 `node --test a.js b.js` 并行时
//      两个进程抢同一个 .state/virtual-world.json（实测跨进程 rename 争用会 EPERM）。
//   2. env 事务 —— 进用例快照、出用例逐键还原。非法 env 泄漏会让后续用例全部红。
//   3. type 化 —— Bridge 每次从 require.cache 里重新取，保证读到的是本用例刚写的 env。

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { WebSocket, WebSocketServer } = require('ws');

const FIXTURE = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'message_text.json'), 'utf8'));

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

function waitForEvent(emitter, event, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout waiting for ${event}`)), timeout);
    const onEvent = (...args) => { clearTimeout(timer); resolve(args); };
    emitter.once(event, onEvent);
  });
}

function waitForMessage(ws, predicate = () => true, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off('message', onMessage);
      reject(new Error('timeout waiting for websocket message'));
    }, timeout);
    const onMessage = data => {
      let value;
      try { value = JSON.parse(data.toString()); } catch (_) { return; }
      if (!predicate(value)) return;
      clearTimeout(timer);
      ws.off('message', onMessage);
      resolve(value);
    };
    ws.on('message', onMessage);
  });
}

function waitForRaw(ws, expected, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off('message', onMessage);
      reject(new Error('timeout waiting for raw websocket message'));
    }, timeout);
    const onMessage = data => {
      if (data.toString() !== expected) return;
      clearTimeout(timer);
      ws.off('message', onMessage);
      resolve(data.toString());
    };
    ws.on('message', onMessage);
  });
}

/** 断言窗口内没有匹配的 JSON 消息到达（用于验证某类帧被丢弃，而不是被转发到核心） */
async function assertNoJsonArrival(ws, predicate, timeout = 150) {
  let hit = null;
  const onMessage = data => {
    let value;
    try { value = JSON.parse(data.toString()); } catch (_) { return; }
    if (predicate(value)) hit = value;
  };
  ws.on('message', onMessage);
  await wait(timeout);
  ws.off('message', onMessage);
  return hit;
}

async function connect(url) {
  const ws = new WebSocket(url);
  await waitForEvent(ws, 'open');
  return ws;
}

async function closeWs(ws) {
  if (!ws || ws.readyState === WebSocket.CLOSED) return;
  await new Promise(resolve => {
    const timer = setTimeout(resolve, 500);
    ws.once('close', () => { clearTimeout(timer); resolve(); });
    try { ws.close(); } catch (_) { clearTimeout(timer); resolve(); }
  });
}

async function waitFor(predicate, timeout = 3000) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start >= timeout) throw new Error('timeout waiting for condition');
    await wait(5);
  }
}

// ── 可变状态 ────────────────────────────────────────────────────────────────
// 数组用 in-place 清空（不重新赋值），这样测试文件里 const 解构出来的引用始终有效。
const state = {
  bridge: null,
  bridgePort: 0,
  protocolPort: 0,
  protocolServer: null
};
const coreSockets = [];
const pluginSockets = [];
const protocolConnections = [];

/** 本用例触碰的全部 env 键。快照/还原以这张表为准，新增 env 要同步加进来。 */
const ENV_KEYS = [
  'AIPLUGIN4_BACKEND_PORT',
  'AIPLUGIN4_BACKEND_HOST',
  'AIPLUGIN4_BACKEND_TOKEN',
  'AIPLUGIN4_BRIDGE_CORE_TOKEN',
  'AIPLUGIN4_BRIDGE_PLUGIN_PATH',
  'AIPLUGIN4_BRIDGE_PLUGIN_TOKEN',
  'AIPLUGIN4_BRIDGE_PROTOCOL_URL',
  'AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN',
  'AIPLUGIN4_BRIDGE_MODE',
  'AIPLUGIN4_BRIDGE_PLATFORM',
  'AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID',
  'AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME',
  'AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ROLE',
  'AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID',
  'AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_NAME',
  'AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID',
  'AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT',
  'AIPLUGIN4_BRIDGE_STATE_DIR',
  'AIPLUGIN4_BRIDGE_ONEBOT_PATH'
];

let envSnapshot = null;
let stateDir = null;

function snapshotEnv() {
  envSnapshot = {};
  for (const key of ENV_KEYS) envSnapshot[key] = process.env[key];
}

function restoreEnv() {
  if (!envSnapshot) return;
  for (const key of ENV_KEYS) {
    const saved = envSnapshot[key];
    if (saved === undefined) delete process.env[key];
    else process.env[key] = saved;
  }
  envSnapshot = null;
}

/**
 * 用例级 env 事务 + 独立 state 目录。
 *
 * 给"裸写 new Bridge()"的用例用（它们不能走 setup()，因为 start() 会 reject）。
 * 返回本用例专属的状态目录。
 */
function isolateEnv(t) {
  snapshotEnv();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-world-'));
  stateDir = dir;
  process.env.AIPLUGIN4_BRIDGE_STATE_DIR = dir;
  if (t && typeof t.after === 'function') {
    t.after(() => {
      restoreEnv();
      fs.rmSync(dir, { recursive: true, force: true });
      if (stateDir === dir) stateDir = null;
    });
  }
  return dir;
}

/** 在清掉 require.cache 之后取到一份全新的 bridge 模块（常量在 require 时求值）。 */
function freshBridgeModule() {
  delete require.cache[require.resolve('../bridge')];
  return require('../bridge');
}

// 启动一个假的 OB11 协议端 WS 服务端（随机端口），可选 token 校验与指定端口
async function startProtocolServer(serverToken = 'test-token', port = 0) {
  protocolConnections.length = 0;
  state.protocolServer = new WebSocketServer({ port });
  state.protocolServer.on('connection', (ws, req) => {
    const url = new URL(req.url, 'http://localhost');
    const hasQuery = url.searchParams.get('access_token') === serverToken;
    const hasHeader = (req.headers.authorization || '') === `Bearer ${serverToken}`;
    if (serverToken && !hasQuery && !hasHeader) { ws.close(1008, 'unauthorized'); return; }
    protocolConnections.push(ws);
  });
  await new Promise(resolve => state.protocolServer.once('listening', resolve));
  state.protocolPort = state.protocolServer.address().port;
  return state.protocolPort;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

/**
 * 启动一个桥实例。
 *
 * 与 1.1.1 的差异（都属 S0 的"先立回归网"）：
 *   - `withProtocol` 默认 **false**：主线用例是 standalone，不该被自动推断判成 relay。
 *   - `pinMode`（默认 true）：把 AIPLUGIN4_BRIDGE_MODE 显式钉死；传 false 才能测 00 §8.1 的自动推断。
 *   - env 事务 + state 隔离：结束时全量还原，不留脏值。
 */
async function setup(options = {}) {
  const {
    withProtocol = false,
    pinMode = true,
    protocolUrl = '',
    protocolToken = 'test-token',
    serverToken = 'test-token',
    env = {}
  } = options;

  snapshotEnv();
  coreSockets.length = 0;
  pluginSockets.length = 0;
  protocolConnections.length = 0;
  state.protocolServer = null;
  state.protocolPort = 0;

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-world-'));
  stateDir = dir;

  // 端口必须问 OS 要（bind :0），不能随机猜：`node --test` 多文件并行跑在不同进程里，
  // 而"47000 + random(1000)"让 74 个桥实例落在同一个 1000 窗口内，按生日问题算撞号概率 ≈ 93%
  // （CI 上真撞过：bridge.test.js 与 relay.test.js 同时拿到 47738 → EADDRINUSE）。
  const bridgePort = await freePort();
  state.bridgePort = bridgePort;
  process.env.AIPLUGIN4_BACKEND_PORT = String(bridgePort);
  process.env.AIPLUGIN4_BACKEND_HOST = '127.0.0.1';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_PATH = '/plugin';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_STATE_DIR = dir;
  delete process.env.AIPLUGIN4_BRIDGE_ONEBOT_PATH;
  delete process.env.AIPLUGIN4_BRIDGE_PLATFORM;
  delete process.env.AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID;
  delete process.env.AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID;
  delete process.env.AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID;
  delete process.env.AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT;
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined || value === null) delete process.env[key];
    else process.env[key] = String(value);
  }

  if (withProtocol) {
    if (protocolUrl) {
      process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL = protocolUrl;
    } else {
      await startProtocolServer(serverToken);
      process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL = `ws://127.0.0.1:${state.protocolPort}`;
    }
    process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN = protocolToken;
  } else {
    delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
    delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN;
  }

  if (pinMode) process.env.AIPLUGIN4_BRIDGE_MODE = withProtocol ? 'relay' : 'standalone';
  else delete process.env.AIPLUGIN4_BRIDGE_MODE;

  const { Bridge } = freshBridgeModule();
  state.bridge = new Bridge();
  await state.bridge.start();
  return state;
}

async function teardown() {
  if (state.bridge) await state.bridge.stop();
  state.bridge = null;
  for (const ws of coreSockets) { try { ws.close(); } catch (_) { /* ignore */ } }
  coreSockets.length = 0;
  for (const ws of pluginSockets) { try { ws.close(); } catch (_) { /* ignore */ } }
  pluginSockets.length = 0;
  for (const ws of protocolConnections) { try { ws.close(); } catch (_) { /* ignore */ } }
  protocolConnections.length = 0;
  if (state.protocolServer) {
    await new Promise(resolve => state.protocolServer.close(() => resolve()));
    state.protocolServer = null;
  }
  restoreEnv();
  if (stateDir) {
    fs.rmSync(stateDir, { recursive: true, force: true });
    stateDir = null;
  }
}

async function connectCore(id = null) {
  // 核心端不占用路径：这里刻意用**不带任何后缀**的 URL 建立连接，
  // 让整套用例默认就在验证"填 ws://host:port 就能连"这条契约（REQ-D1-14）。
  const ws = await connect(`ws://127.0.0.1:${state.bridgePort}?access_token=test-token`);
  coreSockets.push(ws);
  if (id !== null) {
    ws.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: `login-${id}` }));
  }
  return ws;
}

/** 按任意路径连核心端，用于验证路径无关性（无路径 / `/` / `/core` / 自定义都该通）。 */
async function connectCoreAt(pathname, query = '?access_token=test-token') {
  const ws = await connect(`ws://127.0.0.1:${state.bridgePort}${pathname}${query}`);
  coreSockets.push(ws);
  return ws;
}

async function connectPlugin() {
  const ws = await connect(`ws://127.0.0.1:${state.bridgePort}/plugin?access_token=test-token`);
  pluginSockets.push(ws);
  return ws;
}

// 返回协议端（假服务端一侧）当前已连接的 socket；relay 下桥是主动连接协议端的一方
function protocolConn() {
  return protocolConnections.find(ws => ws.readyState === WebSocket.OPEN) || null;
}
async function waitForProtocol(timeout = 5000) {
  await waitFor(() => protocolConn() !== null, timeout);
  return protocolConn();
}

function groupTarget(groupId = '20002', selfId = '10001') {
  return { selfId, messageType: 'group', groupId, userId: '30002' };
}
function privateTarget(userId = '30002', selfId = '10001') {
  return { selfId, messageType: 'private', userId, groupId: undefined };
}

function sendGroupAction(ws, groupId, text, echo) {
  ws.send(JSON.stringify({ action: 'send_group_msg', params: { group_id: groupId, message: [{ type: 'text', data: { text } }] }, echo }));
}

// 插件控制 WS /plugin 协议：core_command 请求，core_command_result/core_command_error 响应
function wsArgs(target, raw, capture = {}, timeoutMs = 1000) {
  return {
    target,
    actor: { userId: String(target.userId || '30002'), nickname: 'AI', role: 'member' },
    raw_message: raw,
    capture: {
      mode: capture.mode,
      forward: capture.forward,
      maxMessages: capture.maxMessages,
      settleMs: capture.settleMs
    },
    timeoutMs
  };
}

function wsStructuredArgs(target, command, args = [], options = {}) {
  const prefix = options.commandPrefix === undefined ? '.' : String(options.commandPrefix);
  const raw = `${prefix}${command}${args.length ? ` ${args.join(' ')}` : ''}`.trim();
  return {
    target,
    actor: { userId: String(target.userId || '30002'), nickname: 'AI', role: 'member' },
    command: { raw, name: command, args },
    capture: {
      mode: options.captureMode,
      forward: options.forward,
      maxMessages: options.maxMessages,
      settleMs: options.settleMs
    },
    timeoutMs: options.timeoutMs || 1000
  };
}

async function wsCall(plugin, payload, requestId = `test_${Date.now()}_${Math.floor(Math.random() * 1e6)}`) {
  const packetPromise = waitForMessage(plugin, packet => packet.requestId === requestId &&
    (packet.type === 'core_command_result' || packet.type === 'core_command_error'));
  plugin.send(JSON.stringify({ type: 'core_command', requestId, payload }));
  return packetPromise;
}

module.exports = {
  test,
  assert,
  FIXTURE,
  WebSocket,
  WebSocketServer,
  wait,
  waitFor,
  waitForEvent,
  waitForMessage,
  waitForRaw,
  assertNoJsonArrival,
  connect,
  closeWs,
  startProtocolServer,
  freePort,
  setup,
  teardown,
  isolateEnv,
  freshBridgeModule,
  ENV_KEYS,
  state,
  protocolConnections,
  connectCore,
  connectCoreAt,
  connectPlugin,
  protocolConn,
  waitForProtocol,
  groupTarget,
  privateTarget,
  sendGroupAction,
  wsArgs,
  wsStructuredArgs,
  wsCall
};
