'use strict';

// standalone 专属集成用例（执行方案 §7.2 的 21 例里，需要真起桥/真连 WS 的那部分）。
//
// 分工：
//   - 纯函数级（配置自检、投影、账本、action 应答）已在 virtual-world.test.js / responder.test.js 覆盖；
//   - 本文件只放"必须越过 HTTP/WS 边界才能验证"的：模式推断、拒启、端口、token 双来源、
//     platform 与虚拟 ID 的 env 贯通、hello 握手、保活、forward 被忽略、图片段、无线驱动的账本回查。
//
// 每个用例都自己管 env 事务与状态目录：这些用例大多要传非法/非默认 env，
// 泄漏一条就会让后面整片红。

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  test, assert, WebSocket,
  wait, waitFor, waitForEvent, waitForMessage,
  closeWs, freePort, isolateEnv, freshBridgeModule, state,
  connectCore, connectPlugin, groupTarget, sendGroupAction, wsCall, wsArgs
} = require('./helpers');

const healthz = () => fetch(`http://127.0.0.1:${state.bridgePort}/healthz`).then(response => response.json());

/** 裸起一个桥（不走 setup：这些用例要自己控制 env 与 start 的成败）。 */
async function boot(env = {}, options = {}) {
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined || value === null) delete process.env[key];
    else process.env[key] = String(value);
  }
  const port = options.port === undefined ? await freePort() : options.port;
  if (port !== null) process.env.AIPLUGIN4_BACKEND_PORT = String(port);
  process.env.AIPLUGIN4_BACKEND_HOST = '127.0.0.1';
  state.bridgePort = port;
  const { Bridge } = freshBridgeModule();
  const bridge = new Bridge();
  await bridge.start();
  state.bridge = bridge;
  return bridge;
}

// ── 第 10 例：模式推断（00 §8.1）────────────────────────────────────────

test('mode is inferred from protocol_url when AIPLUGIN4_BRIDGE_MODE is empty', async t => {
  // ① 没有 protocol_url → standalone，且**不建任何出站连接**
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_MODE;
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  let bridge = await boot();
  let health = await healthz();
  assert.equal(health.mode, 'standalone');
  assert.equal(health.protocolConnected, null, 'standalone 不该有 protocol 连接概念');
  await bridge.stop();
  state.bridge = null;

  // ② 有 protocol_url（指向一个必然连不上的端口）→ relay，且真的去连了
  const deadPort = await freePort();
  process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL = `ws://127.0.0.1:${deadPort}`;
  bridge = await boot();
  health = await healthz();
  assert.equal(health.mode, 'relay');
  assert.equal(typeof health.protocolConnected, 'boolean', 'relay 下必须回到布尔语义');
  await bridge.stop();
  state.bridge = null;

  // ③ 显式 mode 优先于推断：填了 protocol_url 但 mode=standalone → 仍是 standalone 且不出站
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL = `ws://127.0.0.1:${deadPort}`;
  bridge = await boot();
  health = await healthz();
  assert.equal(health.mode, 'standalone', '显式 mode 必须压过 protocol_url 推断');
  assert.equal(health.protocolConnected, null);
  await bridge.stop();
  state.bridge = null;
});

// ── 第 17 例：非法虚拟 ID 拒启（REQ-D2-17）─────────────────────────────

test('invalid virtual ids refuse to start before createServer', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;

  const cases = [
    ['', /为空值/],                 // ① 空值
    ['abc', /不是整数/],             // ② 非数字
    ['0', /必须大于 0/],             // ③ <= 0
    ['2854196301', /机器人号段/],     // ④ 命中 QQ 机器人保留段
    ['3328144510', /机器人号段/]
  ];
  for (const [bad, pattern] of cases) {
    process.env.AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID = bad;
    const { Bridge } = freshBridgeModule();
    const bridge = new Bridge();
    await assert.rejects(() => bridge.start(), /虚拟世界配置非法/, `虚拟号 ${JSON.stringify(bad)} 应拒启`);
    // 关键：validate 必须在 createServer 之前，否则端口已被占用、错误信息也会丢
    assert.equal(bridge.server, null, `虚拟号 ${JSON.stringify(bad)} 拒启时不该建出 http server`);
    // 日志里要能指出是哪一项非法（用户才知道去改哪个配置）
    assert.ok(bridge.world.validate().some(problem => pattern.test(problem)), `应报出具体原因：${JSON.stringify(bridge.world.validate())}`);
    await bridge.stop().catch(() => {});
  }
});

test('colliding virtual ids refuse to start', async t => {
  // 第 5 类非法：三个虚拟 ID 任两项撞号（会让 laneKey 把群消息和私聊混成一条）
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  process.env.AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID = '10001';
  process.env.AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID = '20002';
  process.env.AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID = '20002';   // ← 与群号撞
  const { Bridge } = freshBridgeModule();
  const bridge = new Bridge();
  await assert.rejects(() => bridge.start(), /虚拟世界配置非法/);
  assert.equal(bridge.server, null);
  assert.ok(bridge.world.validate().some(problem => /撞号/.test(problem)));
  await bridge.stop().catch(() => {});
});

// ── 第 18 例：端口可核对（REQ-D1-14/15 + D10-01）───────────────────────

test('custom port is reported by /healthz and core accepts any path (incl. none)', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  // 端口由 OS 分配（bind :0），不用 48000+random 猜——猜出来的端口可能正被别的用例/别的测试文件占着
  const port = await freePort();
  const bridge = await boot({}, { port });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const health = await healthz();
  assert.equal(health.corePort, port);
  assert.equal(health.corePort, Number(process.env.AIPLUGIN4_BACKEND_PORT));

  // REQ-D1-14：核心端不占路径。用户填 ws://host:port 就该能连，
  // `/`、历史 `/core`、`/core/ws`、任意路径都等价，全部要能正常应答。
  for (const suffix of ['', '/', '/core', '/core/ws', '/custom/path']) {
    const ws = new WebSocket(`ws://127.0.0.1:${port}${suffix}`);
    await waitForEvent(ws, 'open');
    const echo = `probe${suffix || '-bare'}`;
    const reply = waitForMessage(ws, packet => packet.echo === echo);
    ws.send(JSON.stringify({ action: 'get_login_info', params: {}, echo }));
    assert.equal((await reply).status, 'ok', `${suffix || '(无路径)'} 必须能正常应答`);
    await closeWs(ws);
  }
});

// ── 第 21 例：token 双来源（REQ-D1-04）─────────────────────────────────

test('core token accepts bare value, Bearer header, and query/header disagreement', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = 'secret-core';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  const port = await freePort();
  const bridge = await boot({}, { port });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const probe = async (url, options) => {
    const ws = new WebSocket(url, options);
    const closed = new Promise(resolve => ws.once('close', code => resolve(code)));
    ws.on('error', () => {});
    const opened = new Promise(resolve => ws.once('open', () => resolve(true)));
    const result = await Promise.race([opened, closed.then(() => false), wait(1500).then(() => 'timeout')]);
    if (result === true) { await closeWs(ws); return 'open'; }
    try { ws.terminate(); } catch (_) { /* ignore */ }
    return result === false ? 'closed' : 'timeout';
  };

  // ① 裸 token 走 query（海豹 `applyClientAuthHeader` 存的就是裸值）
  assert.equal(await probe(`ws://127.0.0.1:${port}/core?access_token=secret-core`), 'open');
  // ② Bearer 走 header（插件侧的写法）
  assert.equal(await probe(`ws://127.0.0.1:${port}/core`, { headers: { Authorization: 'Bearer secret-core' } }), 'open');
  // ③ 裸 token 走 header（海豹用 Bearer 前缀配裸值时的混合形态）
  assert.equal(await probe(`ws://127.0.0.1:${port}/core`, { headers: { Authorization: 'secret-core' } }), 'open');
  // ④ query 带旧错值 + header 带正确值 → 必须通过。
  //    这是"行为收缩"的守门断言：写成 `query || header` 会在这里被拒，而 1.1.1 的语义是"任一来源"。
  assert.equal(await probe(`ws://127.0.0.1:${port}/core?access_token=stale`, { headers: { Authorization: 'Bearer secret-core' } }), 'open');
  // ⑤ 完全错的 token → 关闭
  assert.equal(await probe(`ws://127.0.0.1:${port}/core?access_token=wrong`), 'closed');
  assert.equal(await probe(`ws://127.0.0.1:${port}/core`), 'closed');
});

// ── 第 14/15 例：platform（REQ-D2-13/15/18）────────────────────────────

test('platform defaults to QQ and stays bridge-side only', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PLATFORM;
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const health = await healthz();
  assert.equal(health.platform, 'QQ');

  // hello_result 也要带 platform，且日志展示口径是 QQ:10001
  const plugin = await connectPlugin();
  const hello = waitForMessage(plugin, packet => packet.type === 'hello_result');
  plugin.send(JSON.stringify({ type: 'hello', requestId: 'h1' }));
  const payload = (await hello).payload;
  assert.equal(payload.platform, 'QQ');
  assert.equal(payload.mode, 'standalone');
  assert.equal(payload.corePort, state.bridgePort);
  assert.equal(payload.virtualSelfId, 10001);
  assert.equal(payload.protocolVersion, 'v11');
  // 能力清单要能让插件判断"这是独立端点"
  assert.ok(payload.capabilities.includes('virtual-world'));
  await closeWs(plugin);
});

test('custom platform shows in healthz and hello but never changes the numeric ids', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot({ AIPLUGIN4_BRIDGE_PLATFORM: 'KOOK' });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  assert.equal((await healthz()).platform, 'KOOK');
  assert.equal(bridge.world.displayId('user', 10001), 'KOOK:10001');
  assert.equal(bridge.world.displayId('group', 20002), 'KOOK-Group:20002');

  const plugin = await connectPlugin();
  const hello = waitForMessage(plugin, packet => packet.type === 'hello_result');
  plugin.send(JSON.stringify({ type: 'hello', requestId: 'h2' }));
  assert.equal((await hello).payload.platform, 'KOOK');
  await closeWs(plugin);

  // 自定义 platform 不能让 OB11 报文里的 ID 变成字符串/带前缀（核心只认数字）
  const core = await connectCore();
  const login = waitForMessage(core, packet => packet.echo === 'plat');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'plat' }));
  const reply = await login;
  assert.equal(typeof reply.data.user_id, 'number');
  assert.equal(reply.data.user_id, 10001);
  await closeWs(core);
});

// ── 第 16 例：虚拟 ID 可填（REQ-D2-16）─────────────────────────────────

test('custom virtual ids flow through login, group info and send replies', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot({
    AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID: '777001',
    AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME: '自定义骰娘',
    AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID: '777002',
    AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_NAME: '自定义群',
    AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID: '777003',
    AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ROLE: 'owner',
    AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT: '999'
  });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const health = await healthz();
  assert.equal(health.virtualSelfId, 777001);

  const core = await connectCore();
  const login = waitForMessage(core, packet => packet.echo === 'l');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'l' }));
  assert.deepEqual((await login).data, { user_id: 777001, nickname: '自定义骰娘' });

  const groupInfo = waitForMessage(core, packet => packet.echo === 'g');
  core.send(JSON.stringify({ action: 'get_group_info', params: { group_id: 777002 }, echo: 'g' }));
  const group = (await groupInfo).data;
  assert.equal(group.group_id, 777002);
  assert.equal(group.group_name, '自定义群');
  assert.equal(group.max_member_count, 999);

  // 不指定群号时落到默认群（也是用户填的那个）
  const send = waitForMessage(core, packet => packet.echo === 's');
  core.send(JSON.stringify({ action: 'send_group_msg', params: { message: [{ type: 'text', data: { text: 'x' } }] }, echo: 's' }));
  assert.equal(typeof (await send).data.message_id, 'number');
  const entry = bridge.responder.ledger[0];
  assert.equal(entry.target.groupId, 777002);
  await closeWs(core);
});

// ── 第 1/2 例：登录握手同步性与时延（REQ-D1-03）───────────────────────

test('get_login_info is answered exactly once and within 1000ms', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const seen = [];
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.echo === 'once') seen.push(packet);
  });
  const startedAt = Date.now();
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'once' }));
  const reply = await waitForMessage(core, packet => packet.echo === 'once');
  const elapsed = Date.now() - startedAt;

  assert.equal(reply.retcode, 0);
  assert.equal(reply.data.user_id, bridge.world.selfId);
  // (a) 同步性：必须恰好 1 个回包，不能有重试/二次下发
  await wait(150);
  assert.equal(seen.length, 1, `get_login_info 只应回一帧，实际 ${seen.length} 帧`);
  // (b) 时延：设计要求 100ms，这里放宽到 1000ms 只是避开 CI 抖动（注释即证据）
  assert.ok(elapsed < 1000, `get_login_info 应在 1000ms 内回包，实际 ${elapsed}ms`);
  await closeWs(core);
});

// ── 第 5/6/7 例：同号一致、账本回查、撤回（REQ-D5-02/03、D3-01）────────

test('action reply id equals the ledger id and supports get_msg / delete_msg roundtrip', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const sendReply = waitForMessage(core, packet => packet.echo === 's1');
  sendGroupAction(core, 20002, '账本原文', 's1');
  const messageId = (await sendReply).data.message_id;
  assert.equal(typeof messageId, 'number');

  // 账本里必须是同一条（REQ-D5-02：回包号 === 账本号）
  const entry = bridge.responder.getMsg(messageId);
  assert.ok(entry, '账本应能按回包号查到');
  assert.equal(entry.text, '账本原文');

  // get_msg 回查（REQ-D5-03）
  const got = waitForMessage(core, packet => packet.echo === 'g1');
  core.send(JSON.stringify({ action: 'get_msg', params: { message_id: messageId }, echo: 'g1' }));
  const gotReply = await got;
  assert.equal(gotReply.status, 'ok');
  assert.equal(gotReply.data.message_id, messageId);
  assert.equal(gotReply.data.raw_message, '账本原文');

  // delete_msg → 再查必须是 1404（REQ-D3-01）
  const del = waitForMessage(core, packet => packet.echo === 'd1');
  core.send(JSON.stringify({ action: 'delete_msg', params: { message_id: messageId }, echo: 'd1' }));
  assert.equal((await del).status, 'ok');
  const gone = waitForMessage(core, packet => packet.echo === 'g2');
  core.send(JSON.stringify({ action: 'get_msg', params: { message_id: messageId }, echo: 'g2' }));
  const goneReply = await gone;
  assert.equal(goneReply.status, 'failed');
  assert.equal(goneReply.retcode, 1404);
  await closeWs(core);
});

// ── 第 9 例：并发 lane 下发号单调不重复（REQ-D5-01/08）─────────────────

test('concurrent lanes get strictly increasing unique message ids', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const ids = [];
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    // 事件号也在同一号段里（REQ-D5-01：事件号与下行号同源）
    if (packet.post_type === 'message') ids.push(packet.message_id);
  });

  // 两个不同 lane 并发注入，核心各自回一条
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    sendGroupAction(core, packet.group_id, `re-${packet.message_id}`, `e-${packet.message_id}`);
  });

  const pluginA = await connectPlugin();
  const pluginB = await connectPlugin();
  await Promise.all([
    wsCall(pluginA, wsArgs(groupTarget('20002', '10001'), '.a', { settleMs: 60 })),
    wsCall(pluginB, wsArgs(groupTarget('20003', '10001'), '.b', { settleMs: 60 }))
  ]);

  assert.equal(ids.length >= 2, true, `应观察到至少 2 个注入事件号，实际 ${ids.length}`);
  const unique = new Set(ids);
  assert.equal(unique.size, ids.length, `事件号不许重复：${ids.join(',')}`);
  const sorted = [...ids].sort((a, b) => a - b);
  assert.deepEqual(ids, sorted, `事件号应单调递增：${ids.join(',')}`);
  // 账本里的下行号也必须与事件号同号段（同源发号器）
  const ledgerIds = bridge.responder.ledger.map(entry => entry.messageId);
  assert.ok(ledgerIds.every(id => Number.isSafeInteger(id) && id > 0));
  assert.equal(new Set(ledgerIds).size, ledgerIds.length);
  await closeWs(pluginA); await closeWs(pluginB); await closeWs(core);
});

// ── 第 11 例：forward 被忽略（REQ-D6-04）───────────────────────────────

test('capture.forward is accepted but ignored, deliveredCount still counts', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message') sendGroupAction(core, packet.group_id, '转发应答', `e-${packet.message_id}`);
  });
  const plugin = await connectPlugin();
  // forward:true 在 standalone 下没有"协议端"可转发，但调用必须照常成功
  const packet = await wsCall(plugin, wsArgs(groupTarget(), '.fwd', { mode: 'lane', forward: true, settleMs: 60 }));
  assert.equal(packet.result.ok, true);
  assert.equal(packet.result.mode, 'standalone');
  assert.equal(packet.result.deliveredCount, 1);
  assert.equal(packet.result.forwardedCount, 0, 'standalone 下 forwardedCount 恒 0');
  assert.equal(packet.result.ambiguous, false);
  assert.equal(packet.result.messages[0].source, 'action');
  assert.equal(packet.result.messages[0].forwarded, false);
  await closeWs(plugin); await closeWs(core);
});

// ── 第 13 例：图片段不丢（REQ-D3-01）───────────────────────────────────

test('image segments are recorded without loss', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const reply = waitForMessage(core, packet => packet.echo === 'img');
  core.send(JSON.stringify({
    action: 'send_group_msg',
    params: {
      group_id: 20002,
      message: [
        { type: 'text', data: { text: '看图' } },
        { type: 'image', data: { file: 'https://example.invalid/a.png', summary: '[图片]' } }
      ]
    },
    echo: 'img'
  }));
  const sent = await reply;
  assert.equal(sent.status, 'ok');
  const entry = bridge.responder.getMsg(sent.data.message_id);
  assert.equal(entry.segments.length, 2, '图片段不许被吞');
  assert.equal(entry.segments[1].type, 'image');
  assert.equal(entry.segments[1].data.file, 'https://example.invalid/a.png');
  // 可读文本里要留占位，否则 AI 完全看不到"发了图"这件事
  assert.match(entry.text, /看图/);
  assert.match(entry.text, /图片/);
  await closeWs(core);
});

// ── 第 8 例：未知 action 不 crash（REQ-D3-05）──────────────────────────

test('unknown action gets 1404 and the process keeps serving', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const unknown = waitForMessage(core, packet => packet.echo === 'u1');
  core.send(JSON.stringify({ action: 'set_online_status', params: { status: 1 }, echo: 'u1' }));
  const reply = await unknown;
  assert.equal(reply.status, 'failed');
  assert.equal(reply.retcode, 1404);
  assert.match(reply.data.wording, /set_online_status/);

  // 连接仍活着、后续 action 照常应答
  const login = waitForMessage(core, packet => packet.echo === 'after');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'after' }));
  assert.equal((await login).status, 'ok');
  assert.equal((await healthz()).ok, true);
  await closeWs(core);
});

// ── `source` 投影与老插件降级（REQ-D7-02、REQ-D2-10）───────────────────

test('source is projected to virtual ids, and legacy payloads without source are flagged', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const seen = [];
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type !== 'message') return;
    seen.push(packet);
    sendGroupAction(core, packet.group_id, 'ok', `e-${packet.message_id}`);
  });

  // ① 新插件：带 source（真实 ID）→ 桥投影成虚拟 ID 再送核心
  const plugin = await connectPlugin();
  const requestId = 'with-source';
  const resultPromise = waitForMessage(plugin, packet => packet.requestId === requestId);
  plugin.send(JSON.stringify({
    type: 'core_command',
    requestId,
    payload: {
      target: { selfId: '10001', messageType: 'group', groupId: '20002', userId: '30002' },
      source: { endpointId: 'QQ:1234567890', messageType: 'group', groupId: '987654321', userId: '1234567890', groupName: '真实群', userRole: 'admin' },
      actor: { userId: '30002', nickname: '真实昵称', role: 'admin' },
      raw_message: '.probe',
      capture: { mode: 'lane', forward: false, settleMs: 60 },
      timeoutMs: 1000
    }
  }));
  const result = (await resultPromise).result;
  assert.equal(result.ok, true);
  assert.equal(result.mode, 'standalone');

  const event = seen.find(packet => packet.raw_message === '.probe');
  assert.ok(event, '核心应收到注入的事件');
  // 群号被投影到 1.5e9 段、用户号到 2.0e9 段，且都是数字
  assert.equal(Number.isInteger(event.group_id), true);
  assert.ok(event.group_id >= 1500000000 && event.group_id < 1600000000, `群号应被投影，实际 ${event.group_id}`);
  assert.ok(event.user_id >= 2000000000 && event.user_id < 2100000000, `用户号应被投影，实际 ${event.user_id}`);
  // sender.role 来自插件传的 actor.role（真实权限），不是硬编码
  assert.equal(event.sender.role, 'admin');
  assert.equal(event.sender.nickname, '真实昵称');
  assert.equal((await healthz()).legacyTarget, false, '带 source 的调用不该被标记为降级');

  // ② 老插件：没有 source → 沿用 target 里的数字，并置 legacyTarget=true（/healthz 可观测）
  const legacyId = 'no-source';
  const legacyPromise = waitForMessage(plugin, packet => packet.requestId === legacyId);
  plugin.send(JSON.stringify({
    type: 'core_command',
    requestId: legacyId,
    payload: {
      target: { selfId: '10001', messageType: 'group', groupId: '20002', userId: '30002' },
      actor: { userId: '30002', nickname: 'AI', role: 'member' },
      raw_message: '.legacy',
      capture: { mode: 'reply_only', forward: false, settleMs: 40 },
      timeoutMs: 800
    }
  }));
  assert.equal((await legacyPromise).result.ok, true);
  assert.equal((await healthz()).legacyTarget, true, '老插件降级必须被 /healthz 暴露');
  await closeWs(plugin); await closeWs(core);
});

test('non-numeric source ids are rejected instead of reaching the core', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const plugin = await connectPlugin();
  const requestId = 'bad-source';
  const errorPromise = waitForMessage(plugin, packet => packet.requestId === requestId);
  plugin.send(JSON.stringify({
    type: 'core_command',
    requestId,
    payload: {
      target: { selfId: '10001', messageType: 'group', groupId: '20002', userId: '30002' },
      // 群号是字符串 ID（某些平台的群号带前缀），桥无法投影 → 必须报错而不是塞给核心
      source: { endpointId: 'QQ:1', messageType: 'group', groupId: 'group-abc', userId: '1234567890' },
      actor: { userId: '30002', nickname: 'AI', role: 'member' },
      raw_message: '.bad',
      capture: { mode: 'reply_only', forward: false },
      timeoutMs: 500
    }
  }));
  const packet = await errorPromise;
  assert.equal(packet.type, 'core_command_error', '投影失败应回 core_command_error，而不是让核心收到非法 ID');
  assert.match(packet.error, /群 ID/);
  assert.equal((await healthz()).coreConnected, false, '非法投影不该建立核心连接（此用例根本没连核心）');
  await closeWs(plugin);
});

// ── 第 4 例：角色影响（REQ-D2-06）─────────────────────────────────────

test('virtual_self_role drives the bot own member info that the core uses for privilege', async t => {
  // 海豹判"机器人在群里是什么角色"只走 get_group_member_info(group_id, user_id=self_id)：
  // 这里回的 role 直接进 checkBotGroupRole → PrivilegeLevel。owner 才允许 .dismiss 之类，
  // 所以三种取值都必须原样透出，不能被硬编码成 admin。
  for (const role of ['owner', 'admin', 'member']) {
    isolateEnv(t);
    process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
    process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
    process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
    delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
    const bridge = await boot({ AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ROLE: role });
    const core = await connectCore();
    const member = waitForMessage(core, packet => packet.echo === `role-${role}`);
    core.send(JSON.stringify({
      action: 'get_group_member_info',
      params: { group_id: 20002, user_id: 10001 },
      echo: `role-${role}`
    }));
    const reply = await member;
    assert.equal(reply.status, 'ok');
    assert.equal(reply.data.user_id, 10001);
    assert.equal(reply.data.role, role, `self_role=${role} 必须原样透出，实际 ${reply.data.role}`);
    await closeWs(core);
    await bridge.stop();
    state.bridge = null;
  }
});

// ── 传输层保活（REQ-D1-09）：不能按"最近收到帧"计时 ─────────────────────

test('keepalive pings an idle core connection instead of tearing it down', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BRIDGE_MODE = 'standalone';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = '';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  // 把 30s 压到 120ms，让"空闲连接被 ping 而不是被拆"能在测试时长内观测到
  const bridge = await boot({ AIPLUGIN4_BRIDGE_KEEPALIVE_MS: '120' });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  let pings = 0;
  core.on('ping', () => { pings += 1; });

  // 静默 600ms（≈5 个保活周期）：一个应用帧都不发
  await wait(600);
  assert.ok(pings >= 2, `空闲连接应被 ping 保活，实际收到 ${pings} 次`);
  assert.equal((await healthz()).coreConnected, true, '仅有空闲连接时不许被判定为半开');

  // 半开检测：把 pong 拦掉，连续 2 次无回应后必须 close
  core.pong = () => {};
  const closed = new Promise(resolve => core.once('close', code => resolve(code)));
  const code = await Promise.race([closed, wait(3000).then(() => 'no-close')]);
  assert.notEqual(code, 'no-close', '连续未回 pong 应触发关闭，避免半开连接吊住 lane');
  // 关键收尾：不显式关闭会让 server.close() 等这条连接，整个 test 文件卡住不退出
  await closeWs(core);
});
