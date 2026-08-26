// aiplugin4 后端：AI 浏览器操作与截图（MCP，基于 @playwright/mcp）
// 架构：每个 MCP 会话一个 createConnection（MCP SDK 的 Server 只能 connect 一个
// transport），从而每个 AI 会话一个独立浏览器 context，天然隔离；插件侧（aiplugin4）
// 按 AI 会话 sessionId 分桶复用会话，空闲/超限时先调 browser_close 再删除会话。
const express = require('express');
const crypto = require('crypto');
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

// 会话表：sessionId -> { transport, server, lastUsedAt }
// server 为 @playwright/mcp createConnection 返回的 MCP Server；close 时会触发
// 内部 backend.dispose()，关闭对应浏览器 context（见 playwright-core createServer 的
// "close" 监听器），作为插件未及时 browser_close 时的兜底回收。
const sessions = new Map();

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
  return Number.isFinite(v) && v > 0 ? v : 30;
}

function makeConfig() {
  return {
    browser: {
      browserName: process.env.AIPLUGIN4_BROWSER_NAME || 'chromium',
      isolated: true,
      launchOptions: { headless: parseHeadless(process.env.AIPLUGIN4_BROWSER_HEADLESS) },
      contextOptions: { viewport: parseViewport(process.env.AIPLUGIN4_BROWSER_VIEWPORT) }
    },
    capabilities: ['core']
  };
}

// MCP streamable-http 端点：必须先于 express.json() 注册，让 transport 自行解析原始 body
app.post('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  let info = sessionId ? sessions.get(sessionId) : undefined;
  if (!info) {
    let server;
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => crypto.randomUUID(),
      onsessioninitialized: (sid) => sessions.set(sid, { transport, server, lastUsedAt: Date.now() })
    });
    server = await createConnection(makeConfig());
    await server.connect(transport);
    info = { transport, server, lastUsedAt: Date.now() };
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
// 关闭 MCP Server 会触发内部 backend.dispose() 关闭浏览器 context。
setInterval(() => {
  const ttl = idleMinutes() * 60 * 1000;
  const now = Date.now();
  for (const [sid, info] of [...sessions]) {
    if (now - info.lastUsedAt <= ttl) continue;
    sessions.delete(sid);
    info.server.close().catch((e) => console.error(`[mcp-browser] 关闭空闲会话 ${sid} 失败:`, e));
    console.log(`[mcp-browser] 空闲会话回收: ${sid}（空闲超过 ${idleMinutes()} 分钟）`);
  }
}, 60 * 1000).unref();

app.get('/healthz', (req, res) => res.json({ ok: true, sessions: sessions.size }));

app.listen(port, host, () => {
  console.log(`[mcp-browser] listening on http://${host}:${port}/mcp`);
});
