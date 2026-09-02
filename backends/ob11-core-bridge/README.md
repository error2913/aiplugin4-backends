# ob11-core-bridge

SealDice 双连接冗余部署下的 OB11 API 中转与核心指令注入通道：海豹另接一条**直连协议端的 OB11 正向 WS** 负责实时消息收发，本中间件只做核心 API 请求 → 协议端的转发与响应回传，以及“注入假消息 → 执行核心指令 → 收集/转发响应”的控制通道。协议端推送给中间件的实时事件（message / notice / request / meta_event）**一律丢弃，不再转发给核心**，避免同一条事件双份进入海豹导致重复处理。扩展指令由 aiplugin4 插件本地直调扩展 `solve` 执行（`run_ext_command`），不经过本中间件。

## 拓扑

```text
协议端 / 模拟器            <──直连──>  SealDice（OB11 正向 WS #1：实时消息收发）
SealDice（OB11 正向 WS #2）<──────>  /core（或 /core/ws）
协议端 / 模拟器            <──────   中间件启动时主动连接协议端 WS（出站）
aiplugin4 插件             <──────>  /plugin
```

默认监听 `0.0.0.0:46880`：

- `/core`、`/core/ws`：SealDice 核心正向 WS（SealDice 主动连接，仅承载 API 请求、假消息注入与响应捕获）
- 协议端（出站）：中间件作为 WS **客户端**，启动时主动连接配置的协议端地址（指数退避重连）
- `/plugin`：插件控制 WS 端点，aiplugin4 直连并发送 `core_command` 请求（核心指令注入）
- `/healthz`：健康检查（`coreConnected` / `protocolConnected` / `pluginConnected` / 客户端数量 / `droppedProtocolEvents`）

> 实时消息入口必须是海豹的直连 WS：中间件收到协议端事件会直接丢弃，海豹未配置直连时将收不到任何实时消息。不支持反向 WS（协议端无需也无法连接中间件）。

## 部署（双连接）

1. 海豹「连接」新增 OB11 正向 WebSocket **直连协议端**（如 `ws://127.0.0.1:6700`），作为实时消息收发主链路，建议排在账号列表首位。
2. 海豹再新增一条 OB11 正向 WebSocket 指向本中间件：

```text
ws://127.0.0.1:46880/core
```

3. 协议端地址由中间件**出站主动连接**：在 aiplugin4「后端」页的 ob11-core-bridge 卡片「⚙ 配置」填写，保存后**重启该后端**生效；未配置或不可达时按指数退避（1s 起、上限 30s）持续重试，期间核心发来的 API 请求（如 `get_login_info`）立即收到 `status: failed`，不会静默等待超时。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIPLUGIN4_BACKEND_HOST` | `0.0.0.0` | 监听地址 |
| `AIPLUGIN4_BACKEND_PORT` | `46880` | 监听端口 |
| `AIPLUGIN4_BRIDGE_CORE_PATH` | `/core` | 核心 WS 路径 |
| `AIPLUGIN4_BRIDGE_CORE_PATHS` | `/core,/core/ws` | 核心 WS 路径集合（逗号分隔，覆盖默认） |
| `AIPLUGIN4_BRIDGE_PROTOCOL_URL` | 空 | 协议端 WS 地址（出站连接） |
| `AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN` | 空 | 协议端 token（可选；同时以 access_token 查询参数与 Bearer 头发送） |
| `AIPLUGIN4_BRIDGE_CORE_TOKEN` | 空 | 核心端（SealDice 连接）token |
| `AIPLUGIN4_BRIDGE_PLUGIN_PATH` | `/plugin` | 插件控制 WS 路径 |
| `AIPLUGIN4_BRIDGE_PLUGIN_TOKEN` | 空 | 插件端 WS token（插件以 access_token 查询参数传入） |

## 插件接入（aiplugin4）

在 aiplugin4「后端」配置分组中填写：

- **核心桥WS地址**：默认 `ws://127.0.0.1:46880/plugin`
- **核心桥Token**（可选）：与 `AIPLUGIN4_BRIDGE_PLUGIN_TOKEN` 一致

`run_core_command` 由插件本地注册为 AI 工具，通过 `/plugin` WebSocket 发送 `core_command` 请求：`target`（selfId / messageType / groupId|userId）+ `actor` + `raw_message`（原样注入）或 `command`（结构化，按 `__commandPrefix` 组装）；`capture` 控制 `mode`（`reply_only` / `lane`）、`forward`、`maxMessages`、`settleMs`，另有 `timeoutMs`、`trigger`、`at`。响应为 `core_command_result`，包含 `ok` / `messages` / `completedBy` / `ambiguous` / `forwardedCount` / `interceptedCount`。

## 消息路由与捕获语义

- 协议端 → 中间件：**仅 echo/API 响应**按 `echo` 路由回发起请求的核心（含 `get_login_info` 身份回填）；所有带 `post_type` 的实时事件被丢弃并计数（`/healthz` 的 `droppedProtocolEvents`）。
- 核心 → 协议端：API 请求原样转发，协议端响应按 `echo` 回核心。
- `run_core_command` 注入的假消息由中间件直接发给核心 WS；核心产生的 `send_*_msg` action 与可关联消息事件按 lane 捕获：
  - `forward=false`：捕获并拦截对应 action/event，不送到协议端，仍向核心返回 action 成功响应，避免核心重试；
  - `forward=true`：继续转发到协议端，并把协议端 API 响应按 `echo` 路由回核心；
  - `reply_only` 只收集带 reply 引用（指向注入消息虚拟 message_id）的响应；`lane` 按 self_id + 群/私聊 + 对方 id 捕获该会话内 bot 消息。
- 收到第一条消息后进入 `settleMs` 空闲窗口；达到 `maxMessages` 或 `timeoutMs` 结束。同一 lane 的调用串行，不同 lane 并行；无法从 OB11 action 本身区分“本次命令响应”与“外部并发发送”时会标记 `ambiguous=true`（协议缺少因果 ID 的可观测限制）。
- 桥断线期间 `run_core_command` 返回“核心 WS 未连接”错误；实时聊天由海豹直连链路承载，不受影响。

## 测试

```bash
npm test
```

测试模拟：SealDice 正向核心 WS（含 `/core/ws` 后缀）、假 OB11 协议端 WS 服务端（中间件出站主动连接，含 token 校验）、协议端断开后的指数退避重连、**协议端事件（message/notice/meta）丢弃且 echo 仍路由回核心**、多核心 self_id 路由、假消息注入与拦截/转发、多消息、lane 串行化、超时、断线、鉴权、原始帧转发，以及协议端未连接时核心 API 请求的快速失败。
