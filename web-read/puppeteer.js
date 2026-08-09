const express = require('express');
const puppeteer = require('puppeteer');

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
      args: ['--no-sandbox', '--disable-setuid-sandbox']
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
