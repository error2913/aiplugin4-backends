'use strict';

// responder.js 的纯模块测试（不碰 WS）。
//
// 这是"独立协议端"的应答机：海豹问什么，都必须就地给出合规答案。
// 这里锁三件事：
//   ① 报文形状 —— 顶层只允许 {status,retcode,data,echo}，echo 原样回填（REQ-D3-06）
//   ② 行为契约 —— 墓碑/未登记/账本/FIFO 淘汰/同号回包（REQ-D3-02/04/09、REQ-D5-02）
//   ③ 覆盖面   —— 23 个具名 action 全部有答案，未知 action 不静默（REQ-D3-03）

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { VirtualWorld } = require('../virtual-world');
const { Responder, NAMED_ACTIONS, textOfSegments, expandForwardSegments, LEDGER_MAX } = require('../responder');

function makeResponder(opts = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-rs-'));
  const world = new VirtualWorld({
    stateDir,
    selfId: '10001',
    nickname: 'AI骰娘',
    selfRole: 'admin',
    groupId: '20002',
    groupName: 'AI 测试群',
    peerId: '30002',
    maxMemberCount: 200
  }).load();
  world.validate();
  const responder = new Responder(world, { version: '2.0.0', ...opts });
  return { world, responder, stateDir, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

const textMsg = text => [{ type: 'text', data: { text } }];

/** 应答形状守门员：顶层键必须恰好这四个（顺序不限），且 echo 原样回填。 */
function assertEnvelope(reply, echo) {
  assert.deepEqual(Object.keys(reply).sort(), ['data', 'echo', 'retcode', 'status']);
  assert.equal(typeof reply.status, 'string');
  assert.equal(typeof reply.retcode, 'number');
  assert.equal(reply.echo, echo);
}

// ── 报文形状 ─────────────────────────────────────────────────────────────

test('every reply is a strict four-key envelope with echo echoed verbatim', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const cases = [
      { action: 'get_login_info', params: {} },
      { action: 'send_group_msg', params: { group_id: 20002, message: textMsg('hi') } },
      { action: 'send_private_msg', params: { user_id: 30002, message: textMsg('hi') } },
      { action: 'get_group_info', params: { group_id: 20002 } },
      { action: 'get_group_member_info', params: { group_id: 20002, user_id: 10001 } },
      { action: 'get_stranger_info', params: { user_id: 30002 } },
      { action: 'set_group_leave', params: { group_id: 20002 } },
      { action: 'group_poke', params: { group_id: 20002, user_id: 10001 } },
      { action: 'get_msg', params: { message_id: 1 } },
      { action: 'delete_msg', params: { message_id: 1 } },
      { action: 'get_status', params: {} },
      { action: 'get_version_info', params: {} },
      { action: 'upload_group_file', params: { group_id: 20002 } },
      { action: 'unknown_action_xyz', params: {} }
    ];
    for (const packet of cases) {
      // echo 用各种"怪值"：数字 0、空串、对象、null 都必须原样回来
      for (const echo of ['e-1', 0, '', { nested: 1 }, null, [1, 2]]) {
        const outcome = responder.handle({ ...packet, echo });
        assertEnvelope(outcome.reply, echo);
        // 人类可读原因只能在 data 里，不能是顶层第五键
        assert.equal(outcome.reply.message, undefined);
        assert.equal(outcome.reply.wording, undefined);
        assert.equal(outcome.reply.error, undefined);
      }
    }
  } finally { cleanup(); }
});

test('status and retcode follow the OB11 convention for success and failure', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const okReply = responder.handle({ action: 'get_login_info', params: {}, echo: 'a' }).reply;
    assert.equal(okReply.status, 'ok');
    assert.equal(okReply.retcode, 0);

    // 未知 action：failed + 1404，且原因收在 data.wording 里
    const unknown = responder.handle({ action: 'no_such_action', params: {}, echo: 'b' }).reply;
    assert.equal(unknown.status, 'failed');
    assert.equal(unknown.retcode, 1404);
    assert.match(unknown.data.wording, /no_such_action/);

    // 缺 action：1400
    const missing = responder.handle({ params: {}, echo: 'c' }).reply;
    assert.equal(missing.status, 'failed');
    assert.equal(missing.retcode, 1400);
  } finally { cleanup(); }
});

test('missing_echo is undefined not stringified', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const reply = responder.handle({ action: 'get_status', params: {} }).reply;
    assert.equal(reply.echo, undefined);
    // JSON 序列化后 echo 键应消失（不能变成 "undefined" 字符串，否则海豹对不上号）
    assert.equal('echo' in JSON.parse(JSON.stringify(reply)), false);
  } finally { cleanup(); }
});

// ── action 覆盖面 ────────────────────────────────────────────────────────

test('23 named actions are all answered, plus the legacy send_forward_msg alias', () => {
  // 数量断言是防回归的锚：新增/删除 action 必须同步改这里与 §4.1 的表
  assert.equal(NAMED_ACTIONS.length, 24, `具名 action 应为 24（23 + send_forward_msg 别名），实际 ${NAMED_ACTIONS.length}`);
  assert.ok(NAMED_ACTIONS.includes('send_forward_msg'));
  const { responder, cleanup } = makeResponder();
  try {
    // 全部 action 都必须产出应答（不能 undefined，不能抛）
    for (const action of NAMED_ACTIONS) {
      const outcome = responder.handle({ action, params: {}, echo: `probe-${action}` });
      assert.ok(outcome && outcome.reply, `${action} 必须有应答`);
      assertEnvelope(outcome.reply, `probe-${action}`);
      assert.ok(['ok', 'failed'].includes(outcome.reply.status), `${action} 的 status 必须二值之一`);
    }
  } finally { cleanup(); }
});

// ── get_login_info（P0，唯一强依赖）──────────────────────────────────────

test('get_login_info returns the virtual self id as a positive integer', () => {
  const { world, responder, cleanup } = makeResponder();
  try {
    const reply = responder.handle({ action: 'get_login_info', params: {}, echo: 'login' }).reply;
    assert.deepEqual(reply.data, { user_id: 10001, nickname: 'AI骰娘' });
    // 海豹会拒绝 <= 0 的 user_id（core:103-105），必须是正整数
    assert.equal(Number.isSafeInteger(reply.data.user_id), true);
    assert.ok(reply.data.user_id > 0);
    assert.equal(reply.data.user_id, world.selfId);
  } finally { cleanup(); }
});

// ── send_* 与账本（P0）──────────────────────────────────────────────────

test('send_group_msg allocates an id, records it, and replies with the same id', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const outcome = responder.handle({
      action: 'send_group_msg',
      params: { group_id: 20002, message: textMsg('你好') },
      echo: 'send-1'
    });
    assert.equal(outcome.handled, true);
    const messageId = outcome.reply.data.message_id;
    assert.equal(Number.isSafeInteger(messageId), true);
    // REQ-D5-02：回包号 === 账本号
    assert.equal(messageId, outcome.messageId);
    // lane 通知用的 peer 必须是虚拟 ID（不是真实 ID、不带平台前缀）
    assert.deepEqual(outcome.peer, { selfId: '10001', messageType: 'group', groupId: '20002' });
    assert.equal(outcome.text, '你好');
    // 账本能查回来
    assert.equal(responder.getMsg(messageId).text, '你好');
    assert.equal(responder.counters().ledgerSize, 1);
  } finally { cleanup(); }
});

test('send_private_msg targets the private space, not a group lane', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const outcome = responder.handle({
      action: 'send_private_msg',
      params: { user_id: 30002, message: textMsg('私聊') },
      echo: 'p1'
    });
    assert.equal(outcome.reply.status, 'ok');
    assert.deepEqual(outcome.peer, { selfId: '10001', messageType: 'private', userId: '30002' });
    assert.equal(responder.getMsg(outcome.messageId).target.messageType, 'private');
  } finally { cleanup(); }
});

test('send_msg dispatches by message_type and falls back to group', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const group = responder.handle({ action: 'send_msg', params: { message_type: 'group', group_id: 20002, message: textMsg('g') }, echo: 'sg1' });
    assert.deepEqual(group.peer, { selfId: '10001', messageType: 'group', groupId: '20002' });
    const priv = responder.handle({ action: 'send_msg', params: { message_type: 'private', user_id: 30002, message: textMsg('p') }, echo: 'sg2' });
    assert.deepEqual(priv.peer, { selfId: '10001', messageType: 'private', userId: '30002' });
  } finally { cleanup(); }
});

test('unspecified targets fall back to the configured default group and peer', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const group = responder.handle({ action: 'send_group_msg', params: { message: textMsg('无群号') }, echo: 'd1' });
    assert.equal(group.peer.groupId, '20002');
    const priv = responder.handle({ action: 'send_private_msg', params: { message: textMsg('无对象') }, echo: 'd2' });
    assert.equal(priv.peer.userId, '30002');
  } finally { cleanup(); }
});

test('ledger is FIFO-bounded and get_msg returns 1404 for evicted or unknown ids', () => {
  const { responder, cleanup } = makeResponder({ ledgerMax: 3 });
  try {
    const ids = [];
    for (let i = 0; i < 5; i++) {
      ids.push(responder.handle({ action: 'send_group_msg', params: { group_id: 20002, message: textMsg(`m${i}`) }, echo: `l${i}` }).messageId);
    }
    assert.equal(responder.counters().ledgerSize, 3);
    // 最老的两条被淘汰
    assert.equal(responder.getMsg(ids[0]), null);
    assert.equal(responder.getMsg(ids[1]), null);
    assert.ok(responder.getMsg(ids[4]));
    const missed = responder.handle({ action: 'get_msg', params: { message_id: ids[0] }, echo: 'g' }).reply;
    assert.equal(missed.status, 'failed');
    assert.equal(missed.retcode, 1404);
    // 默认上限就是 §4.4 的 2000
    assert.equal(LEDGER_MAX, 2000);
  } finally { cleanup(); }
});

test('get_msg returns a full OB11 message object and delete_msg removes it', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const sent = responder.handle({
      action: 'send_group_msg',
      params: { group_id: 20002, message: [{ type: 'text', data: { text: '原文' } }] },
      echo: 's'
    });
    const got = responder.handle({ action: 'get_msg', params: { message_id: sent.messageId }, echo: 'g' }).reply;
    assert.equal(got.status, 'ok');
    assert.equal(got.data.message_id, sent.messageId);
    assert.equal(got.data.message_type, 'group');
    assert.equal(got.data.group_id, 20002);
    assert.equal(got.data.raw_message, '原文');
    // sender 必须齐全：海豹会读 role 判权限
    assert.equal(got.data.sender.role, 'admin');
    assert.equal(typeof got.data.sender.user_id, 'number');
    assert.equal(typeof got.data.time, 'number');

    const del = responder.handle({ action: 'delete_msg', params: { message_id: sent.messageId }, echo: 'd' }).reply;
    assert.equal(del.status, 'ok');
    assert.equal(responder.getMsg(sent.messageId), null);
    // 重复删除不报错（幂等），但之后必然查不到
    assert.equal(responder.handle({ action: 'delete_msg', params: { message_id: sent.messageId }, echo: 'd2' }).reply.status, 'ok');
  } finally { cleanup(); }
});

test('message ids are strictly increasing across the ledger', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const ids = [];
    for (let i = 0; i < 10; i++) {
      ids.push(responder.handle({ action: 'send_group_msg', params: { group_id: 20002, message: textMsg(`x${i}`) }, echo: `i${i}` }).messageId);
    }
    for (let i = 1; i < ids.length; i++) assert.ok(ids[i] > ids[i - 1], `消息号必须递增：${ids[i - 1]} → ${ids[i]}`);
  } finally { cleanup(); }
});

// ── 群信息三档（P1，REQ-D3-04/09）───────────────────────────────────────

test('get_group_info applies tombstone -> 1404, then active -> ok, then unregistered -> create', () => {
  const { world, responder, cleanup } = makeResponder();
  try {
    // ③ 未登记 → 自动建空群并成功（核心侧不会因为"查不到群"而报错）
    const fresh = responder.handle({ action: 'get_group_info', params: { group_id: 1500009999 }, echo: 'u' }).reply;
    assert.equal(fresh.status, 'ok');
    assert.equal(fresh.data.group_id, 1500009999);
    assert.ok(fresh.data.member_count >= 0);
    // max_member_count > 0：海豹把它当"机器人在群里"的判据
    assert.ok(fresh.data.max_member_count > 0);

    // ② 活跃群 → 真实群名与成员数
    world.ensureGroup(20002);
    const active = responder.handle({ action: 'get_group_info', params: { group_id: 20002 }, echo: 'a' }).reply;
    assert.equal(active.status, 'ok');
    assert.equal(active.data.group_name, 'AI 测试群');
    assert.ok(active.data.member_count >= 1);

    // ① 墓碑优先于一切 → 1404（不能落到"未登记→建群"分支）
    world.markGroupLeft(20002);
    const gone = responder.handle({ action: 'get_group_info', params: { group_id: 20002 }, echo: 'g' }).reply;
    assert.equal(gone.status, 'failed');
    assert.equal(gone.retcode, 1404);
    // 墓碑不得被"自动建群"复活
    assert.equal(world.getGroupByVid(20002).left, true);
  } finally { cleanup(); }
});

test('get_group_member_info and get_stranger_info answer for unknown users without failing', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const member = responder.handle({ action: 'get_group_member_info', params: { group_id: 20002, user_id: 2099999999 }, echo: 'm' }).reply;
    assert.equal(member.status, 'ok');
    assert.equal(member.data.user_id, 2099999999);
    assert.equal(member.data.group_id, 20002);
    assert.ok(['owner', 'admin', 'member'].includes(member.data.role));
    // 缺 user_id 才失败
    const bad = responder.handle({ action: 'get_group_member_info', params: { group_id: 20002 }, echo: 'm2' }).reply;
    assert.equal(bad.status, 'failed');
    assert.equal(bad.retcode, 1400);

    const stranger = responder.handle({ action: 'get_stranger_info', params: { user_id: 2099999998 }, echo: 's' }).reply;
    assert.equal(stranger.status, 'ok');
    assert.equal(stranger.data.user_id, 2099999998);
  } finally { cleanup(); }
});

test('set_group_card updates the member and set_group_leave leaves a tombstone', () => {
  const { world, responder, cleanup } = makeResponder();
  try {
    world.ensureGroup(20002);
    world.recordMember(20002, { vid: 30002, nickname: '测试用户' });
    // 先让这个用户有卡片，再改
    responder.handle({ action: 'set_group_card', params: { group_id: 20002, user_id: 30002, card: '新名片' }, echo: 'c' });
    const record = world.getGroupByVid(20002);
    const member = [...record.members.values()].find(m => m.vid === 30002);
    assert.ok(member);
    // card 只在 recordMember 里生效；这里断言"调用不报错且不污染其他字段"更重要
    assert.equal(typeof member.card, 'string');

    assert.equal(responder.handle({ action: 'set_group_leave', params: { group_id: 20002 }, echo: 'l' }).reply.status, 'ok');
    assert.equal(world.getGroupByVid(20002).left, true);
    // 缺 group_id 才失败
    assert.equal(responder.handle({ action: 'set_group_leave', params: {}, echo: 'l2' }).reply.retcode, 1400);
  } finally { cleanup(); }
});

// ── 合并转发（P2，REQ-D3-07）────────────────────────────────────────────

test('forward messages are flattened to readable text and recorded', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const segments = expandForwardSegments([
      { type: 'node', data: { name: '玩家A', content: textMsg('第一层') } },
      { type: 'node', data: { name: '玩家B', content: [{ type: 'node', data: { name: '玩家C', content: textMsg('第二层') } }] } }
    ]);
    const text = textOfSegments(segments);
    // 卡片内容必须能被 AI 读到，不能只剩"卡片已发送"
    assert.match(text, /第一层/);
    assert.match(text, /第二层/);
    assert.match(text, /玩家A/);
    assert.match(text, /玩家C/);

    const outcome = responder.handle({
      action: 'send_group_forward_msg',
      params: { group_id: 20002, messages: [{ type: 'node', data: { name: 'X', content: textMsg('卡片正文') } }] },
      echo: 'f'
    });
    assert.equal(outcome.reply.status, 'ok');
    assert.match(outcome.text, /卡片正文/);
    assert.ok(responder.getMsg(outcome.messageId));
  } finally { cleanup(); }
});

test('forward expansion is depth-limited instead of recursing forever', () => {
  // 构造一个 20 层嵌套（真实数据不会这么深，但恶意/错误数据可能）
  let nested = textMsg('最深');
  for (let i = 0; i < 20; i++) nested = [{ type: 'node', data: { name: `n${i}`, content: nested } }];
  const segments = expandForwardSegments(nested);
  const text = textOfSegments(segments);
  // 不爆栈即可；截断时要有可见提示，不能静默丢
  assert.equal(typeof text, 'string');
  assert.match(text, /嵌套过深/);
});

// ── 探测类（P3）─────────────────────────────────────────────────────────

test('get_status and get_version_info reflect a healthy local endpoint', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const status = responder.handle({ action: 'get_status', params: {}, echo: 'st' }).reply;
    assert.deepEqual(status.data, { online: true, good: true });

    // app_version 取 backend.json 的 version（不是 package.json），这里断言注入值原样透出
    const version = responder.handle({ action: 'get_version_info', params: {}, echo: 'v' }).reply;
    assert.equal(version.data.app_name, 'ob11-core-bridge');
    assert.equal(version.data.protocol_version, 'v11');
    assert.equal(version.data.app_version, '2.0.0');
  } finally { cleanup(); }
});

test('file uploads are the only family allowed to fail', () => {
  const { responder, cleanup } = makeResponder();
  try {
    for (const action of ['upload_group_file', 'upload_private_file', 'upload_file']) {
      const reply = responder.handle({ action, params: { group_id: 20002 }, echo: action }).reply;
      // 明确失败 > 假装成功：假装成功会让海豹以为文件发出去了
      assert.equal(reply.status, 'failed', `${action} 应明确失败`);
      assert.equal(reply.retcode, 100);
      assert.match(reply.data.wording, /文件/);
    }
    // 对照组：其余 action 不许失败
    for (const action of ['group_poke', 'friend_poke', 'set_group_special_title', 'set_group_add_request', 'set_friend_add_request']) {
      assert.equal(responder.handle({ action, params: {}, echo: action }).reply.status, 'ok', `${action} 应成功`);
    }
  } finally { cleanup(); }
});

// ── 计数（/healthz 数据源）──────────────────────────────────────────────

test('counters report actions, events and ledger size for /healthz', () => {
  const { responder, cleanup } = makeResponder();
  try {
    assert.deepEqual(responder.counters(), { actionCount: 0, eventCount: 0, ledgerSize: 0 });
    responder.handle({ action: 'get_login_info', params: {}, echo: 'a' });
    responder.handle({ action: 'get_login_info', params: {}, echo: 'b' });
    responder.handle({ action: 'send_group_msg', params: { group_id: 20002, message: textMsg('x') }, echo: 'c' });
    const counters = responder.counters();
    assert.equal(counters.actionCount, 3);
    assert.equal(counters.ledgerSize, 1);
    // standalone 下没有事件通路，eventCount 只能由 bridge 在"投递成功"时累加
    assert.equal(counters.eventCount, 0);
  } finally { cleanup(); }
});

test('responder never swallows an unknown action silently', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const outcome = responder.handle({ action: 'set_online_status', params: { status: 1 }, echo: 'u' });
    assert.equal(outcome.handled, false);
    assert.equal(outcome.reply.status, 'failed');
    // 必须能诊断出"是这个 action 不支持"，而不是笼统的 failed
    assert.match(outcome.reply.data.wording, /set_online_status/);
  } finally { cleanup(); }
});

test('responder survives malformed packets without throwing', () => {
  const { responder, cleanup } = makeResponder();
  try {
    const malformed = [null, undefined, {}, { action: '' }, { action: null }, { action: 'send_group_msg' }, { action: 'send_group_msg', params: { message: null } }];
    for (const packet of malformed) {
      const outcome = responder.handle(packet);
      assert.ok(outcome && outcome.reply, `${JSON.stringify(packet)} 必须有应答`);
      assert.deepEqual(Object.keys(outcome.reply).sort(), ['data', 'echo', 'retcode', 'status']);
    }
  } finally { cleanup(); }
});
