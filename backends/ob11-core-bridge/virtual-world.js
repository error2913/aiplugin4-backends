'use strict';

// 虚拟世界：桥作为"独立 OB11 协议端"时那个账号世界（群 / 用户 / 映射表）的唯一所有者。
//
// 设计约束：
//   - **纯模块**：不 require ws、不碰网络、不 require bridge.js（单向依赖的最底层）。
//   - **ID 稳定性**：同一个真实群/用户在任何一次重启后都必须映射到同一个虚拟 ID。
//     落盘在 `<stateDir>/virtual-world.json`，原子写（tmp + rename）。
//   - **ID 合法性**：所有对外 ID 必须是十进制正整数；`message_id` 还必须是 JS 安全整数。
//   - **禁段**：绝不能落在 QQ 机器人 UIN 段内，否则海豹的 isQQBotUserID 会把虚拟账号
//     当成机器人并走 bot 专属分支。
//
// **只有一项配置 ID**：`virtual_self_id`（机器人自己的号，`get_login_info.user_id` 与所有 self_id）。
// 群与用户**没有**"默认对象"：每一个虚拟群号/用户号都是真实 ID 的确定性映射，第一次遇到就
// 惰性建表。所以"未指定群号"的调用一律是错误调用，由 responder 报 1400 —— 虚拟世界里不存在
// 一个凭空存在的群或私聊对象。

const fs = require('node:fs');
const path = require('node:path');

const SCHEMA = 1;

// 三个 id 空间互不相交：群号段与用户号段不重叠，
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
// 机器人在群里的角色 / 虚拟群人数上限：不再作为配置项暴露（用户只需要填平台、号、昵称）。
//   - 角色 admin：海豹用它算 PrivilegeLevel；admin 足够跑 .dismiss 之外的绝大多数指令。
//   - 人数上限 200：必须恒 > 0，否则海豹把"0"当成机器人已退群并删会话
//     （`platform_adapter_onebot.go:478-483`）。
const SELF_ROLE = 'admin';
const MAX_MEMBER_COUNT = 200;
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
   * @param {string} opts.stateDir   状态目录（相对 cwd 或绝对路径）
   * @param {string} opts.platform   桥自称的平台名（仅展示用）
   * @param {string|number} opts.selfId 虚拟机器人号（原始配置值，可能是非法输入）
   * @param {string} opts.nickname   虚拟机器人昵称
   */
  constructor(opts = {}) {
    this.stateDir = opts.stateDir || '.state';
    this.platform = String(opts.platform || 'QQ');
    this.nickname = String(opts.nickname === undefined || opts.nickname === null ? 'AI骰娘' : opts.nickname).trim() || 'AI骰娘';

    // 原始配置值（可能是非法输入，validate() 负责挑出来）
    this.rawSelfId = opts.selfId;

    // 解析后的数字（validate() 成功后才可信）
    this.selfId = 0;

    this.schema = SCHEMA;
    this.lastMessageId = 0;
    this.groups = new Map();   // realGroupId(string) -> { realId, vid, name, left, members: Map }
    this.users = new Map();    // realUid(string) -> vid(number)
    this.realSelfId = '';
    this._byVid = new Map();   // vid(number) -> group record
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
   * `virtual_self_id` 四类非法值：① 空值 ② 非数字 ③ <= 0 ④ 命中机器人 UIN 段。
   * 通过时把数字写回实例字段，供后续投影使用。
   */
  validate() {
    const problems = [];
    const raw = this.rawSelfId;
    const label = '虚拟机器人号 virtual_self_id';
    const envName = 'AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID';
    const parsed = parsePositiveInt(raw);
    if (parsed.error === 'empty') {
      problems.push(`${label}（${envName}）为空值，必须填写正整数`);
    } else if (parsed.error === 'not-integer') {
      problems.push(`${label}（${envName}）不是整数：${String(raw)}`);
    } else if (parsed.error === 'not-positive') {
      problems.push(`${label}（${envName}）必须大于 0：${String(raw)}`);
    } else if (isForbiddenBotUin(parsed.value)) {
      problems.push(`${label}（${envName}）落在 QQ 机器人号段内，必须换一个：${parsed.value}`);
    } else {
      this.selfId = parsed.value;
    }
    return problems;
  }

  // ── 投影（真实 ID → 虚拟 ID）────────────────────────────────────────────

  _reservedVids() {
    return new Set([this.selfId].filter(Boolean));
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
   * 群号与用户号都是**必填**：虚拟世界里没有"默认群/默认私聊对象"可以兜底，
   * 缺 ID 就是残缺调用，必须报错而不是悄悄塞进一个凭空存在的对象。
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
    if (rawUser === undefined || rawUser === null || String(rawUser) === '') {
      throw new Error('投影来源缺少用户 ID（source.userId）');
    }
    // 先把所有入参校验完再动映射表：残缺调用不该留下半条映射（否则一次失败调用会污染世界）
    const realUser = VirtualWorld._requirePositive(rawUser, '投影来源的用户 ID');
    let realGroup = null;
    if (messageType === 'group') {
      if (source.groupId === undefined || source.groupId === null || String(source.groupId) === '') {
        throw new Error('投影来源缺少群 ID（source.groupId）');
      }
      realGroup = VirtualWorld._requirePositive(source.groupId, '投影来源的群 ID');
    }

    const userId = this.virtualUserId(realUser);
    let groupId;
    if (messageType === 'group') {
      groupId = this.virtualGroupId(realGroup);
      const record = this._byVid.get(groupId);
      if (record) {
        if (source.groupName) record.name = String(source.groupName);
        const member = record.members.get(String(realUser));
        const role = VALID_SELF_ROLES.has(String(source.userRole)) ? String(source.userRole) : 'member';
        if (member) {
          if (source.userRole) member.role = role;
        } else if (source.userRole) {
          record.members.set(String(realUser), { vid: userId, nickname: '', card: '', role });
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

  /** 惰性建群的默认群名（没有"用户填的默认群名"这回事）。 */
  static defaultGroupName(vid) {
    return `群${vid}`;
  }

  _ensureGroupByVid(vid, extra = {}) {
    const existing = this._byVid.get(vid);
    if (existing) {
      if (extra.name) existing.name = extra.name;
      return existing;
    }
    const record = {
      realId: String(vid),
      vid,
      name: extra.name || VirtualWorld.defaultGroupName(vid),
      left: false,
      members: new Map(),
      placeholder: extra.placeholder !== false
    };
    this.groups.set(record.realId, record);
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
    const record = this._ensureGroupByVid(Number(vid), { placeholder: true });
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
      group_name: record.name || VirtualWorld.defaultGroupName(record.vid),
      member_count: memberCount,
      max_member_count: Math.max(MAX_MEMBER_COUNT, memberCount),
      group_all_shut: 0,
      group_remark: ''
    };
  }

  /**
   * OB11 `get_group_member_info` 的 data。
   *
   * 三档：机器人自己 → 固定角色；已登记成员 → 映射表角色；未知 → `member`。
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
        role: SELF_ROLE,
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

  /** `set_group_leave`：置墓碑而不是删记录。 */
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
   * 单调递增的消息号。号段从 `Date.now()*1000` 起跳，
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
  SELF_ROLE,
  MAX_MEMBER_COUNT,
  LEDGER_DEFAULT_MAX
};
