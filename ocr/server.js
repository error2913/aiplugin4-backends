import express from 'express';
import { loadImage, InputError } from './downloader.js';
import { recognize, stats, shutdown } from './ocr.js';

const app = express();
// 由 aiplugin4-backends launcher 注入，直接运行时回退到原 HOST/PORT
const PORT = Number(process.env.AIPLUGIN4_BACKEND_PORT || process.env.PORT || 18699);
const HOST = process.env.AIPLUGIN4_BACKEND_HOST || process.env.HOST || '127.0.0.1';
const TOKEN = process.env.AIPLUGIN4_BACKEND_TOKEN || '';

app.use(express.json({ limit: process.env.MAX_BODY_MB || '12mb' }));

// 本地服务 CORS，方便调试工具与其他服务调用
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type,Authorization');
  if (req.method === 'OPTIONS') {
    return res.sendStatus(204);
  }
  next();
});

// token 鉴权（同 aiplugin4-backends 其他后端）：Authorization: Bearer <token> 或 X-Token: <token>
if (TOKEN) {
  app.use((req, res, next) => {
    const auth = req.headers['authorization'] || '';
    if (auth === `Bearer ${TOKEN}` || (req.headers['x-token'] || '') === TOKEN) return next();
    res.status(401).json({ ok: false, error: 'unauthorized' });
  });
}

app.get('/', (req, res) => {
  res.json({
    service: 'sealdice-ocr-backend',
    engine: 'tesseract.js',
    endpoints: ['/health', '/api/ocr', '/api/ocr/url', '/api/ocr/base64'],
  });
});

app.get('/health', (req, res) => {
  res.json({ ok: true, ...stats() });
});

app.post('/api/ocr', async (req, res) => {
  const { url, imageUrl, base64, mime, lang, psm, whitelist } = req.body || {};
  const target = url || imageUrl;
  try {
    res.json(await runOcr({ target, base64, mime, lang, psm, whitelist }));
  } catch (err) {
    respondError(res, err);
  }
});

app.post('/api/ocr/url', async (req, res) => {
  const { url, imageUrl, lang, psm, whitelist } = req.body || {};
  const target = url || imageUrl;
  try {
    if (!target) throw new InputError('缺少 url 字段');
    res.json(await runOcr({ target, lang, psm, whitelist }));
  } catch (err) {
    respondError(res, err);
  }
});

app.post('/api/ocr/base64', async (req, res) => {
  const { base64, mime, lang, psm, whitelist } = req.body || {};
  try {
    if (!base64) throw new InputError('缺少 base64 字段');
    res.json(await runOcr({ base64, mime, lang, psm, whitelist }));
  } catch (err) {
    respondError(res, err);
  }
});

async function runOcr({ target, base64, mime, lang, psm, whitelist }) {
  const t0 = Date.now();
  const normLang =
    typeof lang === 'string' && lang.trim() ? lang.trim().replace(/\s+/g, '') : undefined;

  let buffer;
  if (base64) {
    buffer = Buffer.from(base64, 'base64');
    if (buffer.length === 0) throw new InputError('base64 内容为空');
  } else {
    buffer = await loadImage(target);
  }

  const data = await recognize(buffer, { lang: normLang, psm, whitelist });
  return {
    ok: true,
    text: data.text ?? '',
    confidence: Math.round((data.confidence ?? 0) * 100) / 100,
    lang: normLang || data.lang || undefined,
    durationMs: Date.now() - t0,
    ...(mime ? { mime } : {}),
  };
}

function respondError(res, err) {
  if (err instanceof InputError) {
    res.status(400).json({ ok: false, error: err.message });
  } else {
    console.error('[ocr] 识别失败:', err);
    res.status(500).json({ ok: false, error: err.message || 'OCR 识别失败' });
  }
}

app.use((req, res) => {
  res.status(404).json({ ok: false, error: `未知路径 ${req.path}` });
});

const server = app.listen(PORT, HOST, () => {
  console.log(`sealdice-ocr-backend 已启动: http://${HOST}:${PORT}`);
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    await shutdown();
    server.close(() => process.exit(0));
  });
}
