import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const LANG_DIR = process.env.OCR_LANG_DIR || path.join(__dirname, 'lang-data');
const MIRROR = process.env.OCR_LANG_MIRROR || 'https://tessdata.projectnaptha.com/4.0.0';
const LANGS = (process.env.OCR_LANGS || 'eng,chi_sim')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

await fs.mkdir(LANG_DIR, { recursive: true });

for (const lang of LANGS) {
  const url = `${MIRROR}/${lang}.traineddata.gz`;
  const dest = path.join(LANG_DIR, `${lang}.traineddata.gz`);
  console.log(`下载 ${url}`);
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`下载失败 ${lang}: HTTP ${res.status} ${res.statusText}`);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  await fs.writeFile(dest, buf);
  console.log(`已保存 ${dest} (${(buf.length / 1024 / 1024).toFixed(2)} MB)`);
}

console.log(`语言包准备完成，共 ${LANGS.length} 个：${LANGS.join(', ')}`);
