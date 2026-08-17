# aiplugin4-backends

aiplugin4 的配套后端服务：流式输出、图片转 base64、用量图表、网页读取、Markdown/HTML 渲染、SealDice 核心/OB11 指令中转。

配合 [aiplugin4](https://github.com/error2913/aiplugin4) 使用：把各服务地址填入插件「后端」配置分组（见 aiplugin4 的 docs/08）。

## 目录

- [快速开始](#快速开始)
- [跨平台](#跨平台)
- [Linux 系统服务](#linux-系统服务)
- [后端文档](#后端文档)
- [目录结构](#目录结构)
- [管理方式](#管理方式)
- [命令行（aibackend）](#命令行aibackend)

## 快速开始

环境要求：Python 3.9+、Node.js 18+（`web-read` / `md-html-render` / `ob11-core-bridge` 三个后端需要 Node）。

```bash
git clone https://github.com/error2913/aiplugin4-backends.git && cd aiplugin4-backends && python launcher.py
```

首次运行即自动安装所需依赖、自动安装 `aibackend` 命令行并**在后台启动**管理界面（不占用终端、无控制台窗口，launcher 启动完成后立即退出）。WebUI 默认监听 `0.0.0.0`（全部网卡），端口与访问 token 首次运行随机生成并保持稳定，打开页面后输入 token 登录（记住一年）。自动开浏览器只在有图形环境时进行（Windows 直接开；Linux/macOS 需检测到 `DISPLAY` / `WAYLAND_DISPLAY` 且显式设置 `BROWSER`），无头服务器只打印访问地址。停止后台 WebUI：`python launcher.py webui-stop` 或 `aibackend webui-stop`。所有管理都在页面里完成：

- 一条指令只安装框架与后端注册表信息（`backends.json`），**不含后端程序**；每个后端在 release 中有独立包、各自版本控制，未安装时卡片显示「安装」，点击才按版本从 GitHub release 下载程序到运行目录 `installed/` 并安装依赖（弹窗实时日志、按钮转圈），装完变「启动」；「卸载」只删运行副本，下载缓存不动
- 依赖精确同步：依赖清单（`requirements.txt` / `package.json`）变化后自动重建 venv / `npm ci`，保证依赖不多不少
- 有后端未安装时，右上角出现「安装全部」，可批量安装（复制/下载程序 + 装依赖）
- 「启动全部」只启动依赖已就绪的后端；若全部依赖未安装会弹出提示
- 「重启全部」先停止全部，再启动依赖已就绪的后端
- 右上角「⬆ 更新」更新本体（检测到新版 release 才更新，直接下载压缩包覆盖，不依赖 git）；每个后端在 release 中有独立包、各自版本控制，卡片出现「⬆ 更新」时只更新该后端
- 卡片显示版本、运行时长、自动拉起次数与内存占用；「卸载」可删除运行副本恢复未安装状态
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

## 后端文档

后端清单、各后端的接口与依赖、MCP 安全模型详见 [docs/后端.md](docs/后端.md)。

## 目录结构

```text
launcher.py            入口：安装 WebUI 依赖并启动管理界面
webui.py               Web 管理界面（纯 Python 标准库）
assets/                WebUI 图标
backends.json          后端注册表（名称/类型/端口/版本/下载源/文件清单）
backends/<名称>/       后端程序缓存（点安装/更新时下载解压到这里，gitignore，源码在独立 shop 分支）
installed/<名称>/      已安装后端运行副本（程序 + 依赖，gitignore，卸载即删）
data/<名称>/           后端数据目录（如 mcp-files-exec 的沙箱工作路径，gitignore，更新/卸载不删除）
```

## 管理方式

管理全部通过 WebUI 完成：后端启停、依赖安装/删除、配置修改、运行日志都在页面里操作。端口/token/监听 IP 写入 `.runtime.json`（已 gitignore），启动时通过环境变量传给后端：`AIPLUGIN4_BACKEND_PORT`、`AIPLUGIN4_BACKEND_TOKEN`（非空时后端校验 `Authorization: Bearer <token>` 或 `X-Token: <token>`）、`AIPLUGIN4_BACKEND_HOST`。

后端专属配置（如 mcp-files-exec 的沙箱工作路径）在卡片「⚙ 配置」弹窗中修改；数据默认放仓库根 `data/<名称>/`，更新与卸载不会删除。

命令行安装/卸载后端：`python launcher.py install-backend <名称>` / `uninstall-backend <名称>`（aibackend 同样支持）；安装按注册表版本从 release 下载独立包（失败自动回退缓存/远端文件）再装依赖，卸载停止进程并删除 `installed/<名称>`。

右上角「🔄 重启 WebUI」可让管理界面重新加载后端清单（新增/修改后端、代码更新后无需手动重启进程）；命令行等价 `launcher.py webui-restart` / `aibackend webui-restart`。

「⬆ 更新」点击后按钮转圈，更新成功会在 2 秒后自动重启 WebUI 使新代码生效；后端卡片按「依赖已装 → 未装」排序，右上角「🙈 隐藏未装依赖」可只显示依赖就绪的后端。

## 更新

- **本体更新**：WebUI 右上角「⬆ 更新」或 `aibackend update`。检测 GitHub 最新 release（`aiplugin4-backends-<版本>.zip`），比本地版本新就下载并直接覆盖仓库文件，不依赖 git——本地文件有改动也不会阻塞更新；更新成功后自动重启 WebUI 使新代码生效。
- **后端更新**：每个后端在 release 里有独立包（`aiplugin4-backends-<后端名>-<版本>.zip`），版本各自独立记录在 `backends.json`。注册表版本高于本地版本时，卡片出现「⬆ 更新」按钮，点击只下载对应后端独立包并重装程序与依赖，不影响其他后端；或 `uninstall-backend` 后重新 `install-backend`。
- **升级残留**：旧版（后端位于仓库顶层）升级到商店模型后，顶层目录里的 `node_modules/`、`.venv/`、缓存等未跟踪残留会自动清理——仅 git 部署时 launcher 启动会检测并整目录删除「git 已不再跟踪」的旧后端目录，商店 `backends/`、`installed/`、`logs/` 与运行配置不受影响。

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
aibackend install-backend web-read          # 安装后端（复制/下载程序 + 安装依赖）
aibackend uninstall-backend web-read        # 卸载后端（停止并删除运行副本）
aibackend update                            # 从 GitHub 更新到最新版（手动，不依赖 git）
aibackend webui                             # 后台启动 Web 管理界面（不占终端）
aibackend webui-stop                        # 停止后台 WebUI
aibackend webui-restart                     # 重启后台 WebUI（重新加载后端清单）
aibackend webui-port 9000                   # 查看/修改 WebUI 端口（修改后自动重启）
aibackend webui-host 0.0.0.0                # 查看/修改 WebUI 监听地址（修改后自动重启）
aibackend webui-token                       # 查看/修改 WebUI 访问 token（reset 重新生成）
aibackend uninstall                         # 卸载 aibackend 命令（删除命令与 PATH 配置）
aibackend service-install                   # [Linux] 注册 systemd 服务（开机自启 + 自动拉起）
aibackend service-uninstall                 # [Linux] 停止并移除 systemd 服务
```

命令行与 WebUI 共用同一套后端进程与状态（`logs/state.json`），可以混用。
