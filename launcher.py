#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aiplugin4 后端管理入口（仅依赖 Python 标准库）。

直接运行本脚本：自动检查/安装 WebUI 自身依赖，然后启动 Web 管理界面
（默认 http://127.0.0.1:8910），后端安装、启停、端口管理都在页面里完成。
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import dataclass

BACKENDS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BACKENDS_DIR  # launcher 位于仓库根目录
CONFIG_FILE = os.path.join(BACKENDS_DIR, "launcher.json")
MANIFEST_FILE = "backend.json"
DEFAULT_LOG_DIR = "logs"
RUNTIME_FILE = ".runtime.json"
WEBUI_PID_FILE = os.path.join(ROOT_DIR, "logs", "webui.pid")
VENV_DIR_NAME = ".venv"
DEPS_MARKER = ".deps_ready"

# 打包时排除的目录/文件
EXCLUDE_DIRS = {"logs", "node_modules", "__pycache__", ".venv", "venv", "dist", ".git"}
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
    """后端运行配置：端口/token/监听IP，优先 .runtime.json（端口兼容旧版 ports 字段）"""
    rt = load_runtime()
    cfg = rt.get("config", {}).get(backend.name) or {}
    port = cfg.get("port")
    if port is None:
        port = rt.get("ports", {}).get(backend.name, backend.port)
    return {
        "port": int(port),
        "token": str(cfg.get("token") or ""),
        "host": str(cfg.get("host") or "0.0.0.0"),
    }


def save_backend_config(name: str, port=None, token=None, host=None) -> dict:
    """保存后端运行配置到 .runtime.json（只更新传入字段），端口同步旧版 ports 字段"""
    rt = load_runtime()
    cfg = rt.setdefault("config", {}).setdefault(name, {})
    if port is not None:
        cfg["port"] = int(port)
        rt.setdefault("ports", {})[name] = int(port)
    if token is not None:
        cfg["token"] = str(token)
    if host is not None:
        cfg["host"] = str(host) or "0.0.0.0"
    save_runtime(rt)
    return cfg


def effective_port(backend: Backend) -> int:
    """有效端口：优先 .runtime.json 中的覆盖值，否则用 backend.json 默认值"""
    return backend_config(backend)["port"]


def effective_webui_port(default: int = 8910) -> int:
    """WebUI 管理界面端口：优先 .runtime.json 中的覆盖值，否则默认 8910"""
    return int(load_runtime().get("webui", {}).get("port", default))


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
        port = 8910
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


def discover_backends() -> list:
    backends = []
    for entry in sorted(os.listdir(BACKENDS_DIR)):
        manifest = os.path.join(BACKENDS_DIR, entry, MANIFEST_FILE)
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
            dir=os.path.join(BACKENDS_DIR, entry),
        ))
    return backends


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
    """为 python 后端创建/复用独立 venv 并按需安装依赖，返回 venv 内的 python 路径"""
    py = venv_python_path(backend.dir)
    marker = os.path.join(backend.dir, VENV_DIR_NAME, DEPS_MARKER)
    current = deps_hash(backend)
    if os.path.isfile(py):
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == current:
                    return py
        except OSError:
            pass
        print(f"[launcher] {backend.name} 依赖清单有变化，重新安装")
    else:
        print(f"[launcher] {backend.name} 首次运行，创建独立虚拟环境...")
        subprocess.check_call([sys.executable, "-m", "venv", os.path.join(backend.dir, VENV_DIR_NAME)])
    if not current:
        print(f"[launcher] 跳过 {backend.name}: 缺少 {backend.deps}")
    else:
        subprocess.check_call([py, "-m", "pip", "install", "-r", os.path.join(backend.dir, backend.deps)])
    with open(marker, "w", encoding="utf-8") as f:
        f.write(current)
    return py


def ensure_node(backend: Backend) -> str:
    """为 node 后端确保依赖就绪（node_modules 存在且安装标记齐全，否则 npm install）"""
    node_modules = os.path.join(backend.dir, "node_modules")
    marker = os.path.join(node_modules, ".install_ok")
    if os.path.isfile(marker):
        return "node"
    print(f"[launcher] {backend.name} 首次运行或依赖不完整，npm install...")
    npm = "npm.cmd" if os.name == "nt" else "npm"  # Windows 下 npm 是 .cmd 垫片
    subprocess.check_call([npm, "install"], cwd=backend.dir)
    with open(marker, "w", encoding="utf-8") as f:
        f.write("ok")
    return "node"


def deps_ready(backend: Backend) -> bool:
    """后端依赖是否已就绪：python 后端看 venv 解释器与 .deps_ready 标记，node 后端看 node_modules/.install_ok"""
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
    return os.path.isfile(os.path.join(backend.dir, "node_modules", ".install_ok"))


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
        )
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _missing_chromium_libs(chrome: str) -> list:
    """ldd 检查 Chromium 缺少哪些共享库（'not found' 行）"""
    try:
        out = subprocess.run(["ldd", chrome], capture_output=True, text=True, timeout=60)
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
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

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
            return False
        cfg = backend_config(backend)
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

    def start(self, backends: list) -> None:
        for backend in backends:
            self.stop_flags[backend.name] = threading.Event()
            self.spawn(backend)
            threading.Thread(target=self._monitor, args=(backend,), daemon=True).start()

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
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req])


def launch_webui(backends, config, supervisor, host: str = "127.0.0.1", port: int = None, open_browser: bool = True) -> None:
    """安装 WebUI 依赖并启动管理界面（阻塞，Ctrl+C 退出）"""
    ensure_webui_deps()
    port = int(port) if port else effective_webui_port()
    try:
        from webui import run_webui
    except ImportError:
        print("[launcher] webui 模块缺失（webui.py）")
        sys.exit(1)
    print(f"[launcher] 启动后端管理界面: http://{host}:{port}")
    run_webui(backends, config, supervisor, host=host, port=port, open_browser=open_browser)


def start_webui_background(host: str = "127.0.0.1", port: int = None, open_browser: bool = True) -> int:
    """后台启动 WebUI（不占用终端、无控制台窗口），返回 pid；已在运行则返回现有 pid"""
    port = int(port) if port else effective_webui_port()
    log_dir = os.path.join(ROOT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    if os.path.exists(WEBUI_PID_FILE):
        try:
            with open(WEBUI_PID_FILE, encoding="utf-8") as f:
                old_pid = int(f.read().strip() or 0)
            if old_pid and Supervisor._pid_alive(old_pid):
                url = f"http://{host}:{port}"
                print(f"[launcher] WebUI 已在后台运行 (pid={old_pid})，无需重复启动: {url}")
                if open_browser and os.name == "nt":
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
        f.write(str(proc.pid))
    print(f"[launcher] WebUI 已在后台启动: http://{host}:{port} (pid={proc.pid}, 日志=logs/webui.log)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    return proc.pid


def stop_webui() -> bool:
    """停止后台 WebUI，返回是否成功停止"""
    if not os.path.exists(WEBUI_PID_FILE):
        print("[launcher] WebUI 未在后台运行（无 pid 文件）")
        return False
    try:
        with open(WEBUI_PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
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
Description=aiplugin4 WebUI
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


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **_no_window_kwargs(),
        )
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
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
    """git pull 更新项目（手动触发）；返回 {"updated": bool, "changelog": str, "output": str}；失败抛异常"""
    old_head = _git_head()
    try:
        proc = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window_kwargs(),
        )
    except FileNotFoundError:
        raise RuntimeError("未找到 git 命令，请先安装 Git")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git pull 超时（>{timeout}s）")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"git pull 失败（exit {proc.returncode}）:\n{output}")
    new_head = _git_head()
    if not new_head or new_head == old_head:
        return {"updated": False, "changelog": "", "output": output or "Already up to date."}
    return {"updated": True, "changelog": _update_changelog(old_head, new_head), "output": output}


def _update_changelog(old_head: str, new_head: str) -> str:
    """有新提交时，收集 CHANGELOG.md 中新增且高于当前 VERSION 的版本段落；没有则退回 git log"""
    try:
        old_text = subprocess.run(
            ["git", "show", f"{old_head}:CHANGELOG.md"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **_no_window_kwargs(),
        ).stdout
        old_versions = set(_changelog_versions(old_text))
        new_file = os.path.join(ROOT_DIR, "CHANGELOG.md")
        try:
            with open(new_file, encoding="utf-8") as f:
                new_text = f.read()
        except OSError:
            new_text = ""
        current = _version_key(read_version())
        sections = []
        for version, body in _changelog_versions(new_text).items():
            if version not in old_versions and _version_key(version) > current:
                body_lines = body.splitlines()
                date_part = ""
                if body_lines and body_lines[0].lstrip().startswith("-"):
                    date_part = body_lines[0].strip()[1:].strip()
                    body = "\n".join(body_lines[1:]).strip()
                head = f"## {version}" + (f" - {date_part}" if date_part else "")
                section = head + (f"\n\n{body}" if body else "")
                sections.append((_version_key(version), section))
        if sections:
            sections.sort(key=lambda item: item[0], reverse=True)
            return "\n\n".join(body for _, body in sections)
    except Exception:  # noqa: BLE001
        pass
    log = subprocess.run(
        ["git", "log", "--oneline", "--no-decorate", f"{old_head}..{new_head}"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        **_no_window_kwargs(),
    ).stdout.strip()
    return log or "已拉取更新"


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


def package_backends() -> list:
    version = read_version()
    out_dir = os.path.join(ROOT_DIR, "dist")
    os.makedirs(out_dir, exist_ok=True)
    zip_out = os.path.join(out_dir, f"aiplugin4-backends-{version}.zip")
    tar_out = os.path.join(out_dir, f"aiplugin4-backends-{version}.tar.gz")
    files = list(_package_files())
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname)
    with tarfile.open(tar_out, "w:gz") as tf:
        for path, arcname in files:
            tf.add(path, arcname)
    print(f"[launcher] 已打包: {zip_out}")
    print(f"[launcher] 已打包: {tar_out}")
    return [zip_out, tar_out]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="launcher",
        description="aiplugin4 后端管理：直接运行本脚本启动 WebUI",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="列出后端与启用/运行状态")
    setup_p = sub.add_parser("setup", help="安装依赖（幂等，python 后端装入独立 venv）")
    setup_p.add_argument("names", nargs="*")
    setup_p.add_argument("--all", action="store_true", help="安装全部后端")
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
    webui_p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    webui_p.add_argument("--port", type=int, default=None, help="监听端口（默认取 webui-port 配置，未配置为 8910）")
    webui_p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    sub.add_parser("webui-stop", help="停止后台 WebUI")
    webui_port_p = sub.add_parser("webui-port", help="查看/修改 WebUI 端口（修改后自动重启 WebUI）")
    webui_port_p.add_argument("value", nargs="?", help="新端口 1-65535，或 reset 恢复默认 8910")
    sub.add_parser("service-install", help="[Linux] 注册 systemd 服务：开机自启 + 自动拉起 WebUI")
    sub.add_parser("service-uninstall", help="[Linux] 移除 systemd 服务")
    args = parser.parse_args()

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

    if args.command == "setup":
        targets = backends if args.all else find(args.names) if args.names else []
        if not targets:
            print("[launcher] 请指定后端名称或使用 --all 安装全部")
            return
        for backend in targets:
            setup_backend(backend)
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
            print(f"[launcher] WebUI 端口: {port}（默认 8910，保存在 .runtime.json）")
        elif args.value == "reset":
            print(f"[launcher] WebUI 端口已恢复默认 8910")
        else:
            print(f"[launcher] WebUI 端口已设为 {port}")
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
        supervisor.start(targets)
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

    if args.command == "service-install":
        install_webui_service()
        return

    if args.command == "service-uninstall":
        uninstall_webui_service()
        return

    parser.error(f"未知命令: {args.command}")


if __name__ == "__main__":
    main()
