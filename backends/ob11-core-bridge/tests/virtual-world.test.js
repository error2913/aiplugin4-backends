'use strict';

// virtual-world.js 的纯模块测试（不碰 WS、不启 HTTP）。
//
// 这个模块是桥的地基：真实 ID → 虚拟 ID 的映射必须**稳定、持久、可校验**。
// 一旦投影不稳定，海豹侧会认为"群号变了"，AI 的上下文与权限判定全部失效。
//
// 只有一项配置 ID：`virtual_self_id`。群与用户没有"默认对象"，
// 每个虚拟号都是真实 ID 的确定性映射，第一次遇到才建表。
//
// 每个用例都用 mkdtemp 独立状态目录：
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
  VALID_SELF_ROLES,
  SELF_ROLE,
  MAX_MEMBER_COUNT
} = require('../virtual-world');

/**
 * 建一个"已通过自检"的世界。
 * 注意必须调 `validate()`：它是把原始配置解析成数字的唯一入口；
 * 不调的话 selfId 还是 0，`get_login_info` 与所有 self_id 全是 0。
 */
function makeWorld(overrides = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ob11-vw-'));
  const world = new VirtualWorld({
    stateDir,
    selfId: '10001',
    nickname: 'AI骰娘',
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
    ...overrides
  }).load();
  return { world, stateDir, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

// ── 合法性校验（virtual_self_id 的 4 类非法值）──────────────────────────

test('validate rejects empty and non-numeric self id', () => {
  for (const [bad, pattern] of [['', /为空值/], ['abc', /不是整数/], ['0', /必须大于 0/]]) {
    const { world, cleanup } = makeRawWorld({ selfId: bad });
    try {
      const problems = world.validate();
      assert.equal(problems.length, 1, `应只报 1 条问题，实际 ${JSON.stringify(problems)}`);
      // 报错文案必须点名是哪个字段（用户要能直接改对），不得只说"配置非法"
      assert.match(problems[0], /self_id/);
      assert.match(problems[0], pattern);
      assert.equal(world.selfId, 0, '非法配置不得写回解析结果');
    } finally { cleanup(); }
  }
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

test('validate accepts the default configuration and hydrates only the bot itself', () => {
  const { world, cleanup } = makeWorld();
  try {
    assert.deepEqual(world.validate(), []);
    assert.equal(world.selfId, 10001);
    // 没有"默认群/默认私聊对象"：世界初始是空的，不凭空存在任何群或用户
    assert.equal(world.groups.size, 0, `不该凭空建群：${JSON.stringify([...world.groups.keys()])}`);
    assert.equal(world.users.size, 0, '不该凭空建用户');
    assert.deepEqual(world.stats(), { groups: 0, users: 0, members: 0, leftGroups: 0, lastMessageId: 0 });
  } finally { cleanup(); }
});

test('role and member limit are constants, not configuration', () => {
  // 用户只需要填平台/号/昵称；角色与人数上限是协议侧常量
  assert.equal(SELF_ROLE, 'admin');
  assert.equal(MAX_MEMBER_COUNT, 200);
  assert.ok(MAX_MEMBER_COUNT > 0, '人数上限必须 > 0，否则海豹判定机器人已退群');
  assert.deepEqual([...VALID_SELF_ROLES].sort(), ['admin', 'member', 'owner']);
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
    // 号段契约：群 1.5e9、用户 2.0e9
    assert.ok(first.groupId >= GROUP_BASE && first.groupId < GROUP_BASE + GROUP_SPAN, `群号应落在群段，实际 ${first.groupId}`);
    assert.ok(first.userId >= USER_BASE && first.userId < USER_BASE + USER_SPAN, `用户号应落在用户段，实际 ${first.userId}`);
    // 群名来自 source.groupName（没有"用户填的默认群名"）
    assert.equal(world.getGroupByVid(first.groupId).name, '真实群');

    // 重复调用必须幂等
    const second = world.project({ messageType: 'group', groupId: realGroup, userId: realUser });
    assert.equal(second.groupId, first.groupId);
    assert.equal(second.userId, first.userId);

    world.save();
    // 换一个实例从同一目录重载：映射必须一模一样（跨重启稳定）
    const reloaded = new VirtualWorld({ stateDir, selfId: '10001' }).load();
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
    assert.equal(projected.groupId, undefined, '私聊不该带群号');
    assert.ok(projected.userId >= USER_BASE && projected.userId < USER_BASE + USER_SPAN);
    // 私聊目标本身也必须能当陌生人查（核心会问 get_stranger_info）
    assert.equal(world.strangerInfo(projected.userId).user_id, projected.userId);
  } finally { cleanup(); }
});

test('projection without a group id or user id throws instead of falling back', () => {
  const { world, cleanup } = makeWorld();
  try {
    // 没有"默认群"可以兜底：缺群号就是残缺调用
    assert.throws(() => world.project({ messageType: 'group', userId: '1234567890' }), /群 ID/);
    // 没有"默认私聊对象"可以兜底：缺用户号同样是残缺调用
    assert.throws(() => world.project({ messageType: 'private' }), /用户 ID/);
    // 非正整数的真实 ID 也不能被悄悄接受
    assert.throws(() => world.project({ messageType: 'group', groupId: 'group-abc', userId: '1' }), /群 ID/);
    // 失败的投影不该在世界里留下任何记录
    assert.equal(world.groups.size, 0);
    assert.equal(world.users.size, 0);
  } finally { cleanup(); }
});

// ── 墓碑语义 ─────────────────────────────────────────────────────────────

test('markGroupLeft leaves a tombstone that survives reload', () => {
  const { world, stateDir, cleanup } = makeWorld();
  try {
    const projected = world.project({ messageType: 'group', groupId: '555000111', userId: '1234567890' });
    const vid = projected.groupId;
    world.markGroupLeft(vid);
    assert.equal(world.getGroupByVid(vid).left, true);
    // 墓碑不是删除：记录还在，所以"未登记自动建群"分支不会被误触发
    world.save();

    const reloaded = new VirtualWorld({ stateDir, selfId: '10001' }).load();
    reloaded.validate();
    const record = reloaded.getGroupByVid(vid);
    assert.ok(record, '墓碑记录应仍然存在');
    assert.equal(record.left, true, '墓碑标记应跨重启保留');
    // 号不再复用到别的群
    assert.equal(reloaded.project({ messageType: 'group', groupId: '555000111', userId: '1234567890' }).groupId, vid);
  } finally { cleanup(); }
});

// ── 消息号 ──────────────────────────────────────────────────────────────

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
    const reloaded = new VirtualWorld({ stateDir: world.stateDir, selfId: '10001' }).load();
    const next = reloaded.allocateMessageId();
    assert.ok(next > ids[ids.length - 1], `重载后消息号必须继续递增：${ids[ids.length - 1]} → ${next}`);
  } finally { cleanup(); }
});

// ── 容错 ────────────────────────────────────────────────────────────────

test('corrupt state file is backed up and the world recovers empty', () => {
  const { world, stateDir, cleanup } = makeWorld();
  try {
    world.project({ messageType: 'group', groupId: '777000888', userId: '1234567890' });
    world.save();
    fs.writeFileSync(world.file, '{ this is not json', 'utf8');

    // 不抛错：坏档备份后按空表重建，桥必须还能启动
    const recovered = new VirtualWorld({ stateDir, selfId: '10001' }).load();
    assert.deepEqual(recovered.validate(), []);
    // 备份文件名是 `virtual-world.json.bad-<ts>`（_backupBad），不是 .bak
    const backups = fs.readdirSync(stateDir).filter(name => name.includes('.bad-'));
    assert.ok(backups.length >= 1, `坏档应被备份，目录内容：${fs.readdirSync(stateDir).join(',')}`);
    // 坏档里的旧映射不应复活（确实是"按空表重建"，不是"部分恢复"）
    assert.equal(recovered.getGroup('777000888'), null);
    // 重建后仍能正常投影（惰性建表）
    const again = recovered.project({ messageType: 'group', groupId: '777000888', userId: '1234567890' });
    assert.ok(again.groupId >= GROUP_BASE);
  } finally { cleanup(); }
});

// ── 展示、群信息与统计 ──────────────────────────────────────────────────

test('displayId formats ids the way Sealdice logs expect, and stats counts records', () => {
  const { world, cleanup } = makeWorld();
  try {
    // 展示口径跟海豹一致：用户 `QQ:123`、群 `QQ-Group:456`。
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
    assert.equal(stats.groups, 1, `只该有投影出来的那 1 个群，实际 ${JSON.stringify(stats)}`);
    assert.ok(stats.users >= 1, `应至少有投影用户，实际 ${JSON.stringify(stats)}`);
  } finally { cleanup(); }
});

test('unregistered group gets a derived name and a positive member limit', () => {
  const { world, cleanup } = makeWorld();
  try {
    const vid = world.project({ messageType: 'group', groupId: '424242424', userId: '1234567890' }).groupId;
    // 群名由 source.groupName 决定；没给就用派生名（不存在"默认群名"配置）
    const info = world.groupInfo(world.getGroupByVid(vid));
    assert.equal(info.group_name, `群${vid}`);
    assert.ok(info.max_member_count > 0, 'max_member_count 恒 > 0，否则海豹会删会话');
    assert.equal(info.member_count >= 1, true);

    // 核心侧自己造一个没登记过的群号（get_group_info 第三档）→ 建空群成功
    const fresh = world.ensureGroup(1500000123);
    assert.equal(fresh.vid, 1500000123);
    assert.equal(world.groupInfo(fresh).group_name, '群1500000123');

    // 机器人自己的成员信息：角色取常量 admin，昵称取配置
    const self = world.memberInfo(vid, 10001);
    assert.equal(self.role, SELF_ROLE);
    assert.equal(self.nickname, 'AI骰娘');
    // 未知成员不报错（报错会让 checkBotGroupRole 拿到 ok=false 而降级）
    assert.equal(world.memberInfo(vid, 2999999999).role, 'member');
  } finally { cleanup(); }
});
