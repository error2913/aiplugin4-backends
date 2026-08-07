#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 aibackend 命令安装到 PATH（Windows / Linux / macOS）。

用法：
  python install_cli.py

安装后重新打开终端，即可在任意目录使用 aibackend（示例：aibackend list）。
"""

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN_DIR = Path.home() / ".aibackend" / "bin"


def main():
    target = ROOT / "aibackend.py"
    if not target.exists():
        print(f"找不到 {target}，请从仓库根目录运行安装脚本")
        sys.exit(1)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        install_windows(target)
    else:
        install_unix(target)
    print(f"安装完成：{BIN_DIR}")
    print("请重新打开终端后使用 aibackend 命令（示例：aibackend list）")


def install_windows(target):
    cmd_file = BIN_DIR / "aibackend.cmd"
    cmd_file.write_text(
        f'@echo off\r\n"{sys.executable}" "{target}" %*\r\n',
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
    if bin_dir not in items:
        items.append(bin_dir)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(items))
    winreg.CloseKey(key)
    try:
        import ctypes

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
    with open(rc_file, "a", encoding="utf-8") as f:
        f.write(f'\nexport PATH="{BIN_DIR}:$PATH"\n')
    print(f"已将 aibackend 加入 {rc_file}，请重新打开终端")


if __name__ == "__main__":
    main()
