# 更新日志

标题格式为 `## <版本号>`，release 工作流按标签版本号读取对应段落作为发布说明；日常更新以「Unreleased」汇总，发版前改成版本号并补日期。

## Unreleased

- 无图形环境（无头服务器）不再尝试自动打开浏览器，只打印访问地址；Linux/macOS 仅在检测到 `DISPLAY` / `WAYLAND_DISPLAY` 时自动开浏览器
- WebUI 默认监听 `0.0.0.0`，端口与访问 token 首次运行随机生成；新增登录页（记住一年），API 需带 token
- 新增 `webui-host` / `webui-token` 命令（launcher 与 aibackend），修改后自动重启 WebUI
- launcher 启动时自动安装/刷新 aibackend 命令行（幂等）
- WebUI 自愈：监听配置不一致时自动重启，启动崩溃打印最近日志
- Linux/macOS 自动开浏览器需有图形环境且显式设置 `BROWSER`（避免 SSH -X 误开）
- WebUI 右上角新增「🔄 重启 WebUI」按钮与 `webui-restart` 命令：重启后重新加载后端清单，新增/修改后端或代码更新后无需手动重启
- 修复「⬆ 更新」按钮未携带 WebUI token 导致 401 的问题，改用统一带 token 的请求
- WebUI：「⬆ 更新」点击后转圈、更新成功 2 秒后自动重启 WebUI；后端卡片按依赖是否就绪排序；新增「🙈 隐藏未装依赖」开关（状态本地记住）
- web-read 与 md-html-render 接入 MCP（Streamable HTTP，挂 `/mcp`）：工具 `scrape_url`/`screenshot_url`、`render_markdown`/`render_html`；原 REST 路由保留兼容

## 0.9.0 - 2026-08-07

- WebUI 卡片新增「⚙ 配置」弹窗：可修改端口、访问 token 与监听 IP，一键随机生成 token；token 默认留空 = 不鉴权
- 后端支持 token 鉴权（`Authorization: Bearer <token>` / `X-Token`）与自定义监听地址（`AIPLUGIN4_BACKEND_HOST`）
- 点击更新：无可用更新时提示「没有可以更新的」，有更新时弹出本次更新日志
- WebUI 已在后台运行时输出访问链接，Windows 自动打开浏览器
- WebUI 点击更新后台执行 git pull，不再弹出控制台黑框
- aibackend Windows 命令不再弹「终止批处理操作(Y/N)?」提示
- launcher 直接运行即后台挂起 WebUI，不占用终端
- WebUI 新增「⬆ 更新」「重启全部」按钮，aibackend 新增 update 命令
- 新增 mcp-files-exec 后端：AI 读写文件与执行受限命令（沙箱 + 命令拦截）
- 新增 aibackend 命令行：启停/日志/监控/依赖管理/更新，彩色 help
- WebUI 端口支持命令行持久化修改（`webui-port`），修改后自动重启
- Linux systemd 服务注册：开机自启 + 异常自动拉起 WebUI（`service-install` / `service-uninstall`）
- Linux 下自动检测并补齐 Puppeteer/Chromium 系统库（`ldd` + apt）
- WebUI 改为每 1 秒自动刷新
- 新增更新日志（CHANGELOG）与项目知识库（AGENTS.md），README 补充后端介绍与目录
- 打包仅保留在 release workflow，不再提供 package 命令

## 0.0.9 - 2026-08-07

- aiplugin4 配套后端管理：直接启动 WebUI，后端按需安装依赖
- 后端启停/全部启停/重启全部/安装全部依赖/删除依赖/端口修改/运行日志
- 卡片展示运行时长、自动拉起次数与内存占用，异常退出自动拉起
- 使用 aiplugin4 插件包图标
- release 自动打包 zip + tar.gz 并发布
