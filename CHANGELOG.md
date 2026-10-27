# 更新日志

标题格式为 `## <版本号>`，release 工作流按标签版本号读取对应段落作为发布说明；日常更新以「Unreleased」汇总，发版前改成版本号并补日期。

## 0.12.13 - 2026-10-27

- `image-host 1.0.16`：模型变体图源（`/m/<id>?max=`、`/m/<id>/b64`）+ `gif_mode`（默认 `first_frame`：gif 给模型时取首帧转 webp/jpeg，原图仍按原样分发给 QQ）+ `gif-first-frame` capability + `/api/meta` 元数据。
  - 新增 `model_variant.py`（纯 PIL：尺寸钳制 512–4096、alpha 处理、首帧策略）；**升级需把该文件一起部署**（见注册表 `files`）。
  - 入库预热覆盖超限图片（含 gif 首帧）；数据库 schema 升到 v4（`blob.w/h` + `variant` 表，级联删除、容量统计含变体），升级后自动迁移。
  - `backend.json` 新增 `gif_mode` 配置项（env `AIPLUGIN4_IMAGE_HOST_GIF_MODE`）。
- `image-url-to-base64 1.0.2`：`max_side` 可选参数（长边缩放、bmp→png、GIF 首帧导出），响应附带 `mime`/`width`/`height`/`resized`；`max_side<=0` 保持旧行为。

## 0.12.12 - 2026-10-20

- `image-host 1.0.15`：
  - `ref` 增加 `tool` / `server` / `via` 元数据；上传接口接收 `tags` 与来源，用于工具产出图片的英文类型标签与来源检索。
  - MCP 增加到 6 个工具：`image_search`（`tool`/`server`/`platform`/`group_id`/`since`/`tags`/`scope` 过滤）、`image_keep`、`image_unkeep`（新增）、`image_rename`、`image_tag`、`image_tags`；`keep`/`unkeep`/`tag` 单次 ≤50 个 id。
  - 工具元数据标签（`来源:`/`群:`/`工具:`/`MCP:`/`类型:` 前缀、纯群号/工具名/时间/文件名等）拒绝写入；来源展示不再出现“未知”。
  - WebUI / MCP 的搜索、标签清单、来源与工具字段同步更新。
- 「📋 复制 MCP 配置」为 image-host 自动写入 `imageRefs: true`：插件据此把 `[img:<sha>]` 规范化为 `[img:h<sha>]` 并注册虚拟图。
- 发布闸门：后端单测改为跑 `shop` 分支 image-host 全量测试（新增 upload / store v3 / MCP 工具用例，共 57 项），避免新逻辑漏测。

## 0.12.11 - 2026-10-20

- 后端配置导出：`stream-output` / `image-url-to-base64` / `media-fetch` / `usage-chart` 的 token 现在一并导出（注册表 `pluginConfig.backend.tokenFrom: "backend"`）；token 留空时照旧省略。
- `media-fetch 1.0.1`：上游 4xx 且响应非 JSON 时返回 `bodyText`，供插件错误分类/展示上游原文。
- `usage-chart 1.0.1`：`/chart` 同时返回 `image_url` 与 `base64`（插件落本地图，访问 token 不需要进图片 URL）。
- 升级提示：更新 `media-fetch` / `usage-chart` 两个后端后重启；插件侧需重新「复制后端配置」以带上 token。

## 0.12.10 - 2026-10-20

- 「📋 复制后端配置」新增「图床对外地址」（只作用于 image-host）：
  - 导出的 `backends["image-host"]` 现在同时包含 `url`（插件访问图床接口的地址，可为 `127.0.0.1`）与 `publicUrl`（**QQ 协议端与模型都能访问的 IP 或域名**），复制后不需要再手动改 JSON；
  - 「图床对外地址」留空时自动探测本机局域网 IP；填了回环地址（127.0.0.1/localhost）会给出警告且不导出 `publicUrl`（插件侧会降级为发图用本地文件、模型用内联 base64）。

## 0.12.9 - 2026-10-20

- image-host 1.0.14：`image_search` 返回值直接附 `[img:<id>]`
  - 工具描述明确「返回的 id 可直接用 [img:ID] 发送」；每条结果末尾附 `[img:ID]`，AI 不用再自己拼；
  - 配合插件 4.27.0：`[img:图床ID]` 会解析为图床稳定地址的图片段（不落插件 storage、不重复上传）。

## 0.12.8 - 2026-10-20

- image-host 1.0.13：WebUI 顶部搜索框合并
  - 去掉独立的「标签筛选」输入框，只保留一个搜索框（名称 / 标签 / ID / 群号统一模糊搜索）；
  - 点「标签」视图里的标签卡片 = 把标签名填进搜索框并回到图库；
  - 来源筛选（全部 / 插件抓的 / 手动上传）保留不变。

## 0.12.7 - 2026-10-20

- image-host 1.0.12：`image_search` 区分长期图库与插件抓取
  - 默认 `scope=gallery`，只搜 `origin=manual`（WebUI 手动上传 / 「收进图库」的图片）；
  - 插件自动抓取的图片需显式传 `scope=plugin`（只看抓取）或 `scope=all`（全部）；
  - 返回值标注 `scope`；图库为空时提示如何查看插件抓取；非法 scope 直接报错；
  - 新增 `test_mcp_search.py` 覆盖默认/三种 scope/schema。

## 0.12.6 - 2026-10-20

- image-host 1.0.11：WebUI「加标签」的已有标签气泡修复
  - 原因：标签建议缓存把一次 401/失败的结果（`[]`）永久缓存；后端改 token/重启后旧会话失效，点「+ 标签」会先 401，登录回来后仍显示「没有可用标签」。
  - 修复：错误不再缓存、空结果不缓存、缓存 5 秒过期；登录成功、增删标签后清缓存；加载失败显示原因 + 重试按钮。
  - 点击建议气泡不再因输入框 blur 提前收起（焦点只在移出编辑器时关闭）。
- 后端配置导出行为同 0.12.5（image-host 缺 token 时跳过）。

## 0.12.5 - 2026-10-20

- 插件配置导出修正：`image-host` 未设置访问 token 时，**后端配置与 MCP 配置都不再导出该项**（excluded 原因 `未设置访问 token`，并给出警告）。
  - 原因：image-host 的写接口与 `/mcp` 都必须带 token；未设置时导出的条目必然 401，表现为插件日志「MCP 服务器 image-host 注册失败: 服务器未初始化：unauthorized」。
  - 处理：启动器 image-host 卡片「⚙ 配置」设置 Token（与插件图床 token 一致）并**重启后端**，再重新「复制后端配置」/「复制 MCP 配置」。
- `backends.json` 元数据未变；更新单测覆盖「缺 token 时跳过 + excluded 原因」。

## 0.12.4 - 2026-10-20

- 后端侧复查修正：
  - `aibackend webui-restart/stop` 与 `launcher.py webui-restart/stop` 现在**优先识别 systemd 服务 `aiplugin4-webui`**：托管时走 `systemctl restart/stop`，不再“杀进程 + 自己起后台”与 systemd 抢端口/留孤儿进程；按端口清理只作为非 systemd 部署的兜底。
  - 复制成功 toast 现在会带生成警告（例如 image-host 未设置 token），不再只在「👁 预览」里可见。
  - 发布 workflow 增加 shop 后端单测（i2b64 / image-host 的 URL HTML 实体解码），与主仓库单测一起作为发版闸门。
- 复查确认：MCP 导出 4 个后端（含 image-host）、默认不写 `platform`；后端导出 6 个项目；注册表版本与 shop 分支一致。

## 0.12.3 - 2026-10-20

- 修正「复制 MCP 配置」：
  - **不再给 `md-html-render` 写 `platform: ["QQ"]`**。平台白名单不应由启动器替用户决定；现在默认不写 `platform`，所有平台可用，需要限制时粘贴后自行加。
  - **补回 `image-host` 的 MCP 配置**（`http://<host>:46680/mcp`）。它的 `/mcp` 需要访问 token，未设置时导出省略 headers 并给出警告。
  - MCP 导出现在覆盖 4 个已安装后端：`mcp-files-exec` / `md-html-render` / `mcp-browser` / `image-host`。
- 同步 README、`docs/后端.md`、`docs/插件配置导出方案.md` 与单测。
- 升级方式：`aibackend update` → `aibackend webui-restart`（若仍报端口占用，先 `fuser -k <端口>/tcp` 或用 `systemctl restart aiplugin4-webui`）。

## 0.12.2 - 2026-10-20

- 修复 `aibackend webui-restart` / `aibackend webui-stop` 在**没有 `logs/webui.pid`**（systemd 托管或前台运行）时不会结束占用 WebUI 端口的旧进程：重启时新进程报 `OSError: [Errno 98] Address already in use`，旧页面继续运行。现在与 `python launcher.py webui-restart` 一致——先按端口找到并结束旧 launcher/webui 进程；检测到 systemd（`INVOCATION_ID`）时交给 systemd 拉起，不抢端口。
- 新增 `tests/test_aibackend_webui.py` 覆盖「无 pid → 按端口清理」「systemd → 不自行拉起」「webui-stop 同样清理」三种情况。
- 升级 0.12.1 / 0.12.0 时如遇重启报端口占用：先 `aibackend update` 到 0.12.2，再 `aibackend webui-restart`；若仍失败，手动 `fuser -k <端口>/tcp`（或 `ss -ltnp | grep <端口>` 找 PID 后 kill）再重启。

## 0.12.1 - 2026-10-20

- **修复 0.12.0 的 WebUI 无法登录**：`webui.py` 内嵌 JS 中的 `join('\n')` 被 Python 三引号字符串提前转义成真实换行，整个 `<script>` 语法错误，输入 token 后页面无反应（也无法从页面点更新）。已改用 `String.fromCharCode(10)`。
- 新增 **发布闸门**：`launcher.py package` 打包前从**运行期** `PAGE` / `SAFE_PAGE` 提取内嵌脚本执行 `node --check`，语法错误直接拒绝打包；release workflow 在打包前先跑 `python3 -m unittest discover -s tests -t . -v`（含内嵌 JS 语法测试）。
- 新增 **安全模式页 `/safe`**：不依赖主页面脚本，输入 WebUI token 后可直接「更新本体」「重启 WebUI」，用于页面脚本损坏时自救（主页面页脚也有入口）。页面无需登录即可打开，但操作仍校验 token。
- 新增 `tests/test_webui_page.py`：直接校验运行期 `PAGE` / `SAFE_PAGE` 的脚本语法，防止 Python 转义问题再次漏过。
- **升级提示**：已装 0.12.0 的机器若 WebUI 输入 token 后无反应，用命令行升级：`aibackend update`（或 `python launcher.py update`），再 `aibackend webui-restart`，浏览器 Ctrl+F5；升级后可用 `/safe` 兜底。

## 0.12.0 - 2026-10-20

- 新增「插件配置导出」：WebUI 工具栏 `📋 复制后端配置` / `📋 复制 MCP 配置` / `👁 预览`，一键生成插件可直接粘贴的 JSON（只含已安装的后端项目；预览可改访问主机、查看纳入/排除项、下载 `.json`；局域网 `http` 下剪贴板 API 不可用时自动降级）。新增 API `GET /api/plugin-config`、`GET /api/mcp-config`，CLI `aibackend plugin-config` / `aibackend mcp-config`（`--host`、`--pretty`）。
- `backends.json` 新增可选 `pluginConfig` 元数据：`backend`（`scheme`/`path`/`tokenFrom`/`tokenRequired`）与 `mcp`（`path`/`platform`）两组；未声明者不参与导出。后端配置覆盖 stream-output / image-url-to-base64 / media-fetch / usage-chart / image-host / ob11-core-bridge；MCP 配置覆盖 mcp-files-exec / md-html-render / mcp-browser（`md-html-render` 默认 `platform: ["QQ"]`）。
- **image-url-to-base64 1.0.1**：修复 QQ 官方 bot 图片 URL 中 HTML 转义 `&amp;` 未解码导致上游 400（#3）。只还原 `&amp;` / `&#38;` / `&#x26;`（不破坏 `&notify=1`）；下载加 `(10, 30)` 秒超时；4xx 不再重试；错误区分上游 4xx / 网络错误 / 自身异常，响应带 `upstream_status`，日志与响应不回显完整 URL/rkey。
- **image-host 1.0.10**：`safe_fetch()` 与重定向 `Location` 同样还原 HTML 实体，修复同一类 QQ 图片 URL 抓取 400。
- **ob11-core-bridge 1.1.1**：新增「插件控制 WS token」配置项（`plugin_token` → `AIPLUGIN4_BRIDGE_PLUGIN_TOKEN`），可在卡片「⚙ 配置」直接填写，对应插件「后端 → 核心桥Token」，无需手工设环境变量。
- 新增 `tests/`（标准库 unittest，覆盖后端配置 / MCP 配置导出）与 `docs/插件配置导出方案.md`；README、`docs/后端.md`、`AGENTS.md` 同步。
- 插件侧（aiplugin4）的「后端配置」JSON 导入与论坛页签拆分尚未实施，等下一版；当前导出的 JSON 已按最终契约冻结。
- **升级提示**：已安装 image-url-to-base64 / image-host / ob11-core-bridge 的用户，卡片出现「⬆ 更新」后点击更新（或卸载重装）才会拿到本次修复与新配置项。

## 0.11.26 - 2026-10-19

- image-host 升级到 1.0.9：WebUI 每张图片新增 **「详情」** 弹层，卡片上被截断/放不下的信息一次看全——完整 sha256 ID、格式/MIME、大小（字节）、缩略图大小、创建时间、最后访问时间、回收站状态、原图地址、b64 出口地址、**全部标签**、**全部来源记录**（平台/群/用户/消息 id/会话/时间/原始链接），底部带「复制链接 / 复制 b64 链接 / 复制 ID」。
- 配套：`/api/list` 的 `sources` 补上 `session_id` 与 `raw_url`，并新增 `mime` / `thumb_size` 字段；回收站里的卡片也能看详情。

## 0.11.25 - 2026-10-19

- image-host 升级到 1.0.8：**同哈希去重的清理适配**。同一张图被多次收发时仍只存一份字节（sha256 内容寻址 + `ref` 引用计数），本次修掉去重场景下的一个清理漏洞：此前若 blob 先由插件抓取（`origin=plugin`），用户之后在 WebUI 手动上传/存图库同一张图，blob 的 origin 不会提升，容量压力下仍会被自动淘汰，违反「手动上传的图永不自动删」。现在**去重命中且来源为 manual 时把 blob 提升为 manual**；插件来源再上传同字节则保持 manual（受保护）。
- 新增 `backends/image-host/tests/test_dedupe_cleanup.py`：直接对 Store 做回归（24 项断言），覆盖同哈希只存一份/磁盘只有一个 blob 文件、逐条 release 不提前释放字节、零引用后保留、去重时 manual 提升、容量压力只淘汰插件图、只剩手动图时返回 507、共享 ref 的软删/恢复/彻底删除与 `change_log` 的 `delete_ref`、彻底删除后重传为新 blob。

## 0.11.24 - 2026-10-19

- 修复后端卡片「配置」里**布尔配置被渲染成文本框、要手输 true/false** 的问题：`backend.json` 中 `type: "boolean"` 现在渲染为**开关**（开/关），`type: "tristate"` 渲染为**自动/开启/关闭**下拉框；保存统一提交 `1`/`0`（三态的空字符串 = 自动），服务端做校验与归一化，`{REPO_ROOT}` 展开时布尔默认值也归一化为 `1`/`0`
- 配套后端：**mcp-browser 1.0.5**（`browser_headless` 默认改为开、`browser_chromium_sandbox` 改为三态控件）、**image-host 1.0.7**（WebUI 开关改为开关控件、默认开）；mcp-files-exec 的两个布尔项（默认开）无需改程序，新控件直接生效

## 0.11.23 - 2026-10-19

- 修复 WebUI「⬆ 更新」/「🔄 重启 WebUI」在 **systemd 或前台直接运行**（没有 `logs/webui.pid`）时不生效的问题：旧 WebUI 进程一直占着端口，新进程 `Address already in use` 后退出，页面继续跑旧代码（表现为更新成功但界面/新功能不出现）
- 重启逻辑现在会在拿不到 pid 文件时，通过 `ss`（Linux，失败回退 `/proc`）或 `netstat -ano`（Windows）找到监听 WebUI 端口的进程，校验命令行包含 `launcher.py` / `webui.py` 后再结束，避免误杀其他服务；systemd 托管时由 `Restart=always` 拉起新代码，手动/前台部署则自行拉起新进程
- 修复后，systemd 部署下直接点「🔄 重启 WebUI」或更新后的自动重启即可加载新代码，不再需要手动登录服务器处理端口占用

## 0.11.22 - 2026-10-19

- 后端卡片新增「🌐 WebUI」按钮：后端在 `backend.json` / `backends.json` 声明 `webui`（`path` / `title` / `proxy` / `inject_token`）后，已安装且运行中的卡片会出现按钮，点击在新标签打开后端自带的 WebUI
- 新增内置反向代理 `/ui/<后端名>/`（纯标准库、流式转发）：只允许本机、已登记、运行中的后端；透传 GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS，自动处理 `Location` 与 `Set-Cookie` 的 `Path` 改写，支持 Range/206、大文件上传与缩略图流式下载；后端 WebUI 只要使用相对 URL（或以 `document.baseURI` 解析）即可经 launcher 打开，**无需把后端端口暴露到公网**
- 反代会话鉴权：launcher 登录后由 API 响应下发仅作用于 `/ui` 的 HttpOnly cookie（值由 WebUI token 派生，换 token 自动失效）；launcher 自身 `/api/*` 仍只认 header token，不降级
- 修复后端卡片「⚙ 配置」弹窗内容区无法上下滚动：弹窗内容区改为可收缩 + `overflow-y:auto`，配置项较多时（如 image-host）可正常滚动到底、底部保存按钮固定可见；日志弹窗同时受益
- image-host 升级 1.0.6：WebUI 前端改用基于 `document.baseURI` 的相对 URL（直接访问 / 经 launcher 反代都可正常工作），并在 `backend.json` 声明 `webui`，卡片可直接打开图床 WebUI

## 0.11.21 - 2026-10-19

- image-host 升级到 1.0.5：**移除「对外地址前缀」与「抓取域名白名单」**——接口返回的 `url` / `thumb_url` / `b64_url` 一律是相对路径（`/i/<id>`），插件自己拼后端地址即可；需要公网域名时在**插件侧**配置，后端不再掺和。抓取只保留基础 SSRF 防护（私网 IP、逐跳重定向校验），不做域名白名单。卡片配置收敛为 **6 项**（数据目录 / WebUI 开关 / WebUI 密码 / 容量上限 / 单图上限 / 回收站保留天数）。

## 0.11.20 - 2026-10-19

- image-host 升级到 1.0.4：配置项精简为 **7 项** —— 数据目录、**WebUI 开关**（`on`/`off`，重启生效）、**WebUI 密码（默认 `123456`）**、对外地址前缀（可选）、容量上限、单图上限、回收站保留天数。移除卡片里的变更日志保留天数、缩略图最大边、抓取并发/超时/域名白名单（仍可用同名环境变量做进阶覆盖）；对外地址前缀改为明确可选并说明用途。

## 0.11.19 - 2026-10-19

- image-host 升级到 1.0.3：未设置 WebUI 密码时 `GET /` 直接返回「WebUI 已关闭」说明页（此前是个永远登不进的登录页），并提示去后端管理「⚙ 配置」填密码；图片分发、API 与 MCP 不受影响。

## 0.11.18 - 2026-10-19

- image-host 升级到 1.0.2：运行期版本号改为从 `backend.json` 读取（此前 `/api/health` 与 MCP `serverInfo` 里仍硬编码 1.0.0，与注册表的 1.0.1 对不上）；1.0.1 引入的后端卡片配置项不变。

## 0.11.17 - 2026-10-19

- image-host 升级到 1.0.1：`backend.json` 补上 `config`，后端卡片「⚙ 配置」里可直接改 **WebUI 访问密码、对外地址前缀、数据目录、容量上限、单图上限、回收站保留天数、变更日志保留天数、缩略图最大边、抓取并发、抓取超时、抓取域名白名单**（共 11 项，全部对应 `AIPLUGIN4_IMAGE_HOST_*` 环境变量，保存后重启后端生效）。此前只有少数默认值硬编码在程序里，用户无法在图床配置里设置密码/容量，等于装了没法用。

## 0.11.16 - 2026-10-19

- 新增 `image-host` 后端（v1.0.0，默认端口 46680）：私人图床 + 图库。按 URL 抓取 QQ 等**短时效图片直链**并落库（sha256 内容寻址、天然哈希去重），对外提供稳定 URL 与 base64 出口，解决「QQ 图床链接过期后模型取不到图」的问题；记录来源（平台/群/用户/消息/会话/时间），支持重命名与标签；内置 WebUI 画廊（密码鉴权；图库 / 回收站 / 标签三视图；批量上传含多选·拖拽·粘贴·URL 导入；批量打标签·删标签·删除·存图库·复制链接，删除前先 `dry_run` 预演真实数量）与 5 个 MCP 工具（`image_search` / `image_rename` / `image_tag` / `image_keep` / `image_tags`）；回收站软删除默认 7 天，容量上限 + LRU 自动淘汰（手动上传/收进图库的图永不自动删），`change_log` 变更日志供插件增量同步。依赖 Flask + requests + Pillow，图片抓取做了 SSRF 加固（scheme/私网/逐跳重定向校验、流式大小限制）。
- 新增 `media-fetch` 后端（默认端口 46679）：媒体接口中转，代插件请求音频/图片接口并把**二进制响应转成 base64 JSON**。海豹的 JS 引擎是 goja，其 fetch 只有 `text()`/`json()`，没有 `arrayBuffer()`/`bytes()`，而 `text()` 会按 UTF-8 解码把原始字节替换成 U+FFFD 永久损坏——因此「整个响应体即音频/图片」的接口（OpenAI 兼容 `/audio/speech`、百度短文本合成 `tsn.baidu.com/text2audio`、阿里 CosyVoice 等）在插件里原本无法使用。该后端把这一步搬到 Python（能无损拿到 `response.content`），请求的 method/headers/body 全部由插件透传、本服务不掺和业务语义；上游返回 JSON 时原样交回（插件按 `data_path` 取数），返回二进制时给 `{base64, contentType, upstreamStatus, bytes}`，并带回上游状态码以便插件识别鉴权/余额错误。另提供 `/health` 探活。

## 0.11.15 - 2026-09-03

- ob11-core-bridge 升级到 1.1.0：协议端实时事件（message/notice/request/meta）不再转发给核心，改为“海豹直连协议端收发 + 桥只做核心 API 中转与 `run_core_command` 指令注入/捕获”的双连接冗余部署，避免同一事件双份进入海豹导致重复处理；`/healthz` 新增 `droppedProtocolEvents` 计数
- 注意：1.1.0 为破坏性变更，升级后海豹必须另接一条直连协议端的 OB11 正向 WS（建议排在账号列表首位），否则收不到任何实时消息；桥断线不再影响聊天与 AI，仅 `run_core_command` 不可用
- mcp-files-exec 升级到 1.0.7：默认工作区按 AI 会话隔离——HTTP 请求携带 `X-Aiplugin4-Session` 头时，相对路径与命令默认工作目录落到 `沙箱根/sessions/<清洗后的会话ID>`，无会话（stdio/老客户端）回退共享沙箱根；绝对路径访问与命令策略不变，审计日志记录会话 ID

## 0.11.14 - 2026-08-28

- mcp-browser 升级到 1.0.4：浏览器惰性启动——tools/list 只返回工具声明，不再拉起 Chromium，只有真正调用 browser_* 工具才启动共享浏览器（插件侧每 ≤60s 的工具列表同步不再让浏览器常驻/续命）；browser_close 成功后立即回收会话并退出浏览器进程，空闲兜底回收（默认 10 分钟，env AIPLUGIN4_BROWSER_IDLE_MINUTES）保留，彻底解决活跃群里 Chromium 常驻吃 CPU 的问题
- 配套：插件无需改动，旧版 1.0.3 后端可平滑升级

## 0.11.13 - 2026-08-28

- mcp-browser 升级到 1.0.3：空闲回收真正生效——后端自持共享浏览器进程（所有 AI 会话共用 1 个 Chromium，会话间仍按独立 context 隔离），空闲超过默认 10 分钟（配置「MCP 会话空闲回收分钟」/ env AIPLUGIN4_BROWSER_IDLE_MINUTES）时关闭浏览器 context 并退出浏览器进程，不再常驻吃 CPU；修复旧版只摘 MCP 监听器、Chromium 带页面常驻的问题
- 配套：插件「MCP会话空闲回收分钟」默认 15→10，「MCP每服务器最大会话数」默认 8→3

## 0.11.12 - 2026-08-26

- 下架 web-read 后端：网页读取能力统一由 mcp-browser 提供（browser_navigate / browser_snapshot / browser_take_screenshot，截图时机由 AI 自主选择）；注册表移除 web-read，已安装副本不受影响，可手动 `aibackend uninstall-backend web-read` 清理

## 0.11.11 - 2026-08-26

- mcp-browser 升级到 1.0.2：修复 root 自动检测被覆盖的问题——沙箱配置默认值改为留空（留空=自动检测 root/容器环境，true/false 才强制覆盖），此前 launcher 总会注入默认 true 导致 root 下仍报沙箱错误
## 0.11.10 - 2026-08-26

- mcp-browser 升级到 1.0.1：修复 root/容器环境 Chromium 沙箱启动失败——root 下自动关闭沙箱（chromiumSandbox: false），其他环境保持默认开启；新增「Chromium 沙箱」配置项（env AIPLUGIN4_BROWSER_CHROMIUM_SANDBOX）可手动覆盖
## 0.11.9 - 2026-08-26

- 新增 mcp-browser 后端（v1.0.0）：基于官方 @playwright/mcp 内嵌，提供 AI 浏览器操作与截图（browser_navigate / browser_click / browser_type / browser_snapshot / browser_take_screenshot / browser_wait_for / browser_close，默认端口 8921）；每个 MCP 会话独立浏览器 context，配合 aiplugin4 插件多会话分桶实现按 AI 会话隔离；支持浏览器类型 / 无头 / 视口配置，内置会话空闲回收兜底（默认 30 分钟）
## 0.11.8 - 2026-08-25

- ob11-core-bridge 升级到 1.0.8：移除 MCP /mcp，改为插件控制 WS /plugin 协议（core_command 请求/响应、ping/pong），aiplugin4 插件端直连该 WS 执行核心指令；移除 MCP SDK/zod 依赖，healthz 新增 pluginConnected/pluginClients
- md-html-render 升级到 1.1.0：渲染结果改为 MCP 图片内容块并附带 structuredContent；工具声明 readOnly/idempotent/openWorld 提示
- web-read 升级到 1.1.0：scrape_url 返回文本与结构化内容，screenshot 改为图片内容块；工具声明 readOnly/openWorld 提示
## 0.11.7 - 2026-08-18

- mcp-files-exec 升级到 1.0.6：兼容后端面板将“0=不限制”配置保存为空字符串的情况，避免后端启动失败；自由路径、危险命令放行与 `download_file` 功能保持不变

## 0.11.6 - 2026-08-18

- mcp-files-exec 升级到 1.0.5：默认允许 AI 直接传入后端主机绝对路径读写文件与执行命令；新增 `download_file(url, path)`，可把 QQ/OB11 附件 URL 下载到任意目标路径
- 新增 `MCP_MAX_DOWNLOAD_BYTES`、`MCP_DOWNLOAD_TIMEOUT` 配置；需要恢复限制时可将 `MCP_ALLOW_EXTERNAL_PATHS` 或 `MCP_ALLOW_DANGEROUS_COMMANDS` 设为 `0`

## 0.11.5 - 2026-08-18

- ob11-core-bridge 升级到 1.0.5：MCP 配置改为标准 mcpServers，工具由 	ools/list 自动发现，仅提供 
un_core_command`n- 
un_core_command 支持结构化 command/rgs 与原始 
aw_message 两种互斥调用模式；
un_ext_command 继续由插件本地实现
- 插件 MCP 默认总开关关闭，保留标准 MCP 图片内容块与渲染/截图结果图片处理逻辑

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


