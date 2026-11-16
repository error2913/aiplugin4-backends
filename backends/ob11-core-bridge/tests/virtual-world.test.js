'use strict';

// virtual-world.js 的纯模块测试（不碰 WS、不启 HTTP）。
//
// 这个模块是 standalone 的地基：真实 ID → 虚拟 ID 的映射必须**稳定、持久、可校验**。
// 一旦投影不稳定，海豹侧会认为"群号变了"，AI 的上下文与权限判定全部失效。
//
// 每个用例都用 mkdtemp 独立状态目录（REQ-D2-11）：
// node --test 并行跑多个文件，Windows 上跨进程 rename 有 EPERM 概率，
// 共享目录会让"保存/重载"用例互相打架。

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  VirtualWorld,
  hashToSegment,
  isForbiddenBotUin,
  GROUP_BASE,
  GROUP_SPAN,
  USER_BASE,
  USER_SPAN,
  VALID_SELF_ROLES
} = require('../virtual-world');

/**
 * 建一个"已通过自检"的世界。
 * 注意必须调 `validate()`：它是把原始配置解析成数字、并建出默认群/默认私聊对象的唯一入口；
 * 不调的话 selfId 还是 0，`_ensureDefaults()` 会直接返回，统计与成员断言全部对不上。
 * 需要测"非法配置"的用例自行传 bad 值并在断言后跳过该步。
 */
function makeWorld(overrides = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-vw-'));
  const world = new VirtualWorld({
    stateDir,
    selfId: '10001',
    nickname: 'AI骰娘',
    selfRole: 'admin',
    groupId: '20002',
    groupName: 'AI 测试群',
    peerId: '30002',
    maxMemberCount: 200,
    ...overrides
  }).load();
  world.validate();
  return { world, stateDir, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

/** 只要一个未 validate 的世界（测自检本身用）。 */
function makeRawWorld(overrides = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-vw-'));
  const world = new VirtualWorld({
    stateDir,
    selfId: '10001',
    nickname: 'AI骰娘',
    selfRole: 'admin',
    groupId: '20002',
    groupName: 'AI 测试群',
    peerId: '30002',
    maxMemberCount: 200,
    ...overrides
  }).load();
  return { world, stateDir, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

// ── 合法性校验（REQ-D2-05：5 类非法配置 + 2 类附属）──────────────────────

test('validate rejects empty and non-numeric ids', () => {
  const { world, cleanup } = makeRawWorld({ selfId: '', groupId: 'abc', peerId: '0' });
  try {
    const problems = world.validate();
    assert.ok(problems.length >= 3, `应至少报 3 条问题，实际 ${JSON.stringify(problems)}`);
    // 报错文案必须点名是哪个字段（用户要能直接改对），不得只说"配置非法"
    const joined = problems.join('\n');
    for (const field of ['self_id', 'group_id', 'peer_id']) {
      assert.match(joined, new RegExp(field), `问题清单应包含 ${field}`);
    }
  } finally { cleanup(); }
});

test('validate rejects the forbidden QQ bot UIN ranges', () => {
  // 这 5 段是 QQ 官方机器人号段：虚拟号撞进去会被海豹的 platform_adapter 判成"自己"，
  // 导致消息被当成自发而丢弃（见 sealdice-core/core/.../platform_adapter_qq_bot.go:14-18）
  const { world, cleanup } = makeRawWorld({ selfId: '3889000001' });
  try {
    const problems = world.validate();
    assert.equal(problems.length, 1);
    assert.match(problems[0], /self_id/);
    assert.match(problems[0], /bot|机器人|保留/i);
  } finally { cleanup(); }
});

test('validate rejects colliding ids across group and user spaces', () => {
  // 群号空间与用户空间必须互斥，否则 laneKey 会把"群 X 的消息"和"用户 X 的私聊"混成一条
  const { world, cleanup } = makeRawWorld({ groupId: '1500000005', peerId: '1500000005' });
  try {
    const problems = world.validate();
    assert.ok(problems.some(p => /重复|相同|冲突/.test(p)), `应报 ID 冲突，实际 ${JSON.stringify(problems)}`);
  } finally { cleanup(); }
});

test('validate rejects zero max_member_count and unknown self role', () => {
  const { world, cleanup } = makeRawWorld({ maxMemberCount: 0, selfRole: 'god' });
  try {
    const problems = world.validate();
    // 报错必须点名环境变量名，用户才知道去面板改哪一项
    assert.ok(problems.some(p => /MAX_MEMBER_COUNT/.test(p)), `应报 max_member_count，实际 ${JSON.stringify(problems)}`);
    assert.ok(problems.some(p => /SELF_ROLE/.test(p)), `应报 self_role，实际 ${JSON.stringify(problems)}`);
    assert.deepEqual([...VALID_SELF_ROLES].sort(), ['admin', 'member', 'owner']);
    // 题目要求"没填就用默认 200"，所以"完全不传 maxMemberCount"必须是合法的
    const { world: defaulted, cleanup: cleanup2 } = makeRawWorld({ maxMemberCount: undefined });
    try { assert.deepEqual(defaulted.validate(), []); } finally { cleanup2(); }
  } finally { cleanup(); }
});

test('validate accepts the default configuration and hydrates defaults', () => {
  const { world, cleanup } = makeWorld();
  try {
    assert.deepEqual(world.validate(), []);
    assert.equal(world.selfId, 10001);
    assert.equal(world.defaultGroupId, 20002);
    assert.equal(world.defaultPeerId, 30002);
    assert.equal(world.maxMemberCount, 200);
    // 默认群/默认私聊对象必须已经在表里，否则 get_group_info 会走"未登记→建空群"分支
    assert.ok(world.getGroupByVid(20002));
  } finally { cleanup(); }
});

// ── forbidden UIN 判定（5 段全覆盖）──────────────────────────────────────

test('isForbiddenBotUin covers all five official QQ bot ranges', () => {
  const forbidden = [3328144510, 66600000, 2854196301, 2854216399, 3889000000, 3889999999, 4010000000, 4019999999];
  const allowed = [10001, 20002, 2854196300, 2854216400, 3889999999 + 1, 4009999999, 4020000000];
  for (const uin of forbidden) assert.equal(isForbiddenBotUin(uin), true, `${uin} 应被判定为保留号段`);
  for (const uin of allowed) assert.equal(isForbiddenBotUin(uin), false, `${uin} 不应被判定为保留号段`);
});

// ── 哈希与投影稳定性 ─────────────────────────────────────────────────────

test('hashToSegment is deterministic and differs per kind', () => {
  // 注意：hashToSegment(kind, realId) 返回的是完整 32 位哈希，取模由 _allocate 负责。
  // 这里只断言"可重复"与"kind 参与哈希"，不对具体数值做假设。
  const pairs = [['group', '987654321'], ['group', '1234567890'], ['user', '1234567890'], ['user', '987654321']];
  for (const [kind, realId] of pairs) {
    const a = hashToSegment(kind, realId);
    const b = hashToSegment(kind, realId);
    assert.equal(a, b, `${kind}:${realId} 的哈希必须可重复`);
    assert.ok(Number.isInteger(a) && a >= 0 && a <= 0xffffffff, `${kind}:${realId} 应是 32 位无符号整数`);
  }
  // 群/用户必须先分空间（否则同一个真实号在群与私聊里会被算到同一偏移，虽然加 base 后仍不撞，
  // 但语义上不该混），所以 kind 必须参与哈希
  assert.notEqual(hashToSegment('group', '1234567890'), hashToSegment('user', '1234567890'));
  // 不同真实号的哈希要有分散性（排除"恒定返回 0"的实现）
  const distinct = new Set(pairs.map(([kind, realId]) => hashToSegment(kind, realId)));
  assert.ok(distinct.size >= 3, `哈希应分散，实际只有 ${distinct.size} 种`);
});

test('projection is stable across calls and across reloads', () => {
  const { world, stateDir, cleanup } = makeWorld();
  try {
    const realGroup = '987654321';
    const realUser = '1234567890';
    const first = world.project({ messageType: 'group', groupId: realGroup, userId: realUser, groupName: '真实群' });
    assert.equal(first.messageType, 'group');
    // 号段契约（REQ-D2-14）：群 1.5e9、用户 2.0e9
    assert.ok(first.groupId >= GROUP_BASE && first.groupId < GROUP_BASE + GROUP_SPAN, `群号应落在群段，实际 ${first.groupId}`);
    assert.ok(first.userId >= USER_BASE && first.userId < USER_BASE + USER_SPAN, `用户号应落在用户段，实际 ${first.userId}`);

    // 重复调用必须幂等
    const second = world.project({ messageType: 'group', groupId: realGroup, userId: realUser });
    assert.equal(second.groupId, first.groupId);
    assert.equal(second.userId, first.userId);

    world.save();
    // 换一个实例从同一目录重载：映射必须一模一样（跨重启稳定，REQ-D2-02/03）
    const reloaded = new VirtualWorld({ stateDir, selfId: '10001', groupId: '20002', peerId: '30002' }).load();
    reloaded.validate();
    const third = reloaded.project({ messageType: 'group', groupId: realGroup, userId: realUser });
    assert.equal(third.groupId, first.groupId);
    assert.equal(third.userId, first.userId);
    // 幂等也保持：再投影一次不生成新号
    const fourth = reloaded.project({ messageType: 'group', groupId: realGroup, userId: realUser });
    assert.equal(fourth.groupId, first.groupId);
    // 落盘再重载：users / groups 计数必须完全一致（不因 load 丢记录或多出记录）
    assert.deepEqual(reloaded.stats(), world.stats());
  } finally { cleanup(); }
});

test('private projection keeps the peer in the user space', () => {
  const { world, cleanup } = makeWorld();
  try {
    const projected = world.project({ messageType: 'private', userId: '1234567890' });
    assert.equal(projected.messageType, 'private');
    assert.ok(projected.userId >= USER_BASE && projected.userId < USER_BASE + USER_SPAN);
    // 私聊目标本身也必须能当陌生人查（核心会问 get_stranger_info）
    assert.equal(world.strangerInfo(projected.userId).user_id, projected.userId);
  } finally { cleanup(); }
});

// ── 墓碑语义（REQ-D3-09）────────────────────────────────────────────────

test('markGroupLeft leaves a tombstone that survives reload', () => {
  const { world, stateDir, cleanup } = makeWorld();
  try {
    const projected = world.project({ messageType: 'group', groupId: '555000111' });
    const vid = projected.groupId;
    world.markGroupLeft(vid);
    assert.equal(world.getGroupByVid(vid).left, true);
    // 墓碑不是删除：记录还在，所以"未登记自动建群"分支不会被误触发
    world.save();

    const reloaded = new VirtualWorld({ stateDir, selfId: '10001', groupId: '20002', peerId: '30002' }).load();
    reloaded.validate();
    const record = reloaded.getGroupByVid(vid);
    assert.ok(record, '墓碑记录应仍然存在');
    assert.equal(record.left, true, '墓碑标记应跨重启保留');
    // 号不再复用到别的群
    assert.equal(reloaded.project({ messageType: 'group', groupId: '555000111' }).groupId, vid);
  } finally { cleanup(); }
});

// ── 消息号（REQ-D5-01/05）───────────────────────────────────────────────

test('allocateMessageId is monotonic and stays a safe integer', () => {
  const { world, cleanup } = makeWorld();
  try {
    const ids = Array.from({ length: 50 }, () => world.allocateMessageId());
    for (let i = 1; i < ids.length; i++) assert.ok(ids[i] > ids[i - 1], `消息号必须严格递增：${ids[i - 1]} → ${ids[i]}`);
    for (const id of ids) {
      assert.equal(Number.isSafeInteger(id), true, `${id} 必须是 JS 安全整数`);
      assert.ok(id > 0, `${id} 必须是正整数`);
    }
    // 上界：Date.now()*1000 在 2255 年前不会溢出安全整数，这里只守住"不超 MAX_SAFE_INTEGER"
    assert.ok(ids[ids.length - 1] < Number.MAX_SAFE_INTEGER);
    // 跨实例（重载后）也不许回退：否则账本会串号。前提是落盘过（save 是显式调用）。
    world.save();
    const reloaded = new VirtualWorld({ stateDir: world.stateDir, selfId: '10001', groupId: '20002', peerId: '30002' }).load();
    const next = reloaded.allocateMessageId();
    assert.ok(next > ids[ids.length - 1], `重载后消息号必须继续递增：${ids[ids.length - 1]} → ${next}`);
  } finally { cleanup(); }
});

// ── 容错（REQ-D2-12）────────────────────────────────────────────────────

test('corrupt state file is backed up and the world recovers empty', () => {
  const { world, stateDir, cleanup } = makeWorld();
  try {
    world.project({ messageType: 'group', groupId: '777000888' });
    world.save();
    fs.writeFileSync(world.file, '{ this is not json', 'utf8');

    // 不抛错：坏档备份后按空表重建，桥必须还能启动
    const recovered = new VirtualWorld({ stateDir, selfId: '10001', groupId: '20002', peerId: '30002' }).load();
    assert.deepEqual(recovered.validate(), []);
    // 备份文件名是 `virtual-world.json.bad-<ts>`（_backupBad），不是 .bak
    const backups = fs.readdirSync(stateDir).filter(name => name.includes('.bad-'));
    assert.ok(backups.length >= 1, `坏档应被备份，目录内容：${fs.readdirSync(stateDir).join(',')}`);
    // 重建后默认群仍在，能正常应答
    assert.ok(recovered.getGroupByVid(20002));
    // 坏档里的旧映射不应复活（确实是"按空表重建"，不是"部分恢复"）
    assert.equal(recovered.getGroup('777000888'), null);
  } finally { cleanup(); }
});

// ── 展示与统计 ──────────────────────────────────────────────────────────

test('displayId formats ids the way Sealdice logs expect, and stats counts records', () => {
  const { world, cleanup } = makeWorld();
  try {
    // 展示口径跟海豹一致：用户 `QQ:123`、群 `QQ-Group:456`（REQ-D2-16）。
    // 注意 DiceMasters 要填的是 `QQ:<virtual_self_id>`（用户口径），不是群口径。
    assert.equal(world.displayId('user', 10001), 'QQ:10001');
    assert.equal(world.displayId('group', 20002), 'QQ-Group:20002');
    // platform 可自定义：换了平台名，展示口径跟着变，但 OB11 报文里的数字 ID 不受影响
    const { world: custom, cleanup: cleanup2 } = makeWorld({ platform: 'KOOK' });
    try {
      assert.equal(custom.displayId('user', 10001), 'KOOK:10001');
      assert.equal(custom.displayId('group', 20002), 'KOOK-Group:20002');
    } finally { cleanup2(); }
    world.project({ messageType: 'group', groupId: '111222333', userId: '444555666' });
    const stats = world.stats();
    assert.ok(stats.groups >= 2, `应有默认群 + 新投影群，实际 ${JSON.stringify(stats)}`);
    assert.ok(stats.users >= 3, `应有机器人自己 + 默认私聊对象 + 投影用户，实际 ${JSON.stringify(stats)}`);
  } finally { cleanup(); }
});
