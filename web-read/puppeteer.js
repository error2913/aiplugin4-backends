const express = require('express');
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

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

app.post('/scrape', async (req, res) => {
  const { url } = req.body;

  if (!url) {
    return res.status(400).json({ error: 'URL is required' });
  }

  let browser;
  try {
    browser = await puppeteer.launch({
      headless: true, 
      args: ['--no-sandbox', '--disable-setuid-sandbox'], // 禁用沙盒
    });

    const page = await browser.newPage();

    await page.goto(url, { waitUntil: 'networkidle2' });

    const data = await page.evaluate(() => {
      return {
        title: document.title,
        content: document.body.innerText,
        links: Array.from(document.querySelectorAll('a')).map(a => a.href),
      };
    });

    res.json(data);
  } catch (error) {
    console.error('Error:', error);
    res.status(500).json({ error: 'An error occurred while scraping the page' });
  } finally {
    if (browser) {
      await browser.close();
    }
  }
});

// 网页截图：用 Puppeteer 无头浏览器对任意 URL 截图，返回 PNG base64。
// body: { url, width?, height?, fullPage?, delay?, waitUntil? }
// 控制器页面使用 WebSocket + 每秒轮询 /api/tick，waitUntil 默认 domcontentloaded
// （networkidle2 会因轮询永不空闲），再用 delay 等前端完成渲染。
app.post('/screenshot', async (req, res) => {
  const { url, width = 1680, height = 1000, fullPage = false, delay = 3000, waitUntil = 'domcontentloaded' } = req.body || {};

  if (!url) {
    return res.status(400).json({ status: 'error', message: 'URL is required' });
  }

  let browser;
  try {
    browser = await puppeteer.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--allow-file-access-from-files']
    });

    const page = await browser.newPage();
    await page.setViewport({
      width: Number(width) || 1680,
      height: Number(height) || 1000,
      deviceScaleFactor: 1
    });

    await page.goto(url, { waitUntil: waitUntil, timeout: 60000 });

    const waitMs = Number(delay) || 0;
    if (waitMs > 0) {
      await new Promise(r => setTimeout(r, waitMs));
    }

    const base64 = await page.screenshot({
      type: 'png',
      encoding: 'base64',
      fullPage: !!fullPage
    });

    res.json({
      status: 'success',
      format: 'png',
      base64,
      width: Number(width) || 1680,
      height: Number(height) || 1000,
      fullPage: !!fullPage
    });
  } catch (error) {
    console.error('Screenshot error:', error);
    res.status(500).json({ status: 'error', message: (error && error.message) ? error.message : String(error) });
  } finally {
    if (browser) {
      await browser.close();
    }
  }
});

// 启动服务器
app.listen(port, host, () => {
  console.log(`Server is running on http://localhost:${port}`);
});
