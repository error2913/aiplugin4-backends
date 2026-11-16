'use strict';

// 端到端冒烟：在同一个进程里跑一个真 Bridge，模拟"海豹核心 + 插件"两端，
// 验证完整通路 —— 插件报文 → 核心收到假事件 → 核心回 send_group_msg
// → Responder 本地应答并记账 → 该消息被投进 lane → 插件拿到结果。
//
// 与测试套件的分工：tests/ 里是窄断言（每个行为一条），这里是"三端一起跑通"的
// 单一场景，断言落在 deliveredCount + mode + /healthz 字段。

const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { WebSocket } = require('ws');
const assert = require('node:assert/strict');

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

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

function waitForMessage(ws, predicate, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off('message', onMessage);
      reject(new Error('timeout waiting for message'));
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

async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((resolve, reject) => {
    ws.once('open', resolve);
    ws.once('error', reject);
  });
  return ws;
}

async function main() {
  const port = await freePort();
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-sim-'));
  process.env.AIPLUGIN4_BACKEND_PORT = String(port);
  process.env.AIPLUGIN4_BACKEND_HOST = '127.0.0.1';
  process.env.AIPLUGIN4_BACKEND_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_STATE_DIR = stateDir;

  const { Bridge } = require('./bridge');
  const bridge = new Bridge();
  await bridge.start();

  try {
    const core = await connect(`ws://127.0.0.1:${port}?access_token=test-token`);
    const plugin = await connect(`ws://127.0.0.1:${port}/plugin?access_token=test-token`);

    // ① 插件先握手（老桥不认 hello 也没关系：这里断言的是新桥的行为）
    const hello = waitForMessage(plugin, packet => packet.type === 'hello_result');
    plugin.send(JSON.stringify({ type: 'hello', requestId: 'sim-hello' }));
    const helloResult = await hello;
    assert.equal(helloResult.payload.mode, 'standalone');
    assert.equal(helloResult.payload.corePort, port);
    assert.equal(helloResult.payload.virtualSelfId, 10001);

    // ② 核心连上后第一件事就是问登录信息，必须同步答出虚拟账号
    const login = waitForMessage(core, packet => packet.action === undefined && packet.echo === 'sim-login');
    core.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'sim-login' }));
    const loginReply = await login;
    assert.equal(loginReply.status, 'ok');
    assert.equal(loginReply.data.user_id, 10001);

    // ③ standalone 的核心不会主动推事件；它只会对假事件回 action。
    let atSeen = null;
    core.on('message', data => {
      let packet;
      try { packet = JSON.parse(data.toString()); } catch (_) { return; }
      if (packet.post_type === 'message' && packet.raw_message.endsWith('.ping')) {
        // REQ-D4-05：at 段必须真的写进假事件（CQ 码前缀 + message 段各一份）
        atSeen = {
          raw: packet.raw_message,
          segments: (packet.message || []).map(segment => segment.type)
        };
        core.send(JSON.stringify({
          action: 'send_group_msg',
          params: { group_id: packet.group_id, message: [{ type: 'text', data: { text: 'pong' } }] },
          echo: 'sim-echo'
        }));
      }
    });

    const requestId = 'sim_req_1';
    const resultPromise = waitForMessage(plugin, packet =>
      packet.type === 'core_command_result' && packet.requestId === requestId
    );

    plugin.send(JSON.stringify({
      type: 'core_command',
      requestId,
      payload: {
        target: { selfId: '10001', messageType: 'group', groupId: '20002', userId: '30002' },
        // 4.32.0 起插件会带 source（真实 ID）；桥据此投影成虚拟 ID。
        // 这里的真实 ID 故意与虚拟号段不重叠，用以验证"投影后确实是虚拟号"。
        source: { endpointId: 'QQ:1234567890', messageType: 'group', groupId: '987654321', userId: '1234567890', groupName: '真实群', userRole: 'admin' },
        actor: { userId: '30002', nickname: 'AI', role: 'admin' },
        raw_message: '.ping',
        at: ['30002'],
        capture: { mode: 'reply_only', forward: false, maxMessages: 10, settleMs: 30 },
        timeoutMs: 1000
      }
    }));

    const response = await resultPromise;
    assert.equal(response.result.ok, true);
    assert.equal(response.result.messages.length, 1);
    assert.equal(response.result.messages[0].text, 'pong');
    assert.equal(response.result.mode, 'standalone');
    assert.equal(response.result.ambiguous, false);
    assert.equal(response.result.deliveredCount, 1);

    // ③b at 段注入：核心收到的 raw_message 带 CQ:at 前缀、message 首段是 at。
    // `at: ['30002']` 里给的是**真实** ID，桥会先投影成虚拟号再写进假事件，
    // 所以这里断言的是"确实是个 2.0e9 段的虚拟用户号"，而不是原样回填 30002。
    assert.ok(atSeen, '核心应收到注入的假事件');
    const atMatch = /^\[CQ:at,qq=(\d+)\] \.ping$/.exec(atSeen.raw);
    assert.ok(atMatch, `raw_message 应是 CQ:at 前缀 + 原文，实际 ${atSeen.raw}`);
    const atVid = Number(atMatch[1]);
    assert.equal(Number.isSafeInteger(atVid), true);
    assert.ok(atVid >= 2000000000 && atVid < 2100000000, `at 应被投影到 2.0e9 段，实际 ${atVid}`);
    assert.deepEqual(atSeen.segments, ['at', 'text']);

    // ④ 指令里提到的虚拟 ID 全部是数字，且不带平台前缀（REQ-D2-15）；
    //    并且确实是投影后的虚拟号（真实群 987654321 → 1.5e9 段，真实用户 → 2.0e9 段）
    assert.equal(typeof response.result.virtualTarget.selfId, 'number');
    assert.equal(typeof response.result.virtualTarget.groupId, 'number');
    assert.equal(response.result.virtualTarget.selfId, 10001);
    assert.equal(response.result.virtualTarget.groupId >= 1500000000 && response.result.virtualTarget.groupId < 1600000000, true,
      `虚拟群号应落在 1.5e9 号段，实际 ${response.result.virtualTarget.groupId}`);

    // ⑤ /healthz 全字段口径
    const health = await fetch(`http://127.0.0.1:${port}/healthz`).then(r => r.json());
    assert.equal(Object.keys(health).length, 15);
    assert.equal(health.mode, 'standalone');
    assert.equal(health.corePort, port);
    assert.equal(health.tokenRequired, true);
    assert.equal(health.ledgerSize > 0, true);

    console.log('simulate-ws OK:', JSON.stringify({
      mode: health.mode,
      corePort: health.corePort,
      virtualSelfId: health.virtualSelfId,
      ledgerSize: health.ledgerSize,
      deliveredCount: response.result.deliveredCount
    }));

    plugin.close();
    core.close();
  } finally {
    await bridge.stop();
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
}

main().catch(error => {
  console.error('simulate-ws FAILED:', error);
  process.exitCode = 1;
});
