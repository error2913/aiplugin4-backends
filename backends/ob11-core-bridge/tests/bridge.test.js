'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
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

let Bridge;
let bridge;
let bridgePort;
let coreSockets;
let protocolServer;
let protocolPort;
let protocolConnections;

// 启动一个假的 OB11 协议端 WS 服务端（随机端口），可选 token 校验与指定端口
async function startProtocolServer(serverToken = 'test-token', port = 0) {
  protocolConnections = [];
  protocolServer = new WebSocketServer({ port });
  protocolServer.on('connection', (ws, req) => {
    const url = new URL(req.url, 'http://localhost');
    const hasQuery = url.searchParams.get('access_token') === serverToken;
    const hasHeader = (req.headers.authorization || '') === `Bearer ${serverToken}`;
    if (serverToken && !hasQuery && !hasHeader) { ws.close(1008, 'unauthorized'); return; }
    protocolConnections.push(ws);
  });
  await new Promise(resolve => protocolServer.once('listening', resolve));
  protocolPort = protocolServer.address().port;
  return protocolPort;
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

async function setup(options = {}) {
  const { withProtocol = true, protocolUrl = '', protocolToken = 'test-token', serverToken = 'test-token' } = options;
  coreSockets = [];
  protocolConnections = [];
  protocolServer = null;
  protocolPort = 0;
  bridgePort = 47000 + Math.floor(Math.random() * 1000);
  process.env.AIPLUGIN4_BACKEND_PORT = String(bridgePort);
  process.env.AIPLUGIN4_BACKEND_HOST = '127.0.0.1';
  process.env.AIPLUGIN4_BRIDGE_CORE_PATH = '/core';
  process.env.AIPLUGIN4_BRIDGE_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = 'test-token';
  delete process.env.AIPLUGIN4_BRIDGE_ONEBOT_PATH;
  if (withProtocol) {
    if (protocolUrl) {
      process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL = protocolUrl;
    } else {
      await startProtocolServer(serverToken);
      process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL = `ws://127.0.0.1:${protocolPort}`;
    }
    process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN = protocolToken;
  } else {
    delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
    delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN;
  }
  delete require.cache[require.resolve('../bridge')];
  ({ Bridge } = require('../bridge'));
  bridge = new Bridge();
  await bridge.start();
}

async function teardown() {
  if (bridge) await bridge.stop();
  bridge = null;
  for (const ws of coreSockets) { try { ws.close(); } catch (_) { /* ignore */ } }
  coreSockets = [];
  for (const ws of protocolConnections) { try { ws.close(); } catch (_) { /* ignore */ } }
  protocolConnections = [];
  if (protocolServer) {
    await new Promise(resolve => protocolServer.close(() => resolve()));
    protocolServer = null;
  }
}

async function connectCore(id = null) {
  const ws = await connect(`ws://127.0.0.1:${bridgePort}/core?access_token=test-token`);
  coreSockets.push(ws);
  if (id !== null) {
    ws.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: `login-${id}` }));
  }
  return ws;
}

// 返回协议端（假服务端一侧）当前已连接的 socket；桥接器出站主动连接协议端
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

function mcpArgs(target, raw, capture = {}, timeoutMs = 1000) {
  const args = {
    action: 'call',
    target,
    actor: { userId: String(target.userId || '30002'), nickname: 'AI', role: 'member' },
    raw_message: raw,
    captureMode: capture.mode,
    forward: capture.forward,
    maxMessages: capture.maxMessages,
    settleMs: capture.settleMs,
    timeoutMs
  };
  return args;
}

function mcpStructuredArgs(target, command, args = [], options = {}) {
  return {
    action: 'call',
    target,
    actor: { userId: String(target.userId || '30002'), nickname: 'AI', role: 'member' },
    command,
    args,
    __commandPrefix: options.commandPrefix || '.',
    captureMode: options.captureMode,
    forward: options.forward,
    maxMessages: options.maxMessages,
    settleMs: options.settleMs,
    timeoutMs: options.timeoutMs || 1000
  };
}

function sendGroupAction(ws, groupId, text, echo) {
  ws.send(JSON.stringify({ action: 'send_group_msg', params: { group_id: groupId, message: [{ type: 'text', data: { text } }] }, echo }));
}

async function mcpRequest(payload, sessionId = '') {
  const headers = { 'content-type': 'application/json', accept: 'application/json, text/event-stream', authorization: 'Bearer test-token' };
  if (sessionId) headers['mcp-session-id'] = sessionId;
  const response = await fetch(`http://127.0.0.1:${bridgePort}/mcp`, { method: 'POST', headers, body: JSON.stringify(payload) });
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); } catch (_) {
    const dataLine = text.split('\n').find(line => line.startsWith('data: '));
    body = dataLine ? JSON.parse(dataLine.slice(6)) : null;
  }
  return { response, body, sessionId: response.headers.get('mcp-session-id') || sessionId };
}

async function mcpSession() {
  const initialized = await mcpRequest({
    jsonrpc: '2.0', id: 1, method: 'initialize',
    params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'test', version: '1' } }
  });
  assert.equal(initialized.response.status, 200);
  assert.equal(initialized.body.result.serverInfo.name, 'ob11-core-bridge');
  const sessionId = initialized.sessionId;
  assert.ok(sessionId);
  const notified = await mcpRequest({ jsonrpc: '2.0', method: 'notifications/initialized' }, sessionId);
  assert.equal(notified.response.status, 202);
  return sessionId;
}

async function mcpCall(name, args, sessionId) {
  const id = 100 + Math.floor(Math.random() * 100000);
  const called = await mcpRequest({ jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args } }, sessionId);
  assert.equal(called.response.status, 200);
  return JSON.parse(called.body.result.content[0].text);
}

test('accepts SealDice core websocket on /core and /core/ws', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const health = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health.coreConnected, true);
  assert.equal(health.coreClients, 1);
  await closeWs(core);

  const coreWs = await connect(`ws://127.0.0.1:${bridgePort}/core/ws?access_token=test-token`);
  const health2 = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health2.coreConnected, true);
  assert.equal(health2.coreClients, 1);
  await closeWs(coreWs);
});

test('transparent forwarding, raw frames, login echo routing, and healthz', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();

  const toCoreRaw = waitForRaw(core, 'raw protocol frame');
  protocol.send('raw protocol frame');
  assert.equal(await toCoreRaw, 'raw protocol frame');

  const toProtocolRaw = waitForRaw(protocol, 'raw core frame');
  core.send('raw core frame');
  assert.equal(await toProtocolRaw, 'raw core frame');

  const event = { ...FIXTURE, raw_message: 'hello', message: [{ type: 'text', data: { text: 'hello' } }] };
  const eventAtCore = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === 'hello');
  protocol.send(JSON.stringify(event));
  assert.equal((await eventAtCore).group_id, 20002);

  const loginAtProtocol = waitForMessage(protocol, packet => packet.action === 'get_login_info' && packet.echo === 'login-check');
  const loginAtCore = waitForMessage(core, packet => packet.echo === 'login-check');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'login-check' }));
  assert.equal((await loginAtProtocol).echo, 'login-check');
  protocol.send(JSON.stringify({ status: 'ok', retcode: 0, data: { user_id: 10001, nickname: 'BridgeBot' }, echo: 'login-check' }));
  assert.equal((await loginAtCore).data.nickname, 'BridgeBot');

  const health = await (await fetch(`http://127.0.0.1:${bridgePort}/healthz`)).json();
  assert.deepEqual(health, { ok: true, coreConnected: true, protocolConnected: true, coreClients: 1, protocolClients: 1 });
  await closeWs(protocol); await closeWs(core);
});

test('core API requests fail fast with status failed when no protocol client is connected', async t => {
  await setup({ withProtocol: false }); t.after(teardown);
  const core = await connectCore();
  const health = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health.protocolConnected, false);

  const login = waitForMessage(core, packet => packet.echo === 'login-orphan');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'login-orphan' }));
  assert.deepEqual(await login, { status: 'failed', retcode: 100, data: null, echo: 'login-orphan' });

  const send = waitForMessage(core, packet => packet.echo === 'send-orphan');
  sendGroupAction(core, 20002, 'orphan', 'send-orphan');
  assert.deepEqual(await send, { status: 'failed', retcode: 100, data: null, echo: 'send-orphan' });
  await closeWs(core);
});

test('invoke captures multiple actions and intercepts them by default', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();
  const sessionId = await mcpSession();

  const seenFake = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.jrrp');
  const coreResponse1 = waitForMessage(core, packet => packet.echo === 'e1');
  const coreResponse2 = waitForMessage(core, packet => packet.echo === 'e2');
  const protocolPackets = [];
  protocol.on('message', data => { try { protocolPackets.push(JSON.parse(data.toString())); } catch (_) {} });

  const coreHandler = data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message' || packet.raw_message !== '.jrrp') return;
    sendGroupAction(core, 20002, '第一条结果', 'e1');
    setTimeout(() => sendGroupAction(core, 20002, '第二条结果', 'e2'), 30);
    sendGroupAction(core, 99999, 'unrelated', 'other');
  };
  core.on('message', coreHandler);
  const resultPromise = mcpCall('run_core_command', mcpArgs(groupTarget(), '.jrrp', { mode: 'reply_only', forward: false, settleMs: 70, maxMessages: 20 }), sessionId);

  assert.equal((await seenFake).message[0].data.text, '.jrrp');
  assert.equal((await coreResponse1).status, 'ok');
  assert.equal((await coreResponse2).status, 'ok');
  const result = await resultPromise;
  assert.equal(result.ok, true);
  assert.equal(result.messages.length, 2);
  assert.equal(result.interceptedCount, 2);
  assert.equal(result.forwardedCount, 0);
  await wait(50);
  assert.equal(protocolPackets.filter(packet => packet.echo === 'e1' || packet.echo === 'e2').length, 0);
  assert.equal(protocolPackets.filter(packet => packet.echo === 'other').length, 1);
  await closeWs(protocol); await closeWs(core);
});

test('lane capture intercepts bot message events', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();
  const sessionId = await mcpSession();
  const protocolPackets = [];
  protocol.on('message', data => { try { protocolPackets.push(JSON.parse(data.toString())); } catch (_) {} });
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.event') {
      core.send(JSON.stringify({ ...FIXTURE, message_id: 91001, raw_message: 'bot-event', message: [{ type: 'text', data: { text: 'bot-event' } }] }));
    }
  });
  const result = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.event', { mode: 'lane', forward: false, settleMs: 40 }), sessionId);
  assert.equal(result.ok, true);
  assert.equal(result.messages.length, 1);
  assert.equal(result.messages[0].source, 'event');
  assert.equal(result.interceptedCount, 1);
  assert.equal(result.forwardedCount, 0);
  await wait(50);
  assert.equal(protocolPackets.some(packet => packet.raw_message === 'bot-event'), false);
  await closeWs(protocol); await closeWs(core);
});

test('forward=true forwards actions and routes protocol API responses back to core', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();
  const sessionId = await mcpSession();
  const protocolActions = [];
  const protocolEvents = [];
  const coreResponses = [];
  protocol.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message') protocolEvents.push(packet);
    if (!packet.action || !packet.echo) return;
    protocolActions.push(packet);
    protocol.send(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 88001 }, echo: packet.echo }));
  });
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.status === 'ok' && packet.echo) coreResponses.push(packet);
    if (packet.post_type === 'message') {
      sendGroupAction(core, packet.group_id, `reply-${packet.raw_message}`, `echo-${packet.raw_message}`);
      setTimeout(() => core.send(JSON.stringify({ ...FIXTURE, message_id: 92001, raw_message: 'forwarded-event', message: [{ type: 'text', data: { text: 'forwarded-event' } }] })), 10);
    }
  });

  const result = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.forward', { mode: 'lane', forward: true, settleMs: 50 }), sessionId);
  assert.equal(result.ok, true);
  assert.equal(result.forwardedCount, 2);
  assert.equal(result.interceptedCount, 0);
  await wait(50);
  assert.equal(protocolActions[0].params.group_id, 20002);
  assert.equal(coreResponses.some(packet => packet.echo === 'echo-.forward'), true);
  assert.equal(protocolEvents.some(packet => packet.raw_message === 'forwarded-event'), true);
  await closeWs(protocol); await closeWs(core);
});

test('same-lane invocations serialize while different lanes run concurrently', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const [sessionA, sessionB, sessionC] = await Promise.all([mcpSession(), mcpSession(), mcpSession()]);
  const started = [];
  const completed = [];
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    const key = `${packet.group_id}:${packet.raw_message}`;
    started.push(key);
    const delay = packet.group_id === 20002 ? 45 : 5;
    setTimeout(() => {
      sendGroupAction(core, packet.group_id, packet.raw_message, `echo-${packet.group_id}-${packet.raw_message}`);
      completed.push(key);
    }, delay);
  });

  const aStarted = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.a');
  const sameA = mcpCall('run_core_command', mcpArgs(groupTarget('20002'), '.a', { settleMs: 10 }), sessionA);
  await aStarted;
  const sameB = mcpCall('run_core_command', mcpArgs(groupTarget('20002'), '.b', { settleMs: 10 }), sessionB);
  const different = mcpCall('run_core_command', mcpArgs(groupTarget('20003'), '.c', { settleMs: 10 }), sessionC);
  const results = await Promise.all([sameA, sameB, different]);
  assert.equal(results.every(result => result.ok === true), true);
  await waitFor(() => started.includes('20002:.b') && completed.includes('20002:.a'), 3000);
  assert.equal(started[0], '20002:.a');
  assert.equal(started.indexOf('20002:.b') > completed.indexOf('20002:.a'), true);
  assert.equal(started.indexOf('20003:.c') < started.indexOf('20002:.b'), true);
  assert.equal(completed.indexOf('20003:.c') < started.indexOf('20002:.b'), true);
  await closeWs(core);
});

test('private send_msg targets private lanes correctly', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const sessionId = await mcpSession();
  const actionPromise = new Promise(resolve => core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message') {
      core.send(JSON.stringify({ action: 'send_msg', params: { message_type: 'private', user_id: 30002, message: 'private reply' }, echo: 'private-echo' }));
    }
    if (packet.echo === 'private-echo') resolve(packet);
  }));
  const [result, response] = await Promise.all([
    mcpCall('run_core_command', mcpArgs(privateTarget(), '.private', { settleMs: 20 }), sessionId),
    actionPromise
  ]);
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'private reply');
  assert.equal(response.status, 'ok');
  await closeWs(core);
});

test('lane mode captures events, maxMessages bounds mixed replies, and reply_only marks ambiguity', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();
  const sessionId = await mcpSession();
  let invocationCount = 0;
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    invocationCount++;
    if (packet.raw_message === '.lane') {
      for (let i = 0; i < 5; i++) core.send(JSON.stringify({ ...FIXTURE, message_id: 7000 + i, raw_message: `lane-${i}`, message: [{ type: 'text', data: { text: `lane-${i}` } }] }));
    } else if (packet.raw_message === '.reply') {
      core.send(JSON.stringify({ ...FIXTURE, message_id: 8001, raw_message: 'unrelated', message: [{ type: 'text', data: { text: 'unrelated' } }] }));
      core.send(JSON.stringify({ ...FIXTURE, message_id: 8002, raw_message: 'reply', message: [{ type: 'reply', data: { id: packet.message_id } }, { type: 'text', data: { text: 'reply' } }] }));
    }
  });
  const laneResult = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.lane', { mode: 'lane', maxMessages: 2, settleMs: 100 }), sessionId);
  assert.equal(laneResult.ok, true);
  assert.equal(laneResult.messages.length, 2);
  assert.equal(laneResult.completedBy, 'max_messages');
  await wait(50);
  const replyResult = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.reply', { mode: 'reply_only', settleMs: 30 }), sessionId);
  assert.equal(replyResult.ok, true);
  assert.equal(replyResult.messages.length, 1);
  assert.equal(replyResult.messages[0].text, 'reply');
  assert.equal(replyResult.ambiguous, true);
  assert.equal(invocationCount, 2);
  await closeWs(protocol); await closeWs(core);
});

test('timeouts, missing core, and core disconnects fail cleanly', async t => {
  await setup(); t.after(teardown);
  const sessionId = await mcpSession();
  const missing = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.missing', {}, 100), sessionId);
  assert.equal(missing.ok, false);
  assert.match(missing.error, /核心 WS/);

  const core = await connectCore();
  const timeout = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.timeout', { settleMs: 20 }, 100), sessionId);
  assert.equal(timeout.ok, true);
  assert.equal(timeout.completedBy, 'timeout');

  const disconnectPromise = mcpCall('run_core_command', mcpArgs(groupTarget(), '.disconnect', {}, 1000), sessionId);
  await waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.disconnect');
  await closeWs(core);
  const disconnect = await disconnectPromise;
  assert.equal(disconnect.ok, false);
  assert.match(disconnect.error, /disconnected/);
});

test('multiple core connections route login responses, events, and invocations by self_id', async t => {
  await setup(); t.after(teardown);
  const core1 = await connectCore();
  const core2 = await connectCore();
  const protocol = await waitForProtocol();
  const login1 = waitForMessage(protocol, packet => packet.echo === 'login-one');
  const login2 = waitForMessage(protocol, packet => packet.echo === 'login-two');
  const loginResponse1 = waitForMessage(core1, packet => packet.echo === 'login-one');
  const loginResponse2 = waitForMessage(core2, packet => packet.echo === 'login-two');
  core1.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'login-one' }));
  core2.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'login-two' }));
  const loginPackets = await Promise.all([login1, login2]);
  protocol.send(JSON.stringify({ status: 'ok', retcode: 0, data: { user_id: 10002, nickname: 'Bot2' }, echo: loginPackets.find(packet => packet.echo === 'login-two').echo }));
  protocol.send(JSON.stringify({ status: 'ok', retcode: 0, data: { user_id: 10001, nickname: 'Bot1' }, echo: loginPackets.find(packet => packet.echo === 'login-one').echo }));
  assert.equal((await loginResponse1).data.user_id, 10001);
  assert.equal((await loginResponse2).data.user_id, 10002);

  const event1 = waitForMessage(core1, packet => packet.post_type === 'message' && packet.raw_message === 'from-bot-1');
  const event2 = waitForMessage(core2, packet => packet.post_type === 'message' && packet.raw_message === 'from-bot-2');
  protocol.send(JSON.stringify({ ...FIXTURE, self_id: 10001, raw_message: 'from-bot-1', message: [{ type: 'text', data: { text: 'from-bot-1' } }] }));
  protocol.send(JSON.stringify({ ...FIXTURE, self_id: 10002, raw_message: 'from-bot-2', message: [{ type: 'text', data: { text: 'from-bot-2' } }] }));
  await Promise.all([event1, event2]);

  const sessionId = await mcpSession();
  core1.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.only-bot-1') sendGroupAction(core1, 20002, 'bot1', 'bot1-echo');
  });
  const result = await mcpCall('run_core_command', mcpArgs(groupTarget('20002', '10001'), '.only-bot-1', { settleMs: 20 }), sessionId);
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'bot1');
  await closeWs(protocol); await closeWs(core1); await closeWs(core2);
});

test('MCP exposes only the core bridge command', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    sendGroupAction(core, packet.group_id, `mcp-${packet.raw_message}`, 'mcp-action');
    setTimeout(() => sendGroupAction(core, packet.group_id, 'mcp-second', 'mcp-action-2'), 15);
  });

  const sessionId = await mcpSession();
  const listed = await mcpRequest({ jsonrpc: '2.0', id: 2, method: 'tools/list' }, sessionId);
  assert.deepEqual(listed.body.result.tools.map(tool => tool.name).sort(), ['run_core_command']);
  const schema = listed.body.result.tools[0].inputSchema;
  assert.deepEqual(Object.keys(schema.properties).sort(), [
    'action', 'args', 'captureMode', 'command', 'forward', 'maxMessages', 'raw_message', 'settleMs', 'timeoutMs'
  ]);
  assert.equal(Object.prototype.hasOwnProperty.call(schema.properties, 'target'), false);
  assert.equal(Object.prototype.hasOwnProperty.call(schema.properties, 'actor'), false);

  const coreResult = await mcpCall('run_core_command', mcpArgs(groupTarget(), '.ban 100', { settleMs: 30 }), sessionId);
  assert.equal(coreResult.ok, true);
  assert.equal(coreResult.messages[0].text, 'mcp-.ban 100');
  await closeWs(core);
});

test('structured MCP calls are converted to the core raw message', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const received = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.help ext');
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.help ext') sendGroupAction(core, packet.group_id, 'structured-ok', 'structured-action');
  });
  const sessionId = await mcpSession();
  const resultPromise = mcpCall('run_core_command', mcpStructuredArgs(groupTarget(), 'help', ['ext'], { settleMs: 20 }), sessionId);
  await received;
  const result = await resultPromise;
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'structured-ok');
  await closeWs(core);
});

test('raw_message cannot be combined with structured command arguments', async t => {
  await setup(); t.after(teardown);
  const sessionId = await mcpSession();
  const both = await mcpCall('run_core_command', {
    action: 'call', raw_message: '.help', command: 'help',
    target: groupTarget(), actor: { userId: '30002', nickname: 'AI', role: 'member' }
  }, sessionId);
  assert.equal(both.ok, false);
  assert.match(both.error, /raw_message.*command|command.*raw_message/i);

  const rawAndArgs = await mcpCall('run_core_command', {
    action: 'call', raw_message: '.help', args: ['ext'],
    target: groupTarget(), actor: { userId: '30002', nickname: 'AI', role: 'member' }
  }, sessionId);
  assert.equal(rawAndArgs.ok, false);
  assert.match(rawAndArgs.error, /raw_message.*command|command.*raw_message/i);
});

test('invalid auth and wrong tokens are rejected', async t => {
  await setup(); t.after(teardown);
  const mcpUnauthorized = await fetch(`http://127.0.0.1:${bridgePort}/mcp`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}'
  });
  assert.equal(mcpUnauthorized.status, 401);
  const badCore = new WebSocket(`ws://127.0.0.1:${bridgePort}/core?access_token=wrong`);
  badCore.on('error', () => {});
  await waitForEvent(badCore, 'close');
});

test('protocol client connects outbound to protocol server with token', async t => {
  await setup(); t.after(teardown);
  const protocol = await waitForProtocol();
  assert.ok(protocol);
  assert.equal(protocol.readyState, WebSocket.OPEN);
  await waitFor(() => bridge.protocolClients.size === 1, 3000);
  const health = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health.protocolConnected, true);
  assert.equal(health.protocolClients, 1);
});

test('protocol client reconnects after protocol server disconnects', async t => {
  await setup(); t.after(teardown);
  const first = await waitForProtocol();
  assert.equal(bridge.protocolClients.size, 1);
  await closeWs(first);
  await waitFor(() => bridge.protocolClients.size === 0, 3000);
  const second = await waitForProtocol(5000);
  assert.notEqual(second, first);
  assert.equal(second.readyState, WebSocket.OPEN);
  const health = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health.protocolConnected, true);
  assert.equal(health.protocolClients, 1);
});

test('protocol client retries until protocol server becomes available', async t => {
  const targetPort = await freePort();
  await setup({ protocolUrl: `ws://127.0.0.1:${targetPort}` });
  t.after(teardown);
  await wait(1200);
  assert.equal(bridge.protocolClients.size, 0);
  await startProtocolServer('test-token', targetPort);
  await waitFor(() => bridge.protocolClients.size === 1, 8000);
  const health = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health.protocolConnected, true);
});

test('wrong protocol token never establishes an authorized connection', async t => {
  await setup({ protocolToken: 'wrong' }); t.after(teardown);
  await wait(1500); // 观察至少一轮重连尝试
  assert.equal(protocolConnections.length, 0);
  assert.equal(bridge.protocolClients.size, 0);
});

async function waitFor(predicate, timeout = 3000) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start >= timeout) throw new Error('timeout waiting for condition');
    await wait(5);
  }
}
