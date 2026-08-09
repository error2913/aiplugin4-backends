const express = require('express');
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { z } = require('zod');

const app = express();
const port = Number(process.env.AIPLUGIN4_BACKEND_PORT || 46799);
const host = process.env.AIPLUGIN4_BACKEND_HOST || '0.0.0.0';
const token = process.env.AIPLUGIN4_BACKEND_TOKEN || '';

if (token) {
  app.use((req, res, next) => {
    const auth = req.headers['authorization'] || '';
    if (auth === `Bearer ${token}` || (req.headers['x-token'] || '') === token) return next();
    res.status(401).json({ error: 'unauthorized' });
  });
}

// MCP streamable-http 端点：必须先于 express.json() 注册，让 transport 自行解析原始 body
const mcpTransports = new Map();
app.post('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  let transport = sessionId ? mcpTransports.get(sessionId) : undefined;
  if (!transport) {
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => crypto.randomUUID(),
      onsessioninitialized: (sid) => mcpTransports.set(sid, transport)
    });
    await createMcpServer().connect(transport);
  }
  try {
    await transport.handleRequest(req, res);
  } catch (e) {
    console.error('[web-read] MCP 请求处理失败:', e);
    if (!res.headersSent) res.status(500).json({ error: 'MCP request failed' });
  }
});

// 内置字体兜底：服务器缺 CJK 字体时，无头浏览器会把中文/曲库特殊符号渲染成 □。
// ScreenCJK.ttf 是用 fontTools 按「曲库全部曲名 + 控制器界面文本」裁剪的子集
// （约 330KB）。启动时把字体装进用户字体目录（Linux ~/.fonts）+ fc-cache，
// Chromium 通过 fontconfig 对缺失字形自然回退，无需改页面 CSS。
function installBundledFont() {
  if (process.platform === 'win32') return; // Windows 自带中文字体，无需安装
  const fontPath = path.join(__dirname, 'fonts', 'ScreenCJK.ttf');
  if (!fs.existsSync(fontPath)) return;
  const home = process.env.HOME || process.env.XDG_CONFIG_HOME || '';
  if (!home) return;
  try {
    const dir = path.join(home, '.fonts');
    fs.mkdirSync(dir, { recursive: true });
    fs.copyFileSync(fontPath, path.join(dir, 'ScreenCJK.ttf'));
    try {
      require('child_process').execSync('fc-cache -f >/dev/null 2>&1 || true');
    } catch (e) {
      // fc-cache 不存在时忽略，fontconfig 会在下次使用时自动扫描
    }
    console.log('[web-read] 内置字体已安装到 ' + dir);
  } catch (e) {
    console.error('[web-read] 安装内置字体失败:', e);
  }
}
installBundledFont();

app.use(express.json());

// ---- 共享逻辑（REST 路由与 MCP 工具共用）----
async function scrapePage(url) {
  let browser;
  try {
    browser = await puppeteer.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox']
    });
    const page = await browser.newPage();
    await page.goto(url, { waitUntil: 'networkidle2' });
    return await page.evaluate(() => ({
      title: document.title,
      content: document.body.innerText,
      links: Array.from(document.querySelectorAll('a')).map(a => a.href)
    }));
  } finally {
    if (browser) await browser.close();
  }
}

async function screenshotPage({ url, width = 1680, height = 1000, fullPage = false, delay = 3000, waitUntil = 'domcontentloaded' }) {
  let browser;
  try {
    browser = await puppeteer.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--allow-file-access-from-files']
    });
    const page = await browser.newPage();
    await page.setViewport({ width: Number(width) || 1680, height: Number(height) || 1000, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil, timeout: 60000 });
    const waitMs = Number(delay) || 0;
    if (waitMs > 0) await new Promise(r => setTimeout(r, waitMs));
    const base64 = await page.screenshot({ type: 'png', encoding: 'base64', fullPage: !!fullPage });
    return { base64, format: 'png', width: Number(width) || 1680, height: Number(height) || 1000, fullPage: !!fullPage };
  } finally {
    if (browser) await browser.close();
  }
}

// ---- MCP server（streamable-http，挂 /mcp；每会话一个 server 实例）----
function createMcpServer() {
  const server = new McpServer({ name: 'web-read', version: '1.0.0' });

  server.tool(
    'scrape_url',
    { url: z.string().describe('需要读取内容的网页链接') },
    async ({ url }) => {
      try {
        const data = await scrapePage(url);
        const text = `标题: ${data.title || '无标题'}\n内容: ${data.content || '无内容'}\n网页包含链接:\n` +
          (data.links && data.links.length > 0
            ? data.links.map((link, index) => `${index + 1}. ${link}`).join('\n')
            : '无链接');
        return { content: [{ type: 'text', text }] };
      } catch (e) {
        return { content: [{ type: 'text', text: `读取网页失败: ${e.message || String(e)}` }], isError: true };
      }
    }
  );

  server.tool(
    'screenshot_url',
    {
      url: z.string().describe('需要截图的网页链接'),
      width: z.number().optional().describe('视口宽度，默认 1680'),
      height: z.number().optional().describe('视口高度，默认 1000'),
      fullPage: z.boolean().optional().describe('是否截取整页（长图），默认 false'),
      delay: z.number().optional().describe('页面加载完成后等待毫秒数，默认 3000')
    },
    async ({ url, width, height, fullPage, delay }) => {
      try {
        const shot = await screenshotPage({ url, width, height, fullPage, delay });
        return { content: [{ type: 'text', text: shot.base64 }] };
      } catch (e) {
        return { content: [{ type: 'text', text: `网页截图失败: ${e.message || String(e)}` }], isError: true };
      }
    }
  );

  return server;
}

// 启动服务器
app.listen(port, host, () => {
  console.log(`Server is running on http://localhost:${port}`);
});
