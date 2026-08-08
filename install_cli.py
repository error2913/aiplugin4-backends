#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 aibackend 命令安装到 PATH（Windows / Linux / macOS）。

用法：
  python install_cli.py

安装后重新打开终端，即可在任意目录使用 aibackend（示例：aibackend list）。
launcher.py 启动时会自动调用本模块的 install()（幂等：不重复追加 PATH 配置，
Linux 每次启动重新写入 shim 保持刷新）。
"""

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN_DIR = Path.home() / ".aibackend" / "bin"


def install() -> str:
    """安装/刷新 aibackend 命令（幂等），返回命令所在目录"""
    target = ROOT / "aibackend.py"
    if not target.exists():
        raise FileNotFoundError(f"找不到 {target}，请从仓库根目录运行")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        install_windows(target)
    else:
        install_unix(target)
    return str(BIN_DIR)


def main():
    try:
        bin_dir = install()
    except FileNotFoundError as e:
        print(e)
        sys.exit(1)
    print(f"安装完成：{bin_dir}")
    print("请重新打开终端后使用 aibackend 命令（示例：aibackend list）")


def install_windows(target):
    cmd_file = BIN_DIR / "aibackend.cmd"
    cmd_file.write_text(
        # 利用 cmd 怪癖：goto 到不存在的标签会让批处理立即结束，
        # 剩余命令在"命令行上下文"执行，Ctrl+C 时不再弹"终止批处理操作吗(Y/N)?"
        '@echo off\r\n'
        '@goto #_undefined_# 2>NUL || @title %COMSPEC% & '
        f'"{sys.executable}" "{target}" %*\r\n',
        encoding="utf-8",
    )
    add_to_user_path_windows(str(BIN_DIR))


def add_to_user_path_windows(bin_dir):
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    )
    try:
        current, _ = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        current = ""
    items = [p for p in current.split(";") if p]
    changed = False
    if bin_dir not in items:
        items.append(bin_dir)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(items))
        changed = True
    winreg.CloseKey(key)
    if changed:
        try:
            import ctypes

            # 广播环境变更，让新打开的终端立即拿到新 PATH
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x1A, 0, "Environment", 0, 1000, None
            )
        except Exception:  # noqa: BLE001
            pass


def install_unix(target):
    script = BIN_DIR / "aibackend"
    script.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{target}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if str(BIN_DIR) in os.environ.get("PATH", "").split(os.pathsep):
        return
    rc_file = None
    for name in (".bashrc", ".zshrc", ".profile"):
        cand = Path.home() / name
        if cand.exists():
            rc_file = cand
            break
    if rc_file is None:
        rc_file = Path.home() / ".profile"
    try:
        text = rc_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if str(BIN_DIR) in text:
        return  # 已配置过，避免重复追加
    with open(rc_file, "a", encoding="utf-8") as f:
        f.write(f'\nexport PATH="{BIN_DIR}:$PATH"\n')
    print(f"已将 aibackend 加入 {rc_file}，请重新打开终端")


if __name__ == "__main__":
    main()
