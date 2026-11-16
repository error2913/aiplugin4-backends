# ob11-core-bridge

**一个独立的 OB11 协议端**：SealDice 用一条普通的 OB11 正向 WebSocket 连上来，即可得到一个**稳定、持久、可自定义**的虚拟账号 / 群 / 用户世界；aiplugin4 的 `run_core_command` 在这条连接上注入消息、执行核心指令、把回复捞回来。

- 不需要真实的 QQ 账号，不需要 go-cqhttp / napcat 之类的协议端，不需要模拟器。
- 不再拦截、不再向任何真实协议端注入消息 —— 桥自己就是那个协议端。
- 老版本的双连接 relay 模式（1.1.x）**完整保留**，填了协议端地址就自动走 relay。

## 两种模式

| | `standalone`（默认） | `relay`（1.1.x 兼容） |
| --- | --- | --- |
| 触发条件 | 未填协议端地址 | 填了 `protocol_url`，或显式 `mode=relay` |
| 出站连接 | **零** | 启动即连协议端，指数退避重连 |
| 海豹连接数 | **1 条**（第二条被 `1013` 拒绝） | 1 条（可多核心，按 `self_id` 路由） |
| OB11 action | 全部**本地应答**（`responder.js`） | 转发给协议端，`echo` 回程路由 |
| 实时事件 | 桥侧**不产生**（只对注入的消息回 action） | 协议端事件丢弃并计数 |
| 消息 ID | 虚拟世界统一发号，账本可查 `get_msg` | 原样透传 |

模式判别顺序：`AIPLUGIN4_BRIDGE_MODE` 显式取 `relay`/`standalone` → 否则看 `AIPLUGIN4_BRIDGE_PROTOCOL_URL` 是否为空。**一般不用填 `mode`。**

## 拓扑

```text
standalone（默认）
  SealDice  ──正向 WS──>  /core 或 /core/ws
      │                        │
      │  action(get_login_info / send_group_msg / ...)
      │                        ▼
      │                  responder.js 就地应答 + 记账
      │                        │
      └── 收到假消息事件 ────────┘

  aiplugin4 插件  ──正向 WS──>  /plugin   （hello / core_command / ping）
```

默认监听 `0.0.0.0:46880`：

- `/core`、`/core/ws`：SealDice 核心正向 WS。**standalone 下你只需要记住端口**（默认 `46880`，在启动器/WebUI 里改）；海豹面板的「目标地址」要填完整 URL（见下）。
- `/plugin`：aiplugin4 插件控制 WS（`hello` / `core_command` / `ping`）。
- `/healthz`：健康检查，16 个字段（见下）。

## 部署（standalone）

1. 在 aiplugin4「后端」页安装并启动 `ob11-core-bridge`，记下端口（默认 `46880`，在启动器/WebUI 里可改）。
2. 打开海豹 WebUI →「连接」→ 新增 **OB11 正向 WebSocket**：
   - 「目标地址」填**完整 URL**：`ws://127.0.0.1:<端口>/core`（如 `ws://127.0.0.1:46880/core`）。
     **必须带 `ws://` 与 `/core`** —— SealDice 的 OB11 适配器把这一格原样交给 WebSocket 客户端，
     不做任何补全（`platform_adapter_onebot.go:646`），只填 `46880` 或 `127.0.0.1` 会静默 `connect_fail`。
   - 若在卡片里配了「核心端 token」，这里的 token 填同一个值（裸值即可，桥同时认 `Bearer <token>`）。
3. 保存后海豹会以虚拟账号 `QQ:<virtual_self_id>`（默认 `10001`）上线，默认群 `20002`、默认私聊对象 `30002`。
4. **强烈建议**把这个虚拟号加进海豹的 `DiceMasters`：`QQ:10001`。否则 `MasterCheck` 永不命中，骰主权限上不到 100，`.ai` 之类的管理指令会被拒。
5. aiplugin4「后端」配置分组里确认「核心桥WS地址」是 `ws://127.0.0.1:<端口>/plugin`（默认 `ws://127.0.0.1:46880/plugin`，这一格同样必须是完整 URL），需要 token 就填「核心桥Token」。

> 为什么"端口"和"URL"要分开说：**端口**是你唯一需要记住的数字（它由启动器的 `/api/port/` 管理，可随时改）；**URL** 是面板里唯一能填的值（适配器不补 scheme、也不补路径）。改了端口要同步改面板那一格。

> **它是真协议端，不是模拟器**：`get_login_info`、`send_group_msg`、`get_group_info`、`get_group_member_info`、`get_msg`、`set_group_leave` 等 23 个 action 全部真实应答，海豹侧的群/成员/角色/权限判定都按这些答案走。

## 虚拟世界

真实环境里的群号、QQ 号不可能凭空造出来，所以桥维护一张**投影表**：真实 ID → 虚拟 ID，虚拟 ID 才是海豹看到的东西。

- **号段**：群 `1500000000`–`1599999999`，用户 `2000000000`–`2099999999`；机器人自己、默认群、默认私聊对象由配置直接指定（默认 `10001` / `20002` / `30002`）。
- **稳定**：同一个真实群/用户永远得到同一个虚拟号；`_source.platform` 之外的映射表落在 `.state/virtual-world.json`，跨重启不变。
- **不撞官方号段**：QQ 官方机器人号段（`3328144510`、`66600000`、`2854196301–2854216399`、`3889000000–3889999999`、`4010000000–4019999999`）被显式避开，否则海豹会把自己的虚拟号判成"官方机器人自己"。
- **退群是墓碑不是删除**：`set_group_leave` 之后 `get_group_info` 恒返回 `1404`，不会因为"查不到就当新群"而复活。
- 想改机器人号/群号/昵称/角色：改卡片配置后**重启后端**；改了机器人号等于换了张脸，海豹那边的历史记录对不上，属预期行为。

> `platform` 只在桥侧生效（日志、`/healthz`、`hello_result`、`_source.platform`）。海豹核心把 `QQ` 写死了（`ep.Platform == "QQ"` 才服务），所以面板上永远显示 QQ；填别的平台不会失败，但也不改变海豹侧的显示。

## 环境变量

单机部署**不需要手工设环境变量** —— 下表每一项都有同名 `config` 键，直接在卡片「⚙ 配置」里填（保存后重启该后端生效）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIPLUGIN4_BACKEND_HOST` | `0.0.0.0` | 监听地址 |
| `AIPLUGIN4_BACKEND_PORT` | `46880` | 监听端口（WebUI 端口管理也会注入） |
| `AIPLUGIN4_BRIDGE_MODE` | 空 | `relay` / `standalone`；留空=自动推断 |
| `AIPLUGIN4_BRIDGE_PLATFORM` | `QQ` | 桥自称的平台名（仅桥侧展示） |
| `AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ID` | `10001` | 虚拟机器人号 |
| `AIPLUGIN4_BRIDGE_VIRTUAL_NICKNAME` | `AI骰娘` | 虚拟机器人昵称 |
| `AIPLUGIN4_BRIDGE_VIRTUAL_SELF_ROLE` | `admin` | 机器人在虚拟群里的角色：`owner`/`admin`/`member` |
| `AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_ID` | `20002` | 默认虚拟群号 |
| `AIPLUGIN4_BRIDGE_VIRTUAL_GROUP_NAME` | `AI 测试群` | 默认虚拟群名 |
| `AIPLUGIN4_BRIDGE_VIRTUAL_PEER_ID` | `30002` | 私聊默认对象号 |
| `AIPLUGIN4_BRIDGE_VIRTUAL_MAX_MEMBER_COUNT` | `200` | 虚拟群人数上限，**必须 > 0**（为 0 会被海豹判成已退群） |
| `AIPLUGIN4_BRIDGE_STATE_DIR` | `.state` | 虚拟世界落盘目录 |
| `AIPLUGIN4_BRIDGE_CORE_PATH` | `/core` | 核心 WS 路径 |
| `AIPLUGIN4_BRIDGE_CORE_PATHS` | `/core,/core/ws` | 核心 WS 路径集合（逗号分隔） |
| `AIPLUGIN4_BRIDGE_CORE_TOKEN` | 空 | 核心端 token（裸值或 `Bearer <token>` 都认） |
| `AIPLUGIN4_BRIDGE_PLUGIN_PATH` | `/plugin` | 插件控制 WS 路径 |
| `AIPLUGIN4_BRIDGE_PLUGIN_TOKEN` | 空 | 插件端 WS token（对应插件「核心桥Token」） |
| `AIPLUGIN4_BRIDGE_KEEPALIVE_MS` | `30000` | 传输层保活：每 N 毫秒 ping 一次核心，连续 2 次无 `pong`（≈3N）才判半开并关闭。核心是 gorilla 客户端、自动回 `pong`，无需改核心 |
| `AIPLUGIN4_BRIDGE_PROTOCOL_URL` | 空 | **仅 relay**：协议端 WS 地址 |
| `AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN` | 空 | **仅 relay**：协议端 token |

启动自检不通过时后端**不会 listen**，日志里逐项打印是哪一个配置非法（空值 / 非数字 / ≤0 / 撞官方号段 / 三个虚拟 ID 互撞 / 人数上限 ≤0 / 角色非法），改完重启即可。

## `/healthz`（16 字段）

| 字段 | standalone | relay |
| --- | --- | --- |
| `ok` | `true` | `true` |
| `coreConnected` / `coreClients` | 有核心连接时 `true` / `1` | 同左（可 > 1） |
| `pluginConnected` / `pluginClients` | 插件连上时 `true` / 数量 | 同左 |
| `mode` | `"standalone"` | `"relay"` |
| `platform` | 配置的平台名 | 同左 |
| `corePort` | 实际监听端口 | 同左 |
| `virtualSelfId` | 虚拟机器人号 | `null` |
| `actionCount` | 已应答的 action 累计数 | 0（relay 不走本地应答） |
| `eventCount` | 已投递给调用方的事件数 | 0 |
| `ledgerSize` | 账本条数（上限 2000，FIFO） | 0 |
| `legacyTarget` | 收到过不带 `source` 的老插件报文 | `false` |
| `protocolConnected` / `protocolClients` / `droppedProtocolEvents` | **`null`** | 原语义 |

> relay 下 `responder` 也被记账（`captureAction` 用同一个发号器），所以 `ledgerSize` 在 relay 下也可能非 0。

## 插件接入（aiplugin4）

- **核心桥WS地址**：`ws://127.0.0.1:46880/plugin`（必须是完整 URL，不能只填端口）
- **核心桥Token**（可选）：与卡片里的 `plugin_token` 一致

连上后插件先发 `hello`，桥回 `hello_result`（`mode` / `platform` / `corePort` / `virtualSelfId` / `capabilities` / `bridgeVersion`）。插件据此决定：

- `mode=standalone` → `core_command` 里额外带 `source`（**真实** ID），由桥投影成虚拟 ID；结果里多了 `mode` / `deliveredCount` / `virtualTarget`，`ambiguous` / `forwardedCount` / `interceptedCount` 恒为 `false` / `0` / `0`（"转发"与"拦截"这两件事都随真实协议端一起消失，真实条数统一看 `deliveredCount`）。
- `mode=relay`（或 `hello` 超时 3s 的老桥）→ 报文与 1.1.x 完全一致。

`core_command` 请求：`target`（selfId / messageType / groupId|userId）+ `actor` + `raw_message` 或 `command`；`capture` 控制 `mode`（`reply_only` / `lane`）、`forward`（standalone 下忽略）、`maxMessages`、`settleMs`；另有 `timeoutMs`、`trigger`、`at`。

## 消息路由与捕获语义（standalone）

- 桥把插件指令包成一条 OB11 `message` 事件（`message_id` 来自虚拟世界发号器），发给核心。
- 核心处理完把回复作为 `send_group_msg` / `send_private_msg` action 发回 —— 桥就地应答 `ok` + 同一个 `message_id`，同时**记账并投递**给同一 lane 的调用。
- 所以 `run_core_command` 的 `messages[]` 里 `source` 恒为 `'action'`：standalone 下核心不会向桥推事件，`source:'event'` 不可能出现。
- 同一 lane 的调用串行，不同 lane 并行；收到第一条后进入 `settleMs` 空闲窗口，达到 `maxMessages` 或 `timeoutMs` 结束。
- 核心响应的 `message_id` 与账本一致，因此 `get_msg` / `delete_msg` 能查到、删掉应用发出的每一条消息（合并转发会被展开成可读文本，AI 能拿到卡片内容）。

> `capture.forward` 在 standalone 下**收下但忽略**：没有真实协议端可转发，"拦截"这个概念本身消失了。

## relay 模式（1.1.x 兼容，至少保留一个大版本）

填了 `protocol_url` 即进入。行为与 1.1.1 逐字一致：海豹另接一条直连协议端的 WS 作为实时消息入口，桥只做 API 中转 + 假消息注入/捕获；协议端推来的实时事件丢弃并计入 `droppedProtocolEvents`。详见 `CHANGELOG.md` 的 1.1.x 段。

升级提示：从 1.1.x 升到 2.0.0 后，**卡片配置项从 3 个变成 13 个**，旧的 `protocol_url` 仍然有效（自动进 relay）；想让虚拟世界生效就把 `protocol_url` 清空。

## 测试

```bash
npm test
```

五个测试文件，共 74 例：

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `tests/bridge.test.js` | 15 | standalone 主链路：`/core` 接入、16 字段 healthz、本地应答、lane 捕获、单核心 `1013` 门禁、插件 WS 协议、参数校验、鉴权 |
| `tests/relay.test.js` | 6 | 1.1.x relay 行为冻结：出站连接、重连、重试、token、echo 回程、事件丢弃 |
| `tests/standalone.test.js` | 18 | 需要越过 HTTP/WS 边界才验证得了的：模式推断、非法虚拟 ID 拒启、端口贯通、token 双来源与"query 错 + header 对"、`platform`/虚拟 ID/`self_role` 的 env 贯通、`hello` 握手、`source` 投影与老插件降级、登录握手同步性、账本回查/撤回、并发发号、forward 被忽略、图片段、未知 action、传输层保活与半开检测 |
| `tests/virtual-world.test.js` | 13 | 配置自检 7 类、官方号段 5 段、投影稳定性与跨重启、墓碑、发号器、坏档恢复 |
| `tests/responder.test.js` | 22 | 四键报文形状、24 个 action 全覆盖、账本 FIFO 与同号、群信息三档、合并转发展开、未知 action 不静默 |

`node simulate-ws.js` 是单场景端到端冒烟（真 Bridge + 模拟核心 + 模拟插件三端连通）。

> 每个用例都用 `mkdtemp` 独立状态目录：`node --test` 并行跑多个文件，Windows 上跨进程 `rename` 有 `EPERM` 概率，共享目录会让"保存/重载"用例互相打架。
>
> 需要改 env 起桥的用例（`standalone.test.js`）必须自己管 env 事务：`bridge.js` 的常量在模块顶层求值，改 env 后必须清 `require.cache` 才能读到新值；泄漏一条非法 env 会让后续整片用例变红。

## 依赖

`ws`（唯一运行时依赖）。版本升级后首次启动会重建 `node_modules`（`node_deps_hash` 指纹变化，走 `npm ci`，约 3–10 秒），属预期行为。
