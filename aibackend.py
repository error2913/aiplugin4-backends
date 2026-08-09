#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aibackend — aiplugin4-backends 的命令行管理工具。

安装命令（把 aibackend 写入 PATH）：
  python install_cli.py

用法示例：
  aibackend help [命令]                查看帮助（如 aibackend help start）
  aibackend list                       查看所有后端状态
  aibackend start --all                后台启动全部（默认后台守护）
  aibackend start <后端名>              后台启动单个
  aibackend start <后端名> --foreground 前台运行（Ctrl+C 停止）
  aibackend stop --all                 停止全部
  aibackend restart <后端名>            重启单个
  aibackend logs <后端名>               查看日志（-n 行数，-f 跟随）
  aibackend info <后端名>               查看进程详情（pid/时长/内存/拉起次数）
  aibackend monitor                    实时监控面板
  aibackend install-backend <后端名>    安装后端（下载程序 + 安装依赖）
  aibackend uninstall-backend <后端名>  卸载后端（停止并删除程序与依赖）
  aibackend update                     从 Git 拉取项目更新
  aibackend webui                      后台启动 Web 管理界面（不占终端）
  aibackend webui-stop                 停止后台 WebUI
  aibackend webui-restart              重启后台 WebUI（重新加载后端清单）
  aibackend webui-port [端口|reset]    查看/修改 WebUI 端口（修改后自动重启）
  aibackend webui-host [地址|reset]    查看/修改 WebUI 监听地址（修改后自动重启）
  aibackend webui-token [token|reset]  查看/修改 WebUI 访问 token（修改后自动重启）
  aibackend uninstall                  卸载 aibackend 命令（删除命令与 PATH 配置）
  aibackend service-install            [Linux] 注册 systemd 服务（开机自启 + 自动拉起）
  aibackend service-uninstall          [Linux] 停止并移除 systemd 服务

命令行与 WebUI 共用同一套后端进程与状态（logs/state.json）。
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

from launcher import (
    ROOT_DIR,
    Supervisor,
    configure_webui_host,
    configure_webui_port,
    configure_webui_token,
    deps_ready,
    discover_backends,
    effective_port,
    install_backend,
    load_config,
    process_memory,
    remove_backend_dir,
    install_webui_service,
    start_webui_background,
    stop_webui,
    uninstall_webui_service,
    update_project,
)

COMMANDS = [
    ("list", "查看所有后端状态"),
    ("help", "查看帮助"),
    ("start", "启动后端（默认后台守护）"),
    ("stop", "停止后端（默认停止全部）"),
    ("restart", "重启后端（默认后台）"),
    ("logs", "查看后端日志"),
    ("info", "查看后端详情"),
    ("monitor", "实时监控面板"),
    ("install-backend", "安装后端（下载程序 + 安装依赖）"),
    ("uninstall-backend", "卸载后端（停止并删除程序与依赖）"),
    ("update", "从 Git 拉取项目更新"),
    ("webui", "后台启动 Web 管理界面（不占终端）"),
    ("webui-stop", "停止后台 WebUI"),
    ("webui-restart", "重启后台 WebUI（先停止再启动，重新加载后端清单）"),
    ("webui-port", "查看/修改 WebUI 端口（修改后自动重启 WebUI）"),
    ("webui-host", "查看/修改 WebUI 监听地址（修改后自动重启 WebUI）"),
    ("webui-token", "查看/修改 WebUI 访问 token（修改后自动重启 WebUI）"),
    ("uninstall", "卸载 aibackend 命令（删除命令与 PATH 配置）"),
    ("service-install", "[Linux] 注册 systemd 服务：开机自启 + 自动拉起 WebUI"),
    ("service-uninstall", "[Linux] 停止并移除 systemd 服务"),
]


def _enable_vt():
    """Windows 旧版控制台（非 Windows Terminal）默认不解析 ANSI 颜色，这里显式开启 VT 支持"""
    if os.name == "nt" and sys.stdout.isatty():
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:  # noqa: BLE001
            pass


_enable_vt()
_USE_COLOR = sys.stdout.isatty()
GREEN = "\x1b[32m" if _USE_COLOR else ""
CYAN = "\x1b[36m" if _USE_COLOR else ""
DIM = "\x1b[2m" if _USE_COLOR else ""
BOLD = "\x1b[1m" if _USE_COLOR else ""
RESET = "\x1b[0m" if _USE_COLOR else ""


def fmt_uptime(secs):
    if secs is None:
        return "-"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def backend_rows(supervisor):
    rows = []
    for b in discover_backends():
        info = supervisor.state.get(b.name) or {}
        running = supervisor.is_running(b.name)
        uptime = None
        if running and info.get("started_at"):
            try:
                started = datetime.strptime(info["started_at"], "%Y-%m-%d %H:%M:%S")
                uptime = max(0, int(time.time() - started.timestamp()))
            except (ValueError, TypeError):
                uptime = None
        mem = None
        if running and info.get("pid"):
            m = process_memory(info.get("pid"))
            if m and m[1]:
                mem = round(m[0] / 1024 / 1024, 1)
        rows.append({
            "name": b.name,
            "running": running,
            "uptime": uptime,
            "restarts": supervisor.state.get("restarts", {}).get(b.name, 0),
            "mem": mem,
            "port": effective_port(b),
            "deps": deps_ready(b),
            "pid": info.get("pid"),
        })
    return rows


def print_list(supervisor):
    rows = backend_rows(supervisor)
    print(f"{'NAME':22s} {'STATUS':5s} {'UPTIME':10s} {'RESTARTS':8s} {'MEM':9s} {'PORT':6s} {'DEPS'}")
    print("-" * 72)
    for r in rows:
        status = "在线" if r["running"] else "离线"
        mem = f"{r['mem']}MB" if r["mem"] is not None else "-"
        deps = "已装" if r["deps"] else "未装"
        print(f"{r['name']:22s} {status:5s} {fmt_uptime(r['uptime']):10s} {r['restarts']:<8d} {mem:9s} {r['port']:<6d} {deps}")
    running = sum(1 for r in rows if r["running"])
    print(f"\n共 {running}/{len(rows)} 个后端在运行")


def resolve(args, allow_all_default=False):
    backends = discover_backends()
    if args.all or (allow_all_default and not args.names):
        return backends
    if not args.names:
        return []
    by_name = {b.name: b for b in backends}
    missing = [n for n in args.names if n not in by_name]
    if missing:
        print(f"未知后端: {', '.join(missing)}")
        sys.exit(1)
    return [by_name[n] for n in args.names]


def daemon_start(targets, all_flag):
    script = os.path.join(ROOT_DIR, "launcher.py")
    cmd = [sys.executable, script, "start"]
    if all_flag:
        cmd.append("--all")
    else:
        cmd += [t.name for t in targets]
    cmd.append("--background")
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
        cwd=ROOT_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def cmd_start(args, supervisor):
    targets = resolve(args)
    if not targets:
        print("请指定后端名称或使用 --all")
        return
    if not args.foreground:
        daemon_start(targets, args.all)
        names = "全部" if args.all else ", ".join(t.name for t in targets)
        print(f"已在后台启动 {names}（首次自动按需安装依赖），可用 aibackend list / logs 查看")
        return
    for t in targets:
        if t.name in supervisor.state.setdefault("stopped", []):
            supervisor.state["stopped"].remove(t.name)
    supervisor._save_state()
    supervisor.start(targets)
    print("已启动，按 Ctrl+C 停止全部")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        supervisor.stop(targets)
        print("\n全部已停止")


def cmd_stop(args, supervisor):
    targets = resolve(args, allow_all_default=True)
    supervisor.stop(targets)
    print("已停止")


def cmd_restart(args, supervisor):
    targets = resolve(args)
    if not targets:
        print("请指定后端名称或使用 --all")
        return
    supervisor.stop(targets)
    time.sleep(0.5)
    if not args.foreground:
        daemon_start(targets, args.all)
        names = "全部" if args.all else ", ".join(t.name for t in targets)
        print(f"已重启 {names}（后台）")
    else:
        supervisor.start(targets)
        print(f"已重启 {'、'.join(t.name for t in targets)}（前台，Ctrl+C 停止）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            supervisor.stop(targets)
            print("\n已停止")


def cmd_logs(args):
    log_path = os.path.join(ROOT_DIR, "logs", args.name + ".log")
    if not os.path.exists(log_path):
        print(f"暂无日志文件: {log_path}")
        return
    if args.follow:
        with open(log_path, "rb") as f:
            data = f.read()
        tail = b"".join(data.splitlines(keepends=True)[-args.lines:])
        sys.stdout.buffer.write(tail)
        sys.stdout.buffer.flush()
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            try:
                while True:
                    time.sleep(0.5)
                    f.seek(pos)
                    new = f.read()
                    if new:
                        sys.stdout.buffer.write(new)
                        sys.stdout.buffer.flush()
                        pos = f.tell()
            except KeyboardInterrupt:
                pass
        return
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    print("".join(lines[-args.lines:]), end="")


def cmd_info(args, supervisor):
    b = next((x for x in discover_backends() if x.name == args.name), None)
    if not b:
        print(f"未知后端: {args.name}")
        sys.exit(1)
    r = next(x for x in backend_rows(supervisor) if x["name"] == args.name)
    print(f"名称     : {r['name']}")
    status = "在线" if r["running"] else "离线"
    if r["running"] and r["pid"]:
        status += f" (pid={r['pid']})"
    print(f"状态     : {status}")
    print(f"运行时长 : {fmt_uptime(r['uptime'])}")
    print(f"自动拉起 : {r['restarts']} 次")
    print(f"内存     : {r['mem']}MB" if r["mem"] is not None else "内存     : -")
    print(f"端口     : {r['port']}（默认 {b.port}）")
    print(f"依赖     : {'已安装' if r['deps'] else '未安装'}")
    print(f"类型     : {b.type}")
    print(f"描述     : {b.description}")
    print(f"日志     : logs/{args.name}.log")


def cmd_help(args, parser):
    if args.topic:
        parser.parse_args([args.topic, "--help"])
        return
    print()
    print(f"  {GREEN}aibackend{RESET}  {BOLD}aiplugin4-backends{RESET} 的命令行管理工具")
    print(f"  {DIM}命令行与 WebUI 共用同一套后端进程与状态（logs/state.json）{RESET}")
    print()
    print(f"  {GREEN}用法:{RESET}")
    print("    aibackend <命令> [参数]")
    print()
    print(f"  {GREEN}命令:{RESET}")
    for name, desc in COMMANDS:
        if name in ("service-install", "service-uninstall") and os.name != "posix":
            continue  # 系统服务仅 Linux，其他平台不展示
        print(f"    {CYAN}{name:<12}{RESET}{desc}")
    print()
    print(f"  {DIM}查看单个命令详细参数：aibackend help <命令>{RESET}")
    print()


def cmd_monitor(args, supervisor):
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            print("aibackend monitor（Ctrl+C 退出）\n")
            print_list(supervisor)
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def cmd_install_backend(args):
    try:
        install_backend(args.name)
    except Exception as e:  # noqa: BLE001
        print(f"安装失败: {e}")
        sys.exit(1)
    print(f"已安装后端: {args.name}")


def cmd_uninstall_backend(args, supervisor):
    backend = next((b for b in discover_backends() if b.name == args.name), None)
    if not backend:
        print(f"未知后端: {args.name}")
        sys.exit(1)
    supervisor.stop([backend])
    time.sleep(1)
    remove_backend_dir(args.name)
    print(f"已卸载后端: {args.name}")


def cmd_update(args):
    try:
        res = update_project()
    except Exception as e:  # noqa: BLE001
        print(f"更新失败：{e}")
        sys.exit(1)
    if not res["updated"]:
        print("没有可以更新的")
        return
    print("更新完成：")
    print(res["changelog"] or res["output"] or "已拉取更新")
    print("提示：若更新了 launcher/webui，请重启对应进程后生效")


def cmd_webui(args):
    start_webui_background(host=args.host, port=args.port, open_browser=not args.no_browser)


def cmd_webui_stop(args):
    stop_webui()


def cmd_webui_restart(args):
    stop_webui()
    time.sleep(0.5)
    start_webui_background(open_browser=False)


def cmd_webui_port(args):
    try:
        port = configure_webui_port(args.value)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    if args.value is None:
        print(f"WebUI 端口: {port}（首次运行随机生成）")
    elif args.value == "reset":
        print(f"WebUI 端口已重新随机生成: {port}")
    else:
        print(f"WebUI 端口已设为 {port}")


def cmd_webui_host(args):
    try:
        host = configure_webui_host(args.value)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    if args.value is None:
        print(f"WebUI 监听地址: {host}（默认 0.0.0.0）")
    elif args.value == "reset":
        print("WebUI 监听地址已恢复默认 0.0.0.0")
    else:
        print(f"WebUI 监听地址已设为 {host}")


def cmd_webui_token(args):
    try:
        token = configure_webui_token(args.value)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    if args.value is None:
        print(f"WebUI token: {token}")
    elif args.value == "reset":
        print(f"WebUI token 已重新生成: {token}")
    else:
        print("WebUI token 已更新")


def cmd_service_install(args):
    install_webui_service()


def cmd_service_uninstall(args):
    uninstall_webui_service()


def _remove_path_windows(bin_dir: str) -> bool:
    """从用户 PATH（注册表）移除 bin_dir，返回是否改动"""
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    )
    try:
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return False
        items = [p for p in current.split(";") if p and p.lower() != bin_dir.lower()]
        changed = len(items) != len([p for p in current.split(";") if p])
        if changed:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(items))
    finally:
        winreg.CloseKey(key)
    if changed:
        try:
            import ctypes

            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x1A, 0, "Environment", 0, 1000, None
            )
        except Exception:  # noqa: BLE001
            pass
    return changed


def _remove_path_unix(bin_dir: str) -> bool:
    """从 .bashrc / .zshrc / .profile 移除安装脚本写入的 PATH 行"""
    changed = False
    for name in (".bashrc", ".zshrc", ".profile"):
        rc = os.path.join(os.path.expanduser("~"), name)
        if not os.path.isfile(rc):
            continue
        try:
            with open(rc, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        new_lines = [ln for ln in lines if not (bin_dir in ln and "PATH" in ln)]
        if len(new_lines) != len(lines):
            try:
                with open(rc, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                changed = True
            except OSError:
                pass
    return changed


def cmd_uninstall(args):
    """卸载 aibackend 命令：删除命令文件并移除 PATH 配置"""
    bin_dir = os.path.join(os.path.expanduser("~"), ".aibackend", "bin")
    shim = os.path.join(bin_dir, "aibackend.cmd" if os.name == "nt" else "aibackend")
    removed_file = False
    try:
        if os.path.isfile(shim):
            os.remove(shim)
            removed_file = True
    except OSError as e:
        print(f"删除命令失败: {e}")
    try:
        if os.path.isdir(bin_dir) and not os.listdir(bin_dir):
            os.rmdir(bin_dir)
    except OSError:
        pass
    if os.name == "nt":
        removed_path = _remove_path_windows(bin_dir)
    else:
        removed_path = _remove_path_unix(bin_dir)
    if removed_path:
        print("已从 PATH 移除:", bin_dir)
    print("已删除 aibackend 命令" if removed_file else "未找到 aibackend 命令（可能已卸载）")
    print("卸载完成，重新打开终端后生效")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="aibackend",
        description="aiplugin4-backends 的命令行管理工具",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="查看所有后端状态")
    help_p = sub.add_parser("help", help="查看帮助")
    help_p.add_argument("topic", nargs="?", help="子命令名称（可选）")

    start_p = sub.add_parser("start", help="启动后端（默认后台守护）")
    start_p.add_argument("names", nargs="*")
    start_p.add_argument("--all", action="store_true", help="启动全部")
    start_p.add_argument("--foreground", action="store_true", help="前台运行，Ctrl+C 停止")

    stop_p = sub.add_parser("stop", help="停止后端（默认停止全部）")
    stop_p.add_argument("names", nargs="*")
    stop_p.add_argument("--all", action="store_true")

    restart_p = sub.add_parser("restart", help="重启后端（默认后台）")
    restart_p.add_argument("names", nargs="*")
    restart_p.add_argument("--all", action="store_true")
    restart_p.add_argument("--foreground", action="store_true")

    logs_p = sub.add_parser("logs", help="查看后端日志")
    logs_p.add_argument("name")
    logs_p.add_argument("-n", "--lines", type=int, default=100, help="显示行数")
    logs_p.add_argument("-f", "--follow", action="store_true", help="跟随输出")

    info_p = sub.add_parser("info", help="查看后端详情")
    info_p.add_argument("name")

    sub.add_parser("monitor", help="实时监控面板")

    install_b_p = sub.add_parser("install-backend", help="安装后端（下载程序 + 安装依赖）")
    install_b_p.add_argument("name")
    sub.add_parser("uninstall-backend", help="卸载后端（停止并删除程序与依赖）").add_argument("name")

    sub.add_parser("update", help="从 Git 拉取项目更新")

    webui_p = sub.add_parser("webui", help="启动 Web 管理界面")
    webui_p.add_argument("--host", default=None)
    webui_p.add_argument("--port", type=int, default=None)
    webui_p.add_argument("--no-browser", action="store_true")
    sub.add_parser("webui-stop", help="停止后台 WebUI")
    sub.add_parser("webui-restart", help="重启后台 WebUI（先停止再启动，重新加载后端清单）")
    webui_port_p = sub.add_parser("webui-port", help="查看/修改 WebUI 端口（修改后自动重启 WebUI）")
    webui_port_p.add_argument("value", nargs="?", help="新端口 1-65535，或 reset 重新随机生成")
    webui_host_p = sub.add_parser("webui-host", help="查看/修改 WebUI 监听地址（修改后自动重启 WebUI）")
    webui_host_p.add_argument("value", nargs="?", help="监听地址(如 0.0.0.0 / 127.0.0.1)，或 reset 恢复默认 0.0.0.0")
    webui_token_p = sub.add_parser("webui-token", help="查看/修改 WebUI 访问 token（修改后自动重启 WebUI）")
    webui_token_p.add_argument("value", nargs="?", help="新 token，或 reset 重新生成")
    sub.add_parser("uninstall", help="卸载 aibackend 命令（删除命令与 PATH 配置）")
    sub.add_parser("service-install", help="[Linux] 注册 systemd 服务：开机自启 + 自动拉起 WebUI")
    sub.add_parser("service-uninstall", help="[Linux] 停止并移除 systemd 服务")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    supervisor = Supervisor(config)

    if not args.command or args.command == "list":
        print_list(supervisor)
        return
    if args.command == "help":
        cmd_help(args, parser)
        return
    if args.command == "start":
        cmd_start(args, supervisor)
    elif args.command == "stop":
        cmd_stop(args, supervisor)
    elif args.command == "restart":
        cmd_restart(args, supervisor)
    elif args.command == "logs":
        cmd_logs(args)
    elif args.command == "info":
        cmd_info(args, supervisor)
    elif args.command == "monitor":
        cmd_monitor(args, supervisor)
    elif args.command == "install-backend":
        cmd_install_backend(args)
    elif args.command == "uninstall-backend":
        cmd_uninstall_backend(args, supervisor)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "webui":
        cmd_webui(args)
    elif args.command == "webui-stop":
        cmd_webui_stop(args)
    elif args.command == "webui-restart":
        cmd_webui_restart(args)
    elif args.command == "webui-port":
        cmd_webui_port(args)
    elif args.command == "webui-host":
        cmd_webui_host(args)
    elif args.command == "webui-token":
        cmd_webui_token(args)
    elif args.command == "uninstall":
        cmd_uninstall(args)
    elif args.command == "service-install":
        cmd_service_install(args)
    elif args.command == "service-uninstall":
        cmd_service_uninstall(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
