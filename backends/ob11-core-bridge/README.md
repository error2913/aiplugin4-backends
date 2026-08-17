# ob11-core-bridge

SealDice PureOneBot 反向 WebSocket 与 OB11 协议端之间的中转站，为 aiplugin4 提供“注入假消息、执行核心/扩展指令、收集响应”的控制通道。

## 拓扑

```text
SealDice PureOneBot reverse WS  <──>  /core 或主动连接 CORE_URL
OB11 协议端 / 模拟器              <──>  /onebot
aiplugin4 插件 MCP 客户端          <──>  /mcp
旧版控制客户端                    <──>  /control（兼容保留）
```

默认监听 `0.0.0.0:46880`：

- `/core`、`/core/ws`：SealDice 核心反向 WS
- `/onebot`：协议端 WS
- `/mcp`：aiplugin4 使用的 Streamable HTTP MCP 端点，提供 `run_ext_command` 与 `run_core_command`
- `/control`：旧版 aiplugin4 控制 WS（兼容保留）
- `/healthz`：健康检查

## 启动

```bash
npm install
npm start
```

若 SealDice 使用反向 WS，推荐让中转主动连接核心：

```text
AIPLUGIN4_BRIDGE_CORE_URL=ws://127.0.0.1:46881/ws
```

也可以让 SealDice 直接连接中转的 `/core`。常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIPLUGIN4_BACKEND_HOST` | `0.0.0.0` | 监听地址 |
| `AIPLUGIN4_BACKEND_PORT` | `46880` | 监听端口 |
| `AIPLUGIN4_BRIDGE_CORE_URL` | 空 | 主动连接 SealDice 反向 WS |
| `AIPLUGIN4_BRIDGE_TOKEN` | 空 | 控制端鉴权；也作为其他端点默认 token |
| `AIPLUGIN4_BRIDGE_CORE_TOKEN` | 跟随 bridge token | 核心端 token |
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

`/mcp` 遵循 Streamable HTTP MCP，工具参数与旧版 `command.invoke` 的请求体一致：

- `run_ext_command`：执行扩展指令。
- `run_core_command`：执行核心指令。
- `target`、`actor`、`command`、`capture`、`timeoutMs`：分别表示目标、假消息发送者、指令内容、捕获策略和超时。

工具返回文本内容中的 JSON 即原有 `command.result`，因此多消息、`forward`、`ambiguous`、超时和断线等行为保持一致。MCP 会话只负责传输，底层仍复用同一套 lane 串行与消息捕获逻辑。

## 控制协议

连接 `/control` 后先发送：

```json
{"type":"hello","protocol":"aiplugin4-core-bridge","version":1,"client":"aiplugin4","token":"..."}
```

再发送 `command.invoke`：

```json
{
  "type": "command.invoke",
  "id": "invoke-1",
  "target": {
    "selfId": "10001",
    "messageType": "group",
    "groupId": "20001",
    "userId": "30001"
  },
  "actor": {"userId":"30001","nickname":"AI","role":"member"},
  "command": {"raw":".ext","name":"ext","args":[]},
  "capture": {
    "mode":"lane",
    "forward":false,
    "maxMessages":50,
    "settleMs":500
  },
  "timeoutMs":10000
}
```

中转会把同一目标群/私聊（lane）的假消息发送给核心，然后监听核心发出的 `send_*_msg` action 和可关联的消息事件：

- `forward=false`：捕获并拦截对应 action/event，不送到协议端；仍向核心返回 action 成功响应，避免核心重试。
- `forward=true`：继续发送到协议端，并把协议端的 API 响应按 `echo` 路由回核心；若要收集协议端产生的 bot 消息，使用 `capture.mode=lane`。
- 收到第一条消息后进入 `settleMs` 空闲窗口；达到 `maxMessages` 或 `timeoutMs` 时结束。
- 同一 lane 的调用串行，不同 lane 可以并行。无法从 OB11 action 本身判断“这是本次命令还是外部并发发送”的场景会标记 `ambiguous=true`；这是协议缺少因果 ID 时的可观测限制，调用方应通过 lane 串行、较短 settle 窗口和业务侧唯一目标降低混淆。

## 测试

```bash
npm test
```

测试会模拟：反向核心 WS、OB11 协议端、MCP 客户端、旧版控制端、主动连接核心、多核心 self_id 路由、拦截/转发、多消息、lane 串行化、超时、断线、鉴权和原始帧转发。
