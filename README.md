# aiplugin4-backends

aiplugin4 的配套后端服务：流式输出、图片转 base64、用量图表、网页读取、Markdown/HTML 渲染。

配合 [aiplugin4](https://github.com/error2913/aiplugin4) 使用：把各服务地址填入插件「后端」配置分组（见 aiplugin4 的 docs/08）。

## 快速开始

环境要求：Python 3.9+、Node.js 18+（仅两个渲染/抓取后端需要 Node）。

```bash
# 直接运行 launcher：自动检查/安装 WebUI 依赖并启动管理界面
python launcher.py
```

打开 http://127.0.0.1:8910（仅本机）。在页面里启动后端：首次启动某后端时，「启动」按钮会变成「安装依赖」——点击后为该后端创建独立 venv / 执行 `npm install`，弹窗实时显示安装日志、按钮转圈；安装完成、日志确认无误后按钮才恢复为「启动」。之后再次启动不再安装，秒开。

> 按需安装：每个后端只安装自己缺失的依赖，不会预装全部；依赖清单（`requirements.txt` / `package.json`）变化后会自动重新安装。Python 后端使用独立 venv，Node 后端使用各自的 `node_modules`。

> 提示：Puppeteer 需要下载 Chromium。若下载慢或失败（如国内网络），可设置镜像 `PUPPETEER_DOWNLOAD_BASE_URL`（例如 `https://registry.npmmirror.com/-/binary/chrome-for-testing`）后重试。

## 后端列表

| 目录 | 服务 | 默认端口 | 类型 |
| --- | --- | --- | --- |
| `stream-output` | 流式输出中转（SSE 分块/轮询） | 3010 | Python |
| `image-url-to-base64` | 图片 URL 转 base64 | 46678 | Python |
| `usage-chart` | token 用量图表 | 3009 | Python |
| `web-read` | 网页 URL 内容读取 | 46799 | Node |
| `md-html-render` | Markdown/HTML 渲染为图片 | 37632 | Node |

## 管理方式

管理全部通过 WebUI 完成：后端启停、依赖安装、端口修改、运行日志都在页面里操作；后端进程异常退出会自动拉起。WebUI 本身是纯 Python 标准库实现，launcher 启动时会自动检查并安装其依赖（当前无额外依赖；若存在 `webui-requirements.txt` 会自动安装）。

每个后端目录下的 `backend.json` 声明类型/入口/依赖/默认端口；端口覆盖写入 `.runtime.json`（已 gitignore），后端通过环境变量 `AIPLUGIN4_BACKEND_PORT` 读取。
