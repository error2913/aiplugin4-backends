'use strict';

// relay（双连接冗余）分支的回归网。
//
// 这些用例在 1.1.1 上全绿，S0 落地时**逐字**冻结它们的行为：
// 断言一个字都不改，只把 setup/连接工具从 ./helpers 里取。
// S3 之后 relay 路径必须仍然全绿 —— 这是"新旧共存"唯一的客观判据
// （对应 00 REQ-D1-02/D1-14、01 §7.1 的"迁出 5 例 + 新增 R6"）。
//
// 本文件所有用例都走 setup({ withProtocol: true }) ⇒ mode 钉死为 relay。

const {
  test, assert, FIXTURE,
  wait, waitFor, waitForMessage, waitForEvent, assertNoJsonArrival,
  closeWs, startProtocolServer, freePort,
  setup, teardown, state, protocolConnections,
  connectCore, connectPlugin, waitForProtocol,
  groupTarget, sendGroupAction, wsArgs, wsCall
} = require('./helpers');

const healthz = () => fetch(`http://127.0.0.1:${state.bridgePort}/healthz`).then(response => response.json());

test('R1 legacy: protocol client connects outbound with token', async t => {
  await setup({ withProtocol: true }); t.after(teardown);
  const protocol = await waitForProtocol();
  assert.ok(protocol);
  assert.equal(protocol.readyState, 1 /* WebSocket.OPEN */);
  await waitFor(() => state.bridge.protocolClients.size === 1, 3000);
  const health = await healthz();
  assert.equal(health.protocolConnected, true);
  assert.equal(health.protocolClients, 1);
});

test('R2 legacy: reconnects after protocol server disconnects', async t => {
  await setup({ withProtocol: true }); t.after(teardown);
  const first = await waitForProtocol();
  // 不能直接断言 size===1：协议端一侧的 connection 事件先于桥这一侧的 open 回调触发，
  // `node --test` 并行跑 5 个文件时这个窗口会被拉到毫秒级，直接断言会偶发失败（实测过一次）。
  await waitFor(() => state.bridge.protocolClients.size === 1, 3000);
  await closeWs(first);
  await waitFor(() => state.bridge.protocolClients.size === 0, 3000);
  const second = await waitForProtocol(5000);
  assert.notEqual(second, first);
  assert.equal(second.readyState, 1 /* WebSocket.OPEN */);
  const health = await healthz();
  assert.equal(health.protocolConnected, true);
  assert.equal(health.protocolClients, 1);
});

test('R3 legacy: retries until protocol server becomes available', async t => {
  const targetPort = await freePort();
  await setup({ withProtocol: true, protocolUrl: `ws://127.0.0.1:${targetPort}` });
  t.after(teardown);
  await wait(1200);
  assert.equal(state.bridge.protocolClients.size, 0);
  await startProtocolServer('test-token', targetPort);
  await waitFor(() => state.bridge.protocolClients.size === 1, 8000);
  const health = await healthz();
  assert.equal(health.protocolConnected, true);
});

test('R4 legacy: wrong protocol token never authorized', async t => {
  await setup({ withProtocol: true, protocolToken: 'wrong' }); t.after(teardown);
  await wait(1500); // 观察至少一轮重连尝试
  assert.equal(protocolConnections.length, 0);
  assert.equal(state.bridge.protocolClients.size, 0);
});

test('R5 legacy: forward=true relays action to protocol side and routes echo back', async t => {
  await setup({ withProtocol: true }); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();
  const plugin = await connectPlugin();
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

  const packet = await wsCall(plugin, wsArgs(groupTarget(), '.forward', { mode: 'lane', forward: true, settleMs: 50 }));
  assert.equal(packet.type, 'core_command_result');
  const result = packet.result;
  assert.equal(result.ok, true);
  assert.equal(result.forwardedCount, 2);
  assert.equal(result.interceptedCount, 0);
  await wait(50);
  assert.equal(protocolActions[0].params.group_id, 20002);
  assert.equal(coreResponses.some(packet => packet.echo === 'echo-.forward'), true);
  assert.equal(protocolEvents.some(packet => packet.raw_message === 'forwarded-event'), true);
  await closeWs(protocol); await closeWs(core);
});

test('R6 legacy: notice/meta events dropped while echo responses still route to core', async t => {
  await setup({ withProtocol: true }); t.after(teardown);
  const core = await connectCore();
  const protocol = await waitForProtocol();

  const notice = { post_type: 'notice', notice_type: 'group_increase', group_id: 20002, user_id: 30003, self_id: 10001, time: Math.floor(Date.now() / 1000) };
  const meta = { post_type: 'meta_event', meta_event_type: 'heartbeat', self_id: 10001, time: Math.floor(Date.now() / 1000), status: { online: true } };
  protocol.send(JSON.stringify(notice));
  protocol.send(JSON.stringify(meta));
  assert.equal(await assertNoJsonArrival(core, packet => packet.post_type === 'notice' || packet.post_type === 'meta_event'), null, 'notice/meta 事件不应转发给核心');

  // API 响应（echo 路由）不受影响：核心发起的请求仍能收到协议端回包
  const requestAtProtocol = waitForMessage(protocol, packet => packet.action === 'get_group_info' && packet.echo === 'group-echo');
  const responseAtCore = waitForMessage(core, packet => packet.echo === 'group-echo');
  core.send(JSON.stringify({ action: 'get_group_info', params: { group_id: 20002 }, echo: 'group-echo' }));
  assert.equal((await requestAtProtocol).action, 'get_group_info');
  protocol.send(JSON.stringify({ status: 'ok', retcode: 0, data: { group_id: 20002, group_name: '测试群' }, echo: 'group-echo' }));
  assert.equal((await responseAtCore).data.group_name, '测试群');

  const health = await healthz();
  assert.equal(health.droppedProtocolEvents, 2);
  await closeWs(protocol); await closeWs(core);
});
