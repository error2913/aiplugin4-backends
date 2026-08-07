#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aiplugin4 后端 Web 管理界面（纯 Python 标准库，无第三方依赖）。

由 launcher.py 直接启动：
  python launcher.py

（也可用 python launcher.py webui [--host 127.0.0.1] [--port 8910] [--no-browser] 调整参数）

默认只监听 127.0.0.1（本机），无鉴权，请勿暴露到公网。
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from launcher import (
    DEFAULT_LOG_DIR,
    Supervisor,
    backend_config,
    deps_ready,
    load_runtime,
    process_memory,
    remove_backend_deps,
    save_backend_config,
    save_runtime,
    update_project,
)

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
  .ops { display: flex; gap: 8px; flex-wrap: wrap; }
  .ops { justify-content: center; }
  .ops button { padding: 6px 12px; font-size: 12.5px; }
  button.loading { opacity: .8; cursor: progress; }
  .spin { display: inline-block; width: 11px; height: 11px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; margin-right: 6px; vertical-align: -1px; animation: rot .8s linear infinite; }
  @keyframes rot { to { transform: rotate(360deg); } }
  .modal {
    position: fixed; inset: 0; background: rgba(5,8,12,.55); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; z-index: 50; padding: 24px;
  }
  .modal.open { display: flex; }
  .dialog {
    width: min(860px, 100%); max-height: 82vh; background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 60px var(--shadow);
  }
  .dialog-head { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .dialog-head b { font-size: 14px; }
  .dialog-head .spacer { flex: 1; }
  .dialog pre {
    margin: 0; padding: 16px 18px; overflow: auto; font-size: 12.5px; line-height: 1.55;
    font-family: Consolas, "Courier New", monospace; color: var(--muted); white-space: pre-wrap; word-break: break-all;
  }
  .close { background: transparent; border: none; font-size: 18px; color: var(--muted); cursor: pointer; padding: 2px 8px; }
  #toast {
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 99; max-width: 70vw;
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 16px; font-size: 13px; display: none; box-shadow: 0 12px 32px var(--shadow);
  }
  footer { margin-top: 26px; color: var(--muted); font-size: 12px; text-align: center; opacity: .7; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img class="logo" src="/icon-256.png" alt="aiplugin4">
    <div>
      <h1>aiplugin4 后端管理</h1>
      <div class="sub">launcher WebUI · 每 3 秒自动刷新 · 后端异常退出会自动拉起</div>
    </div>
    <div class="header-right">
      <button id="themeBtn" onclick="cycleTheme()">主题</button>
      <button onclick="updateNow()">⬆ 更新</button>
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
    <div style="padding:18px 22px; display:grid; gap:16px;">
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
    </div>
    <div style="padding:12px 18px; text-align:right; border-top:1px solid var(--border); display:flex; gap:8px; justify-content:flex-end;">
      <button onclick="closeConfig()">取消</button>
      <button class="primary" onclick="saveConfig()">保存</button>
    </div>
  </div>
</div>
<div id="toast"></div>
<footer>aiplugin4 · launcher.py webui</footer>
<script>
let current = null;
let currentType = 'backend';
let cfgName = null;
const installing = new Set();
const deleting = new Set();
let allInstalling = false;
let allTargets = [];
let allDone = 0;
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
async function api(path, method, body){
  try {
    const r = await fetch(path, {method: method||'GET', body: body || undefined});
    const j = await r.json();
    if (!j.ok) throw new Error(j.message || ('HTTP ' + r.status));
    return j;
  } catch(e){ toast('请求失败: ' + e.message); throw e; }
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
  const list = j.backends;
  document.getElementById('stTotal').textContent = list.length;
  document.getElementById('stRun').textContent = list.filter(b => b.running).length;
  document.getElementById('grid').innerHTML = list.map(b => `
    <div class="card ${b.running ? 'running' : ''}">
      <div class="row1">
        <span class="name">${esc(b.name)}</span>
        <span class="badge ${b.type === 'python' ? 'py' : 'node'}">${esc(b.type).toUpperCase()}</span>
        <span class="status ${b.running ? 'on' : 'off'}"><span class="dot ${b.running ? 'on' : ''}"></span>${b.running ? '运行中' : '已停止'}</span>
      </div>
      <div class="desc">${esc(b.description)}</div>
      <div class="meta">
        <button class="mini" onclick="openConfig('${b.name}')" title="端口 / token / 监听IP">⚙ 配置</button>
        ${b.token ? `<span class="chip token" title="访问 token">🔑 ${esc(b.token)}</span>` : ''}
        ${b.running && b.pid ? `<span class="chip idle">pid ${b.pid}</span>` : ''}
      </div>
      <div class="meta">
        <span class="chip idle">⏱ ${b.running ? fmtUptime(b.uptime_secs) : '未运行'}</span>
        <span class="chip idle">🔄 自动拉起 ${b.restarts} 次</span>
        ${b.running && b.mem_mb != null ? `<span class="chip idle">💾 ${b.mem_mb}MB / ${b.mem_pct}%</span>` : ''}
      </div>
      <div class="ops">
        ${installing.has(b.name)
          ? `<button class="primary loading" disabled><span class="spin"></span>安装中</button>`
          : b.running
            ? `<button class="danger" onclick="run('${b.name}','stop')">停止</button>`
            : b.deps_ready
              ? `<button class="primary" onclick="run('${b.name}','start')">启动</button>`
              : `<button class="primary" onclick="setupNow('${b.name}')">安装依赖</button>`}
        <button onclick="showLog('${b.name}')">日志</button>
        ${b.deps_ready
          ? deleting.has(b.name)
            ? `<button class="danger loading" disabled><span class="spin"></span>删除中</button>`
            : `<button class="danger" onclick="delDeps('${b.name}')">删除依赖</button>`
          : ''}
      </div>
    </div>`).join('');
  renderInstallAll(list);
}
function renderInstallAll(list){
  const area = document.getElementById('installAllArea');
  if (!area) return;
  const missing = list.filter(b => !b.deps_ready && !b.running).length;
  if (allInstalling){
    area.innerHTML = `<button class="primary loading" disabled><span class="spin"></span>安装中 ${allDone}/${allTargets.length}</button>`;
  } else if (missing > 0){
    area.innerHTML = `<button class="primary" onclick="setupAll()">安装全部依赖 (${missing})</button>`;
  } else {
    area.innerHTML = '';
  }
}
async function setupNow(name){
  installing.add(name);
  refresh();
  showInstallLog(name);
  try {
    await api('/api/setup/' + name, 'POST');
    await pollInstall(name);
  } catch(e){}
  installing.delete(name);
  refresh();
}
async function delDeps(name){
  deleting.add(name);
  refresh();
  try {
    await api('/api/deps-delete/' + name, 'POST');
    toast('已删除依赖：' + name);
  } catch(e){}
  deleting.delete(name);
  refresh();
}
async function pollInstall(name){
  while (true){
    const j = await api('/api/setup-log/' + name);
    const pre = document.getElementById('logBody');
    pre.textContent = j.log || '(暂无日志)';
    pre.scrollTop = pre.scrollHeight;
    if (!j.running){
      toast(j.failed ? '安装失败：' + name : '安装完成：' + name);
      return;
    }
    await new Promise(r => setTimeout(r, 1200));
  }
}
async function run(name, act){ await api('/api/' + act + '/' + name, 'POST'); toast(act==='start' ? '已启动：' + name : '已停止：' + name); refresh(); }
async function allAct(act){
  const j = await api('/api/' + act + '-all', 'POST');
  if ((act === 'start' || act === 'restart') && j.started && j.started.length === 0 && j.skipped && j.skipped.length){
    showAlert('后端依赖均未安装，已全部跳过。\\n可先点右上角「安装全部依赖」，装完后再启动。');
  } else {
    toast(act==='start' ? '已启动全部' : act==='restart' ? '已重启全部' : '已停止全部');
  }
  refresh();
}
async function updateNow(){
  try {
    const r = await fetch('/api/update', {method: 'POST'});
    const j = await r.json();
    if (!j.ok){ showAlert('更新失败：\\n\\n' + (j.output || j.message || '')); return; }
    if (!j.updated){ showAlert('没有可以更新的'); return; }
    showAlert('更新完成：\\n\\n' + (j.changelog || j.output || '已拉取更新'));
  } catch(e){
    showAlert('更新失败：' + e.message);
  }
}
function showAlert(msg){
  document.getElementById('alertBody').textContent = msg;
  document.getElementById('alertModal').classList.add('open');
}
function closeAlert(){ document.getElementById('alertModal').classList.remove('open'); }
async function setupAll(){
  const list = await api('/api/backends');
  allTargets = list.backends.filter(b => !b.deps_ready && !b.running).map(b => b.name);
  allDone = 0;
  if (!allTargets.length) return;
  allInstalling = true;
  showInstallLog('全部依赖');
  refresh();
  try {
    for (const name of allTargets){
      document.getElementById('logTitle').textContent = '安装全部依赖 (' + allDone + '/' + allTargets.length + ')：' + name;
      document.getElementById('logBody').textContent = '(等待安装日志...)';
      await api('/api/setup/' + name, 'POST');
      await pollInstall(name);
      allDone++;
    }
    toast('全部依赖安装完成');
  } catch(e){}
  allInstalling = false;
  refresh();
}
async function openConfig(name){
  cfgName = name;
  const j = await api('/api/config/' + name);
  document.getElementById('configTitle').textContent = '配置：' + name;
  document.getElementById('cfgPort').value = j.port;
  document.getElementById('cfgToken').value = j.token || '';
  document.getElementById('cfgHost').value = j.host || '0.0.0.0';
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
  await api('/api/config/' + cfgName, 'POST', JSON.stringify({port, token, host}));
  toast('配置已保存：' + cfgName + '（重启后端生效）');
  closeConfig();
  refresh();
}
function showInstallLog(name){
  current = name; currentType = 'setup';
  document.getElementById('logTitle').textContent = '安装依赖：' + name;
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
document.addEventListener('keydown', e => { if (e.key === 'Escape'){ closeLog(); closeAlert(); closeConfig(); } });
applyTheme();
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


def run_webui(backends, config, supervisor: Supervisor, host: str = "127.0.0.1", port: int = 8910, open_browser: bool = True) -> None:
    """启动 Web 管理界面（阻塞，Ctrl+C 退出）"""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.get("log_dir", DEFAULT_LOG_DIR))
    by_name = {b.name: b for b in backends}
    setup_state = {}  # name -> {"lines": [...], "running": bool, "failed": bool}
    setup_lock = threading.Lock()

    def start_setup(name: str) -> None:
        """后台执行 launcher.py setup <name>，日志实时追加到 setup_state；已在运行时直接复用"""
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
                    [sys.executable, script, "setup", name],
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

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 关闭默认访问日志刷屏
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, message, status=500):
            self._json({"ok": False, "message": message}, status)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
                        "pid": info.get("pid"),
                    })
                self._json({"ok": True, "backends": rows})
                return
            if self.path.startswith("/api/config/"):
                name = self.path[len("/api/config/"):]
                backend = by_name.get(name)
                if not backend:
                    self._err(f"未知后端: {name}", 404)
                    return
                cfg = backend_config(backend)
                self._json({"ok": True, "port": cfg["port"], "token": cfg["token"], "host": cfg["host"], "default_port": backend.port})
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
            path = self.path
            try:
                if path == "/api/start-all":
                    targets = [b for b in backends if deps_ready(b)]
                    skipped = [b.name for b in backends if not deps_ready(b)]
                    supervisor.start(targets)
                    self._json({
                        "ok": True,
                        "message": "started",
                        "started": [b.name for b in targets],
                        "skipped": skipped,
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
                    supervisor.start(targets)
                    self._json({
                        "ok": True,
                        "message": "restarted",
                        "started": [b.name for b in targets],
                        "skipped": skipped,
                    })
                    return
                if path == "/api/stop-all":
                    supervisor.stop(backends)
                    self._json({"ok": True, "message": "stopped"})
                    return
                if path == "/api/update":
                    try:
                        res = update_project()
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
                    cfg = save_backend_config(name, port=value, token=token, host=host)
                    self._json({"ok": True, "port": cfg["port"], "token": cfg["token"], "host": cfg["host"]})
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
                    backend = by_name.get(name)
                    if not backend:
                        self._err(f"未知后端: {name}", 404)
                        return
                    if action == "setup":
                        start_setup(name)
                        self._json({"ok": True, "message": "setup started"})
                        return
                    if action == "deps-delete":
                        if supervisor.is_running(name):
                            supervisor.stop([backend])
                            time.sleep(1)
                        remove_backend_deps(backend)
                        self._json({"ok": True, "message": "deps removed"})
                        return
                    if action == "start":
                        if name in supervisor.state.get("stopped", []):
                            supervisor.state["stopped"].remove(name)
                            supervisor._save_state()
                        supervisor.start([backend])
                        self._json({"ok": True})
                        return
                    if action == "stop":
                        supervisor.stop([backend])
                        self._json({"ok": True})
                        return
                self._err("not found", 404)
            except Exception as e:  # noqa: BLE001
                self._err(str(e))

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[launcher] WebUI 已启动: http://{host}:{port}（Ctrl+C 退出）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[launcher] WebUI 已停止")
    finally:
        server.server_close()
