'use strict';

// 虚拟世界：桥作为"独立 OB11 协议端"时那个账号/群/用户世界的唯一所有者。
//
// 设计约束（来自 00 REQ-D2-* / 01 §3.1）：
//   - **纯模块**：不 require ws、不碰网络、不 require bridge.js（单向依赖的最底层）。
//   - **ID 稳定性**：同一个真实群/用户在任何一次重启后都必须映射到同一个虚拟 ID。
//     落盘在 `<stateDir>/virtual-world.json`，原子写（tmp + rename）。
//   - **ID 合法性**：所有对外 ID 必须是十进制正整数；`message_id` 还必须是 JS 安全整数。
//   - **禁段**：绝不能落在 QQ 机器人 UIN 段内，否则海豹的 isQQBotUserID 会把虚拟账号
//     当成机器人并走 bot 专属分支。
//
// 三个配置 ID 的语义（易混，先讲清）：
//   virtual_self_id   —— 机器人自己的虚拟号，`get_login_info.user_id` 与所有 self_id。
//   virtual_group_id  —— **默认群**的虚拟号。桥启动时用它建一个占位群（real id 就是它自己），
//                         供"没有 source 的老插件"和"未指定群"的私聊兜底。
//   virtual_peer_id   —— **默认私聊对象**的虚拟号，同上兜底。
//   真实群/用户 → 虚拟 ID 走确定性 hash，并会避开上面三个保留值。

const fs = require('node:fs');
const path = require('node:path');

const SCHEMA = 1;

// 三个 id 空间互不相交（00 REQ-D2-08）：群号段与用户号段不重叠，
// 且都远离常见 QQ 号段，降低与真实号码撞车的观感。
const GROUP_BASE = 1500000000;
const GROUP_SPAN = 100000000;
const USER_BASE = 2000000000;
const USER_SPAN = 100000000;

// QQ 机器人 UIN 段（sealdice-core `platform_adapter_qq_bot.go:14-18`，共 5 段）
const FORBIDDEN_EXACT = new Set([3328144510, 66600000]);
const FORBIDDEN_RANGES = [
  [2854196301, 2854216399],
  [3889000000, 3889999999],
  [4010000000, 4019999999]
];

const VALID_SELF_ROLES = new Set(['owner', 'admin', 'member']);
const LEDGER_DEFAULT_MAX = 2000; // 账本上限在 responder 里，这里只管状态文件大小

const log = (level, message) => console[level](`[ob11-core-bridge] ${message}`);

/** 是否落在 QQ 机器人 UIN 段（命中即禁止用作虚拟账号）。 */
function isForbiddenBotUin(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return false;
  if (FORBIDDEN_EXACT.has(n)) return true;
  return FORBIDDEN_RANGES.some(([low, high]) => n >= low && n <= high);
}

/**
 * 确定性 hash：同一 (kind, realId) 永远得到同一个偏移量。
 * FNV-1a 32 位足够（SPAN 是 1e8，32 位余数分布够用），且实现只有几行、无依赖。
 */
function hashToSegment(kind, realId) {
  const text = `${kind}:${realId}`;
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash;
}

/** 解析"必须是十进制正整数"的配置项，返回 { value } 或 { error }。 */
function parsePositiveInt(raw) {
  const text = raw === undefined || raw === null ? '' : String(raw).trim();
  if (!text) return { error: 'empty' };
  if (!/^\d+$/.test(text)) return { error: 'not-integer' };
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value <= 0) return { error: 'not-positive' };
  return { value };
}

class VirtualWorld {
  /**
   * @param {object} opts
   * @param {string} opts.stateDir        状态目录（相对 cwd 或绝对路径）
   * @param {string} opts.platform        桥自称的平台名（仅展示用）
   * @param {string|number} opts.selfId   虚拟机器人号（原始配置值，可能是非法输入）
   * @param {string} opts.nickname        虚拟机器人昵称
   * @param {string} opts.selfRole        机器人在虚拟群里的角色
   * @param {string|number} opts.groupId  默认群虚拟号
   * @param {string} opts.groupName       默认群名
   * @param {string|number} opts.peerId   默认私聊对象虚拟号
   * @param {string|number} opts.maxMemberCount 虚拟群人数上限（必须 > 0）
   */
  constructor(opts = {}) {
    this.stateDir = opts.stateDir || '.state';
    this.platform = String(opts.platform || 'QQ');
    this.nickname = String(opts.nickname === undefined || opts.nickname === null ? 'AI骰娘' : opts.nickname).trim() || 'AI骰娘';
    this.selfRole = String(opts.selfRole === undefined || opts.selfRole === null ? 'admin' : opts.selfRole).trim().toLowerCase() || 'admin';
    this.groupName = String(opts.groupName === undefined || opts.groupName === null ? 'AI 测试群' : opts.groupName).trim() || 'AI 测试群';

    // 原始配置值（可能是非法输入，validate() 负责挑出来）
    this.rawSelfId = opts.selfId;
    this.rawGroupId = opts.groupId;
    this.rawPeerId = opts.peerId;
    // undefined ⇒ 用默认 200；显式传空字符串/maxMemberCount:'' 才算"用户填错"（validate 会报）
    this.rawMaxMemberCount = opts.maxMemberCount === undefined ? 200 : opts.maxMemberCount;

    // 解析后的数字（validate() 成功后才可信）
    this.selfId = 0;
    this.defaultGroupId = 0;
    this.defaultPeerId = 0;
    this.maxMemberCount = 0;

    this.schema = SCHEMA;
    this.lastMessageId = 0;
    this.groups = new Map();   // realGroupId(string) -> { realId, vid, name, left, members: Map }
    this.users = new Map();    // realUid(string) -> vid(number)
    this.realSelfId = '';
    this._byVid = new Map();   // vid(number) -> group record
    this._seen = null;         // require 时常量已定，这里只是占位
    this._saveCounter = 0;
    this._dirty = false;
  }

  // ── 路径与持久化 ─────────────────────────────────────────────────────────

  get file() {
    return path.join(this.stateDir, 'virtual-world.json');
  }

  /** 读状态文件。任何异常都不抛：坏文件备份后按空表启动。 */
  load() {
    try {
      fs.mkdirSync(this.stateDir, { recursive: true });
    } catch (e) {
      log('warn', `虚拟世界状态目录不可创建（${this.stateDir}）：${e.message}`);
    }
    let text;
    try {
      text = fs.readFileSync(this.file, 'utf8');
    } catch (_) {
      this.rebuild();
      return this;
    }
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      this._backupBad(`JSON 损坏：${e.message}`);
      this.rebuild();
      return this;
    }
    if (!data || typeof data !== 'object' || Number(data.schema) !== SCHEMA) {
      this._backupBad(`schema 不匹配：${data && data.schema}`);
      this.rebuild();
      return this;
    }
    try {
      this._hydrate(data);
    } catch (e) {
      this._backupBad(`结构异常：${e.message}`);
      this.rebuild();
    }
    return this;
  }

  _backupBad(reason) {
    const target = `${this.file}.bad-${Date.now()}`;
    try {
      fs.renameSync(this.file, target);
      log('warn', `虚拟世界状态不可用（${reason}），已备份到 ${target}，按空表启动`);
    } catch (e) {
      log('warn', `虚拟世界状态不可用（${reason}），且备份失败：${e.message}`);
    }
  }

  _hydrate(data) {
    this.lastMessageId = Number.isSafeInteger(data.lastMessageId) ? data.lastMessageId : 0;
    this.realSelfId = data.self && data.self.realSelfId ? String(data.self.realSelfId) : '';
    this.users = new Map();
    for (const [realId, vid] of Object.entries(data.users || {})) {
      const n = Number(vid);
      if (Number.isSafeInteger(n) && n > 0) this.users.set(String(realId), n);
    }
    this.groups = new Map();
    this._byVid = new Map();
    for (const [realId, raw] of Object.entries(data.groups || {})) {
      if (!raw || typeof raw !== 'object') continue;
      const vid = Number(raw.vid);
      if (!Number.isSafeInteger(vid) || vid <= 0) continue;
      const members = new Map();
      for (const [realUid, m] of Object.entries(raw.members || {})) {
        if (!m || typeof m !== 'object') continue;
        const mvid = Number(m.vid);
        if (!Number.isSafeInteger(mvid) || mvid <= 0) continue;
        members.set(String(realUid), {
          vid: mvid,
          nickname: String(m.nickname || ''),
          card: String(m.card || ''),
          role: VALID_SELF_ROLES.has(String(m.role)) ? String(m.role) : 'member'
        });
        if (!this.users.has(String(realUid))) this.users.set(String(realUid), mvid);
      }
      const record = {
        realId: String(realId),
        vid,
        name: String(raw.name || ''),
        left: !!raw.left,
        members
      };
      this.groups.set(record.realId, record);
      this._byVid.set(record.vid, record);
    }
  }

  rebuild() {
    this.lastMessageId = 0;
    this.realSelfId = '';
    this.users = new Map();
    this.groups = new Map();
    this._byVid = new Map();
    return this;
  }

  toJSON() {
    const groups = {};
    for (const [realId, g] of this.groups) {
      if (g.placeholder && g.members.size === 0) continue; // 占位群不落盘，重启后按需重建
      const members = {};
      for (const [realUid, m] of g.members) members[realUid] = { vid: m.vid, nickname: m.nickname, card: m.card, role: m.role };
      groups[realId] = { vid: g.vid, name: g.name, left: g.left, members };
    }
    const users = {};
    for (const [realUid, vid] of this.users) users[realUid] = vid;
    return {
      schema: SCHEMA,
      lastMessageId: this.lastMessageId,
      self: { realSelfId: this.realSelfId, vid: this.selfId },
      groups,
      users
    };
  }

  /** 原子写：先写唯一临时文件再 rename，避免读到半截 JSON。 */
  save() {
    const data = JSON.stringify(this.toJSON());
    let lastError = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const tmp = `${this.file}.${process.pid}.${this._saveCounter++}.tmp`;
      try {
        fs.mkdirSync(this.stateDir, { recursive: true });
        fs.writeFileSync(tmp, data, 'utf8');
        fs.renameSync(tmp, this.file);
        this._dirty = false;
        return true;
      } catch (e) {
        lastError = e;
        try { fs.rmSync(tmp, { force: true }); } catch (_) { /* ignore */ }
        // Windows 上跨进程 rename 争用会 EPERM：同步 sleep 几毫秒再试（不能用忙等，会白烧 CPU）
        try { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5 * (attempt + 1)); } catch (_) { /* ignore */ }
      }
    }
    // 落盘失败不应让进程崩：内存态仍然正确，只是重启后会丢这次变更
    log('warn', `虚拟世界状态写入失败（${lastError && lastError.message}），本次变更仅在内存中生效`);
    return false;
  }

  // ── 自检 ─────────────────────────────────────────────────────────────────

  /**
   * 启动自检：返回问题列表（空数组=通过）。
   *
   * 五类非法值（00 REQ-D2-17 显式定义）：① 空值 ② 非数字 ③ <=0 ④ 命中机器人 UIN 段
   * ⑤ 三个 ID 任两项撞号。外加两个独立项：人数上限必须 > 0、角色必须在三值内。
   * 通过时把数字写回实例字段，供后续投影使用。
   */
  validate() {
    const problems = [];
    const resolved = {};

    const check = (envName, label, raw) => {
      const parsed = parsePositiveInt(raw);
      if (parsed.error === 'empty') {
        problems.push(`${label}（${envName}）为空值，必须填写正整数`);
        return null;
      }
      if (parsed.error === 'not-integer') {
        problems.push(`${label}（${envName}）不是整数：${String(raw)}`);
        return null;
      }
      if (parsed.error === 'not-positive') {
        problems.push(`${label}（${envName}）必须大于 0：${String(raw)}`);
        return null;
      }
      if (isForbiddenBotUin(parsed.value)) {
        problems.push(`${label}（${envName}）落在 QQ 机器人号段内，必须换一个：${parsed.value}`);
        return null;
      }
      return parsed.value;
    };

    const selfId = check('AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID', '虚拟机器人号 virtual_self_id', this.rawSelfId);
    const groupId = check('AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID', '默认虚拟群号 virtual_group_id', this.rawGroupId);
    const peerId = check('AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID', '默认私聊对象号 virtual_peer_id', this.rawPeerId);

    const pairs = [
      ['虚拟机器人号 virtual_self_id', selfId, '默认虚拟群号 virtual_group_id', groupId],
      ['虚拟机器人号 virtual_self_id', selfId, '默认私聊对象号 virtual_peer_id', peerId],
      ['默认虚拟群号 virtual_group_id', groupId, '默认私聊对象号 virtual_peer_id', peerId]
    ];
    for (const [nameA, a, nameB, b] of pairs) {
      if (a && b && a === b) problems.push(`${nameA} 与 ${nameB} 撞号：${a}（三个虚拟 ID 必须互不相同）`);
    }

    const maxMember = parsePositiveInt(this.rawMaxMemberCount);
    if (maxMember.error === 'empty') problems.push('虚拟群人数上限（AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT）为空值');
    else if (maxMember.error) problems.push(`虚拟群人数上限（AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT）必须是正整数：${String(this.rawMaxMemberCount)}`);
    else if (maxMember.value === 0) problems.push('虚拟群人数上限不能为 0（海豹会判定机器人已退群）');
    else resolved.maxMemberCount = maxMember.value;

    if (!VALID_SELF_ROLES.has(this.selfRole)) {
      problems.push(`机器人在虚拟群里的角色（AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ROLE）只能是 owner/admin/member：${this.selfRole}`);
    }

    resolved.selfId = selfId;
    resolved.groupId = groupId;
    resolved.peerId = peerId;

    if (problems.length === 0) {
      this.selfId = selfId;
      this.defaultGroupId = groupId;
      this.defaultPeerId = peerId;
      this.maxMemberCount = resolved.maxMemberCount;
      this._ensureDefaults();
    }
    return problems;
  }

  /** 建好两个"默认"实体：默认群 + 默认私聊对象。 */
  _ensureDefaults() {
    if (!this.selfId) return;
    const group = this._ensureGroupByVid(this.defaultGroupId, { name: this.groupName, placeholder: true });
    if (!group.members.has(String(this.selfId))) {
      group.members.set(String(this.selfId), { vid: this.selfId, nickname: this.nickname, card: '', role: this.selfRole });
    }
    if (!group.members.has(String(this.defaultPeerId))) {
      group.members.set(String(this.defaultPeerId), { vid: this.defaultPeerId, nickname: '测试用户', card: '', role: 'member' });
    }
    // 成员同时也要登记进 users：`_hydrate()` 会把每个群成员都补进 users，
    // 若这里不补，同一个状态"存盘前 / 重载后"的 users 表会不一致（差一个 selfId），
    // 导致 stats 与陌生人查询在两个时点给出不同答案。
    if (!this.users.has(String(this.selfId))) this.users.set(String(this.selfId), this.selfId);
    if (!this.users.has(String(this.defaultPeerId))) this.users.set(String(this.defaultPeerId), this.defaultPeerId);
  }

  // ── 投影（真实 ID → 虚拟 ID）────────────────────────────────────────────

  _reservedVids() {
    return new Set([this.selfId, this.defaultGroupId, this.defaultPeerId].filter(Boolean));
  }

  _allocate(kind, realId) {
    const base = kind === 'group' ? GROUP_BASE : USER_BASE;
    const span = kind === 'group' ? GROUP_SPAN : USER_SPAN;
    const reserved = this._reservedVids();
    const taken = new Set(this._byVid.keys());
    for (const vid of this.users.values()) taken.add(vid);
    const start = hashToSegment(kind, realId) % span;
    for (let step = 0; step < span; step++) {
      const vid = base + ((start + step) % span);
      if (reserved.has(vid) || taken.has(vid)) continue;
      if (isForbiddenBotUin(vid)) continue;
      return vid;
    }
    throw new Error(`虚拟 ${kind === 'group' ? '群号' : '用户号'}号段已耗尽`);
  }

  /** 真实群号 → 虚拟群号（稳定、持久化）。 */
  virtualGroupId(realGroupId) {
    const realId = String(realGroupId);
    if (realId === String(this.defaultGroupId)) return this.defaultGroupId;
    const existing = this.groups.get(realId);
    if (existing) return existing.vid;
    const vid = this._allocate('group', realId);
    const record = { realId, vid, name: '', left: false, members: new Map(), placeholder: false };
    this.groups.set(realId, record);
    this._byVid.set(vid, record);
    this._dirty = true;
    log('info', `虚拟群映射新建：${realId} -> ${vid}`);
    return vid;
  }

  /** 真实用户号 → 虚拟用户号（稳定、持久化）。 */
  virtualUserId(realUserId) {
    const realId = String(realUserId);
    if (realId === String(this.defaultPeerId)) return this.defaultPeerId;
    if (this.selfId && realId === String(this.selfId)) return this.selfId;
    const existing = this.users.get(realId);
    if (existing) return existing;
    const vid = this._allocate('user', realId);
    this.users.set(realId, vid);
    this._dirty = true;
    return vid;
  }

  static _requirePositive(value, label) {
    const parsed = parsePositiveInt(value);
    if (parsed.error) throw new Error(`${label} 不是合法的正整数：${value === undefined || value === null ? '空' : String(value)}`);
    return parsed.value;
  }

  /**
   * 投影一次调用来源。
   *
   * @param {object} source {endpointId, messageType, groupId, userId, groupName, userRole}
   * @returns {{selfId:number, messageType:'group'|'private', groupId:number|undefined,
   *            userId:number, groupName:string, userRole:string}}
   */
  project(source = {}) {
    const messageType = source.messageType === 'private' ? 'private' : 'group';
    const rawUser = source.userId !== undefined && source.userId !== null && String(source.userId) !== ''
      ? source.userId
      : source.trigger;
    const hasRealUser = rawUser !== undefined && rawUser !== null && String(rawUser) !== '';
    // 真实用户号缺失时（例如只给了群号）不能报错：退化成默认私聊对象，
    // 但**不得**把它当成真实用户去登记成员（否则默认对象会被绑到任意真实号上）。
    const realUser = hasRealUser ? VirtualWorld._requirePositive(rawUser, '投影来源的用户 ID') : null;
    const userId = realUser === null ? this.defaultPeerId : this.virtualUserId(realUser);

    let groupId;
    if (messageType === 'group') {
      if (source.groupId === undefined || source.groupId === null || String(source.groupId) === '') {
        groupId = this.defaultGroupId;
      } else {
        groupId = this.virtualGroupId(VirtualWorld._requirePositive(source.groupId, '投影来源的群 ID'));
      }
      const record = this._byVid.get(groupId);
      if (record) {
        if (source.groupName) record.name = String(source.groupName);
        if (realUser !== null) {
          const member = record.members.get(String(realUser));
          const role = VALID_SELF_ROLES.has(String(source.userRole)) ? String(source.userRole) : 'member';
          if (member) {
            if (source.userRole) member.role = role;
          } else if (source.userRole) {
            record.members.set(String(realUser), { vid: userId, nickname: '', card: '', role });
          }
        }
      }
    }

    if (source.endpointId !== undefined && source.endpointId !== null) {
      const match = /(\d+)\s*$/.exec(String(source.endpointId));
      if (match && !this.realSelfId) this.realSelfId = match[1];
    }

    return {
      selfId: this.selfId,
      messageType,
      groupId,
      userId,
      groupName: source.groupName ? String(source.groupName) : '',
      userRole: VALID_SELF_ROLES.has(String(source.userRole)) ? String(source.userRole) : 'member'
    };
  }

  /** 展示用 ID：`QQ:123` / `QQ-Group:456`。只用于日志与 hello_result，绝不进 OB11 报文。 */
  displayId(kind, id) {
    return kind === 'group' ? `${this.platform}-Group:${id}` : `${this.platform}:${id}`;
  }

  // ── 群与成员 ─────────────────────────────────────────────────────────────

  getGroup(realGroupId) {
    return this.groups.get(String(realGroupId)) || null;
  }

  getGroupByVid(vid) {
    const n = Number(vid);
    if (!Number.isSafeInteger(n)) return null;
    return this._byVid.get(n) || null;
  }

  _ensureGroupByVid(vid, extra = {}) {
    const existing = this._byVid.get(vid);
    if (existing) {
      if (extra.name) existing.name = extra.name;
      return existing;
    }
    const realId = existing === null ? String(vid) : String(vid);
    const record = {
      realId,
      vid,
      name: extra.name || this.groupName,
      left: false,
      members: new Map(),
      placeholder: extra.placeholder !== false
    };
    this.groups.set(realId, record);
    this._byVid.set(vid, record);
    return record;
  }

  /**
   * 未登记群 → 建"空群"。`get_group_info` 的第三档（墓碑→活跃→新建）。
   * 注意：本方法接收**虚拟群号**（core 侧看到的就是虚拟号）。
   */
  ensureGroup(vid) {
    const existing = this.getGroupByVid(vid);
    if (existing) return existing;
    const record = this._ensureGroupByVid(Number(vid), { name: this.groupName, placeholder: true });
    this._dirty = true;
    return record;
  }

  /** 记录/更新一个虚拟群成员（来自 source 的真实昵称与角色）。 */
  recordMember(groupVid, opts = {}) {
    const record = this.getGroupByVid(groupVid);
    if (!record) return null;
    const realUid = opts.realUid !== undefined && opts.realUid !== null ? String(opts.realUid) : '';
    const vid = Number(opts.vid);
    if (!Number.isSafeInteger(vid) || vid <= 0) return null;
    const key = realUid || String(vid);
    const member = {
      vid,
      nickname: String(opts.nickname || ''),
      card: String(opts.card || ''),
      role: VALID_SELF_ROLES.has(String(opts.role)) ? String(opts.role) : 'member'
    };
    record.members.set(key, member);
    this._dirty = true;
    return member;
  }

  /**
   * OB11 `get_group_info` 的 data。
   * `max_member_count` 恒 > 0 —— 海豹把它当"机器人是否在群里"的判据
   * （`platform_adapter_onebot.go:478-483`，0 会直接被删除会话）。
   */
  groupInfo(recordOrVid) {
    const record = typeof recordOrVid === 'object' && recordOrVid !== null
      ? recordOrVid
      : this.getGroupByVid(recordOrVid);
    if (!record) return null;
    const memberCount = Math.max(record.members.size, 1);
    return {
      group_id: record.vid,
      group_name: record.name || this.groupName,
      member_count: memberCount,
      max_member_count: Math.max(this.maxMemberCount, memberCount),
      group_all_shut: 0,
      group_remark: ''
    };
  }

  /**
   * OB11 `get_group_member_info` 的 data。
   *
   * 三档（00 §3.5）：机器人自己 → 配置角色；已登记成员 → 映射表角色；未知 → `member`。
   * 未知**不报错**：报错会让 `checkBotGroupRole` 拿到 ok=false，调用方降级。
   */
  memberInfo(groupVid, userVid, opts = {}) {
    const uid = Number(userVid);
    const record = this.getGroupByVid(groupVid);
    const selfId = this.selfId;
    if (uid === selfId) {
      return {
        group_id: Number(groupVid),
        user_id: selfId,
        nickname: this.nickname,
        card: '',
        role: this.selfRole,
        sex: 'unknown',
        age: 0,
        area: '',
        join_time: 0,
        last_sent_time: 0,
        level: '',
        unfriendly: false,
        title: '',
        title_expire_time: 0,
        card_changeable: false,
        shut_up_timestamp: 0
      };
    }
    let member = null;
    if (record) {
      for (const [key, value] of record.members) {
        if (value.vid === uid) { member = value; break; }
        if (key === String(uid)) { member = value; break; }
      }
    }
    return {
      group_id: Number(groupVid),
      user_id: uid,
      nickname: member && member.nickname ? member.nickname : (opts.nickname || `用户${uid}`),
      card: member ? member.card : '',
      role: member ? member.role : 'member',
      sex: 'unknown',
      age: 0,
      area: '',
      join_time: 0,
      last_sent_time: 0,
      level: '',
      unfriendly: false,
      title: '',
      title_expire_time: 0,
      card_changeable: false,
      shut_up_timestamp: 0
    };
  }

  /** `set_group_leave`：置墓碑而不是删记录（00 REQ-D3-09）。 */
  markGroupLeft(groupVid) {
    const record = this.getGroupByVid(groupVid);
    if (!record) return null;
    record.left = true;
    record.placeholder = false;
    this._dirty = true;
    this.save();
    return record;
  }

  /** `get_stranger_info` 的 data。 */
  strangerInfo(userVid, opts = {}) {
    const uid = Number(userVid);
    let nickname = opts.nickname || '';
    if (!nickname) {
      for (const record of this.groups.values()) {
        for (const member of record.members.values()) {
          if (member.vid === uid && member.nickname) { nickname = member.nickname; break; }
        }
        if (nickname) break;
      }
    }
    if (!nickname) nickname = uid === this.selfId ? this.nickname : `用户${uid}`;
    return { user_id: uid, nickname, sex: 'unknown', age: 0 };
  }

  // ── 发号器（账本与回包共用）──────────────────────────────────────────────

  /**
   * 单调递增的消息号。号段从 `Date.now()*1000` 起跳（00 REQ-D5-05），
   * 保证重启后新号 > 历史账本里的旧号，并且在 `Number.MAX_SAFE_INTEGER` 以内。
   */
  allocateMessageId() {
    const nowBased = Date.now() * 1000;
    const next = Math.max(nowBased, this.lastMessageId + 1);
    if (!Number.isSafeInteger(next) || next >= Number.MAX_SAFE_INTEGER - 1000) {
      // 极端情况（号段用尽/时钟异常）：重置到当前时间基准，仍保证不重复
      this.lastMessageId = nowBased;
      return this.lastMessageId;
    }
    this.lastMessageId = next;
    this._dirty = true;
    return next;
  }

  stats() {
    let members = 0;
    let left = 0;
    for (const record of this.groups.values()) {
      members += record.members.size;
      if (record.left) left += 1;
    }
    return { groups: this.groups.size, users: this.users.size, members, leftGroups: left, lastMessageId: this.lastMessageId };
  }

  /** 供 /healthz 用：当前生效的虚拟号配置。 */
  selfInfo() {
    return {
      selfId: this.selfId,
      nickname: this.nickname,
      role: this.selfRole,
      defaultGroupId: this.defaultGroupId,
      defaultPeerId: this.defaultPeerId,
      maxMemberCount: this.maxMemberCount,
      platform: this.platform
    };
  }
}

module.exports = {
  VirtualWorld,
  hashToSegment,
  isForbiddenBotUin,
  SCHEMA,
  GROUP_BASE,
  GROUP_SPAN,
  USER_BASE,
  USER_SPAN,
  VALID_SELF_ROLES,
  LEDGER_DEFAULT_MAX
};
