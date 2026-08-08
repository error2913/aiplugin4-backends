# OCR 后端（Node.js + tesseract.js）

已收录进 [aiplugin4-backends](../../README.md)（错误后端），默认端口 **18699**（与海豹插件默认配置一致，无需改插件地址）。源码来自 `sealdice-OCR-plugin/backend`，仅做了 launcher 契约适配（`AIPLUGIN4_BACKEND_*` 环境变量与 token 鉴权）。

## 启动

通过 WebUI / `aibackend` 命令启动即可（自动 `npm install`）：

```bash
aibackend setup ocr      # 安装依赖
aibackend start ocr      # 启动
```

首次启动后，可先预下载语言包（可选，不下载则首次识别时按需拉取）：

```bash
cd ocr && npm run setup-langs   # 预下载 eng,chi_sim 到 lang-data/
```

独立运行（绕过 launcher）：

```bash
npm install
npm start             # 默认 http://127.0.0.1:18699
```

启动后先访问 `http://127.0.0.1:18699/health` 确认服务正常。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIPLUGIN4_BACKEND_PORT` | `18699` | launcher 注入的监听端口（优先于 `PORT`） |
| `AIPLUGIN4_BACKEND_HOST` | `0.0.0.0` | launcher 注入的监听地址（优先于 `HOST`） |
| `AIPLUGIN4_BACKEND_TOKEN` | 空 | launcher 注入的访问 token；非空时校验 `Authorization: Bearer <token>` 或 `X-Token: <token>` |
| `PORT` / `HOST` | `18699` / `127.0.0.1` | 独立运行时使用的回退变量 |
| `OCR_DEFAULT_LANG` | `chi_sim+eng` | 默认识别语言，多个用 `+` |
| `OCR_LANG_DIR` | `./lang-data` | 语言包目录 |
| `OCR_CACHE_DIR` | `./cache` | 编译缓存目录 |
| `OCR_DEBUG` | `0` | `1` 时输出 tesseract.js 阶段日志 |
| `MAX_BODY_MB` | `12` | JSON 请求体上限（base64 图片） |

`setup-langs.js` 额外支持：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OCR_LANGS` | `eng,chi_sim` | 逗号分隔的预下载语言列表 |
| `OCR_LANG_MIRROR` | `https://tessdata.projectnaptha.com/4.0.0` | 语言包镜像 |

## API

### GET /health

```json
{
  "ok": true,
  "engine": "tesseract.js",
  "workerReady": true,
  "workerLang": "chi_sim+eng",
  "pendingJobs": 0,
  "totalJobs": 3,
  "defaultLang": "chi_sim+eng",
  "langDir": "...",
  "cacheDir": "..."
}
```

### POST /api/ocr

通用入口，二选一提供 `url` 或 `base64`：

```json
{
  "url": "https://example.com/a.png",
  "lang": "chi_sim+eng",
  "psm": 6,
  "whitelist": "0123456789"
}
```

`url` 支持 `http(s)://` 链接、`file://` 协议、本机绝对路径（Windows 或 Unix）。
`psm` 为 Tesseract 页面分割模式；`whitelist` 为字符白名单。

成功响应：

```json
{
  "ok": true,
  "text": "识别出的文字",
  "confidence": 94,
  "lang": "chi_sim+eng",
  "durationMs": 132
}
```

失败响应（HTTP 400/500）：

```json
{ "ok": false, "error": "错误原因" }
```

### POST /api/ocr/url

只接受 URL（含本地路径）：

```bash
curl -X POST http://127.0.0.1:18699/api/ocr/url \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/a.png","lang":"chi_sim+eng"}'
```

### POST /api/ocr/base64

```bash
curl -X POST http://127.0.0.1:18699/api/ocr/base64 \
  -H "Content-Type: application/json" \
  -d '{"base64":"<图片base64>","mime":"image/png","lang":"chi_sim+eng"}'
```

## 说明

- OCR 任务串行执行（单 worker），并发请求会排队，`/health` 的 `pendingJobs` 反映队列长度。
- 语言包首次加载后会解压缓存到 `cache/`，之后请求不再重复下载；`lang-data/` 与 `cache/` 均为运行时数据，不入库、不打进发布包。
- 后端会按请求抓取任意 URL，属于 SSRF 面：默认监听 `0.0.0.0`，若非必要请通过「⚙ 配置」把监听 IP 改为 `127.0.0.1` 或设置访问 token，不要直接暴露到公网。
