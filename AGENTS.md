# AGENTS.md — aiplugin4-backends 项目知识库

给 AI 代理与本仓库维护者的项目说明。改代码前先读这份文件，能省掉大量上下文。

## 项目是什么

aiplugin4 的配套后端管理仓库：一个 launcher 管理若干 HTTP/MCP 后端服务（流式输出、图片转 base64、用量图表、网页读取、Markdown/HTML 渲染、MCP 文件与命令执行），提供：

- WebUI 管理界面（`webui.py`，纯 Python 标准库，无第三方依赖）
- 命令行管理工具（`aibackend`，类似 pm2）
- 按需安装依赖（Python 后端独立 venv / Node 后端 `node_modules`，不预装全部）
- 进程守护：异常退出自动拉起，卡片展示运行时长/内存/自动拉起次数
- 发布：Git tag 触发 GitHub Actions 自动打包 zip + tar.gz 并发布（含更新日志）

## 快速启动

```bash
python launcher.py                     # 自动装依赖 + 自动安装 aibackend，后台启动 WebUI（默认 0.0.0.0，端口与访问 token 首次随机生成）
python launcher.py webui-stop          # 停止后台 WebUI
python install_cli.py                  # 安装 aibackend 命令（Windows 生成 .cmd，Linux 写入 shell 配置）
aibackend help                         # 查看所有命令
```

## 组件与文件

| 文件 | 职责 |
| --- | --- |
| `launcher.py` | 核心入口：后端发现/启停/依赖安装/运行时配置/WebUI 启停/更新/打包/systemd 服务 |
| `webui.py` | Web 管理界面：内嵌 HTML/CSS/JS（`PAGE` 常量）+ 标准库 HTTP 服务 |
| `aibackend.py` | 命令行工具（复用 launcher 逻辑），彩色 help |
| `install_cli.py` | 安装 `aibackend` 到 PATH（Windows `.aibackend\bin\aibackend.cmd` / Linux shell 脚本）；launcher 启动时自动调用 `install()` 幂等刷新 |
| `backends.json` | 后端注册表：`name` / `type` / `entry` / `deps` / `port` / `version` / `source`（raw URL）/ `files` / 可选 `config` schema |
| `backends/<名称>/` | 后端商店源码（`backend.json` + 服务代码，随仓库分发，发布包不含） |
| `installed/<名称>/` | 已安装后端运行副本（程序 + 依赖，gitignore，卸载即删） |
| `CHANGELOG.md` | 更新日志：`## <版本号>` 段落，release 与更新弹窗都从这里取 |
| `VERSION` | 当前版本号（release 时由 tag 写入） |
| `launcher.json` | 全局配置：`auto_restart`、`restart_backoff_seconds`、`log_dir` |
| `webui-requirements.txt` | 可选；不存在则 WebUI 视为纯标准库、跳过安装 |

## 数据与状态文件（logs/ 与根目录，均 gitignore）

- `.runtime.json`：每后端运行时配置 `config/<name> = {port, token, host}`；`ports` 为旧版字段（读兼容、写同步）；`webui` 段含 `port`（首次随机生成五位数）/`host`（默认 `0.0.0.0`）/`token`（首次自动生成）。读写统一走 `launcher.backend_config()` / `save_backend_config()` / `configure_webui_port()` / `configure_webui_host()` / `configure_webui_token()`。
- `logs/state.json`：Supervisor 进程状态（`pid`、`started_at`、`restarts`、`stopped` 标记）。
- `logs/webui.pid`：后台 WebUI 进程号，格式 `pid host port`（自愈用）。
- `logs/<backend>.log`：各后端日志；`logs/webui.log`：WebUI 日志。

## 关键流程

### 一键启动（`python launcher.py`）
`ensure_cli_installed()`（自动安装/刷新 aibackend，幂等）→ `ensure_webui_deps()`（无 webui-requirements.txt 时为空操作）→ `start_webui_background()`：检测 pid 文件避免重复启动，pid 里记录的 host/port 与当前配置不一致时自动重启（自愈），启动崩溃打印最近日志；detach 子进程（Windows `DETACHED_PROCESS | CREATE_NO_WINDOW`，Linux `start_new_session`）；WebUI 默认监听 `0.0.0.0`，端口与访问 token 首次运行随机生成；自动开浏览器统一走 `_can_open_browser()`——Windows 直接开，Linux/macOS 需有 `DISPLAY`/`WAYLAND_DISPLAY` **且**显式设置 `BROWSER`，无头服务器只打印访问地址（不调用 xdg-open）。

### 后端启动（`Supervisor.spawn`）
安装后端（`install_backend`）：按注册表把商店 `backends/<name>` 的文件复制到运行目录 `installed/<name>`（商店缺失时按 `source` 从 raw URL 下载），再装依赖；失败自动清掉半成品。卸载（`remove_backend_dir`）只删 `installed/<name>`。依赖精确同步：`ensure_venv`/`ensure_node` 用依赖指纹（`deps_hash`/`node_deps_hash`）判断，清单变化即重建 venv / `npm ci`（node 有 lockfile 时）。启动时注入 `AIPLUGIN4_BACKEND_PORT / _HOST / _TOKEN` 与 `backend.json` `config` schema 声明的自定义 env → 子进程日志重定向到 `logs/<name>.log`，`CREATE_NO_WINDOW`。`_monitor` 线程负责异常退出后按退避时间自动拉起；手动停止写入 `stopped` 标记则不再拉起。

### 后端 token/监听 IP
后端读取 `AIPLUGIN4_BACKEND_HOST`（默认 `0.0.0.0`）与 `AIPLUGIN4_BACKEND_TOKEN`（默认空）。token 非空时校验请求头 `Authorization: Bearer <token>` 或 `X-Token: <token>`，否则 401。六个后端均已实现（Flask/FastAPI 中间件、express 中间件、mcp-files-exec 的 ASGI 包装）。

`web-read` 与 `md-html-render` 仅提供 **MCP（Streamable HTTP，挂 `/mcp`，每会话一个 McpServer 实例）**，已移除 REST 路由：工具为 `scrape_url` / `screenshot_url`（web-read）、`render_markdown` / `render_html`（md-html-render，返回 PNG base64 文本）；token 中间件对 `/mcp` 同样生效。注意 `/mcp` 必须在 `express.json()` 之前注册，让 transport 自行解析原始 body。

### 更新（`aibackend update` / WebUI「⬆ 更新」）
`launcher.update_project()`：记录旧 HEAD → `git pull --ff-only` → HEAD 未变则 `updated=False`（前端弹「没有可以更新的」）；有更新则用 `_update_changelog()` 收集 CHANGELOG.md 里「旧 HEAD 没有且高于当前 VERSION」的版本段落，取不到则退回 `git log`。

### Linux systemd 服务
`python launcher.py service-install`（或 `aibackend service-install`）：停止旧后台 WebUI → 生成 unit（前台跑 `launcher.py webui --no-browser`，`Restart=always`）→ `systemctl enable --now`。非 root 自动加 sudo；无 systemctl（SysV/Upstart/OpenRC）时明确报错退出。

### 发布
Git tag `v*` 触发 `.github/workflows/release.yml`：tag 去掉 `v` 写进 VERSION → `launcher.py package` 生成 `dist/aiplugin4-backends-<version>.zip/.tar.gz` → 从 CHANGELOG.md 按版本号提取段落作为 release 描述。发版前记得把 CHANGELOG 的 `Unreleased` 改成版本号并补日期。

## WebUI API

所有 API 均需 WebUI 访问 token（登录页输入，记住一年；请求带 `Authorization: Bearer <token>` 或 `X-Token: <token>`，否则 401）。token 由 `.runtime.json` 的 `webui.token` 提供，`launcher.py webui-token` / `aibackend webui-token` 可查看/修改/重新生成。

- `GET /api/backends`：卡片数据（已安装项 + 注册表未安装项，含 `version` / `installed` / `running` / `uptime_secs` / `restarts` / `mem_*` / `deps_ready` / `options`）
- `POST /api/install/<name>`、`POST /api/uninstall/<name>`、`GET /api/setup-log/<name>`：商店安装/卸载/日志轮询（异步）
- `POST /api/backend-update/<name>`：按注册表更新后端程序与依赖
- `POST /api/webui-restart`：重启 WebUI（响应先返回，随后由独立进程执行 `launcher.py webui-restart`，用于重新加载后端清单）
- `GET|POST /api/config/<name>`：查询/保存 {port, token, host}
- `POST /api/port/<name>`、`/api/port/<name>/reset`：旧版端口接口（写同一份配置，保留兼容）
- `POST /api/setup/<name>`、`/api/deps-delete/<name>`、`GET /api/setup-log/<name>`：依赖安装/删除/日志轮询
- `POST /api/start-all` / `stop-all` / `restart-all` / `start/<name>` / `stop/<name>`
- `POST /api/update`：更新，返回 `{ok, updated, changelog, output}`
- `GET /api/logs/<name>`：后端日志（末 300 行）

## 开发约定

- WebUI 必须保持纯标准库（不引入 webui-requirements.txt）；后端依赖只在各自 venv/node_modules 按需安装，不要手动装。
- 新增后端 = 新建目录 + `backend.json`（含默认端口）+ 入口脚本读取 `AIPLUGIN4_BACKEND_PORT`；如需 token/监听 IP 支持，按上面六后端的模式接入 `AIPLUGIN4_BACKEND_TOKEN/_HOST`。
- 新增后端要走「商店 + 注册表」：源码放 `backends/<名称>/`，并在 `backends.json` 注册（`files` 列出所有需要分发的文件、`version`、`source` raw URL）；未注册的后端不会被 WebUI/CLI 发现与安装。
- 确保不弹黑框：任何在 WebUI/后台（无控制台）进程里执行的子进程调用，Windows 下必须带 `CREATE_NO_WINDOW`——统一用 `launcher._no_window_kwargs()` 注入，新增 git 命令一律走该辅助函数；改完用 `rg -n "subprocess"` 排查所有调用点，逐处确认。
- 日志统一 UTF-8：子进程环境加 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`（同 `Supervisor.spawn` 的写法）。
- 运行时配置读写只通过 `launcher.backend_config()` / `save_backend_config()`，不要直接改 `.runtime.json` 结构。
- 新增「重启 WebUI」入口时三处同步：WebUI 页面按钮（`POST /api/webui-restart`）+ `launcher.py webui-restart` + `aibackend.py webui-restart`。
- 命令行与 WebUI 共用同一套后端进程与状态（`logs/state.json`），改动两处入口都要同步（如新增子命令：`launcher.py` + `aibackend.py` + README）。
- 平台差异：Windows 与 Linux 行为保持一致；系统服务类命令在非 Linux 平台提示「仅支持 Linux」并不展示在 help（`aibackend` 的 cmd_help 按 `os.name` 过滤）。
- 更新日志：改动记录进 CHANGELOG.md（日常写在 `Unreleased`）。

## 常用排查

- 后端起不来：看 `logs/<name>.log`；依赖没装完看 WebUI 安装日志（`/api/setup-log/<name>`）。
- 端口被占/生效的是旧值：`.runtime.json` 里 `config/<name>.port` 覆盖默认端口，改完需重启后端。
- WebUI 黑框：后台场景子进程缺 `CREATE_NO_WINDOW`；点击更新弹黑框是 `update_project()` 的 `git pull` 缺该标志。
- aibackend 命令失效：重新跑 `python install_cli.py`（改 shim 生成逻辑后必须重装）。
