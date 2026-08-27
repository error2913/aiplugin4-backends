// aiplugin4 后端：AI 浏览器操作与截图（MCP，基于 @playwright/mcp）
// 架构：后端自持一个共享浏览器进程（chromium/firefox/webkit），每个 MCP 会话一个独立
// BrowserContext（通过 createConnection 的 contextGetter 注入；库路径下必须 isolated:false
// 才会使用注入的 context）。插件侧（aiplugin4）按 AI 会话 sessionId 分桶复用会话，
// 空闲/超限时先调 browser_close 再删除会话。
// 回收：browser_close 只触发库内部 dispose（摘监听器），并不会关闭 context/浏览器进程；
// 因此本后端以 60s 兜底 sweep 为准，空闲超过 session_idle_minutes（默认 10 分钟）时
// server.close() + context.close()，最后一个会话再 browser.close() 关掉浏览器进程，
// 彻底释放 CPU/内存。旧实现只 server.close()，Chromium 带页面常驻导致 CPU 过高。
const express = require('express');
const crypto = require('crypto');
const playwright = require('playwright');
const { createConnection } = require('@playwright/mcp');
const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');

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

// 会话表：sessionId -> { transport, server, context, closed, lastUsedAt }
// context 为后端自持的 BrowserContext；回收时 server.close()（摘 MCP 监听器）+
// context.close()（关页面），最后一个会话再 browser.close() 关浏览器进程。
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
      // contextGetter 路径下库使用注入 context 的 contexts()[0]；isolated 必须为 false，
      // 否则库会自己 newContext 而绕过我们自持的 context（导致无法真正关闭）。
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

/** 真正回收一个会话：摘 MCP 监听器 + 关 context；最后一个会话同时关共享浏览器 */
async function closeSession(sid, info) {
  if (!info || info.closed) return;
  info.closed = true;
  sessions.delete(sid);
  try { if (info.server) await info.server.close(); } catch (e) { console.error(`[mcp-browser] 关闭 MCP server ${sid} 失败:`, e); }
  try { await info.context.close(); } catch (e) { console.error(`[mcp-browser] 关闭浏览器 context ${sid} 失败:`, e); }
  console.log(`[mcp-browser] 会话已回收: ${sid}`);
  if (sessions.size === 0 && sharedBrowser) {
    try { await sharedBrowser.close(); } catch (e) { console.error('[mcp-browser] 关闭共享浏览器失败:', e); }
    sharedBrowser = null;
    console.log('[mcp-browser] 共享浏览器已关闭（无活动会话）');
  }
}

// MCP streamable-http 端点：必须先于 express.json() 注册，让 transport 自行解析原始 body
app.post('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  let info = sessionId ? sessions.get(sessionId) : undefined;
  if (!info) {
    let server;
    let context;
    try {
      const browser = await getSharedBrowser();
      context = await browser.newContext({ viewport: parseViewport(process.env.AIPLUGIN4_BROWSER_VIEWPORT) });
    } catch (e) {
      console.error('[mcp-browser] 启动浏览器失败:', e);
      if (!res.headersSent) res.status(500).json({ error: 'browser launch failed' });
      return;
    }
    info = { transport: null, server: null, context, closed: false, lastUsedAt: Date.now() };
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => crypto.randomUUID(),
      onsessioninitialized: (sid) => {
        info.transport = transport;
        info.server = server;
        info.lastUsedAt = Date.now();
        sessions.set(sid, info);
      }
    });
    try {
      server = await createConnection(makeConfig(), async () => context);
      await server.connect(transport);
      info.transport = transport;
      info.server = server;
    } catch (e) {
      console.error('[mcp-browser] 创建 MCP 会话失败:', e);
      try { await context.close(); } catch (_) { /* 忽略 */ }
      if (!res.headersSent) res.status(500).json({ error: 'MCP session create failed' });
      return;
    }
    // 保险：transport 关闭（会话异常终止等）时立即回收，不等兜底 sweep
    transport.onclose = () => {
      if (!info || info.closed) return;
      const sid = transport.sessionId || '';
      void closeSession(sid, info);
    };
  }
  info.lastUsedAt = Date.now();
  try {
    await info.transport.handleRequest(req, res);
  } catch (e) {
    console.error('[mcp-browser] MCP 请求处理失败:', e);
    if (!res.headersSent) res.status(500).json({ error: 'MCP request failed' });
  }
});

// 兜底空闲回收：插件侧会主动 browser_close，这里防止插件崩溃/漏回收导致浏览器常驻。
// 旧实现只 server.close() 不关浏览器；现在真正关闭 context/浏览器进程。
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
