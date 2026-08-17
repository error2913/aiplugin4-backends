'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { WebSocket } = require('ws');

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
let outboundCoreServer;
let outboundCoreSockets;

async function setup(coreUrl = '') {
  coreSockets = [];
  bridgePort = 47000 + Math.floor(Math.random() * 1000);
  process.env.AIPLUGIN4_BACKEND_PORT = String(bridgePort);
  process.env.AIPLUGIN4_BACKEND_HOST = '127.0.0.1';
  process.env.AIPLUGIN4_BRIDGE_CORE_PATH = '/core';
  process.env.AIPLUGIN4_BRIDGE_ONEBOT_PATH = '/onebot';
  process.env.AIPLUGIN4_BRIDGE_CONTROL_PATH = '/control';
  process.env.AIPLUGIN4_BRIDGE_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN = 'test-token';
  if (coreUrl) process.env.AIPLUGIN4_BRIDGE_CORE_URL = coreUrl;
  else delete process.env.AIPLUGIN4_BRIDGE_CORE_URL;
  delete require.cache[require.resolve('../bridge')];
  ({ Bridge } = require('../bridge'));
  bridge = new Bridge();
  await bridge.start();
}

async function teardown() {
  if (bridge) await bridge.stop();
  bridge = null;
  coreSockets = [];
  if (outboundCoreServer) {
    for (const ws of outboundCoreSockets || []) await closeWs(ws);
    await new Promise(resolve => outboundCoreServer.close(() => resolve()));
    outboundCoreServer = null;
    outboundCoreSockets = null;
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

async function connectProtocol() {
  return connect(`ws://127.0.0.1:${bridgePort}/onebot?access_token=test-token`);
}

 test('connects outbound to a SealDice reverse websocket and preserves the same capture path', async t => {
  outboundCoreServer = new (require('ws').WebSocketServer)({ port: 0, host: '127.0.0.1' });
  await waitForEvent(outboundCoreServer, 'listening');
  const outboundPort = outboundCoreServer.address().port;
  outboundCoreSockets = [];
  outboundCoreServer.on('connection', ws => {
    outboundCoreSockets.push(ws);
    ws.on('message', data => {
      let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
      if (packet.post_type !== 'message') return;
      ws.send(JSON.stringify({
        action: 'send_group_msg',
        params: { group_id: packet.group_id, message: [{ type: 'text', data: { text: `reply-${packet.raw_message}` } }] },
        echo: 'outbound-reply'
      }));
    });
  });
  await setup(`ws://127.0.0.1:${outboundPort}/ws`); t.after(teardown);
  const protocol = await connectProtocol();
  const { ws: control, hello } = await connectControl();
  assert.equal(hello.coreConnected, true);
  const protocolPackets = [];
  protocol.on('message', data => { try { protocolPackets.push(JSON.parse(data.toString())); } catch (_) {} });
  const resultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'outbound-1');
  control.send(JSON.stringify(fakeCommand('outbound-1', groupTarget(), '.outbound', { mode: 'reply_only', forward: false, settleMs: 40 })));
  const result = await resultPromise;
  assert.equal(result.ok, true);
  assert.equal(result.messages.length, 1);
  assert.equal(result.interceptedCount, 1);
  await wait(50);
  assert.equal(protocolPackets.some(packet => packet.echo === 'outbound-reply'), false);
  await closeWs(control); await closeWs(protocol);
});

test('accepts SealDice reverse websocket suffix /core/ws', async t => {
  await setup(); t.after(teardown);
  const core = await connect(`ws://127.0.0.1:${bridgePort}/core/ws?access_token=test-token`);
  const health = await fetch(`http://127.0.0.1:${bridgePort}/healthz`).then(response => response.json());
  assert.equal(health.coreConnected, true);
  assert.equal(health.coreClients, 1);
  await closeWs(core);
});

async function connectControl() {
  const ws = await connect(`ws://127.0.0.1:${bridgePort}/control?access_token=test-token`);
  ws.send(JSON.stringify({ type: 'hello', protocol: 'aiplugin4-core-bridge', version: 1, client: 'test', token: 'test-token' }));
  const hello = await waitForMessage(ws, message => message.type === 'hello.ok');
  return { ws, hello };
}

function fakeCommand(id, target, raw, capture = {}, timeoutMs = 1000) {
  return {
    type: 'command.invoke', id, target,
    actor: { userId: String(target.userId || '30002'), nickname: 'AI', role: 'member' },
    command: { raw, name: raw.replace(/^\\S+/, ''), args: [] }, capture, timeoutMs
  };
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

test('transparent forwarding, raw frames, login echo routing, and healthz', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await connectProtocol();

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
  assert.deepEqual(health, { ok: true, coreConnected: true, coreClients: 1, protocolClients: 1 });
  await closeWs(protocol); await closeWs(core);
});

test('control invoke captures multiple actions and intercepts them by default', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await connectProtocol();
  const { ws: control, hello } = await connectControl();
  assert.equal(hello.coreConnected, true);

  const seenFake = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.jrrp');
  const coreResponse1 = waitForMessage(core, packet => packet.echo === 'e1');
  const coreResponse2 = waitForMessage(core, packet => packet.echo === 'e2');
  const commandMessage = waitForMessage(control, packet => packet.type === 'command.message' && packet.id === 'invoke-1');
  const resultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'invoke-1');
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
  control.send(JSON.stringify(fakeCommand('invoke-1', groupTarget(), '.jrrp', { mode: 'reply_only', forward: false, settleMs: 70, maxMessages: 20 })));

  assert.equal((await seenFake).message[0].data.text, '.jrrp');
  assert.equal((await coreResponse1).status, 'ok');
  assert.equal((await coreResponse2).status, 'ok');
  assert.equal((await commandMessage).intercepted, true);
  const result = await resultPromise;
  assert.equal(result.ok, true);
  assert.equal(result.messages.length, 2);
  assert.equal(result.interceptedCount, 2);
  assert.equal(result.forwardedCount, 0);
  await wait(50);
  assert.equal(protocolPackets.filter(packet => packet.echo === 'e1' || packet.echo === 'e2').length, 0);
  assert.equal(protocolPackets.filter(packet => packet.echo === 'other').length, 1);
  await closeWs(control); await closeWs(protocol); await closeWs(core);
});

test('lane capture intercepts bot message events and returns them to control', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await connectProtocol();
  const { ws: control } = await connectControl();
  const protocolPackets = [];
  protocol.on('message', data => { try { protocolPackets.push(JSON.parse(data.toString())); } catch (_) {} });
  const controlMessage = waitForMessage(control, packet => packet.type === 'command.message' && packet.id === 'event-1');
  const resultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'event-1');
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.event') {
      core.send(JSON.stringify({ ...FIXTURE, message_id: 91001, raw_message: 'bot-event', message: [{ type: 'text', data: { text: 'bot-event' } }] }));
    }
  });
  control.send(JSON.stringify(fakeCommand('event-1', groupTarget(), '.event', { mode: 'lane', forward: false, settleMs: 40 })));
  assert.equal((await controlMessage).intercepted, true);
  const result = await resultPromise;
  assert.equal(result.messages.length, 1);
  assert.equal(result.messages[0].source, 'event');
  assert.equal(result.interceptedCount, 1);
  assert.equal(result.forwardedCount, 0);
  await wait(50);
  assert.equal(protocolPackets.some(packet => packet.raw_message === 'bot-event'), false);
  await closeWs(control); await closeWs(protocol); await closeWs(core);
});

test('forward=true forwards actions and routes protocol API responses back to core', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await connectProtocol();
  const { ws: control } = await connectControl();
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

  const resultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'forward-1');
  control.send(JSON.stringify(fakeCommand('forward-1', groupTarget(), '.forward', { mode: 'lane', forward: true, settleMs: 50 })));
  const result = await resultPromise;
  assert.equal(result.ok, true);
  assert.equal(result.forwardedCount, 2);
  assert.equal(result.interceptedCount, 0);
  await wait(50);
  assert.equal(protocolActions[0].params.group_id, 20002);
  assert.equal(coreResponses.some(packet => packet.echo === 'echo-.forward'), true);
  assert.equal(protocolEvents.some(packet => packet.raw_message === 'forwarded-event'), true);
  await closeWs(control); await closeWs(protocol); await closeWs(core);
});

test('same-lane invocations serialize while different lanes run concurrently', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const { ws: control } = await connectControl();
  const started = [];
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    started.push(`${packet.group_id}:${packet.raw_message}`);
    const delay = packet.group_id === 20002 ? 45 : 5;
    setTimeout(() => sendGroupAction(core, packet.group_id, packet.raw_message, `echo-${packet.group_id}-${packet.raw_message}`), delay);
  });

  const sameResults = [];
  control.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.type === 'command.result' && (packet.id === 'same-a' || packet.id === 'same-b')) sameResults.push(packet);
  });
  control.send(JSON.stringify(fakeCommand('same-a', groupTarget('20002'), '.a', { settleMs: 10 })));
  control.send(JSON.stringify(fakeCommand('same-b', groupTarget('20002'), '.b', { settleMs: 10 })));
  const differentResult = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'different');
  control.send(JSON.stringify(fakeCommand('different', groupTarget('20003'), '.c', { settleMs: 10 })));
  const different = await differentResult;
  assert.equal(different.ok, true);
  await waitFor(() => sameResults.length === 2, 3000);
  assert.deepEqual(sameResults.map(item => item.id), ['same-a', 'same-b']);
  assert.deepEqual(started.slice(0, 2), ['20002:.a', '20003:.c']);
  assert.equal(started.indexOf('20002:.b') > started.indexOf('20002:.a'), true);
  await closeWs(control); await closeWs(core);
});

test('private send_msg targets private lanes correctly', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const { ws: control } = await connectControl();
  const actionPromise = new Promise(resolve => core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message') {
      core.send(JSON.stringify({ action: 'send_msg', params: { message_type: 'private', user_id: 30002, message: 'private reply' }, echo: 'private-echo' }));
    }
    if (packet.echo === 'private-echo') resolve(packet);
  }));
  const resultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'private-1');
  control.send(JSON.stringify(fakeCommand('private-1', privateTarget(), '.private', { settleMs: 20 })));
  const [result, response] = await Promise.all([resultPromise, actionPromise]);
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'private reply');
  assert.equal(response.status, 'ok');
  await closeWs(control); await closeWs(core);
});

test('lane mode captures events, maxMessages bounds mixed replies, and reply_only marks ambiguity', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const protocol = await connectProtocol();
  const { ws: control } = await connectControl();
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
  const laneResultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'lane-1');
  control.send(JSON.stringify(fakeCommand('lane-1', groupTarget(), '.lane', { mode: 'lane', maxMessages: 2, settleMs: 100 })));
  const laneResult = await laneResultPromise;
  assert.equal(laneResult.ok, true);
  assert.equal(laneResult.messages.length, 2);
  assert.equal(laneResult.completedBy, 'max_messages');
  await wait(50);
  const replyResultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'reply-1');
  control.send(JSON.stringify(fakeCommand('reply-1', groupTarget(), '.reply', { mode: 'reply_only', settleMs: 30 })));
  const replyResult = await replyResultPromise;
  assert.equal(replyResult.ok, true);
  assert.equal(replyResult.messages.length, 1);
  assert.equal(replyResult.messages[0].text, 'reply');
  assert.equal(replyResult.ambiguous, true);
  assert.equal(invocationCount, 2);
  await closeWs(control); await closeWs(protocol); await closeWs(core);
});

test('timeouts, missing core, and core disconnects fail cleanly', async t => {
  await setup(); t.after(teardown);
  const { ws: control } = await connectControl();
  const missing = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'missing');
  control.send(JSON.stringify(fakeCommand('missing', groupTarget(), '.missing', {}, 100)));
  const missingResult = await missing;
  assert.equal(missingResult.ok, false);
  assert.match(missingResult.error, /核心 WS/);

  const core = await connectCore();
  const timeoutResultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'timeout');
  control.send(JSON.stringify(fakeCommand('timeout', groupTarget(), '.timeout', { settleMs: 20 }, 100)));
  const timeoutResult = await timeoutResultPromise;
  assert.equal(timeoutResult.ok, true);
  assert.equal(timeoutResult.completedBy, 'timeout');

  const disconnectResultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'disconnect');
  control.send(JSON.stringify(fakeCommand('disconnect', groupTarget(), '.disconnect', {}, 1000)));
  await waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.disconnect');
  await closeWs(core);
  const disconnectResult = await disconnectResultPromise;
  assert.equal(disconnectResult.ok, false);
  assert.match(disconnectResult.error, /disconnected/);
  await closeWs(control);
});

test('multiple core connections route login responses, events, and invocations by self_id', async t => {
  await setup(); t.after(teardown);
  const core1 = await connectCore();
  const core2 = await connectCore();
  const protocol = await connectProtocol();
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

  const { ws: control } = await connectControl();
  core1.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.only-bot-1') sendGroupAction(core1, 20002, 'bot1', 'bot1-echo');
  });
  const resultPromise = waitForMessage(control, packet => packet.type === 'command.result' && packet.id === 'bot-1');
  control.send(JSON.stringify(fakeCommand('bot-1', groupTarget('20002', '10001'), '.only-bot-1', { settleMs: 20 })));
  const result = await resultPromise;
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'bot1');
  await closeWs(control); await closeWs(protocol); await closeWs(core1); await closeWs(core2);
});

test('invalid auth and malformed control hello are rejected', async t => {
  await setup(); t.after(teardown);
  const bad = new WebSocket(`ws://127.0.0.1:${bridgePort}/core?access_token=wrong`);
  bad.on('error', () => {});
  await waitForEvent(bad, 'close');

  const control = await connect(`ws://127.0.0.1:${bridgePort}/control?access_token=test-token`);
  control.on('error', () => {});
  control.send(JSON.stringify({ type: 'not-hello' }));
  await waitForEvent(control, 'close');
});

async function waitFor(predicate, timeout = 3000) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start >= timeout) throw new Error('timeout waiting for condition');
    await wait(5);
  }
}
