#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 文件与命令后端：AI 可通过 MCP 读写文件、执行受限命令。

安全设计：
  1. 路径沙箱：所有文件操作必须落在 MCP_SANDBOX_ROOTS 允许的目录内（realpath 校验，防符号链接逃逸）
  2. 命令拦截：危险命令按规则拦截；设置 MCP_ALLOWED_COMMANDS 后进入白名单模式，只放行指定前缀
  3. 执行隔离：命令在工作目录沙箱内执行，带超时强制终止（杀进程树），输出截断
  4. 审计日志：每次调用（含被拦截的命令）都记录到日志文件

传输方式：
  默认 streamable-http（端口取 AIPLUGIN4_BACKEND_PORT，默认 3910）；
  加 --stdio 可切换为 stdio 传输（供本地 MCP 客户端直接拉起）。

环境变量：
  MCP_SANDBOX_ROOTS        允许的沙箱根目录，os.pathsep 分隔（默认：进程当前目录）
  MCP_ALLOWED_COMMANDS     命令白名单前缀，os.pathsep 分隔；设置后只允许这些前缀
  MCP_MAX_FILE_BYTES       单文件读写上限（默认 1048576）
  MCP_MAX_OUTPUT_BYTES     命令输出上限（默认 1048576）
  MCP_DEFAULT_TIMEOUT      命令默认超时秒数（默认 30）
  MCP_LOG_FILE             审计日志路径（默认 <仓库>/logs/mcp-files-exec.log）
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

DEFAULT_MAX_FILE = int(os.environ.get("MCP_MAX_FILE_BYTES", 1048576))
DEFAULT_MAX_OUTPUT = int(os.environ.get("MCP_MAX_OUTPUT_BYTES", 1048576))
DEFAULT_TIMEOUT = int(os.environ.get("MCP_DEFAULT_TIMEOUT", 30))
_AUTH_TOKEN = os.environ.get("AIPLUGIN4_BACKEND_TOKEN", "")

_CWD = os.path.abspath(os.getcwd())
_SANDBOX_ROOTS = [
    os.path.abspath(p)
    for p in os.environ.get("MCP_SANDBOX_ROOTS", _CWD).split(os.pathsep)
    if p.strip()
] or [_CWD]

_ALLOWED_PREFIXES = [
    p.strip()
    for p in os.environ.get("MCP_ALLOWED_COMMANDS", "").replace("\n", os.pathsep).split(os.pathsep)
    if p.strip()
]

_LOG_DIR = os.path.join(os.path.dirname(_CWD), "logs")
_AUDIT_LOG = os.environ.get("MCP_LOG_FILE") or os.path.join(_LOG_DIR, "mcp-files-exec.log")

# 危险命令拦截规则：(正则, 拦截原因)
DANGER_RULES = [
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f?|[a-z]*f[a-z]*r?)?\s+[/~][^\s]*", re.I), "递归删除根目录/家目录"),
    (re.compile(r"\brmdir\s+[/~]", re.I), "删除根目录/家目录"),
    (re.compile(r"\b(del|rd)\s+/(s|q|f|a)+", re.I), "Windows 强制删除"),
    (re.compile(r"\b(sudo|su|doas|runas)\b", re.I), "提权命令"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt|init|telinit)\b", re.I), "关机/重启命令"),
    (re.compile(r"\b(mkfs|format|fdisk|diskpart|parted)\b", re.I), "磁盘格式化/分区命令"),
    (re.compile(r"\bdd\s+.*\bof=/dev/", re.I), "直接写入块设备"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd)", re.I), "重定向到块设备"),
    (re.compile(r"\bcurl\s+.*\|\s*(ba|z)?sh", re.I), "管道下载执行"),
    (re.compile(r"\bwget\s+.*\|\s*(ba|z)?sh", re.I), "管道下载执行"),
    (re.compile(r"\bchmod\s+(-R\s+)?(777|666|a\+w)\s+[/~]", re.I), "根目录/家目录权限放开"),
    (re.compile(r"\bchown\s+-R\s+[^\s]+\s+[/~]", re.I), "根目录/家目录递归改属主"),
    (re.compile(r"\breg\s+(delete|add)\b", re.I), "注册表修改"),
    (re.compile(r"\b(Remove-Item|rm)\s+.*(-Recurse|-Force)", re.I), "PowerShell 强制删除"),
    (re.compile(r":\(\)\s*\{.*\}", re.I), "fork 炸弹"),
    (re.compile(r"\b(mount|umount)\b", re.I), "挂载操作"),
    (re.compile(r"\b(update-grub|grub-install)\b", re.I), "引导程序修改"),
    (re.compile(r"\bopenssl\s+.*\b(genrsa|req|pkcs12)", re.I), "敏感密钥操作"),
]


def _audit(tool: str, ok: bool, detail: str = "") -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": tool,
        "ok": bool(ok),
        "detail": detail[:2000],
    }
    try:
        os.makedirs(os.path.dirname(_AUDIT_LOG), exist_ok=True)
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _auth_asgi_wrapper(inner):
    """纯 ASGI 包装：token 校验（Authorization: Bearer <token> 或 X-Token: <token>）"""
    token = _AUTH_TOKEN

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        if headers.get("authorization") == f"Bearer {token}" or headers.get("x-token") == token:
            await inner(scope, receive, send)
            return
        body = b'{"error":"unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    return app


def _resolve(path: str) -> str:
    """校验路径必须位于沙箱根目录内（realpath 防符号链接逃逸）"""
    real = os.path.realpath(os.path.abspath(path))
    for root in _SANDBOX_ROOTS:
        rr = os.path.realpath(root)
        if real == rr or real.startswith(rr + os.sep):
            return real
    raise ValueError(f"拒绝访问：{path} 不在允许的沙箱目录内（允许: {', '.join(_SANDBOX_ROOTS)}）")


def _intercept(command: str) -> Optional[str]:
    """命令拦截：白名单模式或危险规则匹配，返回拦截原因（None 表示放行）"""
    cmd = command.strip()
    if not cmd:
        return "空命令"
    if _ALLOWED_PREFIXES:
        if not any(cmd.startswith(p) for p in _ALLOWED_PREFIXES):
            return f"命令不在白名单内（MCP_ALLOWED_COMMANDS 允许的前缀: {', '.join(_ALLOWED_PREFIXES)}）"
        return None
    lower = cmd.lower()
    for pattern, reason in DANGER_RULES:
        if pattern.search(lower):
            return f"命中危险命令拦截规则：{reason}"
    return None


def _exec_command(command: str, cwd: str, timeout: int) -> dict:
    """执行命令：超时强制终止进程树，输出截断"""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        **kwargs,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            except OSError:
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    proc.kill()
                except OSError:
                    pass
        output, _ = proc.communicate()
        proc.returncode = -1
    truncated = len(output) > DEFAULT_MAX_OUTPUT
    note = f"\n[命令执行超时（>{timeout}s），已强制终止]" if timed_out else ""
    if truncated:
        output = output[:DEFAULT_MAX_OUTPUT]
        note += f"\n[输出过长，已截断为前 {DEFAULT_MAX_OUTPUT} 字符]"
    return {"output": output + note, "exit_code": proc.returncode, "timed_out": timed_out}


mcp = FastMCP("aiplugin4-files-exec")


@mcp.tool()
def read_file(path: str) -> str:
    """读取文本文件内容。path 必须是沙箱目录内的文件路径。"""
    target = _resolve(path)
    if not os.path.isfile(target):
        raise ValueError(f"文件不存在: {path}")
    size = os.path.getsize(target)
    if size > DEFAULT_MAX_FILE:
        raise ValueError(f"文件过大（{size} 字节 > 上限 {DEFAULT_MAX_FILE}）")
    with open(target, encoding="utf-8", errors="replace") as f:
        content = f.read()
    _audit("read_file", True, path)
    return content


@mcp.tool()
def list_dir(path: str = ".") -> str:
    """列出沙箱目录内的文件与子目录（名称、类型、大小）。"""
    target = _resolve(path)
    if not os.path.isdir(target):
        raise ValueError(f"目录不存在: {path}")
    lines = []
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        if os.path.isdir(full):
            lines.append(f"[dir ] {name}")
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            lines.append(f"[file] {name} ({size} 字节)")
    _audit("list_dir", True, path)
    return "\n".join(lines) if lines else "(空目录)"


@mcp.tool()
def write_file(path: str, content: str, append: bool = False) -> str:
    """写入文本文件（默认覆盖；append=True 追加）。只能写沙箱目录内。"""
    target = _resolve(path)
    if os.path.isdir(target):
        raise ValueError(f"目标是目录: {path}")
    if os.path.exists(target) and os.path.getsize(target) > DEFAULT_MAX_FILE:
        raise ValueError(f"目标文件过大（>{DEFAULT_MAX_FILE} 字节），拒绝写入")
    os.makedirs(os.path.dirname(target) or target, exist_ok=True)
    mode = "a" if append else "w"
    with open(target, mode, encoding="utf-8") as f:
        f.write(content)
    _audit("write_file", True, f"{path} append={append} {len(content)} 字符")
    return f"已写入 {len(content)} 字符 → {path}"


@mcp.tool()
def delete_file(path: str) -> str:
    """删除沙箱目录内的单个文件（目录需先用 run_command 谨慎处理或自行清空）。"""
    target = _resolve(path)
    if os.path.isdir(target):
        raise ValueError(f"目标是目录: {path}")
    if not os.path.isfile(target):
        raise ValueError(f"文件不存在: {path}")
    os.remove(target)
    _audit("delete_file", True, path)
    return f"已删除: {path}"


@mcp.tool()
def run_command(command: str, cwd: Optional[str] = None, timeout: Optional[int] = None) -> str:
    """在沙箱内执行 shell 命令并返回输出。

    带命令拦截（危险命令直接拒绝）、超时强制终止、输出截断与审计。
    cwd 必须位于沙箱目录内；timeout 默认 30 秒，最大 300 秒。
    """
    reason = _intercept(command)
    if reason:
        _audit("run_command", False, f"{command} | 拦截: {reason}")
        raise ValueError(f"命令被拦截：{reason}")
    workdir = _resolve(cwd) if cwd else _SANDBOX_ROOTS[0]
    if not os.path.isdir(workdir):
        raise ValueError(f"工作目录不存在: {workdir}")
    timeout = timeout or DEFAULT_TIMEOUT
    timeout = max(1, min(int(timeout), 300))
    result = _exec_command(command, workdir, timeout)
    _audit("run_command", True, f"{command} | exit={result['exit_code']} | {len(result['output'])} 字符")
    return (
        f"$ {command}\n"
        f"(cwd: {workdir}, exit code: {result['exit_code']})\n"
        f"{result['output']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP 文件与命令后端（沙箱 + 命令拦截）")
    parser.add_argument("--stdio", action="store_true", help="使用 stdio 传输（默认 streamable-http）")
    parser.add_argument("--host", default=os.environ.get("AIPLUGIN4_BACKEND_HOST", "0.0.0.0"), help="streamable-http 监听地址")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AIPLUGIN4_BACKEND_PORT", "3910")))
    args = parser.parse_args()

    # 注意：stdio 模式下 stdout 是 MCP 协议通道，任何非协议输出都会破坏连接，一律走 stderr
    print(
        f"[mcp-files-exec] 沙箱根目录: {_SANDBOX_ROOTS}\n"
        f"[mcp-files-exec] 命令白名单: {_ALLOWED_PREFIXES or '(未启用，使用危险规则拦截)'}\n"
        f"[mcp-files-exec] 审计日志: {_AUDIT_LOG}",
        file=sys.stderr,
        flush=True,
    )
    if args.stdio:
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        app = None
        if hasattr(mcp, "streamable_http_app"):
            try:
                import uvicorn
            except ImportError:
                uvicorn = None
            if uvicorn is not None:
                inner = mcp.streamable_http_app()
                app = _auth_asgi_wrapper(inner) if _AUTH_TOKEN else inner
        if app is None:
            if _AUTH_TOKEN:
                print(
                    "[mcp-files-exec] 当前 mcp/uvicorn 版本不支持 token 校验，拒绝启动（请升级依赖后重试）",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(1)
            mcp.run(transport="streamable-http")
        else:
            uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
