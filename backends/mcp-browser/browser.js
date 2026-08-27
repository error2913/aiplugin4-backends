// aiplugin4 后端：AI 浏览器操作与截图（MCP，基于 @playwright/mcp 工具集）
// 架构：
// - 惰性启动：tools/list 只返回工具 schema，绝不碰浏览器；只有真正调用 browser_*
//   工具（tools/call）才启动共享 Chromium 并新建独立 BrowserContext。插件侧每 ≤60s
//   一次的 tools/list 同步不再拉起/续命浏览器。
// - 会话：每个 MCP 会话一个独立 BrowserContext（浏览器进程全局共享 1 个），按 AI 会话隔离。
// - 回收：browser_close 成功后响应发完立即回收该会话（关 context，连带关闭页面；最后
//   一个会话再关浏览器进程）；另有 60s 兜底 sweep，空闲超过 session_idle_minutes
//   （默认 10 分钟）的会话真正关闭。lastUsedAt 只在 tools/call 刷新，因此活跃群里
//   只做 tools/list 的默认会话也会被回收，彻底解决旧版 Chromium 常驻吃 CPU 的问题。
const express = require('express');
const crypto = require('crypto');
const playwright = require('playwright');
const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { ListToolsRequestSchema, CallToolRequestSchema } = require('@modelcontextprotocol/sdk/types.js');
const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { tools } = require('playwright-core/lib/coreBundle');
const ub = require('playwright-core/lib/utilsBundle');

// 兼容 Node <19：MCP SDK（webStandardStreamableHttp.js）内部直接调用全局
// crypto.randomUUID()，旧版 Node 没有全局 crypto，会导致 /mcp 返回
// HTTP 400 "Parse error: crypto is not defined"。用 Node 内置的 crypto.webcrypto
// 补齐全局对象（Node 15+ 自带 webcrypto）。
if (typeof globalThis.crypto === 'undefined' || typeof globalThis.crypto.randomUUID !== 'function') {
  const webcrypto = crypto.webcrypto || crypto;
  try {
    Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
  } catch (e) {
    globalThis.crypto = webcrypto;
  }
  if (typeof globalThis.crypto.randomUUID !== 'function' && typeof crypto.randomUUID === 'function') {
    globalThis.crypto.randomUUID = crypto.randomUUID;
  }
}

const app = express();
const port = Number(process.env.AIPLUGIN4_BACKEND_PORT || 8921);
const host = process.env.AIPLUGIN4_BACKEND_HOST || '0.0.0.0';
const token = process.env.AIPLUGIN4_BACKEND_TOKEN || '';

if (token) {
  app.use((req, res, next) => {
    const auth = req.headers['authorization'] || '';
    if (auth === `Bearer ${token}` || (req.headers['x-token'] || '') === token) return next();
    res.status(401).json({ error: 'unauthorized' });
  });
}

// 会话表：sessionId -> { transport, server, context, backend, backendPromise, closed, lastUsedAt, reclaimAfterResponse }
// context 为后端自持的 BrowserContext；backend 为 playwright BrowserBackend（首次 tools/call 才创建）。
const sessions = new Map();
// 全局共享浏览器：惰性启动，无活动会话时关闭并置空，避免常驻吃 CPU/内存。
let sharedBrowser = null;

function parseHeadless(value) {
  if (value === undefined || value === null || value === '') return true;
  const v = String(value).trim().toLowerCase();
  return ['1', 'true', 'yes', 'on'].includes(v);
}

function parseViewport(value) {
  const m = String(value || '').trim().match(/^(\d+)\s*[xX*]\s*(\d+)$/);
  if (!m) return { width: 1280, height: 720 };
  const width = Number(m[1]);
  const height = Number(m[2]);
  if (!width || !height) return { width: 1280, height: 720 };
  return { width, height };
}

function idleMinutes() {
  const v = Number(process.env.AIPLUGIN4_BROWSER_IDLE_MINUTES);
  return Number.isFinite(v) && v > 0 ? v : 10;
}

function parseChromiumSandbox(value) {
  if (value !== undefined && value !== null && value !== '') {
    const v = String(value).trim().toLowerCase();
    return !['0', 'false', 'no', 'off'].includes(v); // 显式 true/1/yes/on -> 开启
  }
  // 未显式设置：root 下 Chromium 沙箱起不来，自动关闭；其他环境保持默认开启
  return !(typeof process.getuid === 'function' && process.getuid() === 0);
}

function browserName() {
  return process.env.AIPLUGIN4_BROWSER_NAME || 'chromium';
}

function makeConfig() {
  return {
    browser: {
      browserName: browserName(),
      // 本后端自持 BrowserContext 传给 BrowserBackend，不再走库的 contextGetter；
      // isolated 保持 false，与旧版行为一致（共享浏览器、按会话独立 context）。
      isolated: false,
      launchOptions: {
        headless: parseHeadless(process.env.AIPLUGIN4_BROWSER_HEADLESS),
        chromiumSandbox: parseChromiumSandbox(process.env.AIPLUGIN4_BROWSER_CHROMIUM_SANDBOX)
      },
      contextOptions: { viewport: parseViewport(process.env.AIPLUGIN4_BROWSER_VIEWPORT) }
    },
    capabilities: ['core']
  };
}

// 工具定义在启动时算好：filteredTools 只做 schema 计算，不启动浏览器。
const toolDefs = tools.filteredTools(makeConfig());

/** 与 playwright 内置 toMcpTool 一致：tools/list 只输出 schema，不初始化任何浏览器资源 */
function toMcpTool(schema) {
  const readOnly = schema.type === 'readOnly' || schema.type === 'assertion';
  return {
    name: schema.name,
    description: schema.description,
    inputSchema: ub.z.toJSONSchema(schema.inputSchema),
    annotations: {
      title: schema.title,
      readOnlyHint: readOnly,
      destructiveHint: !readOnly,
      openWorldHint: true
    }
  };
}

/** 与 playwright 内置 mergeTextParts 一致：合并连续的 text 片段 */
function mergeTextParts(result) {
  const content = [];
  const textParts = [];
  for (const part of result.content) {
    if (part.type === 'text') {
      textParts.push(part.text);
      continue;
    }
    if (textParts.length > 0) {
      content.push({ type: 'text', text: textParts.join('\n') });
      textParts.length = 0;
    }
    content.push(part);
  }
  if (textParts.length > 0) content.push({ type: 'text', text: textParts.join('\n') });
  return { ...result, content };
}

/** 获取全局共享浏览器（惰性启动；进程崩溃后自动重启） */
async function getSharedBrowser() {
  if (sharedBrowser && sharedBrowser.isConnected()) return sharedBrowser;
  const lib = playwright[browserName()] || playwright.chromium;
  const launchOptions = {
    headless: parseHeadless(process.env.AIPLUGIN4_BROWSER_HEADLESS)
  };
  // chromiumSandbox 仅 Chromium 支持；其他浏览器类型不传，避免启动报错
  if (browserName() === 'chromium') {
    launchOptions.chromiumSandbox = parseChromiumSandbox(process.env.AIPLUGIN4_BROWSER_CHROMIUM_SANDBOX);
  }
  sharedBrowser = await lib.launch(launchOptions);
  console.log(`[mcp-browser] 共享浏览器已启动: ${browserName()}`);
  return sharedBrowser;
}

/**
 * 惰性初始化会话的后端：首次 tools/call 才启动浏览器 + 新建独立 context。
 * 并发调用只初始化一次（backendPromise 去重）。
 */
async function ensureBackend(info) {
  if (info.backend) return info.backend;
  if (info.backendPromise) return info.backendPromise;
  info.backendPromise = (async () => {
    const browser = await getSharedBrowser();
    const context = await browser.newContext({ viewport: parseViewport(process.env.AIPLUGIN4_BROWSER_VIEWPORT) });
    if (info.closed) {
      // 初始化期间会话被回收（极端竞态）：丢弃刚建的 context，不挂到会话上
      await context.close().catch(() => {});
      return null;
    }
    info.context = context;
    const backend = new tools.BrowserBackend(makeConfig(), context, toolDefs, async () => {});
    await backend.initialize({ cwd: '', clientName: 'mcp-browser' });
    if (info.closed) {
      await backend.dispose().catch(() => {});
      await context.close().catch(() => {});
      return null;
    }
    info.backend = backend;
    info.backendPromise = null;
    return backend;
  })();
  try {
    return await info.backendPromise;
  } catch (e) {
    info.backendPromise = null;
    throw e;
  }
}

/** 真正回收一个会话：释放后端 + 摘 MCP 监听器 + 关 context；最后一个会话同时关共享浏览器 */
async function closeSession(sid, info) {
  if (!info || info.closed) return;
  info.closed = true;
  sessions.delete(sid);
  try { if (info.backend) await info.backend.dispose(); } catch (e) { console.error(`[mcp-browser] 释放后端 ${sid} 失败:`, e); }
  try { if (info.server) await info.server.close(); } catch (e) { console.error(`[mcp-browser] 关闭 MCP server ${sid} 失败:`, e); }
  try { if (info.context) await info.context.close(); } catch (e) { console.error(`[mcp-browser] 关闭浏览器 context ${sid} 失败:`, e); }
  console.log(`[mcp-browser] 会话已回收: ${sid}`);
  if (sessions.size === 0 && sharedBrowser) {
    try { await sharedBrowser.close(); } catch (e) { console.error('[mcp-browser] 关闭共享浏览器失败:', e); }
    sharedBrowser = null;
    console.log('[mcp-browser] 共享浏览器已关闭（无活动会话）');
  }
}

/** 创建一个 MCP 会话（只挂 Server + transport，不碰浏览器；浏览器等首次 tools/call 再启动） */
function createSession() {
  const info = {
    sid: '',
    transport: null,
    server: null,
    context: null,
    backend: null,
    backendPromise: null,
    closed: false,
    lastUsedAt: Date.now(),
    reclaimAfterResponse: false
  };
  const server = new Server({ name: 'mcp-browser', version: '1.0.4' }, { capabilities: { tools: {} } });
  // tools/list：只返回 schema，绝不初始化浏览器 —— 插件侧每 ≤60s 的同步不再续命
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: toolDefs.map(t => toMcpTool(t.schema))
  }));
  // tools/call：真正的浏览器操作才惰性启动浏览器；lastUsedAt 只在这里刷新
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    info.lastUsedAt = Date.now();
    try {
      // 会话从未初始化过浏览器时，browser_close 直接视为已关闭，避免为关闭而拉起浏览器
      if (request.params.name === 'browser_close' && !info.backend && !info.backendPromise) {
        info.reclaimAfterResponse = true;
        return { content: [{ type: 'text', text: 'Browser is not open.' }], isError: false };
      }
      const backend = await ensureBackend(info);
      if (!backend) {
        return { content: [{ type: 'text', text: '### Error\nSession closed while initializing browser' }], isError: true };
      }
      const result = await backend.callTool(request.params.name, request.params.arguments || {}, undefined);
      const merged = mergeTextParts(result);
      // browser_close 已把页面清空：等本次响应发完，由 /mcp 处理器回收整个会话
      // browser_close 语义是关闭整个会话（playwright-mcp 内部已 dispose 后端、清空 tab，
      // 但 page 由本后端自持的 context 管理）：响应发完后立即由 /mcp 处理器回收会话，
      // closeSession 会 close context（连带关闭所有 page），最后一个会话再关浏览器进程。
      if (request.params.name === 'browser_close' && !result.isError) {
        info.reclaimAfterResponse = true;
      }
      return merged;
    } catch (error) {
      return { content: [{ type: 'text', text: '### Error\n' + String(error) }], isError: true };
    }
  });
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => crypto.randomUUID(),
    onsessioninitialized: (sid) => {
      info.sid = sid;
      info.transport = transport;
      info.server = server;
      info.lastUsedAt = Date.now();
      sessions.set(sid, info);
    }
  });
  server.connect(transport).catch(e => console.error('[mcp-browser] MCP server connect 失败:', e));
  // transport 关闭（客户端 DELETE / 连接中断 / server.close）时立即回收，不等兜底 sweep
  transport.onclose = () => {
    if (!info || info.closed) return;
    void closeSession(info.sid, info);
  };
  info.transport = transport;
  info.server = server;
  return info;
}

/** 会话 id 未知（已被回收/不存在）：按 Streamable HTTP 规范返回 404 + MCP 错误。
 *  错误消息含 "session"/"会话" 字样，插件客户端会识别为会话失效并自动重新 initialize。 */
function sessionNotFound(res) {
  res.status(404).json({
    jsonrpc: '2.0',
    id: null,
    error: { code: -32002, message: 'Session not found or expired（会话不存在或已失效），请重新 initialize' }
  });
}

// MCP streamable-http 端点：必须先于 express.json() 注册，让 transport 自行解析原始 body
app.post('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  let info = sessionId ? sessions.get(sessionId) : undefined;
  if (sessionId && !info) return sessionNotFound(res);
  if (!info) {
    try {
      info = createSession();
    } catch (e) {
      console.error('[mcp-browser] 创建 MCP 会话失败:', e);
      if (!res.headersSent) res.status(500).json({ error: 'MCP session create failed' });
      return;
    }
  }
  try {
    await info.transport.handleRequest(req, res);
  } catch (e) {
    console.error('[mcp-browser] MCP 请求处理失败:', e);
    if (!res.headersSent) res.status(500).json({ error: 'MCP request failed' });
  }
  // browser_close 已关闭会话：响应发完后立即回收会话，不等 60s 兜底 sweep
  if (info.reclaimAfterResponse && !info.closed) {
    info.reclaimAfterResponse = false;
    await closeSession(info.sid || sessionId || '', info);
  }
});

// DELETE /mcp：客户端显式结束会话（规范支持；插件未使用，留作兼容）
app.delete('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  const info = sessionId ? sessions.get(sessionId) : undefined;
  if (sessionId && !info) return sessionNotFound(res);
  try {
    await info.transport.handleRequest(req, res);
  } catch (e) {
    console.error('[mcp-browser] MCP DELETE 处理失败:', e);
    if (!res.headersSent) res.status(500).json({ error: 'MCP request failed' });
  }
});

// 兜底空闲回收：插件侧会主动 browser_close，这里防止插件崩溃/漏回收导致浏览器常驻。
// 只统计 tools/call（真实使用），tools/list 同步不会续命，活跃群默认会话也能被回收。
setInterval(async () => {
  const ttl = idleMinutes() * 60 * 1000;
  const now = Date.now();
  for (const [sid, info] of [...sessions]) {
    if (now - info.lastUsedAt <= ttl) continue;
    console.log(`[mcp-browser] 空闲会话回收: ${sid}（空闲超过 ${idleMinutes()} 分钟）`);
    await closeSession(sid, info);
  }
}, 60 * 1000).unref();

app.get('/healthz', (req, res) => res.json({
  ok: true,
  sessions: sessions.size,
  sharedBrowser: !!(sharedBrowser && sharedBrowser.isConnected())
}));

// 进程退出时关闭浏览器，避免重启残留 Chromium 子进程
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, async () => {
    try { if (sharedBrowser) await sharedBrowser.close(); } catch (e) { /* 忽略 */ }
    process.exit(0);
  });
}

app.listen(port, host, () => {
  console.log(`[mcp-browser] listening on http://${host}:${port}/mcp`);
});
