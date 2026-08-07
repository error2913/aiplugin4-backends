#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aibackend — aiplugin4-backends 的 pm2 风格命令行管理工具。

安装命令（把 aibackend 写入 PATH）：
  python install_cli.py

用法示例：
  aibackend list                       查看所有后端状态
  aibackend start --all                后台启动全部（默认后台守护，类似 pm2）
  aibackend start stream-output        后台启动单个
  aibackend start stream-output --foreground   前台运行（Ctrl+C 停止）
  aibackend stop --all                 停止全部
  aibackend restart stream-output      重启单个
  aibackend logs stream-output         查看日志（-n 行数，-f 跟随）
  aibackend info stream-output         查看进程详情（pid/时长/内存/拉起次数）
  aibackend monitor                    实时监控面板
  aibackend setup --all                安装全部后端依赖
  aibackend del-deps stream-output     删除单个后端依赖
  aibackend webui                      启动 Web 管理界面

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
    deps_ready,
    discover_backends,
    effective_port,
    launch_webui,
    load_config,
    package_backends,
    process_memory,
    remove_backend_deps,
    setup_backend,
)


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


def cmd_monitor(args, supervisor):
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            print("aibackend monitor（Ctrl+C 退出）\n")
            print_list(supervisor)
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def cmd_setup(args):
    targets = resolve(args)
    if not targets:
        print("请指定后端名称或使用 --all")
        return
    for b in targets:
        setup_backend(b)
    print("依赖安装完成")


def cmd_del_deps(args, supervisor):
    targets = resolve(args)
    if not targets:
        print("请指定后端名称或使用 --all")
        return
    for b in targets:
        if supervisor.is_running(b.name):
            supervisor.stop([b])
            time.sleep(0.5)
        remove_backend_deps(b)
    print("依赖已删除")


def cmd_webui(args):
    config = load_config()
    supervisor = Supervisor(config)
    launch_webui(
        discover_backends(),
        config,
        supervisor,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="aibackend",
        description="aiplugin4-backends 的 pm2 风格命令行管理工具",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="查看所有后端状态")

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

    setup_p = sub.add_parser("setup", help="安装后端依赖")
    setup_p.add_argument("names", nargs="*")
    setup_p.add_argument("--all", action="store_true")

    del_p = sub.add_parser("del-deps", help="删除后端依赖")
    del_p.add_argument("names", nargs="*")
    del_p.add_argument("--all", action="store_true")

    webui_p = sub.add_parser("webui", help="启动 Web 管理界面")
    webui_p.add_argument("--host", default="127.0.0.1")
    webui_p.add_argument("--port", type=int, default=8910)
    webui_p.add_argument("--no-browser", action="store_true")

    sub.add_parser("package", help="打包 dist/ 压缩包（zip + tar.gz）")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    supervisor = Supervisor(config)

    if not args.command or args.command == "list":
        print_list(supervisor)
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
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "del-deps":
        cmd_del_deps(args, supervisor)
    elif args.command == "webui":
        cmd_webui(args)
    elif args.command == "package":
        package_backends()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
