# aiplugin4-backends

aiplugin4 的配套后端服务：流式输出、图片转 base64、用量图表、网页读取、Markdown/HTML 渲染。

配合 [aiplugin4](https://github.com/error2913/aiplugin4) 使用：把各服务地址填入插件「后端」配置分组（见 aiplugin4 的 docs/08）。

## 后端列表

| 目录 | 服务 | 默认端口 | 类型 |
| --- | --- | --- | --- |
| `stream-output` | 流式输出中转（SSE 分块/轮询） | 3010 | Python |
| `image-url-to-base64` | 图片 URL 转 base64 | 46678 | Python |
| `usage-chart` | token 用量图表 | 3009 | Python |
| `web-read` | 网页 URL 内容读取 | 46799 | Node |
| `md-html-render` | Markdown/HTML 渲染为图片 | 37632 | Node |

## 一键管理（launcher.py）

纯 Python 标准库，Windows / Linux 通用。默认不启动任何后端；首次启动某后端时自动创建独立 venv 并安装依赖，进程异常退出会自动拉起。

```bash
python launcher.py list                 # 查看后端与运行状态
python launcher.py start --all          # 启动全部（或 start <名称...>）
python launcher.py stop --all           # 停止
python launcher.py status               # 运行状态
python launcher.py port stream-output 3015   # 改端口（reset 恢复默认）
python launcher.py webui                # Web 管理界面（默认 http://127.0.0.1:8910）
python launcher.py package              # 打包 dist/aiplugin4-backends-<版本>.zip
```

每个后端目录下的 `backend.json` 声明类型/入口/依赖/默认端口；端口覆盖写入 `.runtime.json`（已 gitignore），后端通过环境变量 `AIPLUGIN4_BACKEND_PORT` 读取。
