import fs from 'node:fs/promises';

const DEFAULT_MAX_BYTES = 25 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 30_000;

export class InputError extends Error {}

/**
 * 把图片输入统一解析为 Buffer。
 * 支持：http(s):// 链接、file:// 协议、本机绝对路径。
 */
export async function loadImage(input, { maxBytes = DEFAULT_MAX_BYTES, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  if (!input || typeof input !== 'string') {
    throw new InputError('imageUrl 不能为空');
  }
  const source = input.trim();

  if (source.startsWith('file://')) {
    let filePath = decodeURIComponent(source.slice('file://'.length));
    // file:///C:/xxx -> C:/xxx
    filePath = filePath.replace(/^\/([A-Za-z]:)/, '$1');
    return readFileChecked(filePath, maxBytes);
  }

  // 本机绝对路径（Windows C:\... 或 Unix /...）
  if (/^[A-Za-z]:[\\/]/.test(source) || source.startsWith('/')) {
    return readFileChecked(source, maxBytes);
  }

  if (!/^https?:\/\//i.test(source)) {
    throw new InputError('仅支持 http(s):// 链接、file:// 或本机文件路径');
  }

  let res;
  try {
    res = await fetch(source, { redirect: 'follow', signal: AbortSignal.timeout(timeoutMs) });
  } catch (err) {
    throw new InputError(`下载图片失败: ${err.message}`);
  }
  if (!res.ok) {
    throw new InputError(`下载图片失败: HTTP ${res.status} ${res.statusText}`);
  }
  if (!res.body) {
    throw new InputError('响应无内容');
  }

  const chunks = [];
  let total = 0;
  try {
    for await (const chunk of res.body) {
      total += chunk.length;
      if (total > maxBytes) {
        throw new InputError(`图片超过大小限制 ${maxBytes} 字节`);
      }
      chunks.push(chunk);
    }
  } catch (err) {
    if (err instanceof InputError) throw err;
    throw new InputError(`读取图片失败: ${err.message}`);
  }
  if (total === 0) {
    throw new InputError('图片内容为空');
  }
  return Buffer.concat(chunks);
}

async function readFileChecked(filePath, maxBytes) {
  let stat;
  try {
    stat = await fs.stat(filePath);
  } catch (err) {
    throw new InputError(`无法读取本地文件 ${filePath}: ${err.message}`);
  }
  if (!stat.isFile()) {
    throw new InputError('路径不是文件');
  }
  if (stat.size > maxBytes) {
    throw new InputError(`图片超过大小限制 ${maxBytes} 字节`);
  }
  return fs.readFile(filePath);
}
