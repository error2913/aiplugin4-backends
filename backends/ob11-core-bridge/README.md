# ob11-core-bridge

SealDice（OB11 正向 WebSocket 客户端）与 OB11 协议端之间的透明中转站，为 aiplugin4 提供“注入假消息、执行核心/扩展指令、收集响应”的控制通道。

## 拓扑

```text
SealDice（OB11 正向 WS 客户端）  <──>  /core（或 /core/ws）
OB11 协议端 / 模拟器              <──>  /onebot
aiplugin4 插件 MCP 客户端          <──>  /mcp
```

默认监听 `0.0.0.0:46880`：

- `/core`、`/core/ws`：SealDice 核心正向 WS（SealDice 主动连接本端点）
- `/onebot`：协议端 WS
- `/mcp`：aiplugin4 使用的 Streamable HTTP MCP 端点，提供 `run_ext_command` 与 `run_core_command`
- `/healthz`：健康检查（返回 `coreConnected` / `protocolConnected` / 客户端数量，便于排障）

## 启动

```bash
npm install
npm start
```

在 SealDice 的「连接」中配置 OB11 正向 WebSocket，目标地址填：

```text
ws://127.0.0.1:46880/core
```

> 协议端（`/onebot`）未连接时，核心发来的 API 请求（如 `get_login_info`）会立即收到 `status: failed` 响应并在日志中记录，不会静默丢弃导致海豹等待 10 秒超时；连接/断开均有日志输出。

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIPLUGIN4_BACKEND_HOST` | `0.0.0.0` | 监听地址 |
| `AIPLUGIN4_BACKEND_PORT` | `46880` | 监听端口 |
| `AIPLUGIN4_BRIDGE_CORE_PATH` | `/core` | 核心 WS 路径（默认同时接受 `/core/ws`） |
| `AIPLUGIN4_BRIDGE_ONEBOT_PATH` | `/onebot` | 协议端 WS 路径 |
| `AIPLUGIN4_BRIDGE_TOKEN` | 空 | 默认鉴权 token，作为 MCP 与协议端默认 token |
| `AIPLUGIN4_BRIDGE_CORE_TOKEN` | 跟随 bridge token | 核心端（SealDice 连接）token |
| `AIPLUGIN4_BRIDGE_PROTOCOL_TOKEN` | 跟随 bridge token | 协议端 token |
| `AIPLUGIN4_BRIDGE_MCP_PATH` | `/mcp` | MCP Streamable HTTP 路径 |

插件在「工具 → MCP服务器配置」中使用标准 mcpServers 配置：

```json
{
  "mcpServers": {
    "ob11-core-bridge": {
      "type": "http",
      "url": "http://127.0.0.1:46880/mcp"
    }
  }
}
```

如启用了 `AIPLUGIN4_BRIDGE_TOKEN`，在该服务器条目中增加 `headers.Authorization` 或 `token`。插件端会通过 MCP 调用 `run_ext_command` / `run_core_command`，而不会直接连接中转 WS。

## MCP 工具

`/mcp` 遵循 Streamable HTTP MCP，提供两个工具：

- `run_ext_command`：执行扩展指令（`builtin` / `non_builtin` 扩展由插件端构造 `command.raw` 区分）。
- `run_core_command`：执行核心指令。
- 参数 `target`、`actor`、`command`、`capture`、`timeoutMs`：分别表示目标、假消息发送者、指令内容、捕获策略和超时。

工具返回文本内容中的 JSON 即调用结果，包含 `ok`、`messages`、`completedBy`、`ambiguous`、`forwardedCount`、`interceptedCount` 等字段。MCP 会话只负责传输，底层复用同一套 lane 串行与消息捕获逻辑。

## 捕获与转发

注入假消息后，中转会监听核心发出的 `send_*_msg` action 和可关联的消息事件：

- `forward=false`：捕获并拦截对应 action/event，不送到协议端；仍向核心返回 action 成功响应，避免核心重试。
- `forward=true`：继续发送到协议端，并把协议端的 API 响应按 `echo` 路由回核心；若要收集协议端产生的 bot 消息，使用 `capture.mode=lane`。
- 收到第一条消息后进入 `settleMs` 空闲窗口；达到 `maxMessages` 或 `timeoutMs` 时结束。
- 同一 lane 的调用串行，不同 lane 可以并行。无法从 OB11 action 本身判断“这是本次命令还是外部并发发送”的场景会标记 `ambiguous=true`；这是协议缺少因果 ID 时的可观测限制，调用方应通过 lane 串行、较短 settle 窗口和业务侧唯一目标降低混淆。

## 测试

```bash
npm test
```

测试会模拟：SealDice 正向核心 WS（含 `/core/ws` 后缀）、OB11 协议端、MCP 客户端、多核心 self_id 路由、拦截/转发、多消息、lane 串行化、超时、断线、鉴权、原始帧转发，以及协议端未连接时核心 API 请求的快速失败。
