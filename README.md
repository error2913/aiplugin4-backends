# aiplugin4-backends

aiplugin4 的配套后端服务：流式输出、图片转 base64、用量图表、网页读取、Markdown/HTML 渲染。

配合 [aiplugin4](https://github.com/error2913/aiplugin4) 使用：把各服务地址填入插件「后端」配置分组（见 aiplugin4 的 docs/08）。

## 目录

- [快速开始](#快速开始)
- [跨平台](#跨平台)
- [Linux 系统服务](#linux-系统服务)
- [后端列表](#后端列表)
- [后端介绍](#后端介绍)
- [目录结构](#目录结构)
- [管理方式](#管理方式)
- [命令行（aibackend）](#命令行aibackend)

## 快速开始

环境要求：Python 3.9+、Node.js 18+（仅 `web-read` / `md-html-render` 两个后端需要 Node）。

```bash
git clone https://github.com/error2913/aiplugin4-backends.git && cd aiplugin4-backends && python launcher.py
```

首次运行即自动安装所需依赖、自动安装 `aibackend` 命令行并**在后台启动**管理界面（不占用终端、无控制台窗口）。WebUI 默认监听 `0.0.0.0`（全部网卡），端口与访问 token 首次运行随机生成并保持稳定，打开页面后输入 token 登录（记住一年）。自动开浏览器只在有图形环境时进行（Windows 直接开；Linux/macOS 需检测到 `DISPLAY` / `WAYLAND_DISPLAY` 且显式设置 `BROWSER`），无头服务器只打印访问地址。停止后台 WebUI：`python launcher.py webui-stop` 或 `aibackend webui-stop`。所有管理都在页面里完成：

- 首次启动某后端时，按钮显示「安装依赖」：点击后创建独立 venv / 执行 `npm install`，弹窗实时显示日志、按钮转圈，装完恢复为「启动」；之后再次启动不再安装，秒开
- 有后端依赖未安装时，右上角出现「安装全部依赖」，可一键补齐
- 「启动全部」只启动依赖已就绪的后端；若全部依赖未安装会弹出提示
- 「重启全部」先停止全部，再启动依赖已就绪的后端
- 右上角「⬆ 更新」从 Git 拉取项目更新（手动，非自动）
- 卡片显示运行时长、自动拉起次数与内存占用；「日志」旁可点「删除依赖」恢复未安装状态
- 卡片「⚙ 配置」弹窗可修改端口、访问 token 与监听 IP（默认 `0.0.0.0`），支持一键随机生成 token；token 默认留空 = 不鉴权
- 后端进程异常退出会自动拉起

> 按需安装：每个后端只安装自己缺失的依赖，不会预装全部；依赖清单（`requirements.txt` / `package.json`）变化后会自动重新安装。Python 后端使用独立 venv，Node 后端使用各自的 `node_modules`。

> 提示：Puppeteer 需要下载 Chromium。下载慢或失败（如国内网络）时，可设置镜像 `PUPPETEER_DOWNLOAD_BASE_URL`（例如 `https://registry.npmmirror.com/-/binary/chrome-for-testing`）后重试。

## 跨平台

Windows 与 Linux 均支持，同一套代码无需改动：

- 依赖按平台处理：Python 后端用独立 venv（Windows 取 `.venv\Scripts\python.exe`，Linux 取 `.venv/bin/python`），Node 后端 Windows 下自动走 `npm.cmd`
- 后台守护：Windows 用 `DETACHED_PROCESS`（不弹控制台黑框），Linux 用 `start_new_session`
- 内存读取：Windows 走系统 API，Linux 读 `/proc`；运行时长/自动拉起次数两平台一致
- 安装 `aibackend` 命令：Windows 生成 `aibackend.cmd`，Linux 生成 shell 脚本并写入 shell 配置（`.bashrc` / `.zshrc` / `.profile`）

Linux 注意：Puppeteer 需要 Chromium 系统依赖。启动 `web-read` / `md-html-render` 时会自动用 `ldd` 检测缺失的共享库，并在 Debian/Ubuntu 下自动 `apt-get install` 补齐（需 root/sudo）；非 apt 发行版或自动安装失败时，按日志提示手动安装（如 `libnss3`、`libatk-1.0-0`、`libx11-xcb1` 等）。

## Linux 系统服务

注册 systemd 服务后，WebUI 开机自启、异常退出自动拉起（`Restart=always`），首次启动自动安装依赖并直接运行：

```bash
python launcher.py service-install      # 注册并立即启动（需 root/sudo）
python launcher.py service-uninstall    # 停止并移除服务
```

服务前台运行，日志通过 `journalctl -u aiplugin4-webui -f` 查看；安装服务前会自动停掉已有后台 WebUI 以释放端口。

## 后端列表

| 目录 | 服务 | 默认端口 | 类型 |
| --- | --- | --- | --- |
| `stream-output` | 流式输出中转（SSE 分块/轮询） | 3010 | Python |
| `image-url-to-base64` | 图片 URL 转 base64 | 46678 | Python |
| `usage-chart` | token 用量图表 | 3009 | Python |
| `web-read` | 网页 URL 内容读取 | 46799 | Node |
| `md-html-render` | Markdown/HTML 渲染为图片 | 37632 | Node |
| `mcp-files-exec` | MCP：AI 读写文件与执行受限命令（沙箱 + 拦截） | 3910 | Python |
| `ocr` | OCR 图片文字识别（tesseract.js，支持 URL / base64 / 本地路径） | 18699 | Node |

## 后端介绍

### stream-output — 流式输出中转

代理 OpenAI 兼容接口，把流式输出按符号/长度切成片段，前端可实时轮询出打字机效果，结束后返回用量统计。

- 默认端口 3010（Python / FastAPI），接口：
  - `POST /start`：body `{url, api_key, body_obj}`（`body_obj` 为 chat.completions 参数），返回任务 `{id}`
  - `GET /poll?id=<id>`：轮询已生成的分块
  - `GET /end?id=<id>`：结束任务，返回片段与 usage
- 依赖：`fastapi`、`openai`、`tiktoken`、`uvicorn`

### image-url-to-base64 — 图片 URL 转 base64

下载图片并转成 base64，自动识别格式（jpg / png / gif / webp），静态 GIF 自动转成 PNG。

- 默认端口 46678（Python / Flask）
- 接口：`POST /image-to-base64`，body `{url}`，返回 `{base64, format}`
- 依赖：`flask`、`Pillow`、`imageio`、`requests`

### usage-chart — token 用量图表

按月/年聚合数据生成图表图片，返回临时图片 URL（约 120 秒后自动清理）。

- 默认端口 3009（Python / FastAPI）
- 接口：`POST /chart`，body `{chart_type, data}`，`chart_type` 支持 `year` / `month`，返回 `{image_url}`（图片在 `/temp_images/<file>.png`）
- 依赖：`fastapi`、`matplotlib`、`python-dateutil`、`uvicorn`

### web-read — 网页读取

用 Puppeteer 无头浏览器抓取网页，返回标题、正文与页面链接。

- 默认端口 46799（Node / Express）
- 接口：`POST /scrape`，body `{url}`，返回 `{title, content, links}`
- 依赖：`express`、`puppeteer`（需要 Chromium，Linux 下 launcher 自动检测并补齐系统库）

### md-html-render — Markdown / HTML 渲染为图片

把 Markdown 或 HTML 渲染成图片，支持 LaTeX 公式、浅色/深色/渐变主题与宽度/质量参数。

- 默认端口 37632（Node / Express）
- 接口：
  - `POST /render/markdown`：body `{markdown, theme?, width?, quality?, hasImages?}`，返回 `{imageId, base64, ...}`
  - `POST /render/html`：body `{html, theme?, width?, quality?, hasImages?}`，返回同结构
  - `GET /images/<imageId>.png`：取图；`DELETE /images/<imageId>`：删图；`GET /health`：健康检查
- 依赖：`express`、`puppeteer`、`marked`（需要 Chromium）

### mcp-files-exec — MCP 文件与命令执行

AI 通过 MCP 协议读写文件、执行受限命令：路径沙箱（realpath 防逃逸）、危险命令拦截、可选命令白名单、审计日志、超时强杀进程树。

- 默认端口 3910（Python / FastMCP），传输 streamable-http；加 `--stdio` 切换为 stdio 传输
- Tools：`read_file`、`write_file`（支持追加）、`run_command`
- 环境变量：`MCP_SANDBOX_ROOTS`（沙箱根目录）、`MCP_ALLOWED_COMMANDS`（命令白名单）、`MCP_MAX_FILE_BYTES` / `MCP_MAX_OUTPUT_BYTES`（读写/输出上限）、`MCP_DEFAULT_TIMEOUT`（命令超时）、`MCP_LOG_FILE`（审计日志）
- 依赖：`mcp`、`uvicorn`

### ocr — OCR 图片文字识别

基于 tesseract.js 的图片文字识别，支持 URL、base64 与本地路径三种输入，可指定语言、识别模式与白名单字符，返回文本与置信度。

- 默认端口 18699（Node / Express，与海豹插件默认配置一致）
- 接口：
  - `GET /health`：健康检查（含识别统计）
  - `POST /api/ocr`：body `{url 或 imageUrl, base64?, mime?, lang?, psm?, whitelist?}`，返回 `{ok, text, confidence, ...}`
- 首次识别自动下载对应语言的 tesseract 语言包（缓存在 `lang-data/` / `cache/`，已 gitignore）
- 依赖：`express`、`tesseract.js`（Node 18+）

> 所有后端统一支持：卡片「⚙ 配置」可改端口、token 与监听 IP；设置 token 后请求需带 `Authorization: Bearer <token>` 或 `X-Token: <token>`，监听 IP 默认 `0.0.0.0`。

## 目录结构

```text
launcher.py            入口：安装 WebUI 依赖并启动管理界面
webui.py               Web 管理界面（纯 Python 标准库）
assets/                WebUI 图标
<后端目录>/            backend.json（类型/入口/依赖/默认端口）+ 服务代码
```

## 管理方式

管理全部通过 WebUI 完成：后端启停、依赖安装/删除、配置修改、运行日志都在页面里操作。端口/token/监听 IP 写入 `.runtime.json`（已 gitignore），启动时通过环境变量传给后端：`AIPLUGIN4_BACKEND_PORT`、`AIPLUGIN4_BACKEND_TOKEN`（非空时后端校验 `Authorization: Bearer <token>` 或 `X-Token: <token>`）、`AIPLUGIN4_BACKEND_HOST`。

## 命令行（aibackend）

安装 `aibackend` 命令（写入用户 PATH，重新打开终端后即可在任意目录使用）：

```bash
python install_cli.py
```

```bash
aibackend help [命令]                       # 查看帮助（如 aibackend help start）
aibackend list                              # 查看所有后端状态
aibackend start --all                       # 后台启动全部（默认后台守护）
aibackend start stream-output               # 后台启动单个
aibackend start stream-output --foreground  # 前台运行，Ctrl+C 停止
aibackend stop --all                        # 停止全部
aibackend restart stream-output             # 重启
aibackend logs stream-output -f             # 查看/跟随日志
aibackend info stream-output                # 进程详情（pid/时长/内存/拉起次数）
aibackend monitor                           # 实时监控面板
aibackend setup --all                       # 安装全部后端依赖
aibackend del-deps stream-output            # 删除单个后端依赖
aibackend update                            # 从 Git 拉取项目更新（手动）
aibackend webui                             # 后台启动 Web 管理界面（不占终端）
aibackend webui-stop                        # 停止后台 WebUI
aibackend webui-port 9000                   # 查看/修改 WebUI 端口（修改后自动重启）
aibackend webui-host 0.0.0.0                # 查看/修改 WebUI 监听地址（修改后自动重启）
aibackend webui-token                       # 查看/修改 WebUI 访问 token（reset 重新生成）
aibackend uninstall                         # 卸载 aibackend 命令（删除命令与 PATH 配置）
aibackend service-install                   # [Linux] 注册 systemd 服务（开机自启 + 自动拉起）
aibackend service-uninstall                 # [Linux] 停止并移除 systemd 服务
```

命令行与 WebUI 共用同一套后端进程与状态（`logs/state.json`），可以混用。

## MCP 文件与命令后端（mcp-files-exec）

给 AI 提供 MCP 工具：读写文件、列目录、删除文件、执行受限命令。默认 `streamable-http` 传输（端口 3910），也可 `--stdio` 本地模式（供 MCP 客户端直接拉起）。

工具：`read_file` / `list_dir` / `write_file` / `delete_file` / `run_command`。

安全设计：

- 路径沙箱：所有文件操作必须落在 `MCP_SANDBOX_ROOTS` 目录内（realpath 校验，防符号链接逃逸）
- 命令拦截：默认按危险规则拦截（rm -rf 根目录、sudo、关机、格式化、管道下载执行、fork 炸弹等）；设置 `MCP_ALLOWED_COMMANDS` 后进入白名单模式，只放行指定前缀
- 执行隔离：命令限定在沙箱工作目录内，超时强制终止进程树，输出截断
- 审计日志：每次调用（含被拦截的命令）记录到 `logs/mcp-files-exec.log`

环境变量：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MCP_SANDBOX_ROOTS` | 允许的沙箱根目录，`os.pathsep` 分隔 | 进程当前目录 |
| `MCP_ALLOWED_COMMANDS` | 命令白名单前缀，`os.pathsep` 分隔 | 未启用（危险规则拦截） |
| `MCP_MAX_FILE_BYTES` | 单文件读写上限 | 1048576 |
| `MCP_MAX_OUTPUT_BYTES` | 命令输出上限 | 1048576 |
| `MCP_DEFAULT_TIMEOUT` | 命令默认超时（秒） | 30 |
| `MCP_LOG_FILE` | 审计日志路径 | `logs/mcp-files-exec.log` |
