'use strict';

// 主测试文件：端点形态（D1）、虚拟世界（D2）、action 应答（D3）、账本（D5）、控制面（D6）。
//
// 本文件持有原 21 例里的 15 例（迁出 5 例去 relay.test.js、删掉 1 例 notice/meta）。
// 默认跑 standalone：非 action 帧一律丢弃 ⇒ `source:'event'` 不可能出现，
// 所以凡是"捕获核心回复"的用例都改成"核心收到注入事件后回一条 action"，
// 断言落在账本 / deliveredCount / `source:'action'`（见执行方案 §7.1）。
// relay 专属行为（出站连接/重连/echo 回程/事件丢弃）全部在 relay.test.js。

const {
  test, assert, FIXTURE,
  wait, waitFor, waitForMessage, waitForRaw, waitForEvent, assertNoJsonArrival,
  closeWs, setup, teardown, state,
  connectCore, connectCoreAt, connectPlugin,
  groupTarget, privateTarget, sendGroupAction, wsArgs, wsStructuredArgs, wsCall
} = require('./helpers');

const healthz = () => fetch(`http://127.0.0.1:${state.bridgePort}/healthz`).then(response => response.json());

// REQ-D1-14：核心端不占用路径。宿主只要给出 host:port，连上就算核心端，
// 无路径 / `/` / 历史写法 `/core`、`/core/ws` / 任意自定义路径都必须等价。
test('accepts core websocket on any path, including no path at all', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const health = await healthz();
  assert.equal(health.coreConnected, true);
  assert.equal(health.coreClients, 1);
  await closeWs(core);

  // 逐个路径各连一次：每次都应能连上且被认成核心端
  for (const pathname of ['', '/', '/core', '/core/ws', '/whatever/else']) {
    const ws = await connectCoreAt(pathname);
    const probe = await healthz();
    assert.equal(probe.coreConnected, true, `路径 ${pathname || '(无)'} 应能作为核心端连上`);
    assert.equal(probe.coreClients, 1, `路径 ${pathname || '(无)'} 应只有 1 个核心客户端`);
    await closeWs(ws);
  }
});

// `:259` 的**重写**（§7.1）：原例断言"裸帧双向透传"，那是 relay 的语义；
// standalone 下核心只作为客户端发 action，裸帧/非 action 帧一律丢弃不转发。
// 本用例因此改断言两件事：① /healthz 16 字段深比较（REQ-D10-01 的验收锚点）
// ② 裸帧不产生任何回包、连接不断、进程存活。
test('standalone healthz shape is 16 fields and raw frames are dropped not forwarded', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const health = await healthz();

  // 16 字段 = 8 新增 + 5 既有保留 + 3 遗留置 null（REQ-D10-01）
  assert.deepEqual(health, {
    ok: true,
    coreConnected: true,
    coreClients: 1,
    pluginConnected: false,
    pluginClients: 0,
    mode: 'standalone',
    platform: 'QQ',
    corePort: state.bridgePort,
    virtualSelfId: 10001,
    actionCount: 0,
    eventCount: 0,
    ledgerSize: 0,
    legacyTarget: false,
    // 三个 relay 遗留字段：必须是 null，不是 0 / false / []（deepEqual 不接受）
    protocolConnected: null,
    protocolClients: null,
    droppedProtocolEvents: null
  });
  assert.equal(Object.keys(health).length, 16);

  // 裸帧：不崩溃、不回包、连接保持
  core.send('raw core frame');
  core.send(JSON.stringify({ post_type: 'meta_event', meta_event_type: 'heartbeat', self_id: 10001 }));
  assert.equal(await assertNoJsonArrival(core, () => true, 120), null, 'standalone 下裸帧与非 action 帧都不应产生任何回包');
  const health2 = await healthz();
  assert.equal(health2.coreConnected, true);
  assert.equal(health2.protocolConnected, null);
  await closeWs(core);
});

// `:313` 的**反转**（§7.1）：1.1.1 里"无协议端 → 立刻回 failed"，
// standalone 下没有协议端这个概念，所有 action 都必须本地答出 ok。
test('standalone answers actions locally instead of failing when no protocol client exists', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();

  const login = waitForMessage(core, packet => packet.echo === 'login-local');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'login-local' }));
  assert.deepEqual(await login, { status: 'ok', retcode: 0, data: { user_id: 10001, nickname: 'AI骰娘' }, echo: 'login-local' });

  const send = waitForMessage(core, packet => packet.echo === 'send-local');
  sendGroupAction(core, 20002, 'local', 'send-local');
  const sendReply = await send;
  assert.equal(sendReply.status, 'ok');
  assert.equal(sendReply.retcode, 0);
  assert.equal(typeof sendReply.data.message_id, 'number');
  await closeWs(core);
});

// `:329` 的**改**（§7.1）——唯一"只删转发断言"就够的一例：
// 它靠核心发 send_group_msg **action** 驱动，standalone 下由 Responder 本地受理。
test('invoke captures multiple actions and intercepts them by default', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const plugin = await connectPlugin();

  const seenFake = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.jrrp');
  const coreResponse1 = waitForMessage(core, packet => packet.echo === 'e1');
  const coreResponse2 = waitForMessage(core, packet => packet.echo === 'e2');

  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message' || packet.raw_message !== '.jrrp') return;
    sendGroupAction(core, 20002, '第一条结果', 'e1');
    setTimeout(() => sendGroupAction(core, 20002, '第二条结果', 'e2'), 30);
    sendGroupAction(core, 99999, 'unrelated', 'other');
  });
  const resultPromise = wsCall(plugin, wsArgs(groupTarget(), '.jrrp', { mode: 'reply_only', forward: false, settleMs: 70, maxMessages: 20 }));

  assert.equal((await seenFake).message[0].data.text, '.jrrp');
  // standalone：两个 send_group_msg 都由 Responder 本地答 ok（原来断言的是"经协议端回程"）
  assert.equal((await coreResponse1).status, 'ok');
  assert.equal((await coreResponse2).status, 'ok');
  const packet = await resultPromise;
  assert.equal(packet.type, 'core_command_result');
  const result = packet.result;
  assert.equal(result.ok, true);
  assert.equal(result.messages.length, 2);
  // standalone 下"拦截"语义消失：interceptedCount / forwardedCount 恒 0，真实条数看 deliveredCount
  assert.equal(result.interceptedCount, 0);
  assert.equal(result.deliveredCount, 2);
  // standalone 下 forwardedCount 恒 0（REQ-D6-04）
  assert.equal(result.forwardedCount, 0);
  assert.equal(result.mode, 'standalone');
  assert.equal(result.ambiguous, false);
  await closeWs(core);
});

// `:367` 的**重写**（§7.1）：原例靠"核心收到假事件后推回一条 bot 消息事件"，
// 而 standalone 分支丢弃所有非 action 帧 ⇒ source:'event' 永不可能出现。
// 改为"核心收到假事件后回 send_group_msg **action**"，断言落在账本 / deliveredCount / source:'action'。
test('lane capture intercepts bot action replies', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const plugin = await connectPlugin();
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.event') {
      sendGroupAction(core, packet.group_id, 'bot-action', 'bot-action-echo');
    }
  });
  const packet = await wsCall(plugin, wsArgs(groupTarget(), '.event', { mode: 'lane', forward: false, settleMs: 40 }));
  assert.equal(packet.type, 'core_command_result');
  const result = packet.result;
  assert.equal(result.ok, true);
  assert.equal(result.messages.length, 1);
  assert.equal(result.messages[0].source, 'action');
  assert.equal(result.messages[0].text, 'bot-action');
  // 三个"旧语义"字段在 standalone 下全部归零，真实条数只看 deliveredCount
  assert.equal(result.interceptedCount, 0);
  assert.equal(result.forwardedCount, 0);
  assert.equal(result.deliveredCount, 1);
  await closeWs(core);
});

test('same-lane invocations serialize while different lanes run concurrently', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const pluginA = await connectPlugin();
  const pluginB = await connectPlugin();
  const pluginC = await connectPlugin();
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
  const sameA = wsCall(pluginA, wsArgs(groupTarget('20002'), '.a', { settleMs: 10 }));
  await aStarted;
  const sameB = wsCall(pluginB, wsArgs(groupTarget('20002'), '.b', { settleMs: 10 }));
  const different = wsCall(pluginC, wsArgs(groupTarget('20003'), '.c', { settleMs: 10 }));
  const packets = await Promise.all([sameA, sameB, different]);
  assert.equal(packets.every(packet => packet.type === 'core_command_result' && packet.result.ok === true), true);
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
  const plugin = await connectPlugin();
  const actionPromise = new Promise(resolve => core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message') {
      core.send(JSON.stringify({ action: 'send_msg', params: { message_type: 'private', user_id: 30002, message: 'private reply' }, echo: 'private-echo' }));
    }
    if (packet.echo === 'private-echo') resolve(packet);
  }));
  const packetPromise = wsCall(plugin, wsArgs(privateTarget(), '.private', { forward: false, settleMs: 20 }));
  const [packet, response] = await Promise.all([packetPromise, actionPromise]);
  assert.equal(packet.type, 'core_command_result');
  const result = packet.result;
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'private reply');
  assert.equal(response.status, 'ok');
  await closeWs(core);
});

// `:486` 的**重写**（§7.1）：
//  ① maxMessages 部分改为"核心回多条 action"驱动（原来靠核心连推 5 条事件，standalone 下没有这条通路）
//  ② ambiguous 歧义部分迁入 relay.test.js；这里只保留"standalone 下 ambiguous 恒 false"的恒定性断言
test('lane mode bounds action replies and reply_only never reports ambiguity in standalone', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const plugin = await connectPlugin();
  let invocationCount = 0;
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    invocationCount++;
    if (packet.raw_message === '.lane') {
      // 同步连发 5 条：第 2 条触发 max_messages 完成后，剩下 3 条就没有活跃 Invocation 可投，
      // 必须被丢弃 —— 若改用 setTimeout 错开发送，它们会漏进下一轮 .reply 的窗口（本用例先前就踩了这个坑）
      for (let i = 0; i < 5; i++) sendGroupAction(core, packet.group_id, `lane-${i}`, `lane-echo-${i}`);
    } else if (packet.raw_message === '.reply') {
      sendGroupAction(core, packet.group_id, 'reply', 'reply-echo');
    }
  });
  const lanePacket = await wsCall(plugin, wsArgs(groupTarget(), '.lane', { mode: 'lane', maxMessages: 2, settleMs: 100 }));
  assert.equal(lanePacket.type, 'core_command_result');
  const laneResult = lanePacket.result;
  assert.equal(laneResult.ok, true);
  assert.equal(laneResult.messages.length, 2);
  assert.equal(laneResult.completedBy, 'max_messages');
  // 等落单的 3 条彻底沉淀，避免它们被下一轮 Invocation 接走
  await wait(50);
  const replyPacket = await wsCall(plugin, wsArgs(groupTarget(), '.reply', { mode: 'reply_only', settleMs: 30 }));
  assert.equal(replyPacket.type, 'core_command_result');
  const replyResult = replyPacket.result;
  assert.equal(replyResult.ok, true);
  assert.equal(replyResult.messages.length, 1);
  assert.equal(replyResult.messages[0].text, 'reply');
  // standalone 下 ambiguous 恒 false（REQ-D6-04）
  assert.equal(replyResult.ambiguous, false);
  assert.equal(invocationCount, 2);
  await closeWs(core);
});

test('timeouts, missing core, and core disconnects fail cleanly', async t => {
  await setup(); t.after(teardown);
  const plugin = await connectPlugin();
  const missing = await wsCall(plugin, wsArgs(groupTarget(), '.missing', {}, 100));
  assert.equal(missing.type, 'core_command_result');
  assert.equal(missing.result.ok, false);
  assert.match(missing.result.error, /核心 WS/);

  const core = await connectCore();
  const timeoutPacket = await wsCall(plugin, wsArgs(groupTarget(), '.timeout', { settleMs: 20 }, 100));
  assert.equal(timeoutPacket.type, 'core_command_result');
  const timeout = timeoutPacket.result;
  assert.equal(timeout.ok, true);
  assert.equal(timeout.completedBy, 'timeout');

  const disconnectPromise = wsCall(plugin, wsArgs(groupTarget(), '.disconnect', {}, 1000));
  await waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.disconnect');
  await closeWs(core);
  const disconnectPacket = await disconnectPromise;
  assert.equal(disconnectPacket.type, 'core_command_result');
  const disconnect = disconnectPacket.result;
  assert.equal(disconnect.ok, false);
  assert.match(disconnect.error, /disconnected/);
});

// `:546` 的**重写**（§7.1）：1.1.1 允许多核心并按 self_id 路由；
// standalone 下是单核心端点，第二条连接必须被 close(1013) 拒绝（REQ-D1-05）。
test('second core connection is rejected with close code 1013 in standalone', async t => {
  await setup(); t.after(teardown);
  const core1 = await connectCore();
  const health = await healthz();
  assert.equal(health.coreClients, 1);

  const { WebSocket } = require('ws');
  const core2 = new WebSocket(`ws://127.0.0.1:${state.bridgePort}/core?access_token=test-token`);
  core2.on('error', () => {});
  const [code, reason] = await new Promise(resolve => {
    core2.once('close', (code, reason) => resolve([code, reason.toString()]));
  });
  assert.equal(code, 1013);
  assert.match(reason, /standalone/);
  const health2 = await healthz();
  assert.equal(health2.coreClients, 1, '第二核心不进 coreClients');
  assert.equal(health2.coreConnected, true);

  // 第一条连接不受影响：还能正常本地应答
  const login = waitForMessage(core1, packet => packet.echo === 'still-alive');
  core1.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'still-alive' }));
  assert.equal((await login).status, 'ok');
  await closeWs(core1);
});

test('plugin WS protocol: ping/pong, request validation, and core_command roundtrip', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const plugin = await connectPlugin();

  const pong = waitForMessage(plugin, packet => packet.type === 'pong');
  plugin.send(JSON.stringify({ type: 'ping' }));
  assert.equal((await pong).type, 'pong');

  const noRequestId = waitForMessage(plugin, packet => packet.type === 'core_command_error');
  plugin.send(JSON.stringify({ type: 'core_command', payload: { target: groupTarget() } }));
  assert.equal((await noRequestId).error, 'requestId is required');

  const badPayload = waitForMessage(plugin, packet => packet.type === 'core_command_error');
  plugin.send(JSON.stringify({ type: 'core_command', requestId: 'r-bad', payload: 'bad' }));
  assert.equal((await badPayload).error, 'payload is required');

  const noTarget = waitForMessage(plugin, packet => packet.type === 'core_command_error');
  plugin.send(JSON.stringify({ type: 'core_command', requestId: 'r-notarget', payload: { raw_message: '.x' } }));
  assert.equal((await noTarget).error, 'target is required');

  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.ban 100') sendGroupAction(core, packet.group_id, 'banned', 'ban-echo');
  });
  const resultPacket = await wsCall(plugin, wsArgs(groupTarget(), '.ban 100', { forward: false, settleMs: 30 }));
  assert.equal(resultPacket.type, 'core_command_result');
  assert.equal(resultPacket.result.ok, true);
  assert.equal(resultPacket.result.messages[0].text, 'banned');
  await closeWs(core);
});

test('structured core_command calls are converted to the core raw message', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const plugin = await connectPlugin();
  const received = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '.help ext');
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.help ext') sendGroupAction(core, packet.group_id, 'structured-ok', 'structured-action');
  });
  const resultPromise = wsCall(plugin, wsStructuredArgs(groupTarget(), 'help', ['ext'], { settleMs: 20 }));
  await received;
  const packet = await resultPromise;
  assert.equal(packet.type, 'core_command_result');
  const result = packet.result;
  assert.equal(result.ok, true);
  assert.equal(result.messages[0].text, 'structured-ok');
  await closeWs(core);
});

test('trigger and at are injected into the fake OB11 message', async t => {
  await setup(); t.after(teardown);
  const core = await connectCore();
  const plugin = await connectPlugin();
  const received = waitForMessage(core, packet => packet.post_type === 'message' && packet.raw_message === '[CQ:at,qq=50005] .rav 1d100 50');
  const resultPromise = wsCall(plugin, {
    target: groupTarget('20002', '10001'),
    actor: { userId: '30002', nickname: 'AI', role: 'member' },
    command: { raw: '.rav 1d100 50', name: 'rav', args: ['1d100', '50'] },
    capture: { mode: 'reply_only', forward: false, settleMs: 20 },
    timeoutMs: 1000,
    trigger: '40004',
    at: ['50005']
  });
  const packet = await received;
  assert.equal(packet.user_id, 40004);
  assert.equal(packet.sender.user_id, 40004);
  assert.deepEqual(packet.message[0], { type: 'at', data: { qq: '50005' } });
  assert.equal(packet.message[1].data.text, '.rav 1d100 50');
  const result = await resultPromise;
  assert.equal(result.type, 'core_command_result');
  assert.equal(result.result.ok, true);
  await closeWs(core);
});

test('core_command payload validation rejects ambiguous or empty commands', async t => {
  await setup(); t.after(teardown);
  const plugin = await connectPlugin();

  const both = waitForMessage(plugin, packet => packet.type === 'core_command_error');
  plugin.send(JSON.stringify({ type: 'core_command', requestId: 'both', payload: {
    target: groupTarget(),
    actor: { userId: '30002', nickname: 'AI', role: 'member' },
    raw_message: '.help',
    command: { raw: '.help', name: 'help', args: [] }
  } }));
  assert.match((await both).error, /raw_message.*command|command.*raw_message/i);

  const neither = waitForMessage(plugin, packet => packet.type === 'core_command_error');
  plugin.send(JSON.stringify({ type: 'core_command', requestId: 'neither', payload: {
    target: groupTarget(),
    actor: { userId: '30002', nickname: 'AI', role: 'member' }
  } }));
  assert.match((await neither).error, /command[.]raw or raw_message is required/);
});

test('invalid auth and wrong tokens are rejected', async t => {
  await setup(); t.after(teardown);
  const { WebSocket } = require('ws');
  const mcpGone = await fetch(`http://127.0.0.1:${state.bridgePort}/mcp`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}'
  });
  assert.equal(mcpGone.status, 404);

  const badCore = new WebSocket(`ws://127.0.0.1:${state.bridgePort}/core?access_token=wrong`);
  badCore.on('error', () => {});
  await waitForEvent(badCore, 'close');

  const badPlugin = new WebSocket(`ws://127.0.0.1:${state.bridgePort}/plugin?access_token=wrong`);
  badPlugin.on('error', () => {});
  await waitForEvent(badPlugin, 'close');

  const core = await connectCore();
  const plugin = await connectPlugin();
  const health = await healthz();
  assert.equal(health.pluginConnected, true);
  assert.equal(health.pluginClients, 1);
  assert.equal(health.coreConnected, true);
  await closeWs(core); await closeWs(plugin);
});
