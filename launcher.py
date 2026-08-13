#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aiplugin4 后端管理入口（仅依赖 Python 标准库）。

直接运行本脚本：自动检查/安装 WebUI 自身依赖，然后启动 Web 管理界面
（默认监听 0.0.0.0，端口与访问 token 首次运行随机生成），后端安装、启停、
端口管理都在页面里完成。每次启动自动安装/刷新 aibackend 命令行（幂等），
运行本脚本启动后台 WebUI 后即退出，不占用终端。
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import dataclass

BACKENDS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BACKENDS_DIR  # launcher 位于仓库根目录
PACKAGES_DIR = os.path.join(BACKENDS_DIR, "backends")  # 后端程序包目录（按需下载/卸载）
INSTALLED_DIR = os.path.join(BACKENDS_DIR, "installed")  # 已安装后端运行目录（gitignore，卸载即删）
CONFIG_FILE = os.path.join(BACKENDS_DIR, "launcher.json")
MANIFEST_FILE = "backend.json"
REGISTRY_FILE = "backends.json"  # 后端注册表（索引：名称/介绍/版本/下载源）
GITHUB_REPO = "error2913/aiplugin4-backends"  # 上游仓库（更新源）
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"
RAW_SHOP_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/shop"  # 后端源码分支（安装/更新回退下载源）
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
DEFAULT_LOG_DIR = "logs"
RUNTIME_FILE = ".runtime.json"
WEBUI_PID_FILE = os.path.join(ROOT_DIR, "logs", "webui.pid")
VENV_DIR_NAME = ".venv"
DEPS_MARKER = ".deps_ready"
# 旧版后端曾位于仓库顶层目录（新模型为 backends/ 商店 + installed/ 运行副本）
LEGACY_BACKEND_DIRS = ["image-url-to-base64", "mcp-files-exec", "md-html-render", "stream-output", "usage-chart", "web-read"]

# 打包时排除的目录/文件
EXCLUDE_DIRS = {"logs", "node_modules", "__pycache__", ".venv", "venv", "dist", ".git", "lang-data", "cache", "backends", "data"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_FILES = {".runtime.json"}  # 本机运行态配置，不进发布包


@dataclass
class Backend:
    name: str
    description: str
    type: str  # python | node
    entry: str
    deps: str
    port: int
    version: str
    dir: str


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"auto_restart": True, "restart_backoff_seconds": [2, 5, 10, 30], "log_dir": DEFAULT_LOG_DIR}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_runtime() -> dict:
    """运行时配置（端口覆盖等），位于 backends/.runtime.json，不随源码提交"""
    try:
        with open(os.path.join(BACKENDS_DIR, RUNTIME_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_runtime(runtime: dict) -> None:
    with open(os.path.join(BACKENDS_DIR, RUNTIME_FILE), "w", encoding="utf-8") as f:
        json.dump(runtime, f, ensure_ascii=False, indent=2)


def backend_config(backend: Backend) -> dict:
    """后端运行配置：端口/token/监听IP + 自定义配置项，优先 .runtime.json（端口兼容旧版 ports 字段）"""
    rt = load_runtime()
    cfg = rt.get("config", {}).get(backend.name) or {}
    port = cfg.get("port")
    if port is None:
        port = rt.get("ports", {}).get(backend.name, backend.port)
    return {
        "port": int(port),
        "token": str(cfg.get("token") or ""),
        "host": str(cfg.get("host") or "0.0.0.0"),
        "options": dict(cfg.get("options") or {}),
    }


def save_backend_config(name: str, port=None, token=None, host=None, options: dict = None) -> dict:
    """保存后端运行配置到 .runtime.json（只更新传入字段），端口同步旧版 ports 字段；options 为自定义配置项"""
    rt = load_runtime()
    cfg = rt.setdefault("config", {}).setdefault(name, {})
    if port is not None:
        cfg["port"] = int(port)
        rt.setdefault("ports", {})[name] = int(port)
    if token is not None:
        cfg["token"] = str(token)
    if host is not None:
        cfg["host"] = str(host) or "0.0.0.0"
    if options is not None:
        cfg["options"] = {str(k): str(v) for k, v in options.items() if v is not None and str(v) != ""}
    save_runtime(rt)
    return cfg


def _expand_config_value(value) -> str:
    """展开自定义配置值中的 {REPO_ROOT} 模板为仓库根目录"""
    return str(value or "").replace("{REPO_ROOT}", ROOT_DIR)


def backend_custom_config(backend: Backend) -> dict:
    """backend.json 声明的自定义配置 schema：{key: {label, type, default, env}}；
    default 支持 {REPO_ROOT} 模板（展开为仓库根目录）"""
    try:
        with open(os.path.join(backend.dir, MANIFEST_FILE), encoding="utf-8") as f:
            data = json.load(f)
        schema = dict(data.get("config") or {})
        for field in schema.values():
            if isinstance(field, dict) and field.get("default") is not None:
                field["default"] = _expand_config_value(field["default"])
        return schema
    except (OSError, ValueError):
        return {}


def effective_port(backend: Backend) -> int:
    """有效端口：优先 .runtime.json 中的覆盖值，否则用 backend.json 默认值"""
    return backend_config(backend)["port"]


def effective_webui_port() -> int:
    """WebUI 管理界面端口：优先 .runtime.json 中的覆盖值；首次运行随机生成五位数端口（10000-65535，
    避开已收录后端的端口）并保存，之后保持稳定"""
    rt = load_runtime()
    port = rt.get("webui", {}).get("port")
    if not port:
        reserved = {b.port for b in discover_backends()}
        reserved |= {int(item.get("port", 0)) for item in load_registry() if item.get("port")}
        while True:
            port = 10000 + secrets.randbelow(55536)  # 10000-65535
            if port not in reserved:
                break
        rt.setdefault("webui", {})["port"] = int(port)
        save_runtime(rt)
    return int(port)


def save_webui_port(port: int) -> None:
    rt = load_runtime()
    rt.setdefault("webui", {})["port"] = int(port)
    save_runtime(rt)


def reset_webui_port() -> None:
    rt = load_runtime()
    rt.get("webui", {}).pop("port", None)
    if not rt.get("webui"):
        rt.pop("webui", None)
    save_runtime(rt)


def configure_webui_port(value=None) -> int:
    """查看/修改 WebUI 端口；修改后若后台 WebUI 在运行则自动重启到新端口，返回有效端口"""
    if value is None:
        return effective_webui_port()
    if value == "reset":
        reset_webui_port()
        port = effective_webui_port()  # 重新随机生成
    else:
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError("端口必须是 1-65535 的整数")
        if not 1 <= port <= 65535:
            raise ValueError("端口必须是 1-65535 的整数")
        save_webui_port(port)
    if stop_webui():
        print("[launcher] 旧 WebUI 已停止，正在用新端口重启...")
        start_webui_background(port=port, open_browser=False)
    return port


def effective_webui_host(default: str = "0.0.0.0") -> str:
    """WebUI 监听地址：优先 .runtime.json 中的覆盖值，否则默认 0.0.0.0（全部网卡）"""
    return str(load_runtime().get("webui", {}).get("host") or default)


def save_webui_host(host: str) -> None:
    rt = load_runtime()
    rt.setdefault("webui", {})["host"] = str(host)
    save_runtime(rt)


def reset_webui_host() -> None:
    rt = load_runtime()
    rt.get("webui", {}).pop("host", None)
    if not rt.get("webui"):
        rt.pop("webui", None)
    save_runtime(rt)


def configure_webui_host(value=None) -> str:
    """查看/修改 WebUI 监听地址；修改后若后台 WebUI 在运行则自动重启，返回有效地址"""
    if value is None:
        return effective_webui_host()
    if value == "reset":
        reset_webui_host()
        host = "0.0.0.0"
    else:
        host = str(value).strip()
        if not host:
            raise ValueError("监听地址不能为空")
        save_webui_host(host)
    if stop_webui():
        print("[launcher] 旧 WebUI 已停止，正在用新地址重启...")
        start_webui_background(host=host, open_browser=False)
    return host


def effective_webui_token() -> str:
    """WebUI 访问 token：首次运行自动生成并保存；webui-token 命令可查看/修改"""
    rt = load_runtime()
    token = rt.get("webui", {}).get("token")
    if not token:
        token = secrets.token_urlsafe(24)
        rt.setdefault("webui", {})["token"] = token
        save_runtime(rt)
    return str(token)


def save_webui_token(token: str) -> None:
    rt = load_runtime()
    rt.setdefault("webui", {})["token"] = str(token)
    save_runtime(rt)


def reset_webui_token() -> None:
    rt = load_runtime()
    rt.get("webui", {}).pop("token", None)
    if not rt.get("webui"):
        rt.pop("webui", None)
    save_runtime(rt)


def configure_webui_token(value=None) -> str:
    """查看/修改 WebUI 访问 token；修改后若后台 WebUI 在运行则自动重启，返回有效 token"""
    if value is None:
        return effective_webui_token()
    if value == "reset":
        reset_webui_token()
        token = effective_webui_token()  # 重新生成
    else:
        token = str(value).strip()
        if not token:
            raise ValueError("token 不能为空")
        save_webui_token(token)
    if stop_webui():
        print("[launcher] 旧 WebUI 已停止，正在用新 token 重启...")
        start_webui_background(open_browser=False)
    return token


def discover_backends() -> list:
    backends = []
    os.makedirs(INSTALLED_DIR, exist_ok=True)
    for entry in sorted(os.listdir(INSTALLED_DIR)):
        manifest = os.path.join(INSTALLED_DIR, entry, MANIFEST_FILE)
        if not os.path.isfile(manifest):
            continue
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
        backends.append(Backend(
            name=data["name"],
            description=data.get("description", ""),
            type=data.get("type", "python"),
            entry=data.get("entry", ""),
            deps=data.get("deps", ""),
            port=int(data.get("port", 0)),
            version=str(data.get("version", "") or ""),
            dir=os.path.join(INSTALLED_DIR, entry),
        ))
    return backends


def load_registry() -> list:
    """读取根目录 backends.json 注册表（可下载后端索引）；文件缺失返回空列表"""
    try:
        with open(os.path.join(BACKENDS_DIR, REGISTRY_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("backends", []))
    except (OSError, ValueError):
        return []


def venv_python_path(backend_dir: str) -> str:
    if os.name == "nt":
        return os.path.join(backend_dir, VENV_DIR_NAME, "Scripts", "python.exe")
    return os.path.join(backend_dir, VENV_DIR_NAME, "bin", "python")


def deps_hash(backend: Backend) -> str:
    dep_file = os.path.join(backend.dir, backend.deps)
    try:
        with open(dep_file, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return ""


def ensure_venv(backend: Backend) -> str:
    """为 python 后端创建/复用独立 venv 并按需安装依赖，返回 venv 内的 python 路径；
    依赖清单变化时重建整个 venv，保证依赖不多不少"""
    py = venv_python_path(backend.dir)
    venv_dir = os.path.join(backend.dir, VENV_DIR_NAME)
    marker = os.path.join(venv_dir, DEPS_MARKER)
    current = deps_hash(backend)
    if os.path.isfile(py):
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == current:
                    return py
        except OSError:
            pass
        print(f"[launcher] {backend.name} 依赖清单有变化，重建虚拟环境（保证依赖不多不少）...")
        remove_backend_deps(backend)
    else:
        print(f"[launcher] {backend.name} 首次运行，创建独立虚拟环境...")
    subprocess.check_call([sys.executable, "-m", "venv", venv_dir], **_no_window_kwargs())
    if not current:
        print(f"[launcher] 跳过 {backend.name}: 缺少 {backend.deps}")
    else:
        for attempt in (1, 2):
            try:
                proc = subprocess.run(
                    [py, "-m", "pip", "install", "-r", os.path.join(backend.dir, backend.deps)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                    **_no_window_kwargs(),
                )
                if proc.returncode != 0:
                    print(f"[launcher] pip 失败输出:\n{(proc.stdout or '')[-2000:]}\n{(proc.stderr or '')[-2000:]}")
                    raise subprocess.CalledProcessError(proc.returncode, proc.args)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                print(f"[launcher] pip 安装失败（网络抖动？），2 秒后重试一次...")
                time.sleep(2)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(current)
    return py


def node_deps_hash(backend: Backend) -> str:
    """node 后端依赖指纹：package.json + package-lock.json 的 md5"""
    h = hashlib.md5()
    for name in ("package.json", "package-lock.json"):
        try:
            with open(os.path.join(backend.dir, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()


def port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """端口是否已有进程在监听（用于启动前检测未记录的残留进程）"""
    if not port:
        return False
    probe = host or "127.0.0.1"
    if probe in ("0.0.0.0", "::"):
        probe = "127.0.0.1"
    try:
        with socket.create_connection((probe, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_node(backend: Backend) -> str:
    """为 node 后端确保依赖就绪；依赖清单变化时删除 node_modules 重建（有 lockfile 用 npm ci，保证不多不少）"""
    node_modules = os.path.join(backend.dir, "node_modules")
    marker = os.path.join(node_modules, ".install_ok")
    current = node_deps_hash(backend)
    if os.path.isfile(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == current:
                    return "node"
        except OSError:
            pass
    if os.path.isdir(node_modules):
        root = os.path.realpath(backend.dir)
        real = os.path.realpath(node_modules)
        if os.path.commonpath([root, real]) == root:
            print(f"[launcher] {backend.name} 依赖清单有变化，重建 node_modules...")
            shutil.rmtree(real, ignore_errors=True)
    print(f"[launcher] {backend.name} 首次运行或依赖不完整，npm install...")
    npm = "npm.cmd" if os.name == "nt" else "npm"  # Windows 下 npm 是 .cmd 垫片
    if os.path.isfile(os.path.join(backend.dir, "package-lock.json")):
        subprocess.check_call([npm, "ci"], cwd=backend.dir, **_no_window_kwargs())
    else:
        subprocess.check_call([npm, "install"], cwd=backend.dir, **_no_window_kwargs())
    # npm install 可能生成/更新 package-lock.json，指纹以安装后的实际文件为准；
    # 否则 deps_ready 每次都会发现漂移，导致“装好了却仍显示未安装”
    with open(marker, "w", encoding="utf-8") as f:
        f.write(node_deps_hash(backend))
    return "node"


def deps_ready(backend: Backend) -> bool:
    """后端依赖是否已就绪：python 后端看 venv 解释器与 .deps_ready 标记，node 后端看 node_modules/.install_ok 指纹"""
    if backend.type == "python":
        py = venv_python_path(backend.dir)
        marker = os.path.join(backend.dir, VENV_DIR_NAME, DEPS_MARKER)
        if not os.path.isfile(py):
            return False
        try:
            with open(marker, encoding="utf-8") as f:
                return f.read().strip() == deps_hash(backend)
        except OSError:
            return False
    marker = os.path.join(backend.dir, "node_modules", ".install_ok")
    if not os.path.isfile(marker):
        return False
    try:
        with open(marker, encoding="utf-8") as f:
            return f.read().strip() == node_deps_hash(backend)
    except OSError:
        return False


def process_memory(pid):
    """返回 (进程 RSS 字节, 系统总物理内存字节)；不可用或进程不存在时返回 None"""
    if not pid:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return None
            try:
                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]
                counters = PROCESS_MEMORY_COUNTERS()
                if not ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), ctypes.sizeof(counters)
                ):
                    return None
                rss = counters.WorkingSetSize
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return rss, mem.ullTotalPhys
        if os.path.exists("/proc"):
            with open(f"/proc/{int(pid)}/status", encoding="utf-8") as f:
                rss_kb = next(int(line.split()[1]) for line in f if line.startswith("VmRSS:"))
            with open("/proc/meminfo", encoding="utf-8") as f:
                total_kb = next(int(line.split()[1]) for line in f if line.startswith("MemTotal:"))
            return rss_kb * 1024, total_kb * 1024
    except Exception:  # noqa: BLE001
        return None
    return None


def remove_backend_deps(backend: Backend) -> None:
    """删除后端已安装的依赖（python 删 .venv，node 删 node_modules），恢复未安装状态"""
    if backend.type == "python":
        target = os.path.join(backend.dir, VENV_DIR_NAME)
    else:
        target = os.path.join(backend.dir, "node_modules")
    root = os.path.realpath(backend.dir)
    real = os.path.realpath(target)
    if os.path.commonpath([root, real]) != root or os.path.basename(real) not in (VENV_DIR_NAME, "node_modules"):
        raise ValueError(f"拒绝删除非后端依赖目录: {target}")
    if not os.path.isdir(real):
        return
    print(f"[launcher] 删除 {backend.name} 依赖: {real}")

    def onerror(func, path, exc_info):
        try:
            os.chmod(path, 0o777)
            func(path)
        except OSError:
            pass

    for _ in range(5):
        try:
            shutil.rmtree(real, onerror=onerror)
            break
        except OSError:
            time.sleep(0.5)
    if os.path.isdir(real):
        raise RuntimeError(f"依赖目录删除失败（可能仍有进程占用）: {real}")


def ensure_environment(backend: Backend) -> list:
    """一键启动：确保依赖就绪，返回启动命令前缀（python 用 venv 内解释器）"""
    if backend.type == "python":
        return [ensure_venv(backend)]
    prefix = [ensure_node(backend)]
    ensure_chromium_libs(backend)  # Linux 下 node 后端自动检测/补齐 Chromium 系统库
    return prefix


# Puppeteer/Chromium 在 Linux 上需要的共享库 -> Debian/Ubuntu apt 包名（ldd 前缀匹配）
_PUPPETEER_LIB_PACKAGES = {
    "libnss3.so": "libnss3",
    "libnssckbi.so": "libnss3",
    "libatk-1.0.so": "libatk1.0-0",
    "libatk-bridge-2.0.so": "libatk-bridge2.0-0",
    "libatspi.so": "libatspi2.0-0",
    "libcups.so": "libcups2",
    "libdrm.so": "libdrm2",
    "libxkbcommon.so": "libxkbcommon0",
    "libxkbcommon-x11.so": "libxkbcommon-x11-0",
    "libxcomposite.so": "libxcomposite1",
    "libxdamage.so": "libxdamage1",
    "libxfixes.so": "libxfixes3",
    "libxrandr.so": "libxrandr2",
    "libxrender.so": "libxrender1",
    "libxshmfence.so": "libxshmfence1",
    "libgbm.so": "libgbm1",
    "libpango-1.0.so": "libpango-1.0-0",
    "libpangocairo-1.0.so": "libpango-1.0-0",
    "libcairo.so": "libcairo2",
    "libasound.so": "libasound2",
    "libx11-xcb.so": "libx11-xcb1",
    "libxcb.so": "libxcb1",
    "libxext.so": "libxext6",
    "libX11.so": "libx11-6",
    "libXss.so": "libxss1",
    "libgtk-3.so": "libgtk-3-0",
    "libgdk-3.so": "libgtk-3-0",
    "libgdk_pixbuf-2.0.so": "libgdk-pixbuf-2.0-0",
    "libglib-2.0.so": "libglib2.0-0",
    "libgobject-2.0.so": "libglib2.0-0",
    "libfontconfig.so": "libfontconfig1",
    "libfreetype.so": "libfreetype6",
    "libexpat.so": "libexpat1",
}


def _node_uses_puppeteer(backend: Backend) -> bool:
    """node 后端是否依赖 puppeteer（读 package.json）"""
    try:
        with open(os.path.join(backend.dir, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    return "puppeteer" in deps


def _chrome_executable(backend: Backend) -> str:
    try:
        out = subprocess.run(
            ["node", "-e", "process.stdout.write(require('puppeteer').executablePath())"],
            cwd=backend.dir,
            capture_output=True,
            text=True,
            timeout=60,
            **_no_window_kwargs(),
        )
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _missing_chromium_libs(chrome: str) -> list:
    """ldd 检查 Chromium 缺少哪些共享库（'not found' 行）"""
    try:
        out = subprocess.run(["ldd", chrome], capture_output=True, text=True, timeout=60, **_no_window_kwargs())
    except Exception:  # noqa: BLE001
        return []
    missing = []
    for line in (out.stdout or "").splitlines():
        if "not found" in line:
            name = line.split("=>")[0].strip()
            if name:
                missing.append(name)
    return missing


def _map_lib_package(soname: str) -> str:
    """ldd soname 常带版本后缀（如 libX11.so.6），按前缀匹配 apt 包名"""
    for key, pkg in _PUPPETEER_LIB_PACKAGES.items():
        if soname.startswith(key):
            return pkg
    return ""


def ensure_chromium_libs(backend: Backend) -> None:
    """Linux 下检测并补齐 Puppeteer/Chromium 系统库（Debian/Ubuntu 用 apt 自动装）"""
    if os.name != "posix" or backend.type != "node":
        return
    if not _node_uses_puppeteer(backend):
        return
    chrome = _chrome_executable(backend)
    if not chrome or not os.path.isfile(chrome):
        print(f"[launcher] {backend.name} 未找到 Chromium 可执行文件，跳过系统库检查")
        return
    missing = _missing_chromium_libs(chrome)
    if not missing:
        return
    packages = sorted({pkg for k in missing if (pkg := _map_lib_package(k))})
    unknown = [k for k in missing if not _map_lib_package(k)]
    if not shutil.which("apt-get"):
        print(f"[launcher] {backend.name} 缺少 Chromium 系统库: {', '.join(missing)}（当前非 apt 发行版，请手动安装对应包）")
        return
    if packages:
        print(f"[launcher] {backend.name} 检测到缺少 Chromium 系统库，自动安装: {', '.join(packages)}")
        if _sudo(["apt-get", "install", "-y"] + packages) != 0:
            print("[launcher] apt-get install 失败，尝试先 apt-get update...")
            _sudo(["apt-get", "update"])
            if _sudo(["apt-get", "install", "-y"] + packages) != 0:
                print(f"[launcher] 自动安装失败，请手动执行: sudo apt-get install -y {' '.join(packages)}", file=sys.stderr)
    if unknown:
        print(f"[launcher] {backend.name} 还缺少未收录的系统库: {', '.join(unknown)}，请手动安装对应包")


class Supervisor:
    """后端进程监督：启动子进程、写日志、异常退出自动拉起（带退避）；
    运行状态持久化到 logs/state.json，支持跨终端 stop/status 管理"""

    def __init__(self, config: dict):
        self.config = config
        self.procs = {}
        self.stop_flags = {}
        self.restart_count = {}
        self.log_dir = os.path.join(BACKENDS_DIR, config.get("log_dir", DEFAULT_LOG_DIR))
        os.makedirs(self.log_dir, exist_ok=True)
        self.state_file = os.path.join(self.log_dir, "state.json")
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        data = json.dumps(self.state, ensure_ascii=False, indent=2)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        # 原子替换，避免崩溃留下半写文件；WebUI 刷新会短暂持有读句柄，
        # Windows 下 replace 可能被占用文件拒绝，重试几次后兜底直接覆盖写
        for _ in range(20):
            try:
                os.replace(tmp, self.state_file)
                return
            except OSError:
                time.sleep(0.05)
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write(data)
        try:
            os.remove(tmp)
        except OSError:
            pass

    def _reload_state(self) -> None:
        """读写前重载 state 文件，避免覆盖其他进程（stop/status）写入的内容"""
        try:
            with open(self.state_file, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if not pid:
            return False
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return ctypes.windll.kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED: 进程存在但无权限
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def is_running(self, name: str) -> bool:
        self._reload_state()
        proc = self.procs.get(name)
        if proc is not None:
            return proc.poll() is None
        info = self.state.get(name)
        if info and self._pid_alive(info.get("pid")):
            return True
        if info:
            self.state.pop(name, None)
            self._save_state()
        return False

    def spawn(self, backend: Backend) -> bool:
        self._reload_state()
        if self.is_running(backend.name):
            return True
        cfg = backend_config(backend)
        # 端口已被监听但 supervisor 无记录：旧会话/升级残留的孤儿进程，跳过启动并明确提示
        if port_in_use(cfg["port"], cfg["host"]):
            print(
                f"[launcher] {backend.name} 端口 {cfg['port']} 已被未记录的进程占用，跳过启动"
                "（可能是旧会话残留，请先结束占用该端口的进程再重试）"
            )
            return False
        log_path = os.path.join(self.log_dir, f"{backend.name}.log")
        log_file = open(log_path, "a", encoding="utf-8")
        log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {backend.name} (host {cfg['host']}, port {cfg['port']}) =====\n")
        log_file.flush()
        # 强制子进程以 UTF-8 输出，避免 Windows 下 GBK 与 launcher 的 UTF-8 日志混编码导致乱码
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["AIPLUGIN4_BACKEND_PORT"] = str(cfg["port"])
        env["AIPLUGIN4_BACKEND_HOST"] = cfg["host"]
        env["AIPLUGIN4_BACKEND_TOKEN"] = cfg["token"]
        # 自定义配置项注入（backend.json config 声明的 env；{REPO_ROOT} 模板展开为仓库根）
        for key, field in backend_custom_config(backend).items():
            env_name = field.get("env")
            if env_name:
                value = _expand_config_value(cfg["options"].get(key, field.get("default", "")))
                env[env_name] = value
                if field.get("create_dir"):
                    for part in value.split(os.pathsep):
                        part = part.strip()
                        if part:
                            os.makedirs(part, exist_ok=True)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # 无控制台父进程（webui/后台模式）下不弹黑框
        proc = subprocess.Popen(
            ensure_environment(backend) + [backend.entry],
            cwd=backend.dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
        self.procs[backend.name] = proc
        self.state[backend.name] = {"pid": proc.pid, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._save_state()
        print(f"[launcher] 已启动 {backend.name} (pid={proc.pid}, host={cfg['host']}, port={cfg['port']}, 日志={log_path})")
        return True

    def _monitor(self, backend: Backend) -> None:
        while True:
            self._reload_state()
            proc = self.procs.get(backend.name)
            if proc is None:
                return
            proc.wait()
            self._reload_state()
            if self.stop_flags.get(backend.name, threading.Event()).is_set():
                self.state.pop(backend.name, None)
                self._save_state()
                return
            if self.procs.get(backend.name) is not proc:
                return
            del self.procs[backend.name]
            self.state.pop(backend.name, None)
            self._save_state()
            self.restart_count[backend.name] = self.restart_count.get(backend.name, 0) + 1
            self.state.setdefault("restarts", {})[backend.name] = self.restart_count[backend.name]
            self._save_state()
            if backend.name in self.state.get("stopped", []):
                print(f"[launcher] {backend.name} 已停止，不再自动拉起")
                return
            if not self.config.get("auto_restart", True):
                print(f"[launcher] {backend.name} 已退出（自动重启已关闭）")
                return
            backoffs = self.config.get("restart_backoff_seconds", [2, 5, 10, 30])
            delay = backoffs[min(self.restart_count[backend.name] - 1, len(backoffs) - 1)]
            print(f"[launcher] {backend.name} 异常退出，{delay}s 后自动拉起（第 {self.restart_count[backend.name]} 次）")
            time.sleep(delay)
            self._reload_state()
            if self.stop_flags.get(backend.name, threading.Event()).is_set():
                return
            if backend.name in self.state.get("stopped", []):
                print(f"[launcher] {backend.name} 已停止，不再自动拉起")
                return
            self.spawn(backend)

    def start(self, backends: list) -> list:
        """启动后端（已在运行视为成功）；返回因端口被未记录进程占用而启动失败的名称列表"""
        failed = []
        for backend in backends:
            self.stop_flags[backend.name] = threading.Event()
            if not self.spawn(backend):
                failed.append(backend.name)
            threading.Thread(target=self._monitor, args=(backend,), daemon=True).start()
        return failed

    def stop(self, backends: list) -> None:
        for backend in backends:
            self._reload_state()
            flag = self.stop_flags.setdefault(backend.name, threading.Event())
            flag.set()
            proc = self.procs.get(backend.name)
            if proc and proc.poll() is None:
                print(f"[launcher] 停止 {backend.name} (pid={proc.pid})")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                info = self.state.get(backend.name)
                if info and self._pid_alive(info.get("pid")):
                    print(f"[launcher] 停止 {backend.name} (pid={info['pid']})")
                    try:
                        os.kill(info["pid"], signal.SIGTERM)
                    except OSError:
                        pass
            self.procs.pop(backend.name, None)
            self.state.pop(backend.name, None)
            if backend.name not in self.state.setdefault("stopped", []):
                self.state["stopped"].append(backend.name)
            self._save_state()
            self.restart_count[backend.name] = 0

    def status(self, backends: list) -> None:
        running = 0
        for backend in backends:
            ok = self.is_running(backend.name)
            running += ok
            state = "[运行中]" if ok else "[已停止]"
            print(f"{state} {backend.name:24s} port={effective_port(backend):<6d} {backend.description}")
        print(f"共 {running}/{len(backends)} 个后端在运行")


def setup_backend(backend: Backend) -> None:
    """安装依赖（幂等，python 后端装入独立 venv）"""
    if backend.type == "python":
        ensure_venv(backend)
    else:
        ensure_node(backend)


def registry_entry(name: str) -> dict:
    for item in load_registry():
        if item.get("name") == name:
            return item
    raise ValueError(f"注册表中不存在后端: {name}")


def download_backend_files(entry: dict, backend_dir: str) -> None:
    """按注册表从远端下载后端程序文件到 backends/<name>；远端不可用时回退到本地同目录文件"""
    name = entry.get("name", "")
    source = (entry.get("source") or "").rstrip("/") or (
        f"{RAW_SHOP_BASE}/backends/{name}"
    )
    files = entry.get("files") or []
    os.makedirs(backend_dir, exist_ok=True)
    for rel in files:
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel or os.path.normpath(rel).startswith(".."):
            continue
        dest = os.path.join(backend_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fetched = False
        try:
            import urllib.request

            with urllib.request.urlopen(f"{source}/{rel}", timeout=60) as resp:
                data = resp.read()
            if data:
                with open(dest, "wb") as f:
                    f.write(data)
                fetched = True
        except Exception:  # noqa: BLE001
            fetched = False
        if not fetched and not os.path.isfile(dest):
            raise RuntimeError(f"下载后端文件失败且本地无副本: {name}/{rel}")


def _extract_backend_package(zip_path: str, dest_dir: str) -> None:
    """把后端独立包（zip 内路径为 backends/<name>/...）解压到 dest_dir（installed/<name>）"""
    base = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            rel = member.filename.replace("\\", "/").lstrip("/")
            parts = rel.split("/")
            if len(parts) >= 3 and parts[0] == "backends":
                rel = "/".join(parts[2:])
            if not rel or rel.endswith("/") or os.path.normpath(rel).startswith(".."):
                continue
            dest = os.path.realpath(os.path.join(base, *rel.split("/")))
            if dest != base and not dest.startswith(base + os.sep):
                continue  # 防 zip 路径穿越
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _download_backend_package(entry: dict, dest_dir: str, timeout: int = 120) -> None:
    """按注册表版本从 GitHub 最新 release 下载后端独立包并解压到 dest_dir"""
    name = entry.get("name", "")
    version = str(entry.get("version", "") or "")
    if not name or not version:
        raise RuntimeError("注册表缺少名称/版本，无法按独立包下载")
    asset = f"aiplugin4-backends-{name}-{version}.zip"
    url = _release_asset(asset)
    if not url:
        raise RuntimeError(f"release 中未找到 {asset}")
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="aib4-install-", suffix=".zip")
        os.close(fd)
        with _http_open(url, timeout=timeout) as resp, open(tmp_path, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
        _extract_backend_package(tmp_path, dest_dir)
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def install_backend(name: str) -> Backend:
    """安装后端：优先按注册表版本从 release 独立包下载程序到 installed/<name>
    （缓存版本一致时直接复制；独立包失败时回退缓存/远端文件），然后安装依赖；返回 Backend"""
    entry = None
    registry_entry_data = None
    try:
        registry_entry_data = registry_entry(name)
    except ValueError:
        pass
    shop_manifest = os.path.join(PACKAGES_DIR, name, MANIFEST_FILE)
    if os.path.isfile(shop_manifest):
        # 优先用缓存里后端的自带清单（可能是独立包更新后的最新文件清单）
        try:
            with open(shop_manifest, encoding="utf-8") as f:
                candidate = json.load(f)
            if candidate.get("name"):
                entry = candidate
        except (OSError, ValueError):
            pass
    if entry is None:
        entry = dict(registry_entry_data or {})
    if registry_entry_data:
        # 注册表作为兜底元数据（缓存清单可能缺 version/source 等字段）
        for k in ("version", "description", "source", "files"):
            if not entry.get(k) and registry_entry_data.get(k):
                entry = {**entry, k: registry_entry_data[k]}
    backend_dir = os.path.join(INSTALLED_DIR, name)
    source_dir = os.path.join(PACKAGES_DIR, name)
    files = entry.get("files") or []
    registry_version = str((registry_entry_data or {}).get("version", "") or "")

    def _copy_from_cache() -> int:
        copied = 0
        if not os.path.isdir(source_dir):
            return 0
        for rel in files:
            rel = rel.replace("\\", "/").lstrip("/")
            if not rel or os.path.normpath(rel).startswith(".."):
                continue
            src = os.path.join(source_dir, *rel.split("/"))
            dst = os.path.join(backend_dir, *rel.split("/"))
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
        return copied

    copied = 0
    # 缓存与注册表版本一致时直接复制（更新流程会先把新版解压进缓存），否则按 release 独立包下载
    cache_version = ""
    if os.path.isfile(shop_manifest):
        try:
            with open(shop_manifest, encoding="utf-8") as f:
                cache_version = str((json.load(f) or {}).get("version", "") or "")
        except (OSError, ValueError):
            pass
    if cache_version and (not registry_version or cache_version == registry_version):
        copied = _copy_from_cache()
    if copied < len(files):
        try:
            os.makedirs(backend_dir, exist_ok=True)
            _download_backend_package(entry, backend_dir)
            missing = [
                rel
                for rel in files
                if not os.path.isfile(
                    os.path.join(backend_dir, *rel.replace("\\", "/").lstrip("/").split("/"))
                )
            ]
            if missing:
                raise RuntimeError(f"独立包缺少文件: {', '.join(missing)}")
            copied = len(files)
        except Exception as e:  # noqa: BLE001
            print(f"[launcher] {name} 独立包下载失败，回退缓存/远端文件: {e}")
            copied = _copy_from_cache()
            if copied < len(files):
                download_backend_files(entry, backend_dir)
                copied = len(files)
    # 始终用合并后的元数据写回清单（缓存/远端清单可能缺 version 等字段）
    with open(os.path.join(backend_dir, MANIFEST_FILE), "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    backend = Backend(
        name=entry.get("name", name),
        description=entry.get("description", ""),
        type=entry.get("type", "python"),
        entry=entry.get("entry", ""),
        deps=entry.get("deps", ""),
        port=int(entry.get("port", 0)),
        version=str(entry.get("version", "") or ""),
        dir=backend_dir,
    )
    print(f"[launcher] 安装后端 {name}，安装依赖...")
    try:
        setup_backend(backend)
    except Exception:
        shutil.rmtree(backend_dir, ignore_errors=True)  # 安装失败：清掉半成品，回到未安装状态
        raise
    print(f"[launcher] 后端 {name} 安装完成")
    return backend


def update_backend(name: str, timeout: int = 120) -> Backend:
    """更新单个后端：优先从 GitHub 最新 release 下载该后端独立包覆盖商店文件，
    独立包缺失/下载失败时回退按注册表从远端下载程序文件；然后重装到 installed/ 并同步依赖。返回新 Backend"""
    entry = _remote_registry().get(name) or registry_entry(name)
    backend_v = str(entry.get("version", "") or "")
    asset_name = f"aiplugin4-backends-{name}-{backend_v}.zip" if backend_v else ""
    tmp_path = ""
    try:
        if asset_name:
            zip_url = _release_asset(asset_name)
            if zip_url:
                fd, tmp_path = tempfile.mkstemp(prefix="aib4-backend-", suffix=".zip")
                os.close(fd)
                with _http_open(zip_url, timeout=timeout) as resp, open(tmp_path, "wb") as f:
                    shutil.copyfileobj(resp, f, 1024 * 1024)
                _extract_zip_into_root(tmp_path)  # 只含 backends/<name>/，落回商店目录
                print(f"[launcher] 后端 {name} 已从 release 更新到 v{backend_v}")
            else:
                print(f"[launcher] release 中未找到 {asset_name}，改为远端文件下载")
        else:
            print(f"[launcher] 注册表无版本信息，按远端文件下载更新 {name}")
    except Exception as e:  # noqa: BLE001
        print(f"[launcher] 后端 {name} release 包更新失败，回退远端文件下载: {e}")
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    # 商店缺文件时按注册表补全（release 包缺失或首次出现的新后端）
    files = entry.get("files") or []
    missing = [
        rel
        for rel in files
        if not os.path.isfile(os.path.join(PACKAGES_DIR, name, *rel.replace("\\", "/").lstrip("/").split("/")))
    ]
    if missing:
        download_backend_files(entry, os.path.join(PACKAGES_DIR, name))
    return install_backend(name)


def remove_backend_dir(name: str) -> None:
    """卸载：只删除 installed/<name>（程序 + 依赖），git 商店里的包不动"""
    real = os.path.realpath(os.path.join(INSTALLED_DIR, name))
    root = os.path.realpath(INSTALLED_DIR)
    if os.path.commonpath([root, real]) != root or os.path.basename(real) != name:
        raise ValueError(f"拒绝删除非已安装后端目录: {real}")
    shutil.rmtree(real, ignore_errors=True)


def read_version() -> str:
    # 读取仓库根目录的 VERSION 文件（发版时由 release 流程写入标签版本）
    version_file = os.path.join(ROOT_DIR, "VERSION")
    try:
        with open(version_file, encoding="utf-8") as f:
            version = f.read().strip()
        if version:
            return version
    except OSError:
        pass
    return "0.0.0"


def ensure_webui_deps() -> None:
    """安装 WebUI 自身依赖（当前为纯标准库实现；若存在 webui-requirements.txt 则自动安装）"""
    req = os.path.join(ROOT_DIR, "webui-requirements.txt")
    if not os.path.isfile(req):
        return
    print("[launcher] 安装 WebUI 依赖...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req], **_no_window_kwargs())


def launch_webui(backends, config, supervisor, host: str = None, port: int = None, open_browser: bool = True) -> None:
    """安装 WebUI 依赖并启动管理界面（阻塞，Ctrl+C 退出）"""
    ensure_webui_deps()
    host = host or effective_webui_host()
    port = int(port) if port else effective_webui_port()
    token = effective_webui_token()
    try:
        from webui import run_webui
    except ImportError:
        print("[launcher] webui 模块缺失（webui.py）")
        sys.exit(1)
    print(f"[launcher] 启动后端管理界面: http://{host}:{port}")
    print(f"[launcher] WebUI 访问 token: {token}（可用 aibackend webui-token 修改）")
    run_webui(backends, config, supervisor, host=host, port=port, open_browser=open_browser)


def _can_open_browser() -> bool:
    """自动打开浏览器是否可行：Windows 桌面直接开；Linux/macOS 需显式设置 $BROWSER 且有图形环境
    （SSH -X 会把 DISPLAY 带过来，但服务器通常没有浏览器，因此默认不自动开）"""
    if os.name == "nt":
        return True
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return bool(os.environ.get("BROWSER"))


def start_webui_background(host: str = None, port: int = None, open_browser: bool = True) -> int:
    """后台启动 WebUI（不占用终端、无控制台窗口），返回 pid；已在运行则返回现有 pid"""
    host = host or effective_webui_host()
    port = int(port) if port else effective_webui_port()
    token = effective_webui_token()
    log_dir = os.path.join(ROOT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    if os.path.exists(WEBUI_PID_FILE):
        try:
            with open(WEBUI_PID_FILE, encoding="utf-8") as f:
                parts = (f.read().strip() or "").split()
            old_pid = int(parts[0]) if parts else 0
            old_host = parts[1] if len(parts) > 1 else None
            old_port = int(parts[2]) if len(parts) > 2 else None
            if old_pid and Supervisor._pid_alive(old_pid):
                if old_host != host or old_port != port:
                    print(f"[launcher] 旧 WebUI 监听配置不同（{old_host or '未知'}:{old_port or '未知'} → {host}:{port}），自动重启...")
                    stop_webui()
                else:
                    url = f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "::") else f"http://{host}:{port}"
                    print(f"[launcher] WebUI 已在后台运行 (pid={old_pid})，无需重复启动: {url}")
                    print(f"[launcher] WebUI 访问 token: {token}（可用 aibackend webui-token 修改）")
                    if open_browser and _can_open_browser():
                        webbrowser.open(url)
                    return old_pid
        except (OSError, ValueError):
            pass
    script = os.path.abspath(__file__)
    cmd = [
        sys.executable, script, "webui", "--no-browser",
        "--host", str(host), "--port", str(port),
    ]
    log_file = open(os.path.join(log_dir, "webui.log"), "a", encoding="utf-8")
    log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 WebUI (port {port}) =====\n")
    log_file.flush()
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT_DIR,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    with open(WEBUI_PID_FILE, "w", encoding="utf-8") as f:
        f.write(f"{proc.pid} {host} {port}")
    url = f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "::") else f"http://{host}:{port}"
    print(f"[launcher] WebUI 已在后台启动: {url} (pid={proc.pid}, 日志=logs/webui.log)")
    print(f"[launcher] WebUI 访问 token: {token}（可用 aibackend webui-token 修改）")
    if open_browser and _can_open_browser():
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    time.sleep(1.0)
    if proc.poll() is not None:
        try:
            with open(os.path.join(log_dir, "webui.log"), encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-10:])
        except OSError:
            tail = "(无法读取日志)"
        print(f"[launcher] WebUI 启动失败（进程已退出），最近日志：\n{tail}", file=sys.stderr)
        try:
            os.remove(WEBUI_PID_FILE)
        except OSError:
            pass
        return 0
    return proc.pid


def stop_webui() -> bool:
    """停止后台 WebUI，返回是否成功停止"""
    if not os.path.exists(WEBUI_PID_FILE):
        print("[launcher] WebUI 未在后台运行（无 pid 文件）")
        return False
    try:
        with open(WEBUI_PID_FILE, encoding="utf-8") as f:
            pid = int((f.read().strip() or "0").split()[0])
    except (OSError, ValueError):
        pid = 0
    stopped = False
    if pid and Supervisor._pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
            print(f"[launcher] 已停止 WebUI (pid={pid})")
        except OSError as e:
            print(f"[launcher] 停止 WebUI 失败: {e}")
    else:
        print("[launcher] WebUI 进程已不存在")
    try:
        os.remove(WEBUI_PID_FILE)
    except OSError:
        pass
    return stopped


def _sudo(args: list) -> int:
    """以 root 执行命令（非 root 时自动加 sudo），仅 Linux"""
    if os.geteuid() == 0:
        return subprocess.call(args)
    if shutil.which("sudo"):
        return subprocess.call(["sudo"] + args)
    print(f"[launcher] 需要 root 权限但未找到 sudo，请用 root 或 su 后重试: {' '.join(args)}", file=sys.stderr)
    return 127


def _webui_service_unit() -> str:
    """生成 systemd unit 内容：前台运行 webui（--no-browser），异常自动拉起"""
    exe = shlex.quote(sys.executable)
    entry = shlex.quote(os.path.abspath(__file__))
    return f"""[Unit]
Description=aiplugin4-backends WebUI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT_DIR}
ExecStart={exe} {entry} webui --no-browser
Restart=always
RestartSec=3
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUTF8=1

[Install]
WantedBy=multi-user.target
"""


def install_webui_service() -> None:
    """注册 systemd 服务：WebUI 开机自启、异常退出自动拉起（仅 Linux）"""
    if os.name != "posix":
        print("[launcher] 系统服务仅支持 Linux")
        sys.exit(1)
    if not shutil.which("systemctl"):
        print(
            "[launcher] 未检测到 systemd（找不到 systemctl），当前系统可能使用 SysV/Upstart/OpenRC 等其他 init，无法注册 systemd 服务",
            file=sys.stderr,
        )
        sys.exit(1)
    stop_webui()  # 释放端口，避免与已有后台 WebUI 冲突
    unit = _webui_service_unit()
    unit_path = "/etc/systemd/system/aiplugin4-webui.service"
    tmp = "/tmp/aiplugin4-webui.service"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(unit)
    try:
        if _sudo(["install", "-m", "644", tmp, unit_path]) != 0:
            print("[launcher] 写入 systemd 单元失败（权限不足或路径不可写）", file=sys.stderr)
            sys.exit(1)
        if _sudo(["systemctl", "daemon-reload"]) != 0:
            print("[launcher] systemctl daemon-reload 失败", file=sys.stderr)
            sys.exit(1)
        if _sudo(["systemctl", "enable", "--now", "aiplugin4-webui"]) != 0:
            print("[launcher] systemctl enable --now 失败，可手动执行：sudo systemctl enable --now aiplugin4-webui", file=sys.stderr)
            sys.exit(1)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    print("[launcher] 已注册 systemd 服务 aiplugin4-webui（开机自启 + 异常自动拉起）")
    print("[launcher] 查看状态: systemctl status aiplugin4-webui")
    print("[launcher] 查看日志: journalctl -u aiplugin4-webui -f")


def uninstall_webui_service() -> None:
    """移除 systemd 服务（仅 Linux）"""
    if os.name != "posix":
        print("[launcher] 系统服务仅支持 Linux")
        sys.exit(1)
    if not shutil.which("systemctl"):
        _sudo(["rm", "-f", "/etc/systemd/system/aiplugin4-webui.service"])
        print("[launcher] 未检测到 systemd，已尝试删除单元文件")
        return
    _sudo(["systemctl", "disable", "--now", "aiplugin4-webui"])
    _sudo(["rm", "-f", "/etc/systemd/system/aiplugin4-webui.service"])
    _sudo(["systemctl", "daemon-reload"])
    print("[launcher] 已移除 systemd 服务 aiplugin4-webui")


def _no_window_kwargs() -> dict:
    """Windows 下无控制台进程（WebUI/后台）跑子进程时不弹黑框"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _http_open(url: str, timeout: int = 60, method: str = None):
    """带 UA 的 urllib 请求（GitHub API / raw 均需要 User-Agent）"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "aiplugin4-backends-updater/1.0"}, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _latest_release() -> dict:
    """查询 GitHub 最新 release（含 tag 与 zip 资产）；API 失败时兜底解析 latest 重定向"""
    try:
        with _http_open(RELEASE_API) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        with _http_open(f"https://github.com/{GITHUB_REPO}/releases/latest", timeout=30, method="HEAD") as resp:
            final = resp.geturl()
        tag = final.rstrip("/").rsplit("/", 1)[-1].lstrip("v")
        return {
            "tag_name": tag,
            "assets": [{
                "name": f"aiplugin4-backends-{tag}.zip",
                "browser_download_url": f"https://github.com/{GITHUB_REPO}/releases/download/v{tag}/aiplugin4-backends-{tag}.zip",
            }],
        }


def _extract_zip_into_root(zip_path: str, skip_prefixes: tuple = (), skip_files: tuple = ()) -> None:
    """把 zip 解压覆盖到仓库根目录（带 zip 路径穿越防护）；skip_prefixes/skip_files 命中则跳过"""
    root = os.path.realpath(ROOT_DIR)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            rel = member.filename.replace("\\", "/").lstrip("/")
            if not rel or rel.endswith("/"):
                continue
            if rel in skip_files or any(rel.startswith(p) for p in skip_prefixes):
                continue
            dest = os.path.realpath(os.path.join(root, *rel.split("/")))
            if dest != root and not dest.startswith(root + os.sep):
                continue  # 防 zip 路径穿越
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _extract_update_zip(zip_path: str) -> None:
    """本体发布包解压覆盖到仓库根目录；跳过运行时/本地目录（installed、logs、backends、data、dist、.git、.runtime.json）"""
    _extract_zip_into_root(
        zip_path,
        skip_prefixes=("installed/", "logs/", "backends/", "dist/", ".git/", "data/"),
        skip_files=(RUNTIME_FILE,),
    )


def _remote_registry() -> dict:
    """拉取远端 backends.json 注册表（{名称: 条目}）；失败返回空 dict"""
    try:
        with _http_open(f"{RAW_BASE}/backends.json") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {item.get("name"): item for item in (data or {}).get("backends", [])}
    except Exception:  # noqa: BLE001
        return {}


def _release_asset(asset_name: str) -> str:
    """在 GitHub 最新 release 中查找指定资产名，返回下载地址（不存在返回空字符串）"""
    try:
        release = _latest_release()
        for asset in release.get("assets") or []:
            if str(asset.get("name", "")) == asset_name:
                return str(asset.get("browser_download_url") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _changelog_versions(text: str) -> dict:
    """解析 CHANGELOG.md，返回 {版本号: 段落内容}，跳过 Unreleased"""
    result = {}
    for m in re.finditer(r"^##\s+([0-9]+(?:\.[0-9]+)*)(.*?)(?=^##\s|\Z)", text or "", re.S | re.M):
        result[m.group(1)] = m.group(2).strip()
    return result


def _version_key(version: str) -> tuple:
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


def update_project(timeout: int = 120) -> dict:
    """下载最新 release 压缩包直接覆盖更新（不依赖 git，本地文件有改动也不会阻塞）；
    返回 {"updated": bool, "changelog": str, "output": str}；失败抛异常"""
    old_version = read_version()
    release = _latest_release()
    tag = str(release.get("tag_name", "")).lstrip("v")
    if not tag or _version_key(tag) <= _version_key(old_version):
        return {"updated": False, "changelog": "", "output": "Already up to date."}
    zip_url = ""
    for asset in release.get("assets") or []:
        if str(asset.get("name", "")).endswith(".zip"):
            zip_url = asset.get("browser_download_url") or ""
            break
    if not zip_url:
        raise RuntimeError(f"最新版本 {tag} 未找到 zip 安装包，请稍后重试")
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="aib4-update-", suffix=".zip")
        os.close(fd)
        with _http_open(zip_url, timeout=timeout) as resp, open(tmp_path, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
        _extract_update_zip(tmp_path)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"下载/解压更新包失败: {e}")
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    changelog = _update_changelog(old_version)
    return {"updated": True, "changelog": changelog, "output": f"已更新到 {tag}"}


def _update_changelog(old_version: str) -> str:
    """收集新版 CHANGELOG.md 中高于旧版本的段落（按版本号从新到旧）"""
    try:
        with open(os.path.join(ROOT_DIR, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    sections = []
    for version, body in _changelog_versions(text).items():
        if _version_key(version) <= _version_key(old_version):
            continue
        body_lines = body.splitlines()
        date_part = ""
        if body_lines and body_lines[0].lstrip().startswith("-"):
            date_part = body_lines[0].strip()[1:].strip()
            body = "\n".join(body_lines[1:]).strip()
        head = f"## {version}" + (f" - {date_part}" if date_part else "")
        sections.append((_version_key(version), head + (f"\n\n{body}" if body else "")))
    sections.sort(key=lambda item: item[0], reverse=True)
    return "\n\n".join(body for _, body in sections)


_UPDATE_CHECK_CACHE = {"ts": 0.0, "data": {}}
_UPDATE_CHECK_RUNNING = False


def refresh_update_check() -> dict:
    """对比 GitHub 最新 release 与本地版本，返回更新检查结果（网络失败返回空）"""
    try:
        release = _latest_release()
    except Exception:  # noqa: BLE001
        return {}
    tag = str(release.get("tag_name", "")).lstrip("v")
    result = {
        "repo_update": bool(tag and _version_key(tag) > _version_key(read_version())),
        "backends": {},
    }
    remote_registry = _remote_registry()
    for backend in discover_backends():
        remote_v = str((remote_registry.get(backend.name) or {}).get("version", "") or "")
        result["backends"][backend.name] = {
            "local": backend.version,
            "remote": remote_v,
            "available": bool(
                backend.version
                and remote_v
                and _version_key(remote_v) > _version_key(backend.version)
            ),
        }
    return result


def update_check(force: bool = False) -> dict:
    """带缓存的更新检查：60 秒内复用结果；过期时后台刷新（force=True 同步刷新）"""
    global _UPDATE_CHECK_RUNNING
    now = time.time()
    if force:
        _UPDATE_CHECK_CACHE["data"] = refresh_update_check()
        _UPDATE_CHECK_CACHE["ts"] = now
        return _UPDATE_CHECK_CACHE["data"]
    if now - _UPDATE_CHECK_CACHE["ts"] > 60 and not _UPDATE_CHECK_RUNNING:
        _UPDATE_CHECK_RUNNING = True

        def _worker():
            global _UPDATE_CHECK_RUNNING
            try:
                _UPDATE_CHECK_CACHE["data"] = refresh_update_check()
                _UPDATE_CHECK_CACHE["ts"] = time.time()
            finally:
                _UPDATE_CHECK_RUNNING = False

        threading.Thread(target=_worker, daemon=True).start()
    return _UPDATE_CHECK_CACHE["data"]


def _package_files():
    """遍历要打包的文件，返回 (绝对路径, 包内相对路径) 列表"""
    for root, dirs, files in os.walk(BACKENDS_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            if name.endswith(EXCLUDE_SUFFIXES):
                continue
            if name in EXCLUDE_FILES:
                continue
            path = os.path.join(root, name)
            arcname = os.path.relpath(path, ROOT_DIR).replace(os.sep, "/")
            yield path, arcname


def _package_files_under(prefix: str):
    """只打包指定前缀目录下的文件（用于每个后端的独立包，绕过对 backends/ 的整体排除）"""
    base = os.path.join(BACKENDS_DIR, prefix.rstrip("/"))
    if not os.path.isdir(base):
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            if name.endswith(EXCLUDE_SUFFIXES) or name in EXCLUDE_FILES:
                continue
            path = os.path.join(root, name)
            arcname = os.path.relpath(path, ROOT_DIR).replace(os.sep, "/")
            yield path, arcname


def _write_archives(zip_out: str, tar_out: str, files: list) -> None:
    """把 (绝对路径, 包内相对路径) 列表同时写成 zip 与 tar.gz"""
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname)
    with tarfile.open(tar_out, "w:gz") as tf:
        for path, arcname in files:
            tf.add(path, arcname)


def package_backends() -> list:
    """打包本体（全部仓库文件）与每个后端独立包（backends/<名称>/，版本取各自 backend.json），返回产物列表"""
    version = read_version()
    out_dir = os.path.join(ROOT_DIR, "dist")
    os.makedirs(out_dir, exist_ok=True)
    files = list(_package_files())
    artifacts = []

    zip_out = os.path.join(out_dir, f"aiplugin4-backends-{version}.zip")
    tar_out = os.path.join(out_dir, f"aiplugin4-backends-{version}.tar.gz")
    _write_archives(zip_out, tar_out, files)
    print(f"[launcher] 已打包本体: {zip_out}")
    print(f"[launcher] 已打包本体: {tar_out}")
    artifacts.extend([zip_out, tar_out])

    for entry in load_registry():
        name = entry.get("name", "")
        if not name:
            continue
        bv = str(entry.get("version", "") or version)
        pfx = f"backends/{name}/"
        pkg_files = list(_package_files_under(pfx))
        if not pkg_files:
            print(f"[launcher] 跳过后端 {name}（商店目录为空）")
            continue
        zip_out = os.path.join(out_dir, f"aiplugin4-backends-{name}-{bv}.zip")
        tar_out = os.path.join(out_dir, f"aiplugin4-backends-{name}-{bv}.tar.gz")
        _write_archives(zip_out, tar_out, pkg_files)
        print(f"[launcher] 已打包后端 {name} v{bv}: {zip_out}")
        print(f"[launcher] 已打包后端 {name} v{bv}: {tar_out}")
        artifacts.extend([zip_out, tar_out])
    return artifacts


def ensure_cli_installed() -> None:
    """launcher 启动时自动安装/刷新 aibackend 命令（幂等，Windows/Linux 均适配）"""
    try:
        import install_cli

        bin_dir = install_cli.install()
        exe = "aibackend.cmd" if os.name == "nt" else "aibackend"
        print(f"[launcher] aibackend 命令已就绪: {os.path.join(bin_dir, exe)}")
        if os.name == "nt":
            print("[launcher] 新打开的终端即可使用 aibackend（当前终端请重新打开）")
        else:
            print("[launcher] 新打开的终端即可使用 aibackend（当前终端可 source ~/.bashrc 立即生效）")
    except Exception as e:  # noqa: BLE001
        print(f"[launcher] aibackend 自动安装失败（可手动执行 python install_cli.py）: {e}", file=sys.stderr)


def cleanup_legacy_backend_dirs() -> None:
    """升级清理：旧版后端目录在仓库顶层，git 只搬被跟踪文件，node_modules/.venv/缓存等未跟踪残留会留在原地。
    仅当目录已完全不被 git 跟踪时，用 git clean 删掉整目录（含忽略文件），避免升级后残留占空间/干扰新模型。"""
    if not shutil.which("git") or not os.path.isdir(os.path.join(BACKENDS_DIR, ".git")):
        return
    for name in LEGACY_BACKEND_DIRS:
        legacy = os.path.join(BACKENDS_DIR, name)
        if not os.path.isdir(legacy):
            continue
        try:
            out = subprocess.run(
                ["git", "ls-files", name],
                cwd=BACKENDS_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                **_no_window_kwargs(),
            )
        except Exception:  # noqa: BLE001
            continue
        if (out.stdout or "").strip():
            continue  # 目录仍被 git 跟踪（用户改过文件），不动
        subprocess.run(
            ["git", "clean", "-fdx", "--", name],
            cwd=BACKENDS_DIR,
            capture_output=True,
            **_no_window_kwargs(),
        )
        print(f"[launcher] 已清理旧版顶层后端残留目录: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="launcher",
        description="aiplugin4 后端管理：直接运行本脚本启动 WebUI",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="列出后端与启用/运行状态")
    install_b_p = sub.add_parser("install-backend", help="安装后端：下载程序文件并安装依赖")
    install_b_p.add_argument("name")
    sub.add_parser("uninstall-backend", help="卸载后端：停止并删除程序与依赖").add_argument("name")
    port_p = sub.add_parser("port", help="查看/修改后端端口（重启后端生效）")
    port_p.add_argument("name")
    port_p.add_argument("value", nargs="?", help="新端口(1-65535)，或 reset 恢复默认")
    start_p = sub.add_parser("start", help="启动后端（首次自动创建 venv 并安装依赖）")
    start_p.add_argument("names", nargs="*")
    start_p.add_argument("--all", action="store_true", help="启动全部后端")
    start_p.add_argument("--background", action="store_true", help="后台运行（Linux 用 setsid，Windows 用 DETACHED_PROCESS）")
    sub.add_parser("stop", help="停止后端").add_argument("names", nargs="*")
    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("package", help="打包 backends/ 为 zip")
    webui_p = sub.add_parser("webui", help="启动 Web 管理界面")
    webui_p.add_argument("--host", default=None, help="监听地址（默认取 webui-host 配置，未配置为 0.0.0.0）")
    webui_p.add_argument("--port", type=int, default=None, help="监听端口（默认取 webui-port 配置，首次运行随机生成五位数）")
    webui_p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    sub.add_parser("webui-stop", help="停止后台 WebUI")
    sub.add_parser("webui-restart", help="重启后台 WebUI（先停止再启动，用于重新加载后端清单）")
    webui_port_p = sub.add_parser("webui-port", help="查看/修改 WebUI 端口（修改后自动重启 WebUI）")
    webui_port_p.add_argument("value", nargs="?", help="新端口 1-65535，或 reset 重新随机生成")
    webui_host_p = sub.add_parser("webui-host", help="查看/修改 WebUI 监听地址（修改后自动重启 WebUI）")
    webui_host_p.add_argument("value", nargs="?", help="监听地址(如 0.0.0.0 / 127.0.0.1)，或 reset 恢复默认 0.0.0.0")
    webui_token_p = sub.add_parser("webui-token", help="查看/修改 WebUI 访问 token（修改后自动重启 WebUI）")
    webui_token_p.add_argument("value", nargs="?", help="新 token，或 reset 重新生成")
    sub.add_parser("service-install", help="[Linux] 注册 systemd 服务：开机自启 + 自动拉起 WebUI")
    sub.add_parser("service-uninstall", help="[Linux] 移除 systemd 服务")
    args = parser.parse_args()

    ensure_cli_installed()
    cleanup_legacy_backend_dirs()

    config = load_config()
    backends = discover_backends()
    supervisor = Supervisor(config)

    if not args.command:
        # 直接运行 launcher：后台启动 WebUI（不占用终端），并自动打开浏览器
        ensure_webui_deps()
        start_webui_background()
        return

    by_name = {b.name: b for b in backends}

    def find(names: list) -> list:
        missing = [n for n in names if n not in by_name]
        if missing:
            print(f"[launcher] 未知后端: {', '.join(missing)}（可用: {', '.join(by_name)}）")
            sys.exit(1)
        return [by_name[n] for n in names]

    if args.command == "list":
        for backend in backends:
            state = "运行中" if supervisor.is_running(backend.name) else "已停止"
            print(f"{backend.name:24s} port={effective_port(backend):<6d} [{state}] {backend.description}")
        return

    if args.command == "install-backend":
        try:
            install_backend(args.name)
        except Exception as e:  # noqa: BLE001
            print(f"[launcher] 安装失败: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "uninstall-backend":
        backend = find([args.name])[0]
        supervisor.stop([backend])
        time.sleep(1)
        remove_backend_dir(args.name)
        print(f"[launcher] 已卸载后端: {args.name}")
        return

    if args.command == "port":
        backend = find([args.name])[0]
        if args.value is None:
            cfg = backend_config(backend)
            print(f"{backend.name} 端口: {cfg['port']}（默认 {backend.port}），监听IP: {cfg['host']}，token: {'已设置' if cfg['token'] else '未设置'}")
            return
        if args.value == "reset":
            runtime = load_runtime()
            runtime.get("config", {}).pop(backend.name, None)
            runtime.get("ports", {}).pop(backend.name, None)
            save_runtime(runtime)
            print(f"{backend.name} 端口已恢复默认 {backend.port}")
            return
        try:
            value = int(args.value)
        except ValueError:
            print("[launcher] 端口必须是 1-65535 的整数")
            sys.exit(1)
        if not 1 <= value <= 65535:
            print("[launcher] 端口必须是 1-65535 的整数")
            sys.exit(1)
        save_backend_config(backend.name, port=value)
        print(f"{backend.name} 端口已设为 {value}（重启后端后生效）")
        return

    if args.command == "webui-port":
        try:
            port = configure_webui_port(args.value)
        except ValueError as e:
            print(f"[launcher] {e}")
            sys.exit(1)
        if args.value is None:
            print(f"[launcher] WebUI 端口: {port}（首次运行随机生成，保存在 .runtime.json）")
        elif args.value == "reset":
            print(f"[launcher] WebUI 端口已重新随机生成: {port}")
        else:
            print(f"[launcher] WebUI 端口已设为 {port}")
        return

    if args.command == "webui-host":
        try:
            host = configure_webui_host(args.value)
        except ValueError as e:
            print(f"[launcher] {e}")
            sys.exit(1)
        if args.value is None:
            print(f"[launcher] WebUI 监听地址: {host}（默认 0.0.0.0，保存在 .runtime.json）")
        elif args.value == "reset":
            print("[launcher] WebUI 监听地址已恢复默认 0.0.0.0")
        else:
            print(f"[launcher] WebUI 监听地址已设为 {host}")
        return

    if args.command == "webui-token":
        try:
            token = configure_webui_token(args.value)
        except ValueError as e:
            print(f"[launcher] {e}")
            sys.exit(1)
        if args.value is None:
            print(f"[launcher] WebUI token: {token}（保存在 .runtime.json）")
        elif args.value == "reset":
            print(f"[launcher] WebUI token 已重新生成: {token}")
        else:
            print("[launcher] WebUI token 已更新")
        return

    if args.command == "start":
        if args.background:
            script = os.path.abspath(__file__)
            cmd = [sys.executable, script, "start"] + list(args.names)
            if args.all:
                cmd.append("--all")
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(
                cmd,
                cwd=BACKENDS_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs
            )
            print("[launcher] 已在后台启动，可用 stop/status 管理")
            return
        if args.names:
            targets = find(args.names)
        elif args.all:
            targets = backends
        else:
            print("[launcher] 请指定后端名称或使用 --all 启动全部（默认不启动任何后端）")
            return
        for backend in targets:
            if backend.name in supervisor.state.setdefault("stopped", []):
                supervisor.state["stopped"].remove(backend.name)  # 手动启动清除停止标记
        supervisor._save_state()
        failed = supervisor.start(targets)
        if failed:
            print(f"[launcher] 端口被占用，跳过: {', '.join(failed)}（请先结束占用进程）")
        print("后端已启动，按 Ctrl+C 停止全部")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            supervisor.stop(targets)
            print("\n全部已停止")
        return

    if args.command == "stop":
        supervisor.stop(find(args.names) if args.names else backends)
        return

    if args.command == "status":
        supervisor.status(backends)
        return

    if args.command == "package":
        package_backends()
        return

    if args.command == "webui":
        # 与直接运行 launcher 等价（子命令由 WebUI 内部调用）
        launch_webui(backends, config, supervisor, host=args.host, port=args.port, open_browser=not args.no_browser)
        return

    if args.command == "webui-stop":
        stop_webui()
        return

    if args.command == "webui-restart":
        stop_webui()
        time.sleep(0.5)
        start_webui_background(open_browser=False)
        return

    if args.command == "service-install":
        install_webui_service()
        return

    if args.command == "service-uninstall":
        uninstall_webui_service()
        return

    parser.error(f"未知命令: {args.command}")


if __name__ == "__main__":
    main()
