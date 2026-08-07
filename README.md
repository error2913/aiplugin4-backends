# aiplugin4-backends

aiplugin4 的配套后端服务：流式输出、图片转 base64、用量图表、网页读取、Markdown/HTML 渲染。

配合 [aiplugin4](https://github.com/error2913/aiplugin4) 使用：把各服务地址填入插件「后端」配置分组（见 aiplugin4 的 docs/08）。

## 快速开始

环境要求：Python 3.9+、Node.js 18+（仅 `web-read` / `md-html-render` 两个后端需要 Node）。

```bash
python launcher.py
```

直接运行 launcher 会自动检查/安装 WebUI 依赖并启动管理界面，随后自动打开 http://127.0.0.1:8910（仅本机）。所有管理都在页面里完成：

- 首次启动某后端时，按钮显示「安装依赖」：点击后创建独立 venv / 执行 `npm install`，弹窗实时显示日志、按钮转圈，装完恢复为「启动」；之后再次启动不再安装，秒开
- 有后端依赖未安装时，右上角出现「安装全部依赖」，可一键补齐
- 「启动全部」只启动依赖已就绪的后端；若全部依赖未安装会弹出提示
- 卡片显示运行时长、自动拉起次数与内存占用；「日志」旁可点「删除依赖」恢复未安装状态
- 后端进程异常退出会自动拉起

> 按需安装：每个后端只安装自己缺失的依赖，不会预装全部；依赖清单（`requirements.txt` / `package.json`）变化后会自动重新安装。Python 后端使用独立 venv，Node 后端使用各自的 `node_modules`。

> 提示：Puppeteer 需要下载 Chromium。下载慢或失败（如国内网络）时，可设置镜像 `PUPPETEER_DOWNLOAD_BASE_URL`（例如 `https://registry.npmmirror.com/-/binary/chrome-for-testing`）后重试。

## 后端列表

| 目录 | 服务 | 默认端口 | 类型 |
| --- | --- | --- | --- |
| `stream-output` | 流式输出中转（SSE 分块/轮询） | 3010 | Python |
| `image-url-to-base64` | 图片 URL 转 base64 | 46678 | Python |
| `usage-chart` | token 用量图表 | 3009 | Python |
| `web-read` | 网页 URL 内容读取 | 46799 | Node |
| `md-html-render` | Markdown/HTML 渲染为图片 | 37632 | Node |

## 目录结构

```text
launcher.py            入口：安装 WebUI 依赖并启动管理界面
webui.py               Web 管理界面（纯 Python 标准库）
assets/                WebUI 图标
<后端目录>/            backend.json（类型/入口/依赖/默认端口）+ 服务代码
```

## 管理方式

管理全部通过 WebUI 完成：后端启停、依赖安装/删除、端口修改、运行日志都在页面里操作。端口覆盖写入 `.runtime.json`（已 gitignore），后端通过环境变量 `AIPLUGIN4_BACKEND_PORT` 读取。

## 命令行（aibackend，pm2 风格）

安装 `aibackend` 命令（写入用户 PATH，重新打开终端后即可在任意目录使用）：

```bash
python install_cli.py
```

```bash
aibackend help [命令]                       # 查看帮助（如 aibackend help start）
aibackend list                              # 查看所有后端状态
aibackend start --all                       # 后台启动全部（默认后台守护，类似 pm2）
aibackend start stream-output               # 后台启动单个
aibackend start stream-output --foreground  # 前台运行，Ctrl+C 停止
aibackend stop --all                        # 停止全部
aibackend restart stream-output             # 重启
aibackend logs stream-output -f             # 查看/跟随日志
aibackend info stream-output                # 进程详情（pid/时长/内存/拉起次数）
aibackend monitor                           # 实时监控面板
aibackend setup --all                       # 安装全部后端依赖
aibackend del-deps stream-output            # 删除单个后端依赖
aibackend webui                             # 启动 Web 管理界面
```

命令行与 WebUI 共用同一套后端进程与状态（`logs/state.json`），可以混用。
