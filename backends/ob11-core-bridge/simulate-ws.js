'use strict';

const net = require('node:net');
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
  process.env.AIPLUGIN4_BACKEND_PORT = String(port);
  process.env.AIPLUGIN4_BACKEND_HOST = '127.0.0.1';
  process.env.AIPLUGIN4_BRIDGE_CORE_PATH = '/core';
  process.env.AIPLUGIN4_BRIDGE_CORE_TOKEN = 'test-token';
  process.env.AIPLUGIN4_BRIDGE_PLUGIN_TOKEN = 'test-token';
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_URL;
  delete process.env.AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN;

  const { Bridge } = require('./bridge');
  const bridge = new Bridge();
  await bridge.start();

  const core = await connect(`ws://127.0.0.1:${port}/core?access_token=test-token`);
  const plugin = await connect(`ws://127.0.0.1:${port}/plugin?access_token=test-token`);

  const requestId = 'sim_req_1';
  const resultPromise = waitForMessage(plugin, packet =>
    packet.type === 'core_command_result' && packet.requestId === requestId
  );

  core.on('message', data => {
    let packet;
    try { packet = JSON.parse(data.toString()); } catch (_) { return; }
    if (packet.post_type === 'message' && packet.raw_message === '.ping') {
      core.send(JSON.stringify({
        action: 'send_group_msg',
        params: { group_id: packet.group_id, message: [{ type: 'text', data: { text: 'pong' } }] },
        echo: 'sim-echo'
      }));
    }
  });

  plugin.send(JSON.stringify({
    type: 'core_command',
    requestId,
    payload: {
      target: { selfId: '10001', messageType: 'group', groupId: '20002', userId: '30002' },
      actor: { userId: '30002', nickname: 'AI', role: 'member' },
      raw_message: '.ping',
      capture: { mode: 'reply_only', forward: false, maxMessages: 10, settleMs: 30 },
      timeoutMs: 1000
    }
  }));

  const response = await resultPromise;
  assert.equal(response.result.ok, true);
  assert.equal(response.result.messages.length, 1);
  assert.equal(response.result.messages[0].text, 'pong');
  assert.equal(response.result.interceptedCount, 1);
  console.log('simulate-ws OK:', JSON.stringify(response.result));

  plugin.close();
  core.close();
  await bridge.stop();
}

main().catch(error => {
  console.error('simulate-ws FAILED:', error);
  process.exitCode = 1;
});
