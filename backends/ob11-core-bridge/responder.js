'use strict';

// action 本地应答 + 消息账本。
//
// 这个模块就是"独立协议端"的应答机：海豹把 OB11 action 发过来，这里就地算出答案，
// 不再转发给任何真实协议端。它也是 `send_*` 的受理方，所以账本（`get_msg` 的数据源）
// 自然长在这里。
//
// 依赖方向（01 §3.3，单向、无环）：
//     bridge.js  ──require──>  responder.js  ──require──>  virtual-world.js
//
// 铁律：本文件**不出现** `Bridge` / `Invocation` / `bridge.js` 字样，也不 require `ws`。
// `handle()` 只把结论**返回**给调用方，由 bridge.js 去发报文、去通知 lane 上的 Invocation。
//
// 应答结构铁律（00 REQ-D3-06）：顶层只允许 `{status, retcode, data, echo}` 四个键，
// `echo` 必须原样回填；人类可读原因一律收在 `data` 里，不开顶层第五键。

const {
  VirtualWorld,
  SELF_ROLE
} = require('./virtual-world');

const LEDGER_MAX = 2000;
const MAX_MESSAGES = 50;

/** 应答封包：四字段，顺序固定（断言用 Object.keys(...).sort() 比对）。 */
function ok(data, echo) {
  return { status: 'ok', retcode: 0, data: data === undefined ? null : data, echo };
}
function fail(retcode, wording, echo) {
  // wording 收在 data 里（REQ-D3-06）——不是顶层第五键
  return { status: 'failed', retcode, data: wording === undefined ? null : { wording }, echo };
}

function readEcho(packet) {
  return packet && packet.echo !== undefined ? packet.echo : undefined;
}

function asNumberId(value) {
  const n = Number(value);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

function textOfSegments(segments) {
  if (!Array.isArray(segments)) {
    if (typeof segments === 'string') return segments;
    return '';
  }
  return segments.map(segment => {
    if (!segment || typeof segment !== 'object') return '';
    if (segment.type === 'text') return String((segment.data && segment.data.text) || '');
    if (segment.type === 'file') return `[文件:${String((segment.data && (segment.data.name || segment.data.file)) || '未知')}]`;
    if (segment.type === 'image') return '[图片]';
    if (segment.type === 'face') return '[表情]';
    if (segment.type === 'at') return `@${String((segment.data && segment.data.qq) || '')}`;
    if (segment.type === 'reply') return '';
    return `[${segment.type || 'segment'}]`;
  }).join('');
}

/**
 * 合并转发：`messages[].data.content` 里嵌套着节点数组，逐层展开成可读文本。
 * 必须展开，否则 AI 收到"卡片已发送"却拿不到任何卡片内容（00 REQ-D3-07）。
 */
function expandForwardSegments(segments, depth = 0) {
  if (!Array.isArray(segments)) return [];
  if (depth > 8) return [{ type: 'text', data: { text: '[合并转发嵌套过深]' } }];
  const out = [];
  for (const segment of segments) {
    if (!segment || typeof segment !== 'object') continue;
    if (segment.type === 'node') {
      const data = segment.data || {};
      const content = data.content !== undefined ? data.content : data.message;
      const header = data.name || data.nickname || data.uin || data.user_id;
      if (header !== undefined) out.push({ type: 'text', data: { text: `[${header}]` } });
      out.push(...expandForwardSegments(content, depth + 1));
      continue;
    }
    out.push(segment);
  }
  return out;
}

/** 从任意 message 里取出段数组（字符串按单段 text 处理）。 */
function toSegments(message) {
  if (Array.isArray(message)) return message;
  if (typeof message === 'string') return [{ type: 'text', data: { text: message } }];
  if (message && typeof message === 'object' && Array.isArray(message.message)) return message.message;
  return [];
}

class Responder {
  /**
   * @param {VirtualWorld} world
   * @param {object} [opts]
   * @param {string} [opts.version] app_version（取 backend.json 的 version，不是 package.json）
   * @param {number} [opts.ledgerMax]
   */
  constructor(world, opts = {}) {
    if (!world || typeof world.allocateMessageId !== 'function') {
      throw new Error('Responder 需要一个 VirtualWorld 实例');
    }
    this.world = world;
    this.version = String(opts.version || '2.0.0');
    this.ledgerMax = Math.max(1, Number(opts.ledgerMax) || LEDGER_MAX);
    this.ledger = [];
    this.ledgerById = new Map();
    this.actionCounts = new Map();
    this.eventCount = 0;
    this.droppedEvents = 0;
  }

  // ── 账本 ───────────────────────────────────────────────────────────────

  _remember(entry) {
    this.ledger.push(entry);
    this.ledgerById.set(entry.messageId, entry);
    while (this.ledger.length > this.ledgerMax) {
      const evicted = this.ledger.shift();
      if (evicted && this.ledgerById.get(evicted.messageId) === evicted) this.ledgerById.delete(evicted.messageId);
    }
    return entry;
  }

  /** `get_msg` 的查询入口；未命中返回 null（调用方转 1404）。 */
  getMsg(messageId) {
    const id = asNumberId(messageId);
    if (id === null) return null;
    return this.ledgerById.get(id) || null;
  }

  /** `delete_msg`：从账本摘掉，之后 `get_msg` 应当查无。 */
  deleteMsg(messageId) {
    const id = asNumberId(messageId);
    if (id === null) return false;
    const entry = this.ledgerById.get(id);
    if (!entry) return false;
    this.ledgerById.delete(id);
    const index = this.ledger.indexOf(entry);
    if (index >= 0) this.ledger.splice(index, 1);
    return true;
  }

  /** 记录一次 send_*：发号 → 记账 → 返回同号（REQ-D5-02：回包与账本必须同号）。 */
  _recordSend(action, params, opts = {}) {
    const messageType = opts.messageType || 'group';
    const groupId = opts.groupId;
    const userId = opts.userId;
    const segments = opts.segments || toSegments(params && params.message);
    const messageId = this.world.allocateMessageId();
    const entry = {
      messageId,
      action,
      target: {
        messageType,
        ...(messageType === 'group' ? { groupId } : { userId })
      },
      segments,
      text: opts.text !== undefined ? String(opts.text) : textOfSegments(segments),
      time: Math.floor(Date.now() / 1000),
      ...(opts.invocationId ? { invocationId: opts.invocationId } : {})
    };
    this._remember(entry);
    return entry;
  }

  /** 供给 bridge.js 做 lane 通知的目标（虚拟 ID，不是真实 ID）。 */
  _peerFor(entry) {
    const target = entry.target || {};
    return {
      selfId: String(this.world.selfId),
      messageType: target.messageType,
      ...(target.messageType === 'group' ? { groupId: String(target.groupId) } : { userId: String(target.userId) })
    };
  }

  _result(reply, entry) {
    if (!entry) return { handled: true, reply };
    return {
      handled: true,
      reply,
      messageId: entry.messageId,
      segments: entry.segments,
      text: entry.text,
      peer: this._peerFor(entry)
    };
  }

  /**
   * 处理一个 action 包。
   *
   * @param {object} packet 形如 `{action, params, echo}`（也接受 `data` 作为 params 别名）
   * @returns {{handled: true, reply: object, messageId?: number, segments?: Array,
   *            text?: string, peer?: object}}
   */
  handle(packet) {
    const action = packet && packet.action ? String(packet.action) : '';
    const params = (packet && (packet.params || packet.data)) || {};
    const echo = readEcho(packet);
    if (!action) return { handled: false, reply: fail(1400, '缺少 action', echo) };

    this.actionCounts.set(action, (this.actionCounts.get(action) || 0) + 1);
    const handler = HANDLERS[action];
    if (!handler) {
      // 未知 action 保留可诊断性（REQ-D3-05/§4.1 末行）：不静默吞、不 crash
      return { handled: false, reply: fail(1404, `standalone 模式不支持该 action：${action}`, echo) };
    }
    return handler(this, params, echo);
  }

  /** /healthz 需要的三个计数。 */
  counters() {
    let actions = 0;
    for (const value of this.actionCounts.values()) actions += value;
    return { actionCount: actions, eventCount: this.eventCount, ledgerSize: this.ledger.length };
  }
}

// ── 各 action 的应答实现 ──────────────────────────────────────────────────
//
// 分档与 00 §4.3 一致：P0 = 生产链路必需（3 个）；P1 = 强烈建议（5 个）；
// P2 = 需应答（戳/合并转发/请求处理）；P3 = 结构级应答即可（面板与版本探测）。

const HANDLERS = {
  // ── P0 ──────────────────────────────────────────────────────────────────
  get_login_info(responder, params, echo) {
    const world = responder.world;
    // 唯一强依赖：必须同步回正整数 user_id（海豹 :103-105 拒绝 <=0）
    return { handled: true, reply: ok({ user_id: world.selfId, nickname: world.nickname }, echo) };
  },

  send_group_msg(responder, params, echo) {
    const world = responder.world;
    // group_id 是**虚拟**群号（核心侧只知道虚拟号），必填：
    // 虚拟世界里没有"默认群"这种东西，缺号就是残缺调用（REQ-D3-10）
    const groupId = asNumberId(params.group_id);
    if (groupId === null) return { handled: false, reply: fail(1400, 'send_group_msg 需要正整数 group_id', echo) };
    const segments = toSegments(params.message);
    // 自动补成员/群信息：核心侧的群名与昵称都来自这里
    const record = world.ensureGroup(groupId);
    if (!record.left) {
      if (params.group_name && !record.name) record.name = String(params.group_name);
    }
    const entry = responder._recordSend('send_group_msg', params, { messageType: 'group', groupId, segments });
    return responder._result(ok({ message_id: entry.messageId }, echo), entry);
  },

  send_private_msg(responder, params, echo) {
    const world = responder.world;
    const userId = asNumberId(params.user_id);
    if (userId === null) return { handled: false, reply: fail(1400, 'send_private_msg 需要正整数 user_id', echo) };
    const segments = toSegments(params.message);
    const entry = responder._recordSend('send_private_msg', params, { messageType: 'private', userId, segments });
    return responder._result(ok({ message_id: entry.messageId }, echo), entry);
  },

  // send_msg 按 message_type 分派（历史遗留名字，插件侧可能直接发）
  send_msg(responder, params, echo) {
    const messageType = params.message_type === 'private' || (params.user_id !== undefined && params.group_id === undefined)
      ? 'private'
      : 'group';
    if (messageType === 'private') return HANDLERS.send_private_msg(responder, params, echo);
    return HANDLERS.send_group_msg(responder, params, echo);
  },

  // ── P1 ──────────────────────────────────────────────────────────────────
  get_group_info(responder, params, echo) {
    const world = responder.world;
    const groupId = asNumberId(params.group_id);
    if (groupId === null) return { handled: false, reply: fail(1400, 'get_group_info 需要正整数 group_id', echo) };
    // 判定顺序固定（REQ-D3-04）：① 墓碑 → 1404 ② 活跃 → 成功 ③ 未登记 → 建空群成功
    const record = world.getGroupByVid(groupId);
    if (record && record.left) {
      return { handled: false, reply: fail(1404, 'standalone 模式：虚拟群已退群', echo) };
    }
    const active = record || world.ensureGroup(groupId);
    return { handled: false, reply: ok(world.groupInfo(active), echo) };
  },

  get_group_member_info(responder, params, echo) {
    const world = responder.world;
    const groupId = asNumberId(params.group_id);
    const userId = asNumberId(params.user_id);
    if (groupId === null) return { handled: false, reply: fail(1400, 'get_group_member_info 需要正整数 group_id', echo) };
    if (userId === null) return { handled: false, reply: fail(1400, 'get_group_member_info 需要正整数 user_id', echo) };
    if (!world.getGroupByVid(groupId)) world.ensureGroup(groupId);
    return { handled: false, reply: ok(world.memberInfo(groupId, userId, { nickname: params.nickname }), echo) };
  },

  get_stranger_info(responder, params, echo) {
    const world = responder.world;
    const userId = asNumberId(params.user_id);
    if (userId === null) return { handled: false, reply: fail(1400, 'get_stranger_info 需要正整数 user_id', echo) };
    return { handled: false, reply: ok(world.strangerInfo(userId, { nickname: params.nickname }), echo) };
  },

  set_group_card(responder, params, echo) {
    const world = responder.world;
    const groupId = asNumberId(params.group_id);
    const userId = asNumberId(params.user_id);
    if (groupId !== null && userId !== null) {
      world.recordMember(groupId, { vid: userId, card: String(params.card || ''), role: undefined });
      world.save();
    }
    return { handled: false, reply: ok(null, echo) };
  },

  set_group_leave(responder, params, echo) {
    const world = responder.world;
    const groupId = asNumberId(params.group_id);
    if (groupId === null) return { handled: false, reply: fail(1400, 'set_group_leave 需要正整数 group_id', echo) };
    // 置墓碑而不是删记录（REQ-D3-09）：删了之后"未登记→自动建群"会让核心误以为还在群里
    world.markGroupLeft(groupId);
    return { handled: false, reply: ok(null, echo) };
  },

  // ── P2 ──────────────────────────────────────────────────────────────────
  group_poke(responder, params, echo) {
    return { handled: false, reply: ok(null, echo) };
  },

  friend_poke(responder, params, echo) {
    return { handled: false, reply: ok(null, echo) };
  },

  send_group_forward_msg(responder, params, echo) {
    const world = responder.world;
    const groupId = asNumberId(params.group_id);
    if (groupId === null) return { handled: false, reply: fail(1400, 'send_group_forward_msg 需要正整数 group_id', echo) };
    const segments = expandForwardSegments(toSegments(params.messages !== undefined ? params.messages : params.message));
    const entry = responder._recordSend('send_group_forward_msg', params, {
      messageType: 'group', groupId, segments, text: textOfSegments(segments)
    });
    return responder._result(ok({ message_id: entry.messageId }, echo), entry);
  },

  send_private_forward_msg(responder, params, echo) {
    const world = responder.world;
    const userId = asNumberId(params.user_id);
    if (userId === null) return { handled: false, reply: fail(1400, 'send_private_forward_msg 需要正整数 user_id', echo) };
    const segments = expandForwardSegments(toSegments(params.messages !== undefined ? params.messages : params.message));
    const entry = responder._recordSend('send_private_forward_msg', params, {
      messageType: 'private', userId, segments, text: textOfSegments(segments)
    });
    return responder._result(ok({ message_id: entry.messageId }, echo), entry);
  },

  // 历史遗留名（go-cqhttp 时代），归一到合并转发
  send_forward_msg(responder, params, echo) {
    const messageType = params.message_type === 'private' || (params.user_id !== undefined && params.group_id === undefined)
      ? 'private'
      : 'group';
    if (messageType === 'private') return HANDLERS.send_private_forward_msg(responder, params, echo);
    return HANDLERS.send_group_forward_msg(responder, params, echo);
  },

  set_group_special_title(responder, params, echo) {
    return { handled: false, reply: ok(null, echo) };
  },

  set_group_add_request(responder, params, echo) {
    return { handled: false, reply: ok(null, echo) };
  },

  set_friend_add_request(responder, params, echo) {
    return { handled: false, reply: ok(null, echo) };
  },

  // ── P3 ──────────────────────────────────────────────────────────────────
  get_msg(responder, params, echo) {
    const entry = responder.getMsg(params.message_id);
    if (!entry) return { handled: false, reply: fail(1404, 'standalone 模式：账本里没有这条消息', echo) };
    const isGroup = entry.target.messageType === 'group';
    return {
      handled: false,
      reply: ok({
        message_id: entry.messageId,
        message: entry.segments || [],
        raw_message: entry.text || '',
        sender: {
          user_id: responder.world.selfId,
          nickname: responder.world.nickname,
          card: '',
          sex: 'unknown',
          age: 0,
          role: SELF_ROLE,
          title: ''
        },
        time: entry.time,
        message_type: entry.target.messageType,
        ...(isGroup ? { group_id: entry.target.groupId } : { user_id: entry.target.userId })
      }, echo)
    };
  },

  delete_msg(responder, params, echo) {
    const messageId = asNumberId(params.message_id);
    if (messageId === null) return { handled: false, reply: fail(1400, 'delete_msg 需要正整数 message_id', echo) };
    responder.deleteMsg(messageId);
    return { handled: false, reply: ok(null, echo) };
  },

  get_status(responder, params, echo) {
    return { handled: false, reply: ok({ online: true, good: true }, echo) };
  },

  get_version_info(responder, params, echo) {
    // app_version 取 backend.json 的 version（升版本后唯一的对外事实源），不是 package.json
    return {
      handled: false,
      reply: ok({
        app_name: 'ob11-core-bridge',
        protocol_version: 'v11',
        app_version: responder.version
      }, echo)
    };
  },

  // ── 唯一允许失败的族（REQ-D3-08）──────────────────────────────────────
  upload_group_file(responder, params, echo) {
    return { handled: false, reply: fail(100, 'standalone 模式无文件落地', echo) };
  },

  upload_private_file(responder, params, echo) {
    return { handled: false, reply: fail(100, 'standalone 模式无文件落地', echo) };
  },

  upload_file(responder, params, echo) {
    return { handled: false, reply: fail(100, 'standalone 模式无文件落地', echo) };
  }
};

/** §4.1 全表：23 个具名 action（+ `send_forward_msg` 历史别名，共 24 个键）。 */
const NAMED_ACTIONS = Object.keys(HANDLERS);

module.exports = {
  Responder,
  ok,
  fail,
  textOfSegments,
  expandForwardSegments,
  toSegments,
  NAMED_ACTIONS,
  LEDGER_MAX,
  MAX_MESSAGES
};
