# ob11-core-bridge

**一个独立的 OB11 协议端**：SealDice 用一条普通的 OB11 正向 WebSocket 连上来，即可得到一个**稳定、持久**的虚拟账号世界；aiplugin4 的 `run_core_command` 在这条连接上注入消息、执行核心指令、把回复捞回来。

- 不需要真实的 QQ 账号，不需要 go-cqhttp / napcat 之类的协议端，不需要模拟器。
- 不拦截、不注入真实消息、**不出站**：桥自己就是那个协议端。
- 零中转：没有任何"协议端地址"可填，也没有 relay 模式。

## 拓扑

```text
  SealDice  ──正向 WS──>  ws://<host>:<端口>   （任意路径，通常不带路径）
      │                        │
      │  action(get_login_info / send_group_msg / ...)
      │                        ▼
      │                  responder.js 就地应答 + 记账
      │                        │
      └── 收到假消息事件 ────────┘

  aiplugin4 插件  ──正向 WS──>  /plugin   （hello / core_command / ping）
```

默认监听 `0.0.0.0:46880`：

- **核心端不占用任何路径**：SealDice 核心正向 WS 连 `ws://<host>:<端口>` 即可（不带路径最省事，`/`、`/core`、任意自定义路径也都等价接受）。**你只需要记住端口**（默认 `46880`，在启动器/WebUI 里改）。
- `/plugin`：aiplugin4 插件控制 WS（`hello` / `core_command` / `ping`）。这是**唯一**靠路径区分来客的端点。
- `/healthz`：健康检查，15 个字段（见下）。

## 部署

1. 在 aiplugin4「后端」页安装并启动 `ob11-core-bridge`，记下端口（默认 `46880`，在启动器/WebUI 里可改）。
2. 打开海豹 WebUI →「连接」→ 新增 **OB11 正向 WebSocket**：
   - 「目标地址」填**完整 WS 地址**：`ws://<主机>:<端口>`（如 `ws://127.0.0.1:46880`），**不要带路径**。
     必须带 `ws://` —— SealDice 的 OB11 适配器把这一格原样交给 WebSocket 客户端，不补 scheme，
     只填 `46880` 会静默 `connect_fail`。路径则完全随意：桥不按路径认核心端，末尾多写 `/core` 之类也照常工作。
   - 「Token」填启动器里给这个后端配的那个 token（裸值即可，桥同时认 `Bearer <token>`）。后端没配 token 就留空。
3. 保存后海豹会以虚拟账号 `QQ:<virtual_self_id>`（默认 `10001`）上线。**群和私聊对象不需要预置**：海豹遇到哪个群/哪个人，桥就在虚拟世界里给它分一个稳定号。
4. **强烈建议**把这个虚拟号加进海豹的 `DiceMasters`：`QQ:10001`。否则 `MasterCheck` 永不命中，骰主权限上不到 100，`.ai` 之类的管理指令会被拒。
5. 启动器会自动导出「核心桥WS地址」（`ws://127.0.0.1:<端口>/plugin`）与「核心桥Token」，一般不用手填。

> 为什么"端口"和"URL"要分开说：**端口**是你唯一需要记住的数字（它由启动器的 `/api/port/` 管理，可随时改）；**URL** 是面板里唯一能填的值（适配器不补 scheme）。改了端口要同步改面板那一格。路径不用管——桥把所有非 `/plugin`、非 `/healthz` 的连接都当核心端。

> **它是真协议端，不是模拟器**：`get_login_info`、`send_group_msg`、`get_group_info`、`get_group_member_info`、`get_msg`、`set_group_leave` 等 23 个 action 全部真实应答，海豹侧的群/成员/角色/权限判定都按这些答案走。

## 配置项（只有三个）

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `platform` | `QQ` | 桥自称的平台名，只影响桥侧日志与 `/healthz` |
| `virtual_self_id` | `10001` | 机器人号（不能落在 QQ 官方机器人号段内） |
| `virtual_nickname` | `AI骰娘` | 机器人昵称 |

就这三项。没有"运行模式"、没有"协议端地址"、没有"默认群/默认私聊对象"、没有"机器人角色/群人数上限" —— 那些要么随 relay 一起删了，要么本来就是协议常量。

> `platform` 只在桥侧生效（日志、`/healthz`、`hello_result`）。海豹核心把 `QQ` 写死了（`ep.Platform == "QQ"` 才服务），所以面板上永远显示 QQ；填别的平台不会失败，但也不改变海豹侧的显示。

## 虚拟世界

真实环境里的群号、QQ 号不可能凭空造出来，所以桥维护一张**投影表**：真实 ID → 虚拟 ID，虚拟 ID 才是海豹看到的东西。

- **号段**：群 `1500000000`–`1599999999`，用户 `2000000000`–`2099999999`；机器人自己的号由配置指定（默认 `10001`）。
- **惰性建表**：第一次见到某个真实群/用户时才分配虚拟号。**没有任何预置的群或私聊对象**：虚拟世界里出现的每一个号，都对应一次真实调用。
- **稳定**：同一个真实群/用户永远得到同一个虚拟号；映射表落在 `.state/virtual-world.json`，跨重启不变。
- **不撞官方号段**：QQ 官方机器人号段（`3328144510`、`66600000`、`2854196301–2854216399`、`3889000000–3889999999`、`4010000000–4019999999`）被显式避开，否则海豹会把自己的虚拟号判成"官方机器人自己"。
- **缺 ID 就报错**：`send_group_msg` 不带 `group_id`、`send_private_msg` 不带 `user_id` 一律回 `1400`。既然不存在默认群/默认对象，就不能悄悄兜底到某个凭空存在的号上。
- **退群是墓碑不是删除**：`set_group_leave` 之后 `get_group_info` 恒返回 `1404`，不会因为"查不到就当新群"而复活。
- 机器人在群里恒为 `admin`，群人数上限恒为 `200`（必须 > 0，为 0 会被海豹判成已退群）。

## 环境变量

单机部署**不需要手工设环境变量** —— 三个配置键都能在卡片「⚙ 配置」里填（保存后重启该后端生效）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIPLUGIN4_BACKEND_HOST` | `0.0.0.0` | 监听地址（启动器注入） |
| `AIPLUGIN4_BACKEND_PORT` | `46880` | 监听端口（启动器注入，WebUI 端口管理可改） |
| `AIPLUGIN4_BACKEND_TOKEN` | 空 | **核心端与插件端共用这一个 token**（启动器统一生成/注入）。留空=不校验 |
| `AIPLUGIN4_BRIDGE_PLATFORM` | `QQ` | 桥自称的平台名（仅桥侧展示） |
| `AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID` | `10001` | 机器人号 |
| `AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME` | `AI骰娘` | 机器人昵称 |
| `AIPLUGIN4_BRIDGE_STATE_DIR` | `.state` | 虚拟世界落盘目录 |
| `AIPLUGIN4_BRIDGE_PLUGIN_PATH` | `/plugin` | 插件控制 WS 路径 |
| `AIPLUGIN4_BRIDGE_KEEPALIVE_MS` | `30000` | 传输层保活：每 N 毫秒 ping 一次核心，连续 2 次无 `pong`（≈3N）才判半开并关闭。核心是 gorilla 客户端、自动回 `pong`，无需改核心 |

启动自检不通过时后端**不会 listen**，日志里会指出机器人号是哪一类非法（空值 / 非数字 / ≤0 / 撞官方号段），改完重启即可。

## `/healthz`（15 字段）

| 字段 | 说明 |
| --- | --- |
| `ok` | `true` |
| `coreConnected` / `coreClients` | 有核心连接时 `true` / `1`（单核心上限） |
| `pluginConnected` / `pluginClients` | 插件连上时 `true` / 数量 |
| `mode` | 恒 `"standalone"`（对插件报文的兼容常量，插件靠它决定走 `source` 投影） |
| `platform` | 配置的平台名 |
| `corePort` | 实际监听端口 |
| `virtualSelfId` | 机器人号 |
| `virtualNickname` | 机器人昵称 |
| `actionCount` | 已应答的 action 累计数 |
| `eventCount` | 已投递给调用方的事件数 |
| `ledgerSize` | 账本条数（上限 2000，FIFO） |
| `legacyTarget` | 收到过不带 `source` 的老插件报文 |
| `tokenRequired` | 是否启用了 token 校验 |

## 插件接入（aiplugin4）

- **核心桥WS地址**：`ws://127.0.0.1:46880/plugin`（必须是完整 URL，不能只填端口）
- **核心桥Token**（可选）：与后端那个 `AIPLUGIN4_BACKEND_TOKEN` 同一个值

连上后插件先发 `hello`，桥回 `hello_result`（`mode` / `platform` / `corePort` / `virtualSelfId` / `capabilities` / `bridgeVersion`）。插件据此决定：

- `mode=standalone` → `core_command` 里额外带 `source`（**真实** ID），由桥投影成虚拟 ID；结果里多了 `mode` / `deliveredCount` / `virtualTarget`，`ambiguous` / `forwardedCount` / `interceptedCount` 恒为 `false` / `0` / `0`（"转发"与"拦截"这两件事都不存在了，真实条数统一看 `deliveredCount`）。
- `hello` 超时 3s 的老桥 → 按老语义发真实 `target`。

`core_command` 请求：`target`（selfId / messageType / groupId|userId）+ `actor` + `raw_message` 或 `command`；`capture` 控制 `mode`（`reply_only` / `lane`）、`forward`（收下但忽略）、`maxMessages`、`settleMs`；另有 `timeoutMs`、`trigger`、`at`。

## 消息路由与捕获语义

- 桥把插件指令包成一条 OB11 `message` 事件（`message_id` 来自虚拟世界发号器），发给核心。
- 核心处理完把回复作为 `send_group_msg` / `send_private_msg` action 发回 —— 桥就地应答 `ok` + 同一个 `message_id`，同时**记账并投递**给同一 lane 的调用。
- 所以 `run_core_command` 的 `messages[]` 里 `source` 恒为 `'action'`：核心不会向桥推事件，`source:'event'` 不可能出现。
- 同一 lane 的调用串行，不同 lane 并行；收到第一条后进入 `settleMs` 空闲窗口，达到 `maxMessages` 或 `timeoutMs` 结束。
- 核心响应的 `message_id` 与账本一致，因此 `get_msg` / `delete_msg` 能查到、删掉应用发出的每一条消息（合并转发会被展开成可读文本，AI 能拿到卡片内容）。

> `capture.forward` **收下但忽略**：没有真实协议端可转发，"拦截"这个概念本身消失了。

## 测试

```bash
npm test
```

四个测试文件，共 68 例：

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `tests/bridge.test.js` | 15 | 主链路：任意路径（含无路径）接入、15 字段 healthz、本地应答、lane 捕获、单核心 `1013` 门禁、插件 WS 协议、参数校验、鉴权 |
| `tests/standalone.test.js` | 17 | 需要越过 HTTP/WS 边界才验证得了的：非法虚拟号拒启、端口贯通与任意路径（含无路径）接入、统一 token 的四种来源组合、`platform`/号/昵称的 env 贯通、`hello` 握手、`source` 投影与老插件降级、登录握手同步性、账本回查/撤回、并发发号、forward 被忽略、图片段、未知 action、传输层保活与半开检测、单核心门禁 |
| `tests/virtual-world.test.js` | 14 | 机器人号 4 类非法值、官方号段 5 段、投影稳定性与跨重启、缺 ID 报错、墓碑、发号器、坏档恢复、派生群名 |
| `tests/responder.test.js` | 22 | 四键报文形状、24 个 action 全覆盖、账本 FIFO 与同号、缺目标回 1400、群信息三档、合并转发展开、未知 action 不静默 |

`node simulate-ws.js` 是单场景端到端冒烟（真 Bridge + 模拟核心 + 模拟插件三端连通）。

> 每个用例都用 `mkdtemp` 独立状态目录：`node --test` 并行跑多个文件，Windows 上跨进程 `rename` 有 `EPERM` 概率，共享目录会让"保存/重载"用例互相打架。
>
> 端口一律 `bind :0` 问 OS 要（`freePort()`），**不要**用 `47000 + Math.random()` 这种猜法：几十个桥实例落在同一个 1000 窗口里，按生日问题算撞号概率超过 90%，CI 上已经真的撞过（两个测试进程同时拿到 `47738` → `EADDRINUSE`）。`bridge.start()` 里 `listen` 失败会 reject 并把 `this.server` 置 `null`，不会变成 `uncaughtException` 把整个测试进程带走。
>
> 需要改 env 起桥的用例（`standalone.test.js`）必须自己管 env 事务：`bridge.js` 的常量在模块顶层求值，改 env 后必须清 `require.cache` 才能读到新值；泄漏一条非法 env 会让后续整片用例变红。

## 依赖

`ws`（唯一运行时依赖）。版本升级后首次启动会重建 `node_modules`（`node_deps_hash` 指纹变化，走 `npm ci`，约 3–10 秒），属预期行为。
