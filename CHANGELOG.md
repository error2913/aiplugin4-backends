# 更新日志

标题格式为 `## <版本号>`，release 工作流按标签版本号读取对应段落作为发布说明；日常更新以「Unreleased」汇总，发版前改成版本号并补日期。

## 0.11.4 - 2026-08-18

- mcp-files-exec 升级到 1.0.2：后端原生工具名改为 `run_shell`，不再注册 `run_command`
- ob11-core-bridge 升级到 1.0.4：MCP 仅提供 `run_core_command`；`run_ext_command` 完全由 aiplugin4 插件本地实现

## 0.11.3 - 2026-08-18

- ob11-core-bridge 升级到 1.0.3：协议端改为中间件**出站主动连接**（移除入站 /onebot），启动时连接协议端 WS 地址并带指数退避重连（1s 起、上限 30s）；协议端未配置/不可达时持续重试并在连上后自动恢复
- 协议端地址与 token 可在 WebUI「后端 → ob11-core-bridge → ⚙ 配置」填写（协议端 WebSocket 地址 / 协议端 token），保存后重启该后端生效；token 同时以 `access_token` 查询参数与 `Authorization: Bearer` 头发送
- 不支持反向 WS：协议端无需连接中间件，全部由中间件主动出站连接

## 0.11.2 - 2026-08-18

- ob11-core-bridge 升级到 1.0.2：修复协议端（`/onebot`）未连接时，核心发来的 API 请求（如 `get_login_info`）被静默丢弃、海豹等待 10 秒超时（`context deadline exceeded`）的问题；现在直接返回 `status: failed` 并记录日志
- 中间件新增核心/协议端连接与断开日志，`/healthz` 新增 `protocolConnected` 字段，便于排障

## 0.11.1 - 2026-08-18

- ob11-core-bridge 升级到 1.0.1：移除旧版 `/control` 控制 WS 与 `AIPLUGIN4_BRIDGE_CORE_URL` 反向出站连接，保持整洁；SealDice 以 OB11 正向 WS 客户端身份连接 `/core`（或 `/core/ws`）
- 插件端统一走 MCP 的 `run_ext_command` / `run_core_command`，lane 串行与捕获逻辑不变，结果由 MCP 一次性返回

## 0.11.0 - 2026-08-18

- 新增 `ob11-core-bridge` 后端：SealDice 核心反向 WS `/core`、OB11 协议端 `/onebot` 与 MCP `/mcp` 之间的透明中转，默认端口 46880（Node）
- MCP 提供 `run_ext_command` / `run_core_command` 工具：注入假消息执行核心/扩展指令并收集响应，支持多消息捕获、lane 串行、拦截/转发、超时与断线处理
- 兼容保留旧版 aiplugin4 控制 WS（`/control`），插件端可平滑升级到 MCP（0.11.1 已移除，统一走 MCP）

## 0.10.4 - 2026-08-14

- 「启动全部 / 重启全部」增加端口占用失败提示：部分后端端口被未记录的进程占用时，toast 显示「已启动全部，N 个端口被占用未启动：name1、name2」

## 0.10.3 - 2026-08-14

- 修复卡片「⬆ 更新」点击后没有转圈反馈的问题：更新期间按钮显示「更新中」转圈，完成后恢复

## 0.10.2 - 2026-08-14

- 修复全新安装时 `installed/<名称>/backend.json` 的 `config` schema 丢失（注册表条目覆盖了程序包自带清单），导致 mcp-files-exec 默认沙箱路径不生效；现在写回清单以程序包为准、注册表字段兜底

## 0.10.1 - 2026-08-14

- mcp-files-exec 默认沙箱工作路径改到仓库根 `data/mcp-files-exec`，审计日志改到 `logs/mcp-files-exec.log`，不再落在程序目录 `installed/` 内——后端更新/重装/卸载都不会动数据
- 自定义配置支持 `{REPO_ROOT}` 模板与 `create_dir` 标记：默认路径按仓库根展开并自动创建目录；WebUI「⚙ 配置」可修改沙箱路径（`MCP_SANDBOX_ROOTS`，分号分隔多个）
- 打包与本体更新跳过 `data/`，用户数据不会被发布包覆盖

## 0.10.0 - 2026-08-14

- 后端程序不再随仓库分发：主分支只保留框架与注册表信息（`backends.json`），一条指令安装（git clone + `python launcher.py`）只装框架，不含后端程序
- 后端源码移到独立 `shop` 分支（供 release 打包与下载回退），release 工作流新增从该分支暂存源码的步骤
- 点「安装」才下载程序：按注册表版本从 GitHub release 下载对应后端独立包（`aiplugin4-backends-<名称>-<版本>.zip`）解压到 `installed/`，失败自动回退缓存/远端文件
- `backends.json` 的 `source` 回退源与 `download_backend_files` 默认源改指向 `shop` 分支

## 0.9.2 - 2026-08-14

- 修复 Node 后端安装后指纹漂移：安装完成后重算依赖指纹并写 `.install_ok`，不再出现「装完仍显示安装按钮」或漏记进程的问题
- 启动前先探测端口占用：端口已被外部进程占用时明确提示，不再让未被 WebUI 记录的孤儿进程悄悄占住端口
- state.json 改为原子写入，避免异常退出时状态文件损坏
- 「启动全部 / 重启全部」返回失败的后端列表，部分失败时提示更明确

## 0.9.1 - 2026-08-14

- 更新改为下载 GitHub release 直接覆盖，不再依赖 git（本地文件有改动也不会阻塞更新）；每个后端在 release 中有独立包并各自版本控制，后端卡片「⬆ 更新」只下载对应后端包覆盖商店并重装
- 无图形环境（无头服务器）不再尝试自动打开浏览器，只打印访问地址；Linux/macOS 仅在检测到 `DISPLAY` / `WAYLAND_DISPLAY` 时自动开浏览器
- WebUI 默认监听 `0.0.0.0`，端口与访问 token 首次运行随机生成；新增登录页（记住一年），API 需带 token
- 新增 `webui-host` / `webui-token` 命令（launcher 与 aibackend），修改后自动重启 WebUI
- launcher 启动时自动安装/刷新 aibackend 命令行（幂等）
- WebUI 自愈：监听配置不一致时自动重启，启动崩溃打印最近日志
- Linux/macOS 自动开浏览器需有图形环境且显式设置 `BROWSER`（避免 SSH -X 误开）
- WebUI 右上角新增「🔄 重启 WebUI」按钮与 `webui-restart` 命令：重启后重新加载后端清单，新增/修改后端或代码更新后无需手动重启
- 修复「⬆ 更新」按钮未携带 WebUI token 导致 401 的问题，改用统一带 token 的请求
- WebUI：「⬆ 更新」点击后转圈、更新成功 2 秒后自动重启 WebUI；后端卡片按依赖是否就绪排序；新增「🙈 隐藏未装依赖」开关（状态本地记住）
- web-read 与 md-html-render 改为仅 MCP 服务（Streamable HTTP，挂 `/mcp`）：工具 `scrape_url`/`screenshot_url`、`render_markdown`/`render_html`；REST 路由已移除
- 后端独立分发：商店 `backends/<名称>/` + 注册表 `backends.json` + 运行目录 `installed/`；安装=复制/下载程序+装依赖，卸载只删运行副本（`install-backend` / `uninstall-backend` 命令）
- 后端卡片状态机（未安装/安装中/运行中/卸载中，异步转圈+日志）、版本号显示与后端更新检查、删除后端
- 依赖精确同步：依赖清单变化自动重建 venv / `npm ci`（node 有 lockfile 时）
- 自定义后端配置：`backend.json` 声明 `config` schema（label/type/default/env），WebUI 配置弹窗编辑并注入环境变量
- WebUI 页面禁用缓存（no-store）、主题按钮移至右下角、修复 PAGE 内 JS 反斜杠转义
- 升级自动清理：launcher 启动时删除旧版顶层后端残留目录（node_modules/.venv/缓存，仅限 git 已不跟踪的旧目录）
- 「安装全部」改为批量安装所有未安装后端；移除手动 setup / del-deps 命令与对应 API（安装/卸载统一走 install-backend / uninstall-backend）
- 清理历史遗留的「错误后端」命名（统一为 aiplugin4-backends / aibackend）

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
