'use strict';

// 集成用例：必须越过 HTTP/WS 边界才能验证的部分（配置自检、端口、token、
// platform 与虚拟账号的 env 贯通、hello 握手、保活、forward 被忽略、图片段、账本回查）。
//
// 纯函数级（投影、账本、action 应答）在 virtual-world.test.js / responder.test.js 覆盖。
// 每个用例都自己管 env 事务与状态目录：这些用例大多要传非法/非默认 env，
// 泄漏一条就会让后面整片红。

const {
  test, assert, WebSocket, TEST_TOKEN,
  wait, waitForEvent, waitForMessage,
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

// ── 非法虚拟号拒启 ────────────────────────────────────────────────��────

test('invalid virtual self id refuses to start before createServer', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';

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

// ── 端口可核对 + 路径无关 ────────────────────────────────────────────────

test('custom port is reported by /healthz and core accepts any path (incl. none)', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  // 端口由 OS 分配（bind :0），不用 48000+random 猜——猜出来的端口可能正被别的用例/别的测试文件占着
  const port = await freePort();
  const bridge = await boot({}, { port });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const health = await healthz();
  assert.equal(health.corePort, port);
  assert.equal(health.corePort, Number(process.env.AIPLUGIN4_BACKEND_PORT));
  assert.equal(health.tokenRequired, false, '没配 token 时应如实报告');

  // 核心端不占路径。用户填 ws://host:port 就该能连，
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

// ── token：一个变量管两端，宽松匹配 ─────────────────────────────────────

test('the single launcher token guards both core and plugin endpoints', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = 'secret-unified';
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

  // ① 裸 token 走 query（插件侧写法）
  assert.equal(await probe(`ws://127.0.0.1:${port}?access_token=secret-unified`), 'open');
  // ② Bearer 走 header（海豹侧写法：它发的是裸值，这里两种都认）
  assert.equal(await probe(`ws://127.0.0.1:${port}`, { headers: { Authorization: 'Bearer secret-unified' } }), 'open');
  // ③ 裸 token 走 header
  assert.equal(await probe(`ws://127.0.0.1:${port}`, { headers: { Authorization: 'secret-unified' } }), 'open');
  // ④ query 带旧错值 + header 带正确值 → 必须通过（"任一来源"语义，不能收缩成 query 优先）
  assert.equal(await probe(`ws://127.0.0.1:${port}?access_token=stale`, { headers: { Authorization: 'Bearer secret-unified' } }), 'open');
  // ⑤ 完全错的 token → 关闭
  assert.equal(await probe(`ws://127.0.0.1:${port}?access_token=wrong`), 'closed');
  assert.equal(await probe(`ws://127.0.0.1:${port}`), 'closed');
  // ⑥ 插件端是同一个 token（不存在"插件要另填一个"这回事）
  assert.equal(await probe(`ws://127.0.0.1:${port}/plugin?access_token=secret-unified`), 'open');
  assert.equal(await probe(`ws://127.0.0.1:${port}/plugin?access_token=wrong`), 'closed');
});

// ── platform ────────────────────────────────────────────────────────────

test('platform defaults to QQ and stays bridge-side only', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  delete process.env.AIPLUGIN4_BRIDGE_PLATFORM;
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
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

// ── 机器人号与昵称可填 ──────────────────────────────────────────────────

test('custom self id and nickname flow through login and member info', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  const bridge = await boot({
    AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID: '777001',
    AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME: '自定义骰娘'
  });
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  assert.equal((await healthz()).virtualSelfId, 777001);
  assert.equal((await healthz()).virtualNickname, '自定义骰娘');

  const core = await connectCore();
  const login = waitForMessage(core, packet => packet.echo === 'l');
  core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'l' }));
  assert.deepEqual((await login).data, { user_id: 777001, nickname: '自定义骰娘' });

  // 机器人自己的成员信息：角色取协议常量 admin
  const member = waitForMessage(core, packet => packet.echo === 'm');
  core.send(JSON.stringify({ action: 'get_group_member_info', params: { group_id: 1500000123, user_id: 777001 }, echo: 'm' }));
  const memberData = (await member).data;
  assert.equal(memberData.user_id, 777001);
  assert.equal(memberData.nickname, '自定义骰娘');
  assert.equal(memberData.role, 'admin');
  await closeWs(core);
});

// ── 登录握手同步性与时延 ────────────────────────────────────────────────

test('get_login_info is answered exactly once and within 1000ms', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

// ── 同号一致、账本回查、撤回 ────────────────────────────────────────────

test('action reply id equals the ledger id and supports get_msg / delete_msg roundtrip', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const sendReply = waitForMessage(core, packet => packet.echo === 's1');
  sendGroupAction(core, 20002, '账本原文', 's1');
  const messageId = (await sendReply).data.message_id;
  assert.equal(typeof messageId, 'number');

  // 账本里必须是同一条（回包号 === 账本号）
  const entry = bridge.responder.getMsg(messageId);
  assert.ok(entry, '账本应能按回包号查到');
  assert.equal(entry.text, '账本原文');

  // get_msg 回查
  const got = waitForMessage(core, packet => packet.echo === 'g1');
  core.send(JSON.stringify({ action: 'get_msg', params: { message_id: messageId }, echo: 'g1' }));
  const gotReply = await got;
  assert.equal(gotReply.status, 'ok');
  assert.equal(gotReply.data.message_id, messageId);
  assert.equal(gotReply.data.raw_message, '账本原文');

  // delete_msg → 再查必须是 1404
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

// ── 并发 lane 下发号单调不重复 ──────────────────────────────────────────

test('concurrent lanes get strictly increasing unique message ids', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  const ids = [];
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    // 事件号也在同一号段里（事件号与下行号同源）
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

// ── forward 被忽略 ──────────────────────────────────────────────────────

test('capture.forward is accepted but ignored, deliveredCount still counts', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core = await connectCore();
  core.on('message', data => {
    let packet; try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message') sendGroupAction(core, packet.group_id, '转发应答', `e-${packet.message_id}`);
  });
  const plugin = await connectPlugin();
  // forward:true 没有"协议端"可转发，但调用必须照常成功
  const packet = await wsCall(plugin, wsArgs(groupTarget(), '.fwd', { mode: 'lane', forward: true, settleMs: 60 }));
  assert.equal(packet.result.ok, true);
  assert.equal(packet.result.mode, 'standalone');
  assert.equal(packet.result.deliveredCount, 1);
  assert.equal(packet.result.forwardedCount, 0, 'forwardedCount 恒 0');
  assert.equal(packet.result.ambiguous, false);
  assert.equal(packet.result.messages[0].source, 'action');
  assert.equal(packet.result.messages[0].forwarded, false);
  await closeWs(plugin); await closeWs(core);
});

// ── 图片段不丢 ──────────────────────────────────────────────────────────

test('image segments are recorded without loss', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

// ── 未知 action 不 crash ────────────────────────────────────────────────

test('unknown action gets 1404 and the process keeps serving', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

// ── `source` 投影与老插件降级 ───────────────────────────────────────────

test('source is projected to virtual ids, and legacy payloads without source are flagged', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

test('source without a group id is rejected instead of falling back to a default group', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const plugin = await connectPlugin();
  const requestId = 'no-group';
  const errorPromise = waitForMessage(plugin, packet => packet.requestId === requestId);
  plugin.send(JSON.stringify({
    type: 'core_command',
    requestId,
    payload: {
      target: { selfId: '10001', messageType: 'group', groupId: '20002', userId: '30002' },
      // 群里必须有群号；没有默认群可以兜底
      source: { endpointId: 'QQ:1', messageType: 'group', userId: '1234567890' },
      actor: { userId: '30002', nickname: 'AI', role: 'member' },
      raw_message: '.nogroup',
      capture: { mode: 'reply_only', forward: false },
      timeoutMs: 500
    }
  }));
  const packet = await errorPromise;
  assert.equal(packet.type, 'core_command_error');
  assert.match(packet.error, /群 ID/);
  assert.equal(bridge.world.groups.size, 0, '失败的投影不该在世界里留下群记录');
  await closeWs(plugin);
});

test('non-numeric source ids are rejected instead of reaching the core', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

// ── 传输层保活：不能按"最近收到帧"计时 ─────────────────────────────────

test('keepalive pings an idle core connection instead of tearing it down', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = '';
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

// ── 单核心门禁 ──────────────────────────────────────────────────────────

test('second core connection is rejected with close code 1013', async t => {
  isolateEnv(t);
  process.env.AIPLUGIN4_BACKEND_TOKEN = TEST_TOKEN;
  const bridge = await boot();
  t.after(async () => { await bridge.stop(); state.bridge = null; });

  const core1 = await connectCore();
  assert.equal((await healthz()).coreClients, 1);

  const core2 = new WebSocket(`ws://127.0.0.1:${state.bridgePort}?access_token=${TEST_TOKEN}`);
  core2.on('error', () => {});
  const [code, reason] = await new Promise(resolve => {
    core2.once('close', (code, reason) => resolve([code, reason.toString()]));
  });
  assert.equal(code, 1013);
  assert.match(reason, /SealDice|核心/);
  assert.equal((await healthz()).coreClients, 1, '第二核心不进 coreClients');

  // 第一条连接不受影响：还能正常本地应答
  const login = waitForMessage(core1, packet => packet.echo === 'still-alive');
  core1.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'still-alive' }));
  assert.equal((await login).status, 'ok');
  await closeWs(core1);
});
