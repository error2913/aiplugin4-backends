#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""media-fetch 端到端自测（纯标准库，用 media-fetch 自己的 venv Python 运行）。

用法（在 main 工作树根目录）::

    C:\\Users\\26335\\Documents\\GitHub\\aiplugin4-backends-shop\\backends\\media-fetch\\.venv\\Scripts\\python.exe \
        scripts\\test-media-fetch.py

脚本会：
1. 起一个假上游（http.server），分别返回二进制 / JSON / 401 非 JSON；
2. subprocess 起 mediafetch.py（独立测试端口，按需设置 AIPLUGIN4_BACKEND_TOKEN）；
3. 逐项断言接口行为；
4. 无论成功失败都杀掉子进程、关闭假上游，不留后台进程。

可用环境变量覆盖：
    MEDIAFETCH_PY    mediafetch.py 路径（默认取同级 shop 工作树）
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 假上游 ------------------------------------------------------------------

BINARY_BODY = b"\xff\xfb\x90\x00\x11\x22"
JSON_BODY = {"code": 0, "data": {"url": "http://example.test/audio.mp3"}}
UNAUTH_TEXT = "invalid api key: sk-test-401"


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server 约定名
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/binary":
            self._send(200, "audio/mpeg", BINARY_BODY)
        elif self.path == "/json":
            self._send(200, "application/json; charset=utf-8", json.dumps(JSON_BODY).encode("utf-8"))
        elif self.path == "/unauth":
            self._send(401, "text/plain; charset=utf-8", UNAUTH_TEXT.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


def _start_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, name="fake-upstream", daemon=True)
    thread.start()
    return server, thread


# ---- HTTP 客户端 -------------------------------------------------------------


def _request(url, method="GET", headers=None, body=None, timeout=10):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _wait_health(base, headers=None, timeout=25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, raw = _request(base + "/health", headers=headers)
            if status == 200:
                return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败属正常
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"media-fetch 在 {timeout}s 内未就绪: {last}")


# ---- media-fetch 子进程 ------------------------------------------------------


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_mediafetch(mediafetch_py, port, token=""):
    env = os.environ.copy()
    env["AIPLUGIN4_BACKEND_HOST"] = "127.0.0.1"
    env["AIPLUGIN4_BACKEND_PORT"] = str(port)
    env["AIPLUGIN4_BACKEND_TOKEN"] = token
    env["PYTHONIOENCODING"] = "utf-8"
    log = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, mediafetch_py],
        cwd=os.path.dirname(os.path.abspath(mediafetch_py)),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def _stop_mediafetch(proc, log, dump=False):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    if dump:
        try:
            log.flush()
            log.seek(0)
            tail = log.read().strip()
            if tail:
                print("---- media-fetch 输出（末尾 2000 字符）----")
                print(tail[-2000:])
                print("-------------------------------------------")
        except Exception:  # noqa: BLE001
            pass
    try:
        log.close()
    except Exception:  # noqa: BLE001
        pass


# ---- 断言 --------------------------------------------------------------------


class _Checker:
    def __init__(self):
        self.passed = 0

    def check(self, ok, message):
        if not ok:
            raise AssertionError(message)
        self.passed += 1
        print(f"PASS  {message}")


# ---- 主流程 ------------------------------------------------------------------


def _resolve_mediafetch_py():
    """按优先级找 mediafetch.py：环境变量 → 仓库缓存目录 → installed 运行副本 → 同级 shop 工作树（开发布局）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    candidates = [
        os.environ.get("MEDIAFETCH_PY", ""),
        os.path.join(repo_root, "backends", "media-fetch", "mediafetch.py"),
        os.path.join(repo_root, "installed", "media-fetch", "mediafetch.py"),
        os.path.join(os.path.dirname(repo_root), "aiplugin4-backends-shop", "backends", "media-fetch", "mediafetch.py"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return candidates[0] or candidates[-1]


def main():
    mediafetch_py = _resolve_mediafetch_py()
    if not os.path.isfile(mediafetch_py):
        print(f"FAIL  找不到 mediafetch.py: {mediafetch_py}")
        return 2

    checker = _Checker()
    upstream = None
    upstream_thread = None
    proc = None
    log = None
    proc2 = None
    log2 = None
    ok = False
    try:
        upstream, upstream_thread = _start_upstream()
        upstream_base = f"http://127.0.0.1:{upstream.server_address[1]}"
        print(f"[e2e] 假上游: {upstream_base}")
        print(f"[e2e] mediafetch.py: {mediafetch_py}")

        # ---------- 阶段 A：不设 token ----------
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        proc, log = _start_mediafetch(mediafetch_py, port, token="")
        print(f"[e2e] media-fetch: {base} (无 token)")

        health = _wait_health(base)
        checker.check(health == {"ok": True, "service": "media-fetch"},
                      "/health 返回 {ok: true, service: \"media-fetch\"}")

        # 1) 上游二进制 → base64 无损
        status, raw = _request(
            base + "/fetch-as-base64", method="POST",
            body={"url": upstream_base + "/binary"},
        )
        checker.check(status == 200, "上游二进制：HTTP 200")
        payload = json.loads(raw.decode("utf-8"))
        checker.check(base64.b64decode(payload.get("base64", "")) == BINARY_BODY,
                      "上游二进制：base64 解码后与原始字节一致")
        checker.check("audio" in str(payload.get("contentType", "")).lower(),
                      "上游二进制：contentType 含 audio")
        checker.check(payload.get("upstreamStatus") == 200,
                      "上游二进制：upstreamStatus = 200")
        checker.check(payload.get("bytes") == len(BINARY_BODY),
                      "上游二进制：bytes 等于原始字节数")

        # 2) 上游 JSON → 原样交回
        status, raw = _request(
            base + "/fetch-as-base64", method="POST",
            body={"url": upstream_base + "/json"},
        )
        checker.check(status == 200, "上游 JSON：HTTP 200")
        payload = json.loads(raw.decode("utf-8"))
        checker.check(payload.get("json") == JSON_BODY,
                      "上游 JSON：json 字段原样返回")
        checker.check(payload.get("upstreamStatus") == 200,
                      "上游 JSON：upstreamStatus = 200")
        checker.check(payload.get("bytes") == len(json.dumps(JSON_BODY).encode("utf-8")),
                      "上游 JSON：bytes 等于上游正文长度")

        # 3) 上游 401 非 JSON → HTTP 200 + upstreamStatus 401 + bodyText
        status, raw = _request(
            base + "/fetch-as-base64", method="POST",
            body={"url": upstream_base + "/unauth"},
        )
        checker.check(status == 200, "上游 401 非 JSON：对插件仍返回 HTTP 200")
        payload = json.loads(raw.decode("utf-8"))
        checker.check(payload.get("upstreamStatus") == 401,
                      "上游 401 非 JSON：upstreamStatus = 401")
        checker.check(UNAUTH_TEXT in str(payload.get("bodyText", "")),
                      "上游 401 非 JSON：bodyText 含上游错误原文")
        checker.check(base64.b64decode(payload.get("base64", "")) == UNAUTH_TEXT.encode("utf-8"),
                      "上游 401 非 JSON：base64 仍为原始错误正文")

        # 4) 缺 url → HTTP 400
        status, raw = _request(base + "/fetch-as-base64", method="POST", body={})
        checker.check(status == 400, "缺 url：HTTP 400")

        _stop_mediafetch(proc, log)
        proc, log = None, None

        # ---------- 阶段 B：设置 token ----------
        port2 = _free_port()
        base2 = f"http://127.0.0.1:{port2}"
        proc2, log2 = _start_mediafetch(mediafetch_py, port2, token="test-token")
        print(f"[e2e] media-fetch: {base2} (token=test-token)")

        _wait_health(base2, headers={"Authorization": "Bearer test-token"})

        status, _ = _request(base2 + "/health")
        checker.check(status == 401, "设置 token：无鉴权头请求 401")

        status, _ = _request(
            base2 + "/health", headers={"Authorization": "Bearer test-token"}
        )
        checker.check(status == 200, "设置 token：Authorization: Bearer 通过")

        status, _ = _request(base2 + "/health", headers={"X-Token": "test-token"})
        checker.check(status == 200, "设置 token：X-Token 通过")

        # 带 token 走一次真实转发，确认鉴权不挡业务
        status, raw = _request(
            base2 + "/fetch-as-base64", method="POST",
            headers={"Authorization": "Bearer test-token"},
            body={"url": upstream_base + "/binary"},
        )
        payload = json.loads(raw.decode("utf-8")) if status == 200 else {}
        checker.check(status == 200 and payload.get("upstreamStatus") == 200,
                      "设置 token：带 Bearer 转发上游成功")

        print(f"\n[e2e] 全部通过（{checker.passed} 项断言）")
        ok = True
        return 0
    except AssertionError as exc:
        print(f"\nFAIL  {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR  {type(exc).__name__}: {exc}")
        return 2
    finally:
        _stop_mediafetch(proc, log, dump=not ok)
        _stop_mediafetch(proc2, log2, dump=not ok)
        if upstream is not None:
            upstream.shutdown()
            upstream.server_close()
        if upstream_thread is not None:
            upstream_thread.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
