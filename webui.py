#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aiplugin4 后端管理 Web 界面（纯 Python 标准库，无第三方依赖）。

由 launcher.py 直接启动：
  python launcher.py

（也可用 python launcher.py webui [--host 0.0.0.0] [--port <端口>] [--no-browser] 调整参数）

默认监听 0.0.0.0（全部网卡），访问 token 首次运行自动生成（launcher webui-token 可改），
端口首次运行随机生成五位数（launcher webui-port 可改），
API 请求需带 Authorization: Bearer <token> 或 X-Token: <token>。
"""

import hashlib
import hmac
import html
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from launcher import (
    _can_open_browser,
    _expand_config_value,
    _no_window_kwargs,
    DEFAULT_LOG_DIR,
    ROOT_DIR,
    Supervisor,
    backend_custom_config,
    backend_config,
    build_plugin_backend_config,
    build_plugin_mcp_config,
    deps_ready,
    discover_backends,
    effective_webui_port,
    effective_webui_host,
    effective_webui_token,
    load_registry,
    load_runtime,
    normalize_webui,
    process_memory,
    remove_backend_dir,
    reset_webui_token,
    save_backend_config,
    save_runtime,
    save_webui_token,
    setup_backend,
    update_check,
    update_backend,
    update_project,
)

# 反代 WebUI 的会话 cookie：只在 /ui 前缀下生效，避免把 launcher 自身的 API 鉴权降级成 cookie。
# 值由 WebUI token 派生，token 一改旧 cookie 立即失效（无需服务端存会话）。
UI_COOKIE_NAME = "aib_ui"
UI_COOKIE_PATH = "/ui"
UI_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
UI_PREFIX = "/ui/"


def _ui_cookie_value(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"aiplugin4-backends-ui-v1", hashlib.sha256).hexdigest()


def _ui_cookie_header(token: str, secure: bool = False) -> str:
    parts = [
        f"{UI_COOKIE_NAME}={_ui_cookie_value(token)}",
        f"Path={UI_COOKIE_PATH}",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={UI_COOKIE_MAX_AGE}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _rewrite_set_cookie(value: str, name: str) -> str:
    """把后端通过反代下发的 cookie 限制到 /ui/<name>/，并去掉 Domain。"""
    parts = [p.strip() for p in value.split(";") if p.strip()]
    out = []
    has_path = False
    for part in parts:
        low = part.lower()
        if low.startswith("domain="):
            continue
        if low.startswith("path="):
            out.append(f"Path=/ui/{name}/")
            has_path = True
            continue
        out.append(part)
    if not has_path:
        out.append(f"Path=/ui/{name}/")
    return "; ".join(out)


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/icon-256.png" type="image/png">
    <title>aiplugin4 后端管理</title>
<style>
  :root {
    --bg: #eef1f6; --panel: #ffffff; --panel-2: #f4f6fa;
    --border: #dde3ec; --text: #1d2635; --muted: #64748b;
    --green: #16a34a; --green-bg: rgba(22,163,74,.1);
    --red: #dc2626; --red-bg: rgba(220,38,38,.1);
    --blue: #2563eb; --blue-bg: rgba(37,99,235,.1);
    --amber: #b45309; --amber-bg: rgba(180,83,9,.1);
    --shadow: rgba(15,23,42,.08);
  }
  [data-theme="dark"] {
    --bg: #0e1116; --panel: #161b24; --panel-2: #1b2230;
    --border: #262f3d; --text: #e6ebf2; --muted: #8b96a8;
    --green: #34d399; --green-bg: rgba(52,211,153,.12);
    --red: #f87171; --red-bg: rgba(248,113,113,.12);
    --blue: #60a5fa; --blue-bg: rgba(96,165,250,.14);
    --amber: #fbbf24; --amber-bg: rgba(251,191,36,.1);
    --shadow: rgba(0,0,0,.45);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0e1116; --panel: #161b24; --panel-2: #1b2230;
      --border: #262f3d; --text: #e6ebf2; --muted: #8b96a8;
      --green: #34d399; --green-bg: rgba(52,211,153,.12);
      --red: #f87171; --red-bg: rgba(248,113,113,.12);
      --blue: #60a5fa; --blue-bg: rgba(96,165,250,.14);
      --amber: #fbbf24; --amber-bg: rgba(251,191,36,.1);
      --shadow: rgba(0,0,0,.45);
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    background: radial-gradient(1100px 520px at 18% -12%, color-mix(in srgb, var(--blue) 14%, transparent), transparent 60%), var(--bg);
    color: var(--text); margin: 0; min-height: 100vh; padding: 32px 28px 60px;
    transition: background .25s ease, color .25s ease;
  }
  .wrap { max-width: 1200px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
  .logo {
    width: 44px; height: 44px; border-radius: 12px; flex: none;
    object-fit: cover; box-shadow: 0 8px 24px color-mix(in srgb, var(--blue) 35%, transparent);
  }
  h1 { font-size: 22px; margin: 0; font-weight: 700; letter-spacing: .3px; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 3px; }
  .header-right { margin-left: auto; display: flex; gap: 10px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .stat {
    background: linear-gradient(180deg, color-mix(in srgb, var(--text) 4%, transparent), transparent);
    border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; text-align: center;
  }
  .stat b { font-size: 26px; display: block; line-height: 1.1; }
  .stat span { color: var(--muted); font-size: 12px; }
  .stat.green b { color: var(--green); }
  .stat.blue b { color: var(--blue); }
  .bar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; justify-content: space-between; align-items: center; }
  .bar-left, .bar-right { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  button {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 9px; padding: 8px 15px; cursor: pointer; font-size: 13px;
    transition: transform .12s ease, background .15s ease, border-color .15s ease;
  }
  button:hover { border-color: color-mix(in srgb, var(--muted) 50%, transparent); }
  button:active { transform: scale(.97); }
  button.primary { background: var(--blue-bg); border-color: color-mix(in srgb, var(--blue) 45%, transparent); color: var(--blue); }
  button.danger { background: var(--red-bg); border-color: color-mix(in srgb, var(--red) 40%, transparent); color: var(--red); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 14px; }
  .empty { grid-column: 1 / -1; border: 1px dashed var(--border); border-radius: 14px; padding: 48px 20px; color: var(--muted); font-size: 13px; text-align: center; background: color-mix(in srgb, var(--panel) 55%, transparent); }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 16px;
    display: flex; flex-direction: column; align-items: center; text-align: center; gap: 12px; box-shadow: 0 1px 3px var(--shadow);
    transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease;
  }
  .card:hover { border-color: color-mix(in srgb, var(--muted) 55%, transparent); }
  .card.running { border-color: color-mix(in srgb, var(--green) 40%, transparent); }
  .row1 { display: flex; align-items: center; justify-content: center; gap: 10px; }
  .name { font-family: Consolas, "Courier New", monospace; font-size: 15px; font-weight: 600; }
  .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; font-weight: 600; letter-spacing: .4px; }
  .badge.py { background: var(--blue-bg); color: var(--blue); }
  .badge.node { background: var(--green-bg); color: var(--green); }
  .status { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .dot.on { background: var(--green); animation: pulse 1.8s infinite; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--green) 45%, transparent); } 70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
  .status.on { color: var(--green); }
  .status.off { color: var(--muted); }
  .desc { color: var(--muted); font-size: 12.5px; line-height: 1.5; min-height: 36px; }
  .meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .meta { justify-content: center; }
  .portbox { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
  .port {
    width: 76px; background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 4px 8px; font-family: Consolas, monospace; font-size: 12.5px;
  }
  .port:focus { outline: none; border-color: var(--blue); }
  button.mini { padding: 4px 8px; font-size: 12px; }
  .chip { font-family: Consolas, monospace; font-size: 12px; color: var(--amber); background: var(--amber-bg); border: 1px solid color-mix(in srgb, var(--amber) 30%, transparent); padding: 3px 9px; border-radius: 8px; }
  .chip.idle { color: var(--muted); background: color-mix(in srgb, var(--muted) 10%, transparent); border-color: color-mix(in srgb, var(--muted) 22%, transparent); }
  .chip.token { color: var(--green); background: color-mix(in srgb, var(--green) 10%, transparent); border-color: color-mix(in srgb, var(--green) 30%, transparent); }
  .cfg-row { display: flex; align-items: center; gap: 8px; }
  .cfg-row .port { flex: 1; width: auto; }
  .cfg-label { font-size: 12.5px; color: var(--muted); display: grid; gap: 6px; }
  .cfg-switch { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
  .cfg-switch input { appearance: none; -webkit-appearance: none; width: 38px; height: 20px; border-radius: 999px; background: var(--border); position: relative; transition: background .15s; cursor: pointer; margin: 0; flex: none; }
  .cfg-switch input::after { content: ''; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left .15s; }
  .cfg-switch input:checked { background: var(--blue); }
  .cfg-switch input:checked::after { left: 20px; }
  .cfg-switch-txt { font-size: 12.5px; color: var(--text); }
  select.port { padding-right: 6px; }
  .ops { display: flex; gap: 8px; flex-wrap: wrap; }
  .ops { justify-content: center; }
  .ops button { padding: 6px 12px; font-size: 12.5px; }
  button.loading { opacity: .8; cursor: progress; }
  .spin { display: inline-block; width: 11px; height: 11px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; margin-right: 6px; vertical-align: -1px; animation: rot .8s linear infinite; }
  @keyframes rot { to { transform: rotate(360deg); } }
  #updateBtn { position: relative; }
  .red-dot { position: absolute; top: 3px; right: 3px; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; box-shadow: 0 0 6px rgba(239,68,68,.8); }
  .modal {
    position: fixed; inset: 0; background: rgba(5,8,12,.55); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; z-index: 50; padding: 24px;
  }
  .modal.open { display: flex; }
  .dialog {
    width: min(860px, 100%); max-height: 82vh; background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 60px var(--shadow);
  }
  .dialog-head, .dialog-foot { flex: 0 0 auto; }
  .dialog-head { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .dialog-head b { font-size: 14px; }
  .dialog-head .spacer { flex: 1; }
  /* 弹窗内容区：必须显式允许收缩 + 滚动，否则配置项一多就会被裁掉且滚不动 */
  .dialog-body {
    flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
    padding: 18px 22px; display: grid; gap: 16px;
  }
  .dialog pre {
    margin: 0; padding: 16px 18px; overflow: auto; flex: 1 1 auto; min-height: 0; font-size: 12.5px; line-height: 1.55;
    font-family: Consolas, "Courier New", monospace; color: var(--muted); white-space: pre-wrap; word-break: break-all;
  }
  .close { background: transparent; border: none; font-size: 18px; color: var(--muted); cursor: pointer; padding: 2px 8px; }
  #toast {
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 99; max-width: 70vw;
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 16px; font-size: 13px; display: none; box-shadow: 0 12px 32px var(--shadow);
  }
  footer { margin-top: 26px; color: var(--muted); font-size: 12px; text-align: center; opacity: .7; }
  @media (max-width: 480px) {
    .modal { padding: 12px; }
    .dialog { max-height: calc(100vh - 24px); }
  }
  @media (max-height: 560px) {
    .modal { padding: 8px; }
    .dialog { max-height: calc(100vh - 16px); }
  }
</style>
</head>
<body>
<div id="loginScreen" style="position:fixed; inset:0; background:var(--bg); display:flex; align-items:center; justify-content:center; z-index:100;">
  <div style="width:min(360px,92vw); background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:28px; display:grid; gap:14px; text-align:center; box-shadow:0 12px 32px var(--shadow);">
    <div style="font-size:20px; font-weight:700;">aiplugin4 后端管理</div>
    <div style="color:var(--muted); font-size:13px;">请输入 WebUI token（登录后记住一年）</div>
    <input id="loginToken" type="password" spellcheck="false" autocomplete="off" placeholder="访问 token"
           style="width:100%; padding:10px 12px; border:1px solid var(--border); border-radius:9px; background:var(--panel-2); color:var(--text); font-size:14px; outline:none;"
           onkeydown="if(event.key==='Enter')login()">
    <button class="primary" onclick="login()" style="padding:10px 0; font-size:14px;">登录</button>
    <div id="loginErr" style="color:var(--red); font-size:12px; min-height:16px;"></div>
  </div>
</div>
<div class="wrap">
  <header>
    <img class="logo" src="/icon-256.png" alt="aiplugin4">
    <div>
      <h1>aiplugin4 后端管理</h1>
      <div class="sub">launcher WebUI</div>
    </div>
    <div class="header-right">
      <button onclick="openToken()">🔑 Token</button>
      <button id="updateBtn" onclick="updateNow()">⬆ 更新<span id="updateDot" class="red-dot" style="display:none"></span></button>
      <button onclick="restartWebUI()">🔄 重启 WebUI</button>
      <button id="hideDepsBtn" onclick="toggleHideNoDeps()">🙈 隐藏未装后端</button>
      <button onclick="refresh()">⟳ 刷新</button>
    </div>
  </header>
  <div class="stats">
    <div class="stat"><b id="stTotal">0</b><span>后端总数</span></div>
    <div class="stat green"><b id="stRun">0</b><span>运行中</span></div>
  </div>
  <div class="bar">
    <div class="bar-left">
      <button class="primary" onclick="allAct('start')">▶ 启动全部</button>
      <button class="danger" onclick="allAct('stop')">■ 停止全部</button>
      <button onclick="allAct('restart')">🔄 重启全部</button>
      <button onclick="copyPluginConfig('backend')" title="复制插件「后端配置」JSON，粘贴到 海豹 → 插件设置 → aiplugin4 → 后端 → 后端配置">📋 复制后端配置</button>
      <button onclick="copyPluginConfig('mcp')" title="复制插件「MCP服务器」JSON，粘贴到 海豹 → 插件设置 → aiplugin4 → MCP → MCP服务器（粘贴会整体替换）">📋 复制 MCP 配置</button>
      <button onclick="openPluginConfig('backend')" title="预览并下载生成的配置">👁 预览</button>
    </div>
    <div class="bar-right" id="installAllArea"></div>
  </div>
  <div class="grid" id="grid"></div>
</div>
<div class="modal" id="modal">
  <div class="dialog">
    <div class="dialog-head">
      <b id="logTitle">日志</b>
      <span class="spacer"></span>
      <button onclick="loadLog()">刷新日志</button>
      <button class="close" onclick="closeLog()">✕</button>
    </div>
    <pre id="logBody"></pre>
  </div>
</div>
<div class="modal" id="alertModal">
  <div class="dialog" style="width:min(420px,92vw)">
    <div class="dialog-head">
      <b>提示</b>
      <span class="spacer"></span>
      <button class="close" onclick="closeAlert()">✕</button>
    </div>
    <div id="alertBody" style="padding:20px 22px; font-size:14px; line-height:1.8;"></div>
    <div style="padding:12px 18px; text-align:right; border-top:1px solid var(--border);">
      <button class="primary" onclick="closeAlert()">确定</button>
    </div>
  </div>
</div>
<div class="modal" id="configModal">
  <div class="dialog" style="width:min(430px,92vw)">
    <div class="dialog-head">
      <b id="configTitle">配置</b>
      <span class="spacer"></span>
      <button class="close" onclick="closeConfig()">✕</button>
    </div>
    <div class="dialog-body">
      <label class="cfg-label">端口
        <input id="cfgPort" class="port" type="number" min="1" max="65535">
      </label>
      <label class="cfg-label">Token（留空 = 不鉴权）
        <div class="cfg-row">
          <input id="cfgToken" class="port" type="text" spellcheck="false" placeholder="留空表示无需 token">
          <button class="mini" onclick="randomToken()" title="一键随机生成 token">🎲 随机</button>
        </div>
      </label>
      <label class="cfg-label">监听 IP
        <input id="cfgHost" class="port" type="text" spellcheck="false" placeholder="0.0.0.0">
      </label>
      <div id="cfgCustom" style="display:grid; gap:16px;"></div>
    </div>
    <div class="dialog-foot" style="padding:12px 18px; text-align:right; border-top:1px solid var(--border); display:flex; gap:8px; justify-content:flex-end;">
      <button onclick="closeConfig()">取消</button>
      <button class="primary" onclick="saveConfig()">保存</button>
    </div>
  </div>
</div>
<div class="modal" id="tokenModal">
  <div class="dialog" style="width:min(460px,92vw)">
    <div class="dialog-head">
      <b>WebUI Token</b>
      <span class="spacer"></span>
      <button class="close" onclick="closeToken()">✕</button>
    </div>
    <div class="dialog-body">
      <label class="cfg-label">服务器当前 Token（已生效）
        <div class="cfg-row">
          <input id="tokServer" class="port" type="text" readonly spellcheck="false" placeholder="加载中...">
        </div>
      </label>
      <label class="cfg-label">新 Token（直接输入，保存后立即生效）
        <div class="cfg-row">
          <input id="tokNew" class="port" type="text" spellcheck="false" placeholder="输入新 token">
          <button class="mini" onclick="genToken()" title="随机生成新 token">🎲 随机</button>
        </div>
      </label>
    </div>
    <div class="dialog-foot" style="padding:12px 18px; text-align:right; border-top:1px solid var(--border); display:flex; gap:8px; justify-content:flex-end;">
      <button onclick="closeToken()">取消</button>
      <button class="primary" onclick="saveServerToken()">保存并生效</button>
    </div>
  </div>
</div>
<div class="modal" id="pluginConfigModal">
  <div class="dialog" style="width:min(860px,94vw)">
    <div class="dialog-head">
      <b>插件配置</b>
      <span class="spacer"></span>
      <button id="pcTabBackend" onclick="switchPluginConfig('backend')">后端配置</button>
      <button id="pcTabMcp" onclick="switchPluginConfig('mcp')">MCP 配置</button>
      <button class="close" onclick="closePluginConfig()">✕</button>
    </div>
    <div class="dialog-body">
      <label class="cfg-label">插件访问主机（插件与后端同机时填 127.0.0.1）
        <div class="cfg-row">
          <input id="pcHost" class="port" type="text" spellcheck="false" style="width:200px;" placeholder="127.0.0.1" onchange="reloadPluginConfig()">
          <button class="mini" onclick="reloadPluginConfig()">重新生成</button>
        </div>
      </label>
      <label class="cfg-label">图床对外地址（IP 或域名；QQ 协议端与模型都要能访问，留空自动探测本机 IP）
        <div class="cfg-row">
          <input id="pcImageHost" class="port" type="text" spellcheck="false" style="width:200px;" placeholder="如 192.168.1.10 或 img.example.com" onchange="reloadPluginConfig()">
        </div>
      </label>
      <div id="pcInfo" style="font-size:12px; color:var(--muted); white-space:pre-wrap; line-height:1.7; margin-top:10px;"></div>
      <div id="pcWarn" style="display:none; font-size:12px; color:#e6b450; white-space:pre-wrap; line-height:1.7; margin-top:6px;"></div>
      <pre id="pcPreview" style="white-space:pre-wrap; word-break:break-all; background:rgba(0,0,0,.22); border:1px solid var(--border); border-radius:8px; padding:12px; margin-top:10px; max-height:46vh; overflow:auto; font-size:12px; line-height:1.55;"></pre>
    </div>
    <div class="dialog-foot" style="padding:12px 18px; text-align:right; border-top:1px solid var(--border); display:flex; gap:8px; justify-content:flex-end;">
      <button onclick="downloadPluginConfig()">下载 .json</button>
      <button onclick="closePluginConfig()">关闭</button>
      <button class="primary" onclick="copyPluginConfigText()">复制</button>
    </div>
  </div>
</div>
<div id="toast"></div>
<footer style="display:flex; align-items:center; justify-content:space-between;">
  <span>aiplugin4-backends · launcher.py webui</span>
  <span><a href="/safe" target="_blank" style="margin-left:12px;color:var(--muted);font-size:12px;text-decoration:none;">安全模式</a><button id="themeBtn" onclick="cycleTheme()" style="margin-left:10px;">主题</button></span>
</footer>
<script>
let current = null;
let currentType = 'backend';
let cfgName = null;
let backendsByName = {};  // /api/backends 的快照，供 WebUI 按钮取端口/路径
const installing = new Set();
const deleting = new Set();
const updatingBackends = new Set();
let allInstalling = false;
let allTargets = [];
let allDone = 0;
const TOKEN_TTL_MS = 365 * 24 * 60 * 60 * 1000;  // 登录后记住一年
let loggedIn = false;
let updating = false;
let hideNoDeps = localStorage.getItem('hide_no_deps') === '1';
let pluginCfgKind = 'backend';
let pluginCfgData = null;
function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtUptime(s){
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  if (d > 0) return d + '天' + h + '小时';
  if (h > 0) return h + '小时' + m + '分';
  if (m > 0) return m + '分' + sec + '秒';
  return sec + '秒';
}
function toast(msg, ms){ const t=document.getElementById('toast'); t.textContent=msg; t.style.display='block'; clearTimeout(t._h); t._h=setTimeout(()=>t.style.display='none', ms || 3000); }
function legacyCopyText(text){
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}
function copyText(text){
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(() => true).catch(() => legacyCopyText(text));
  }
  return Promise.resolve(legacyCopyText(text));
}
async function fetchPluginConfig(kind, host){
  const input = document.getElementById('pcHost');
  const picked = (host || (input && input.value) || localStorage.getItem('plugin_export_host') || '127.0.0.1').trim() || '127.0.0.1';
  let path = '/api/' + (kind === 'backend' ? 'plugin-config' : 'mcp-config') + '?host=' + encodeURIComponent(picked);
  if (kind === 'backend') {
    const imgInput = document.getElementById('pcImageHost');
    const imgHost = ((imgInput && imgInput.value) || localStorage.getItem('plugin_export_image_host') || '').trim();
    if (imgHost) path += '&image_host=' + encodeURIComponent(imgHost);
  }
  return await api(path);
}
async function copyPluginConfig(kind){
  try {
    const data = await fetchPluginConfig(kind);
    if (!data.included.length) {
      toast(kind === 'backend'
        ? '没有已安装的后端项目可复制（安装后重试；MCP 项目请用「复制 MCP 配置」）'
        : '没有已安装的 MCP 项目可复制（先安装 mcp-files-exec / md-html-render / mcp-browser / image-host）', 6000);
      return;
    }
    const ok = await copyText(data.text);
    if (!ok) {
      openPluginConfig(kind);
      toast('复制失败，已打开预览，请手动全选复制', 5000);
      return;
    }
    const n = data.included.length;
    const warnSuffix = (data.warnings && data.warnings.length) ? '；警告：' + data.warnings.join('；') : '';
    toast((kind === 'backend'
      ? `已复制 ${n} 项后端配置（含 Token）→ 海豹 → 插件设置 → aiplugin4 → 后端 → 后端配置`
      : `已复制 ${n} 项 MCP 配置 → 海豹 → 插件设置 → aiplugin4 → MCP → MCP服务器（需先开「是否启用MCP」；粘贴会整体替换）`)
      + warnSuffix, warnSuffix ? 9000 : 7000);
  } catch (e) { /* api() 已提示错误 */ }
}
function openPluginConfig(kind){
  pluginCfgKind = kind || 'backend';
  document.getElementById('pcHost').value = localStorage.getItem('plugin_export_host') || '127.0.0.1';
  document.getElementById('pcImageHost').value = localStorage.getItem('plugin_export_image_host') || '';
  document.getElementById('pcPreview').textContent = '';
  document.getElementById('pcInfo').textContent = '';
  document.getElementById('pcWarn').textContent = '';
  document.getElementById('pluginConfigModal').classList.add('open');
  switchPluginConfig(pluginCfgKind);
}
function closePluginConfig(){ document.getElementById('pluginConfigModal').classList.remove('open'); }
async function switchPluginConfig(kind){
  pluginCfgKind = kind === 'mcp' ? 'mcp' : 'backend';
  const b = document.getElementById('pcTabBackend');
  const m = document.getElementById('pcTabMcp');
  if (b) b.className = pluginCfgKind === 'backend' ? 'primary' : '';
  if (m) m.className = pluginCfgKind === 'mcp' ? 'primary' : '';
  await reloadPluginConfig();
}
async function reloadPluginConfig(){
  const host = (document.getElementById('pcHost').value || '127.0.0.1').trim() || '127.0.0.1';
  localStorage.setItem('plugin_export_host', host);
  const imgInput = document.getElementById('pcImageHost');
  const imgHost = (imgInput && imgInput.value || '').trim();
  localStorage.setItem('plugin_export_image_host', imgHost);
  try {
    pluginCfgData = await fetchPluginConfig(pluginCfgKind, host);
  } catch (e) {
    pluginCfgData = null;
    document.getElementById('pcPreview').textContent = '生成失败：' + (e && e.message ? e.message : e);
    return;
  }
  const d = pluginCfgData;
  // 留空时后端会自动探测本机 IP：把实际使用的对外地址回填到输入框，避免"看到的和复制的不一致"
  if (pluginCfgKind === 'backend' && d.image_host_public && imgInput && !imgInput.value) {
    imgInput.value = d.image_host_public;
  }
  document.getElementById('pcPreview').textContent = JSON.stringify(d.config, null, 2);
  const lines = [];
  if (d.included.length) {
    lines.push('纳入：' + d.included.map(x => x.name + (x.token ? '（含 token）' : '')).join('、'));
  } else {
    lines.push('没有已安装的项目可导出');
  }
  if (pluginCfgKind === 'backend') {
    lines.push('图床对外地址：' + (d.image_host_public || '未配置（发图/模型取图会降级）'));
  }
  if (d.excluded.length) {
    lines.push('未纳入：' + d.excluded.map(x => x.name + '（' + x.reason + '）').join('、'));
  }
  if (pluginCfgKind === 'backend') {
    lines.push('提示：MCP 类项目用「复制 MCP 配置」；论坛配置不属于后端项目。');
  } else {
    lines.push('注意：粘贴会整体替换「MCP服务器」，手写的其它服务器请先合并。');
  }
  document.getElementById('pcInfo').textContent = lines.join(String.fromCharCode(10));
  const warn = document.getElementById('pcWarn');
  if (d.warnings && d.warnings.length) {
    warn.textContent = d.warnings.join(String.fromCharCode(10));
    warn.style.display = 'block';
  } else {
    warn.textContent = '';
    warn.style.display = 'none';
  }
}
async function copyPluginConfigText(){
  if (!pluginCfgData) return;
  const ok = await copyText(pluginCfgData.text);
  if (ok) toast('已复制，去海豹对应配置项粘贴', 3000);
  else toast('复制失败，请手动全选复制', 5000);
}
function downloadPluginConfig(){
  if (!pluginCfgData) return;
  const blob = new Blob([pluginCfgData.text], {type: 'application/json'});
  const a = document.createElement('a');
  const url = URL.createObjectURL(blob);
  a.href = url;
  a.download = (pluginCfgKind === 'backend' ? 'aiplugin4-backend-config' : 'aiplugin4-mcp-config') + '.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function getStoredToken(){
  const t = localStorage.getItem('webui_token');
  const exp = Number(localStorage.getItem('webui_token_exp') || 0);
  if (t && exp && Date.now() > exp) {
    localStorage.removeItem('webui_token');
    localStorage.removeItem('webui_token_exp');
    return '';
  }
  return t || '';
}
function setStoredToken(v){
  if (v) {
    localStorage.setItem('webui_token', v);
    localStorage.setItem('webui_token_exp', String(Date.now() + TOKEN_TTL_MS));
  } else {
    localStorage.removeItem('webui_token');
    localStorage.removeItem('webui_token_exp');
  }
}
async function api(path, method, body){
  const headers = {};
  const t = getStoredToken();
  if (t) headers['X-Token'] = t;
  try {
    const r = await fetch(path, {method: method||'GET', headers: headers, body: body || undefined});
    if (r.status === 401) {
      loggedIn = false;
      showLogin();
      throw new Error('需要 WebUI token');
    }
    const j = await r.json();
    if (!j.ok) throw new Error(j.message || ('HTTP ' + r.status));
    return j;
  } catch(e){
    if (!(e && e.message === '需要 WebUI token') && !document.getElementById('tokenModal').classList.contains('open')) toast('请求失败: ' + e.message);
    throw e;
  }
}
function showLogin(){ document.getElementById('loginScreen').style.display = 'flex'; }
function hideLogin(){ document.getElementById('loginScreen').style.display = 'none'; }
async function login(){
  const v = document.getElementById('loginToken').value.trim();
  const err = document.getElementById('loginErr');
  if (!v) { err.textContent = '请输入 token'; return; }
  setStoredToken(v);
  try {
    await refresh();
    loggedIn = true;
    hideLogin();
    err.textContent = '';
  } catch(e) {
    setStoredToken('');
    err.textContent = 'token 错误，请重试';
  }
}
function openToken(){
  document.getElementById('tokenModal').classList.add('open');
  document.getElementById('tokNew').value = '';
  api('/api/webui-token').then(j => {
    document.getElementById('tokServer').value = j.token || '';
  }).catch(() => {
    document.getElementById('tokServer').value = '(无法读取，请重试)';
  });
}
function closeToken(){ document.getElementById('tokenModal').classList.remove('open'); }
function genToken(){
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  document.getElementById('tokNew').value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}
async function saveServerToken(){
  const v = document.getElementById('tokNew').value.trim();
  if (!v) { toast('请输入新 token，或点击 🎲 随机生成'); return; }
  const j = await api('/api/webui-token', 'POST', JSON.stringify({ token: v }));
  setStoredToken(j.token);
  closeToken();
  toast('WebUI token 已更新并立即生效');
  refresh();
}
async function doRestartWebUI(){
  try { await api('/api/webui-restart', 'POST'); } catch(e){}
  toast('正在重启 WebUI，稍后自动刷新...');
  setTimeout(() => location.reload(), 2500);
}
async function restartWebUI(){
  if (!confirm('确定重启 WebUI 吗？会短暂断开连接，约 2 秒后自动刷新。')) return;
  await doRestartWebUI();
}
function applyTheme(){
  const t = localStorage.getItem('theme') || 'auto';
  document.documentElement.dataset.theme = t === 'auto' ? '' : t;
  document.getElementById('themeBtn').textContent = t === 'auto' ? '🌓 主题：跟随系统' : (t === 'light' ? '☀️ 主题：浅色' : '🌙 主题：深色');
}
function cycleTheme(){
  const cur = localStorage.getItem('theme') || 'auto';
  const next = cur === 'auto' ? 'light' : cur === 'light' ? 'dark' : 'auto';
  localStorage.setItem('theme', next);
  applyTheme();
}
async function refresh(){
  const j = await api('/api/backends');
  backendsByName = {};
  for (const item of (j.backends || [])) backendsByName[item.name] = item;
  const updMap = (j.updates && j.updates.backends) || {};
  const updSet = new Set();
  for (const k in updMap) if (updMap[k].available) updSet.add(k);
  const dot = document.getElementById('updateDot');
  if (dot) dot.style.display = (j.updates && j.updates.repo_update) ? 'inline-block' : 'none';
  let list = (j.backends || []).slice();
  if (hideNoDeps) list = list.filter(b => b.installed);
  list.sort((a, b) => (a.installed ? 0 : 1) - (b.installed ? 0 : 1));  // 已安装的项目排前面
  document.getElementById('stTotal').textContent = list.length;
  document.getElementById('stRun').textContent = list.filter(b => b.running).length;
  document.getElementById('grid').innerHTML = list.length ? list.map(b => `
    <div class="card ${b.running ? 'running' : ''}">
      <div class="row1">
        <span class="name">${esc(b.name)}</span>
        <span class="badge ${b.type === 'python' ? 'py' : 'node'}">${esc(b.type).toUpperCase()}</span>
        <span class="status ${b.running ? 'on' : 'off'}"><span class="dot ${b.running ? 'on' : ''}"></span>${!b.installed ? '未安装' : b.running ? '运行中' : '已停止'}</span>
      </div>
      <div class="desc">${esc(b.description)}</div>
      <div class="meta">
        ${b.version ? `<span class="chip idle" title="版本号">v${esc(b.version)}</span>` : ''}
        ${b.token ? `<span class="chip token" title="访问 token">🔑 ${esc(b.token)}</span>` : ''}
        ${b.running && b.pid ? `<span class="chip idle">pid ${b.pid}</span>` : ''}
      </div>
      <div class="meta">
        ${b.installed ? `<span class="chip idle">⏱ ${b.running ? fmtUptime(b.uptime_secs) : '未运行'}</span>` : ''}
        ${b.installed ? `<span class="chip idle">🔄 自动拉起 ${b.restarts} 次</span>` : ''}
        ${b.installed && b.running && b.mem_mb != null ? `<span class="chip idle">💾 ${b.mem_mb}MB / ${b.mem_pct}%</span>` : ''}
      </div>
      <div class="ops">
        ${!b.installed
          ? installing.has(b.name)
            ? `<button class="primary loading" disabled><span class="spin"></span>安装中</button><button onclick="showInstallLog('${b.name}')">日志</button>`
            : `<button class="primary" onclick="installNow('${b.name}')">安装</button>`
          : b.running
            ? `<button class="danger" onclick="run('${b.name}','stop')">停止</button><button onclick="restartBackend('${b.name}')">重启</button>`
            : `<button class="primary" onclick="run('${b.name}','start')">启动</button>`}
        ${b.installed && b.webui && b.webui.path
          ? b.running
            ? `<button class="primary" onclick="openWebui('${b.name}')" title="在新标签打开 ${esc(b.webui.title || 'WebUI')}">🌐 WebUI</button>`
            : `<button disabled title="后端未运行，启动后可打开 WebUI">🌐 WebUI</button>`
          : ''}
        ${b.installed ? `<button onclick="openConfig('${b.name}')">配置</button>` : ''}
        ${b.installed && updSet.has(b.name)
          ? updatingBackends.has(b.name)
            ? `<button class="primary loading" disabled><span class="spin"></span>更新中</button>`
            : `<button class="primary" onclick="updateBackend('${b.name}')">⬆ 更新</button>`
          : ''}
        ${b.installed ? `<button onclick="showLog('${b.name}')">日志</button>` : ''}
        ${b.installed
          ? deleting.has(b.name)
            ? `<button class="danger loading" disabled><span class="spin"></span>卸载中</button><button onclick="showInstallLog('${b.name}')">日志</button>`
            : `<button class="danger" onclick="uninstallBackend('${b.name}')">卸载</button>`
          : ''}
      </div>
    </div>`).join('') : '<div class="empty">暂无已收录的后端 — 放入含 <code>backend.json</code> 的后端目录后会自动出现在这里</div>';
  renderInstallAll(list);
}
function renderInstallAll(list){
  const area = document.getElementById('installAllArea');
  if (!area) return;
  const missing = list.filter(b => !b.installed).length;
  if (allInstalling){
    area.innerHTML = `<button class="primary loading" disabled><span class="spin"></span>安装中 ${allDone}/${allTargets.length}</button>`;
  } else if (missing > 0){
    area.innerHTML = `<button class="primary" onclick="installAllNow()">安装全部 (${missing})</button>`;
  } else {
    area.innerHTML = '';
  }
}
async function pollInstall(name){
  while (true){
    const j = await api('/api/setup-log/' + name);
    // 只有当前正在查看该后端的安装日志时才刷新弹窗，避免并行安装时互相覆盖
    if (current === name && currentType === 'setup'){
      const pre = document.getElementById('logBody');
      pre.textContent = j.log || '(暂无日志)';
      pre.scrollTop = pre.scrollHeight;
    }
    if (!j.running){
      if (current === name && currentType === 'setup') toast(j.failed ? '安装失败：' + name : '安装完成：' + name);
      return j.failed;
    }
    await new Promise(r => setTimeout(r, 1200));
  }
}
async function run(name, act){
  try {
    const j = await api('/api/' + act + '/' + name, 'POST');
    if (j.warning) toast(j.warning, 5000);
    else toast(act==='start' ? '已启动：' + name : '已停止：' + name);
  } catch(e){
    if (!(e && e.message === '需要 WebUI token')) showAlert((act==='start' ? '启动' : '停止') + '失败：' + e.message);
  }
  refresh();
}
async function restartBackend(name){
  await api('/api/restart/' + name, 'POST');
  toast('已重启：' + name);
  refresh();
}
function openWebui(name){
  const b = backendsByName[name];
  if (!b || !b.webui || !b.webui.path) return;
  let url;
  if (b.webui.proxy !== false){
    // 走 launcher 同源反代：只暴露 launcher 端口，后端自身的端口不必对外开放
    url = '/ui/' + encodeURIComponent(name) + '/';
  } else {
    const path = b.webui.path || '/';
    url = location.protocol + '//' + location.hostname + ':' + b.port + path;
  }
  window.open(url, '_blank', 'noopener');
}
async function updateBackend(name){
  updatingBackends.add(name);
  refresh();
  try {
    const j = await api('/api/update-backend/' + name, 'POST');
    toast(j.updated ? '已更新并重启：' + name : '没有可更新的：' + name);
  } catch(e){
    if (!(e && e.message === '需要 WebUI token')) showAlert('更新失败：' + e.message);
  }
  updatingBackends.delete(name);
  refresh();
}
async function uninstallBackend(name){
  if (!confirm('确定卸载后端「' + name + '」吗？会停止进程并删除 installed/ 下的程序与依赖（下载缓存不受影响）。')) return;
  deleting.add(name);
  refresh();
  showInstallLog(name);
  try {
    await api('/api/uninstall/' + name, 'POST');
    await pollInstall(name);
    toast('已卸载：' + name);
  } catch(e){
    if (!(e && e.message === '需要 WebUI token')) showAlert('卸载失败：' + e.message);
  }
  deleting.delete(name);
  refresh();
}
async function installNow(name){
  installing.add(name);
  refresh();
  showInstallLog(name);
  try {
    await api('/api/install/' + name, 'POST');
    await pollInstall(name);
  } catch(e){}
  installing.delete(name);
  refresh();
}
async function allAct(act){
  const j = await api('/api/' + act + '-all', 'POST');
  if ((act === 'start' || act === 'restart') && j.started && j.started.length === 0 && j.skipped && j.skipped.length){
    showAlert('后端均未安装，已全部跳过。\\n可先点右上角「安装全部」，装完后再启动。');
  } else {
    const label = act==='start' ? '已启动全部' : act==='restart' ? '已重启全部' : '已停止全部';
    if ((act === 'start' || act === 'restart') && j.failed && j.failed.length){
      toast(`${label}，${j.failed.length} 个端口被占用未启动：${j.failed.join('、')}`, 6000);
    } else {
      toast(label);
    }
  }
  refresh();
}
async function updateNow(){
  if (updating) return;
  updating = true;
  const btn = document.getElementById('updateBtn');
  btn.innerHTML = '<span class="spin"></span>更新中';
  btn.disabled = true;
  try {
    const j = await api('/api/update', 'POST');
    if (!j.ok){ showAlert('更新失败：\\n\\n' + (j.output || j.message || '')); return; }
    if (!j.updated){ showAlert('没有可以更新的'); return; }
    showAlertHtml('更新完成：<br><br>' + fmtChangelog(j.changelog || j.output || '已拉取更新') + '<br><span style="color:var(--muted)">后端已一并重启，2 秒后自动重启 WebUI</span>');
    setTimeout(doRestartWebUI, 2000);
  } catch(e){
    if (!(e && e.message === '需要 WebUI token')) showAlert('更新失败：' + e.message);
  } finally {
    updating = false;
    btn.innerHTML = '⬆ 更新';
    btn.disabled = false;
  }
}
function toggleHideNoDeps(){
  hideNoDeps = !hideNoDeps;
  localStorage.setItem('hide_no_deps', hideNoDeps ? '1' : '0');
  updateHideDepsBtn();
  refresh();
}
function updateHideDepsBtn(){
  const btn = document.getElementById('hideDepsBtn');
  if (btn) btn.textContent = hideNoDeps ? '🙈 显示全部' : '🙈 隐藏未装后端';
}
function showAlert(msg){
  document.getElementById('alertBody').textContent = msg;
  document.getElementById('alertModal').classList.add('open');
}
function showAlertHtml(html){
  document.getElementById('alertBody').innerHTML = html;
  document.getElementById('alertModal').classList.add('open');
}
function fmtChangelog(text){
  const lines = (text || '').split('\\n');
  let html = '';
  for (const raw of lines){
    const line = raw.trim();
    if (!line) { html += '<br>'; continue; }
    if (line.startsWith('## ')) html += '<b style="font-size:14px;color:var(--text)">' + esc(line.slice(3)) + '</b><br>';
    else if (line.startsWith('### ')) html += '<b>' + esc(line.slice(4)) + '</b><br>';
    else if (line.startsWith('- ')) html += '• ' + esc(line.slice(2)) + '<br>';
    else html += esc(line) + '<br>';
  }
  return html;
}
function closeAlert(){ document.getElementById('alertModal').classList.remove('open'); }
async function installAllNow(){
  const list = await api('/api/backends');
  allTargets = list.backends.filter(b => !b.installed).map(b => b.name);
  allDone = 0;
  if (!allTargets.length) return;
  allInstalling = true;
  // 同时触发所有未安装后端的安装，相当于同时点击每个卡片的「安装」按钮
  allTargets.forEach(n => installing.add(n));
  refresh();
  try {
    const results = await Promise.all(allTargets.map(async name => {
      await api('/api/install/' + name, 'POST');
      const failed = await pollInstall(name);
      allDone++;
      return {name, failed};
    }));
    const failed = results.filter(r => r.failed).map(r => r.name);
    if (failed.length) toast(failed.length + ' 个安装失败：' + failed.join('、'));
    else toast('全部安装完成');
  } catch(e){}
  allTargets.forEach(n => installing.delete(n));
  allInstalling = false;
  refresh();
}
function cfgTruthy(v){
  const s = String(v == null ? '' : v).trim().toLowerCase();
  return ['1','true','yes','on','是','开启'].includes(s);
}
function cfgTri(v){
  const s = String(v == null ? '' : v).trim().toLowerCase();
  if (['1','true','yes','on'].includes(s)) return '1';
  if (['0','false','no','off'].includes(s)) return '0';
  return '';
}
async function openConfig(name){
  cfgName = name;
  const j = await api('/api/config/' + name);
  document.getElementById('configTitle').textContent = '配置：' + name;
  document.getElementById('cfgPort').value = j.port;
  document.getElementById('cfgToken').value = j.token || '';
  document.getElementById('cfgHost').value = j.host || '0.0.0.0';
  const box = document.getElementById('cfgCustom');
  box.innerHTML = '';
  const schema = j.config_schema || {};
  const opts = j.options || {};
  for (const key in schema){
    const f = schema[key];
    const label = document.createElement('label');
    label.className = 'cfg-label';
    const title = document.createElement('span');
    title.textContent = f.label || key;
    label.appendChild(title);
    const cur = opts[key] != null ? opts[key] : f.default;
    if (f.type === 'boolean'){
      const wrap = document.createElement('span');
      wrap.className = 'cfg-switch';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.cfgKey = key;
      cb.dataset.cfgType = 'boolean';
      cb.checked = cfgTruthy(cur);
      const txt = document.createElement('span');
      txt.className = 'cfg-switch-txt';
      txt.textContent = cb.checked ? '开' : '关';
      cb.addEventListener('change', () => { txt.textContent = cb.checked ? '开' : '关'; });
      wrap.append(cb, txt);
      label.appendChild(wrap);
    } else if (f.type === 'tristate'){
      const sel = document.createElement('select');
      sel.className = 'port';
      sel.dataset.cfgKey = key;
      sel.dataset.cfgType = 'tristate';
      for (const [val, text] of [['', '自动'], ['1', '开启'], ['0', '关闭']]){
        const o = document.createElement('option');
        o.value = val; o.textContent = text; sel.appendChild(o);
      }
      sel.value = cfgTri(cur);
      label.appendChild(sel);
    } else {
      const input = document.createElement('input');
      input.className = 'port';
      input.type = f.type === 'number' ? 'number' : 'text';
      input.value = cur != null ? cur : '';
      input.dataset.cfgKey = key;
      label.appendChild(input);
    }
    box.appendChild(label);
  }
  document.getElementById('configModal').classList.add('open');
}
function closeConfig(){
  document.getElementById('configModal').classList.remove('open');
  cfgName = null;
}
function randomToken(){
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  document.getElementById('cfgToken').value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}
async function saveConfig(){
  const port = parseInt(document.getElementById('cfgPort').value, 10);
  const token = document.getElementById('cfgToken').value.trim();
  const host = document.getElementById('cfgHost').value.trim() || '0.0.0.0';
  if (!port || port < 1 || port > 65535){ toast('端口必须是 1-65535'); return; }
  const options = {};
  document.querySelectorAll('#cfgCustom [data-cfg-key]').forEach(el => {
    const type = el.dataset.cfgType || 'text';
    if (type === 'boolean') options[el.dataset.cfgKey] = el.checked ? '1' : '0';
    else if (type === 'tristate') options[el.dataset.cfgKey] = el.value;
    else options[el.dataset.cfgKey] = el.value.trim();
  });
  await api('/api/config/' + cfgName, 'POST', JSON.stringify({port, token, host, options}));
  toast('配置已保存：' + cfgName + '（重启后端生效）');
  closeConfig();
  refresh();
}
function showInstallLog(name){
  current = name; currentType = 'setup';
  document.getElementById('logTitle').textContent = '操作：' + name;
  document.getElementById('logBody').textContent = '(等待安装日志...)';
  document.getElementById('modal').classList.add('open');
}
async function showLog(name){
  current = name; currentType = 'backend';
  document.getElementById('logTitle').textContent = '日志：' + name;
  document.getElementById('modal').classList.add('open');
  await loadLog();
}
function closeLog(){ document.getElementById('modal').classList.remove('open'); current = null; currentType = 'backend'; }
async function loadLog(){
  if (!current) return;
  const j = currentType === 'setup' ? await api('/api/setup-log/' + current) : await api('/api/logs/' + current);
  const pre = document.getElementById('logBody');
  pre.textContent = j.log || '(暂无日志)';
  pre.scrollTop = pre.scrollHeight;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape'){ closeLog(); closeAlert(); closeConfig(); closeToken(); } });
applyTheme();
updateHideDepsBtn();
setInterval(() => { if (loggedIn) refresh().catch(() => {}); }, 1000);
(async () => {
  if (getStoredToken()) {
    try { await refresh(); loggedIn = true; hideLogin(); return; } catch(e){}
  }
  showLogin();
})();
</script>
</body>
</html>"""


# 安全模式：管理页脚本异常导致无法登录/更新时的恢复入口（不依赖主页面脚本）。
SAFE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aiplugin4-backends · 安全模式</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background:#12141a; color:#e7e9ee; max-width:760px; margin:40px auto; padding:0 20px; line-height:1.7; }
  h1 { font-size:20px; }
  p { color:#9aa1ad; font-size:13px; }
  input { background:#1b1e26; border:1px solid #2a2f3a; color:#e7e9ee; border-radius:8px; padding:8px 10px; width:260px; }
  button { background:#232833; border:1px solid #333a48; color:#e7e9ee; border-radius:8px; padding:8px 14px; cursor:pointer; margin-right:8px; }
  button:hover { background:#2c3341; }
  pre { background:#171a21; border:1px solid #2a2f3a; border-radius:8px; padding:12px; white-space:pre-wrap; word-break:break-all; font-size:12px; min-height:80px; }
  .row { margin:14px 0; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  a { color:#9aa1ad; font-size:13px; }
</style>
</head>
<body>
<h1>aiplugin4-backends · 安全模式</h1>
<p>管理页面脚本异常时，用本页更新本体或重启 WebUI。需要 WebUI 访问 token（与登录页一致）。</p>
<div class="row"><input id="tok" type="password" placeholder="WebUI token" autocomplete="off"></div>
<div class="row">
  <button id="btnUpdate">⬆ 更新本体</button>
  <button id="btnRestart">🔄 重启 WebUI</button>
  <a href="/">返回管理页</a>
</div>
<pre id="log">就绪。</pre>
<script>
(function(){
  var tokenInput = document.getElementById('tok');
  var log = document.getElementById('log');
  function token(){ return (tokenInput.value || '').trim() || localStorage.getItem('webui_token') || ''; }
  function say(msg){ log.textContent += msg + String.fromCharCode(10); }
  function api(path, body){
    return fetch(path, {
      method: 'POST',
      headers: { 'X-Token': token(), 'Content-Type': 'application/json' },
      body: body || undefined
    }).then(function(r){
      return r.json().catch(function(){ return { ok: r.ok }; }).then(function(j){
        if (!r.ok) throw new Error((j && (j.message || j.error)) || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  document.getElementById('btnUpdate').onclick = function(){
    say('正在更新…');
    api('/api/update').then(function(j){
      say(j.updated ? ('更新完成：' + (j.changelog || j.output || '已更新')) : '没有可更新的版本');
    }).catch(function(e){ say('更新失败：' + e.message); });
  };
  document.getElementById('btnRestart').onclick = function(){
    say('正在重启 WebUI…');
    api('/api/webui-restart').then(function(){ say('已请求重启，约 2 秒后刷新本页。'); })
      .catch(function(e){ say('重启失败：' + e.message); });
  };
})();
</script>
</body>
</html>"""


def run_webui(backends, config, supervisor: Supervisor, host: str = None, port: int = None, open_browser: bool = True) -> None:
    """启动 Web 管理界面（阻塞，Ctrl+C 退出）"""
    host = host or effective_webui_host()
    port = int(port) if port else effective_webui_port()
    token = effective_webui_token()
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.get("log_dir", DEFAULT_LOG_DIR))
    by_name = {b.name: b for b in backends}
    setup_state = {}  # name -> {"lines": [...], "running": bool, "failed": bool}
    setup_lock = threading.Lock()

    def start_setup(name: str, words: tuple = ("setup",)) -> None:
        """后台执行 launcher.py <words> <name>（setup / install-backend），日志实时追加；已在运行时直接复用"""
        with setup_lock:
            if setup_state.get(name, {}).get("running"):
                return
            setup_state[name] = {"lines": [], "running": True, "failed": False}

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher.py")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        def run_setup():
            ok = False
            try:
                kwargs = {}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # 后台安装，不弹控制台黑框
                proc = subprocess.Popen(
                    [sys.executable, script, *words, name],
                    cwd=os.path.dirname(script),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    **kwargs,
                )
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    with setup_lock:
                        st = setup_state.get(name)
                        if st is not None:
                            if len(st["lines"]) >= 2000:
                                st["lines"].pop(0)
                            st["lines"].append(line)
                proc.wait()
                ok = proc.returncode == 0
            except Exception as e:  # noqa: BLE001
                with setup_lock:
                    if name in setup_state:
                        setup_state[name]["lines"].append(f"[webui] 安装进程异常: {e}")
            finally:
                with setup_lock:
                    st = setup_state.get(name)
                    if st is not None:
                        st["running"] = False
                        st["failed"] = not ok

        threading.Thread(target=run_setup, daemon=True).start()

    def start_install(name: str) -> None:
        """后台安装后端：launcher.py install-backend <name>（下载程序 + 安装依赖）"""
        start_setup(name, words=("install-backend",))

    def start_uninstall(name: str) -> None:
        """后台卸载后端：launcher.py uninstall-backend <name>（停进程 + 删 installed/<name>）"""
        start_setup(name, words=("uninstall-backend",))

    def _restart_webui() -> None:
        """响应返回后延迟触发：由独立进程先停旧 WebUI 再启动新 WebUI（重新加载后端清单）"""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher.py")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, script, "webui-restart"],
            cwd=os.path.dirname(script),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 关闭默认访问日志刷屏
            pass

        def _authorized(self) -> bool:
            tok = getattr(self, "webui_token", "")
            if not tok:
                return True
            auth = self.headers.get("Authorization") or ""
            if auth == f"Bearer {tok}" or (self.headers.get("X-Token") or "") == tok:
                # header 鉴权成功时顺手下发 /ui 前缀的会话 cookie，供 window.open 打开 WebUI 反代时使用
                self._issue_ui_cookie = True
                return True
            if self.path.split("?", 1)[0].startswith(UI_PREFIX):
                # 只有 /ui 反代接受 cookie；launcher 自己的 /api/* 仍必须带 header token
                cookie = self.headers.get("Cookie") or ""
                for item in cookie.split(";"):
                    key, _, value = item.strip().partition("=")
                    if key == UI_COOKIE_NAME and hmac.compare_digest(value, _ui_cookie_value(tok)):
                        return True
            return False

        def _is_secure_request(self) -> bool:
            return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

        def _ui_cookie(self) -> str:
            return _ui_cookie_header(getattr(self, "webui_token", ""), self._is_secure_request())

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if getattr(self, "_issue_ui_cookie", False) and getattr(self, "webui_token", ""):
                self.send_header("Set-Cookie", self._ui_cookie())
            self.end_headers()
            self.wfile.write(body)

        def _err(self, message, status=500):
            self._json({"ok": False, "message": message}, status)

        def _ui_unauthorized(self):
            """反代页面是浏览器导航，401 JSON 对用户不友好，返回可点回登录页的 HTML。"""
            body = (
                "<!doctype html><meta charset='utf-8'><title>需要登录</title>"
                "<body style='margin:0;min-height:100vh;display:grid;place-items:center;"
                "font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#eef1f6;color:#1f2937'>"
                "<div style='max-width:420px;padding:28px 32px;background:#fff;border:1px solid #e3e6ea;"
                "border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.06);text-align:center'>"
                "<h2 style='margin:0 0 10px;font-size:18px'>需要 WebUI 登录</h2>"
                "<p style='margin:0 0 16px;line-height:1.6;color:#4b5563'>"
                "请先在 launcher 管理页输入访问 token，再回来打开后端 WebUI。</p>"
                "<a href='/' style='display:inline-block;padding:9px 18px;border-radius:9px;"
                "background:#2563eb;color:#fff;text-decoration:none'>返回登录</a>"
                "</div></body>"
            ).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            nonlocal backends, by_name
            backends = discover_backends()
            by_name = {b.name: b for b in backends}
            path_only, _, query = self.path.partition("?")
            if path_only not in ("/", "/safe", "/icon.png", "/icon-256.png") and not self._authorized():
                if path_only.startswith(UI_PREFIX):
                    self._ui_unauthorized()
                else:
                    self._err("unauthorized", 401)
                return
            if path_only.startswith(UI_PREFIX):
                self._proxy("GET", path_only, query)
                return
            if self.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path_only == "/safe":
                body = SAFE_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/icon.png", "/icon-256.png"):
                icon_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "assets",
                    os.path.basename(self.path),
                )
                try:
                    with open(icon_path, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "max-age=86400")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    self._err("not found", 404)
                return
            if self.path == "/api/webui-token":
                self._json({"ok": True, "token": self.webui_token})
                return
            if path_only == "/api/plugin-config":
                try:
                    params = urllib.parse.parse_qs(query)
                    host = params.get("host", ["127.0.0.1"])[0]
                    image_host = params.get("image_host", [""])[0]
                    result = build_plugin_backend_config(host, None, image_host)
                except ValueError as e:
                    self._err(str(e), 400)
                    return
                result["ok"] = True
                self._json(result)
                return
            if path_only == "/api/mcp-config":
                try:
                    host = urllib.parse.parse_qs(query).get("host", ["127.0.0.1"])[0]
                    result = build_plugin_mcp_config(host)
                except ValueError as e:
                    self._err(str(e), 400)
                    return
                result["ok"] = True
                self._json(result)
                return
            if self.path == "/api/backends":
                rows = []
                for b in backends:
                    info = supervisor.state.get(b.name) or {}
                    cfg = backend_config(b)
                    running = supervisor.is_running(b.name)
                    uptime_secs = None
                    if running and info.get("started_at"):
                        try:
                            started = datetime.strptime(info["started_at"], "%Y-%m-%d %H:%M:%S")
                            uptime_secs = max(0, int(time.time() - started.timestamp()))
                        except (ValueError, TypeError):
                            uptime_secs = None
                    mem_mb = None
                    mem_pct = None
                    if running and info.get("pid"):
                        mem = process_memory(info.get("pid"))
                        if mem and mem[1]:
                            mem_mb = round(mem[0] / 1024 / 1024, 1)
                            mem_pct = round(mem[0] / mem[1] * 100, 2)
                    rows.append({
                        "name": b.name,
                        "description": b.description,
                        "type": b.type,
                        "version": b.version,
                        "port": cfg["port"],
                        "host": cfg["host"],
                        "token": cfg["token"],
                        "default_port": b.port,
                        "running": running,
                        "uptime_secs": uptime_secs,
                        "restarts": supervisor.state.get("restarts", {}).get(b.name, 0),
                        "mem_mb": mem_mb,
                        "mem_pct": mem_pct,
                        "deps_ready": deps_ready(b),
                        "installed": deps_ready(b),  # 程序 + 依赖都就绪才算已安装
                        "pid": info.get("pid"),
                        "webui": b.webui or None,
                    })
                installed_names = {b.name for b in backends}
                for item in load_registry():
                    name = item.get("name")
                    if not name or name in installed_names:
                        continue
                    rows.append({
                        "name": name,
                        "description": item.get("description", ""),
                        "type": item.get("type", "python"),
                        "version": str(item.get("version", "") or ""),
                        "port": int(item.get("port", 0)),
                        "host": "",
                        "token": "",
                        "default_port": int(item.get("port", 0)),
                        "running": False,
                        "uptime_secs": None,
                        "restarts": 0,
                        "mem_mb": None,
                        "mem_pct": None,
                        "deps_ready": False,
                        "installed": False,
                        "pid": None,
                        "webui": normalize_webui(item.get("webui")) or None,
                    })
                self._json({"ok": True, "backends": rows, "updates": update_check()})
                return
            if self.path.startswith("/api/config/"):
                name = self.path[len("/api/config/"):]
                backend = by_name.get(name)
                if not backend:
                    self._err(f"未知后端: {name}", 404)
                    return
                cfg = backend_config(backend)
                schema = backend_custom_config(backend)
                options = {}
                for key, field in schema.items():
                    options[key] = cfg["options"].get(key, _expand_config_value(field.get("default", "")))
                self._json({
                    "ok": True,
                    "port": cfg["port"],
                    "token": cfg["token"],
                    "host": cfg["host"],
                    "default_port": backend.port,
                    "options": options,
                    "config_schema": schema,
                })
                return
            if self.path.startswith("/api/setup-log/"):
                name = self.path[len("/api/setup-log/"):]
                with setup_lock:
                    st = setup_state.get(name) or {"lines": [], "running": False, "failed": False}
                    log = "\n".join(st["lines"])
                self._json({"ok": True, "running": st["running"], "failed": st["failed"], "log": log})
                return
            if self.path.startswith("/api/logs/"):
                name = self.path[len("/api/logs/"):]
                log_file = os.path.join(log_dir, name + ".log")
                try:
                    with open(log_file, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    self._json({"ok": True, "log": "".join(lines[-300:])})
                except OSError:
                    self._json({"ok": True, "log": ""})
                return
            self._err("not found", 404)

        def do_POST(self):
            nonlocal backends, by_name
            backends = discover_backends()
            by_name = {b.name: b for b in backends}
            if not self._authorized():
                self._err("unauthorized", 401)
                return
            path_only, _, query = self.path.partition("?")
            if path_only.startswith(UI_PREFIX):
                self._proxy("POST", path_only, query)
                return
            path = self.path
            try:
                if path == "/api/webui-token":
                    body = self._read_json()
                    if body.get("reset"):
                        reset_webui_token()
                        token = effective_webui_token()
                    else:
                        token = str(body.get("token", "") or "").strip()
                        if not token:
                            self._err("token 不能为空", 400)
                            return
                        save_webui_token(token)
                    Handler.webui_token = token  # 立即生效，无需重启
                    self._json({"ok": True, "token": token})
                    return
                if path == "/api/webui-restart":
                    self._json({"ok": True, "message": "restarting"})
                    threading.Timer(0.3, _restart_webui).start()
                    return
                if path == "/api/start-all":
                    targets = [b for b in backends if deps_ready(b)]
                    skipped = [b.name for b in backends if not deps_ready(b)]
                    failed = supervisor.start(targets)
                    self._json({
                        "ok": True,
                        "message": "started",
                        "started": [b.name for b in targets if b.name not in failed],
                        "skipped": skipped,
                        "failed": failed,
                    })
                    return
                if path == "/api/restart-all":
                    supervisor.stop(backends)
                    time.sleep(0.5)
                    targets = [b for b in backends if deps_ready(b)]
                    skipped = [b.name for b in backends if not deps_ready(b)]
                    for name in targets:
                        if name in supervisor.state.setdefault("stopped", []):
                            supervisor.state["stopped"].remove(name)
                    supervisor._save_state()
                    failed = supervisor.start(targets)
                    self._json({
                        "ok": True,
                        "message": "restarted",
                        "started": [b.name for b in targets if b.name not in failed],
                        "skipped": skipped,
                        "failed": failed,
                    })
                    return
                if path == "/api/stop-all":
                    supervisor.stop(backends)
                    self._json({"ok": True, "message": "stopped"})
                    return
                if path == "/api/update":
                    try:
                        res = update_project()
                        if res["updated"]:
                            # 更新成功：先停全部 → 同步依赖（清单变化会重建，保证不多不少）→ 再启动
                            supervisor.stop(backends)
                            time.sleep(0.5)
                            for b in backends:
                                try:
                                    setup_backend(b)
                                except Exception as e:  # noqa: BLE001
                                    print(f"[webui] 后端 {b.name} 依赖同步失败，跳过启动: {e}")
                            targets = [b for b in backends if deps_ready(b)]
                            for name in targets:
                                if name in supervisor.state.setdefault("stopped", []):
                                    supervisor.state["stopped"].remove(name)
                            supervisor._save_state()
                            supervisor.start(targets)
                        self._json({
                            "ok": True,
                            "message": "updated" if res["updated"] else "no-update",
                            "updated": res["updated"],
                            "changelog": res["changelog"],
                            "output": res["output"],
                        })
                    except Exception as e:  # noqa: BLE001
                        self._json({"ok": False, "message": str(e), "output": str(e)})
                    return
                if path.startswith("/api/config/"):
                    name = path[len("/api/config/"):]
                    backend = by_name.get(name)
                    if not backend:
                        self._err(f"未知后端: {name}", 404)
                        return
                    body = self._read_json()
                    try:
                        value = int(body.get("port"))
                    except (TypeError, ValueError):
                        self._err("端口必须是 1-65535 的整数", 400)
                        return
                    if not 1 <= value <= 65535:
                        self._err("端口必须是 1-65535 的整数", 400)
                        return
                    token = str(body.get("token", "") or "")
                    host = str(body.get("host", "") or "0.0.0.0")
                    options = {}
                    raw_options = body.get("options") or {}
                    for key, field in backend_custom_config(backend).items():
                        if key not in raw_options:
                            continue
                        val = str(raw_options[key]).strip()
                        ftype = field.get("type")
                        truthy = ("1", "true", "yes", "on", "是", "开启")
                        if ftype == "number" and val:
                            try:
                                int(val)
                            except ValueError:
                                self._err(f"配置项「{field.get('label', key)}」必须是数字", 400)
                                return
                        elif ftype == "boolean" and val:
                            low = val.lower()
                            if low in truthy:
                                val = "1"
                            elif low in ("0", "false", "no", "off", "否", "关闭"):
                                val = "0"
                            else:
                                self._err(f"配置项「{field.get('label', key)}」必须是开/关", 400)
                                return
                        elif ftype == "tristate":
                            low = val.lower()
                            if low in truthy:
                                val = "1"
                            elif low in ("0", "false", "no", "off", "否", "关闭"):
                                val = "0"
                            elif low in ("", "auto", "自动"):
                                val = ""
                            else:
                                self._err(f"配置项「{field.get('label', key)}」必须是 自动/开/关", 400)
                                return
                        if not val:
                            val = str(field.get("default", "") or "")
                        options[key] = val
                    cfg = save_backend_config(name, port=value, token=token, host=host, options=options)
                    self._json({"ok": True, "port": cfg["port"], "token": cfg["token"], "host": cfg["host"], "options": options})
                    return
                if path.startswith("/api/port/"):
                    rest = path[len("/api/port/"):]
                    if rest.endswith("/reset"):
                        name = rest[:-len("/reset")]
                        backend = by_name.get(name)
                        if not backend:
                            self._err(f"未知后端: {name}", 404)
                            return
                        runtime = load_runtime()
                        runtime.get("config", {}).pop(name, None)
                        runtime.get("ports", {}).pop(name, None)
                        save_runtime(runtime)
                        self._json({"ok": True, "port": backend.port})
                        return
                    name = rest
                    backend = by_name.get(name)
                    if not backend:
                        self._err(f"未知后端: {name}", 404)
                        return
                    value = self._read_json().get("port")
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        self._err("端口必须是 1-65535 的整数", 400)
                        return
                    if not 1 <= value <= 65535:
                        self._err("端口必须是 1-65535 的整数", 400)
                        return
                    cfg = save_backend_config(name, port=value)
                    self._json({"ok": True, "port": cfg["port"]})
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "api":
                    action, name = parts[1], parts[2]
                    if action == "install":
                        start_install(name)
                        self._json({"ok": True, "message": "install started"})
                        return
                    backend = by_name.get(name)
                    if not backend:
                        self._err(f"未知后端: {name}", 404)
                        return
                    if action == "start":
                        if name in supervisor.state.get("stopped", []):
                            supervisor.state["stopped"].remove(name)
                            supervisor._save_state()
                        failed = supervisor.start([backend])
                        warning = None
                        if failed:
                            warning = f"{name} 端口被未记录的进程占用，未启动（请先结束占用进程）"
                        self._json({"ok": True, "warning": warning})
                        return
                    if action == "stop":
                        supervisor.stop([backend])
                        self._json({"ok": True})
                        return
                    if action == "restart":
                        supervisor.stop([backend])
                        if name in supervisor.state.get("stopped", []):
                            supervisor.state["stopped"].remove(name)
                            supervisor._save_state()
                        time.sleep(0.5)
                        supervisor.start([backend])
                        self._json({"ok": True, "message": "restarted"})
                        return
                    if action == "update-backend":
                        try:
                            if supervisor.is_running(name):
                                supervisor.stop([backend])
                            new_backend = update_backend(name)  # 下载该后端独立包 → 覆盖商店 → 重装程序 + 同步依赖
                            if name in supervisor.state.get("stopped", []):
                                supervisor.state["stopped"].remove(name)
                                supervisor._save_state()
                            supervisor.start([new_backend])
                        except Exception as e:  # noqa: BLE001
                            self._json({"ok": False, "message": str(e)})
                            return
                        self._json({
                            "ok": True,
                            "updated": True,
                            "message": "updated",
                            "deps_ready": deps_ready(new_backend),
                        })
                        return
                    if action == "uninstall":
                        start_uninstall(name)
                        self._json({"ok": True, "message": "uninstall started"})
                        return
                self._err("not found", 404)
            except Exception as e:  # noqa: BLE001
                self._err(str(e))

        # ---------------------------------------------------------------- 后端 WebUI 反代
        def do_HEAD(self):
            self._proxy_method("HEAD")

        def do_PUT(self):
            self._proxy_method("PUT")

        def do_PATCH(self):
            self._proxy_method("PATCH")

        def do_DELETE(self):
            self._proxy_method("DELETE")

        def do_OPTIONS(self):
            self._proxy_method("OPTIONS")

        def _proxy_method(self, method: str):
            nonlocal backends, by_name
            backends = discover_backends()
            by_name = {b.name: b for b in backends}
            path_only, _, query = self.path.partition("?")
            if not self._authorized():
                if path_only.startswith(UI_PREFIX):
                    self._ui_unauthorized()
                else:
                    self._err("unauthorized", 401)
                return
            if path_only.startswith(UI_PREFIX):
                self._proxy(method, path_only, query)
            else:
                self._err("not found", 404)

        def _proxy_error(self, status: int, title: str, detail: str = ""):
            body = (
                "<!doctype html><meta charset='utf-8'><title>{0}</title>"
                "<body style='margin:0;min-height:100vh;display:grid;place-items:center;"
                "font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#eef1f6;color:#1f2937'>"
                "<div style='max-width:460px;padding:28px 32px;background:#fff;border:1px solid #e3e6ea;"
                "border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.06);text-align:center'>"
                "<h2 style='margin:0 0 10px;font-size:18px'>{1}</h2>"
                "<p style='margin:0 0 16px;line-height:1.6;color:#4b5563'>{2}</p>"
                "<a href='/' style='display:inline-block;padding:9px 18px;border-radius:9px;"
                "background:#2563eb;color:#fff;text-decoration:none'>返回 launcher</a>"
                "</div></body>"
            ).format(status, html.escape(title), html.escape(detail))
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        @staticmethod
        def _rewrite_location(value: str, name: str, port: int) -> str:
            """把后端返回的根路径 / 自身绝对地址重写到 /ui/<name>/ 前缀下。"""
            if not value:
                return value
            for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                if value.lower().startswith(origin):
                    value = value[len(origin):] or "/"
                    break
            if value.startswith("/") and not value.startswith("//") and not value.startswith(UI_PREFIX):
                return f"/ui/{name}{value}"
            return value

        def _proxy(self, method: str, path: str, query: str):
            """把 /ui/<name>/... 反代到 127.0.0.1:<后端端口><path>，流式转发，不缓冲整个响应。"""
            rest = path[len(UI_PREFIX):]
            name, _, sub = rest.partition("/")
            name = urllib.parse.unquote(name)
            backend = by_name.get(name)
            if not backend or not backend.webui:
                self._proxy_error(404, "没有这个带 WebUI 的后端", "该后端未安装、未声明 WebUI，或名称不正确。")
                return
            if not supervisor.is_running(name):
                self._proxy_error(503, "后端未运行", "请先在卡片上启动该后端，再打开它的 WebUI。")
                return
            meta = backend.webui
            if meta.get("proxy") is False:
                self._proxy_error(403, "该后端未启用 launcher 反代", "请在卡片上直连后端端口打开 WebUI。")
                return
            if path == UI_PREFIX + name:
                # 补尾斜杠，页面里的相对路径才会落在 /ui/<name>/ 下
                self.send_response(302)
                self.send_header("Location", f"/ui/{name}/" + (f"?{query}" if query else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            parts = [p for p in urllib.parse.unquote(sub).split("/") if p]
            if any(p in (".", "..") for p in parts):
                self._proxy_error(400, "非法路径", "WebUI 反代不接受包含 . 或 .. 的路径。")
                return
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                self._proxy_error(411, "不支持的请求体", "WebUI 反代暂不支持 chunked 请求体，请改用带 Content-Length 的请求。")
                return
            cfg = backend_config(backend)
            port = int(cfg["port"])
            upstream_host = "127.0.0.1" if cfg["host"] in ("0.0.0.0", "::", "") else cfg["host"]
            base = meta.get("path") or "/"
            # base 为后端内 WebUI 根路径；请求子路径接在其后（base="/" 时直接拼）
            upstream_path = "/" + sub if base in ("", "/") else base.rstrip("/") + "/" + sub
            target = upstream_path + (("?" + query) if query else "")
            hop_by_hop = {
                "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailers", "transfer-encoding", "upgrade",
            }
            webui_token = getattr(self, "webui_token", "")
            conn = http.client.HTTPConnection(upstream_host, port, timeout=30)
            started = False
            try:
                body_len = int(self.headers.get("Content-Length") or 0)
                conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
                sent_backend_token = False
                for key, value in self.headers.items():
                    low = key.lower()
                    if low in hop_by_hop or low in ("host", "content-length"):
                        continue
                    # launcher 自己的鉴权头不要透给后端（避免泄露 launcher token）
                    if webui_token and low == "x-token" and value.strip() == webui_token:
                        continue
                    if webui_token and low == "authorization" and value.strip() == f"Bearer {webui_token}":
                        continue
                    if low == "cookie":
                        kept = [
                            part for part in value.split(";")
                            if not part.strip().startswith(UI_COOKIE_NAME + "=")
                        ]
                        if not kept:
                            continue
                        value = ";".join(kept)
                    if low in ("x-token", "authorization"):
                        sent_backend_token = True
                    conn.putheader(key, value)
                if meta.get("inject_token") and cfg.get("token") and not sent_backend_token:
                    conn.putheader("X-Token", cfg["token"])
                conn.putheader("Host", f"{upstream_host}:{port}")
                forwarded_for = self.headers.get("X-Forwarded-For") or (
                    self.client_address[0] if self.client_address else ""
                )
                if forwarded_for:
                    conn.putheader("X-Forwarded-For", forwarded_for)
                conn.putheader("X-Forwarded-Proto", self.headers.get("X-Forwarded-Proto") or "http")
                if self.headers.get("Host"):
                    conn.putheader("X-Forwarded-Host", self.headers["Host"])
                conn.putheader("X-Forwarded-Prefix", f"/ui/{name}")
                if body_len:
                    conn.putheader("Content-Length", str(body_len))
                conn.endheaders()
                remaining = body_len
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    conn.send(chunk)
                    remaining -= len(chunk)
                resp = conn.getresponse()
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    low = key.lower()
                    if low in hop_by_hop or low in ("transfer-encoding", "date", "server"):
                        continue
                    if low == "location":
                        value = self._rewrite_location(value, name, port)
                    elif low == "set-cookie":
                        value = _rewrite_set_cookie(value, name)
                    self.send_header(key, value)
                if getattr(self, "_issue_ui_cookie", False) and webui_token:
                    self.send_header("Set-Cookie", self._ui_cookie())
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                started = True
                if method != "HEAD":
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                if not started:
                    try:
                        self._proxy_error(502, "连接后端失败", str(e))
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    Handler.webui_token = token
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "::") else f"http://{host}:{port}"
    print(f"[launcher] WebUI 已启动: {url}（Ctrl+C 退出）")
    print(f"[launcher] WebUI 访问 token: {token}（可用 aibackend webui-token 修改）")
    if open_browser and _can_open_browser():
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[launcher] WebUI 已停止")
    finally:
        server.server_close()
