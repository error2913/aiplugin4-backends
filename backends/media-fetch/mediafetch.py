"""媒体接口中转：代插件向供应商发起请求，并把响应安全地交回插件。

为什么需要它
------------
海豹（SealDice）的 JS 引擎是 goja，其 fetch 实现（fy0/gojax）的 Response **只有
text()/json()**，没有 arrayBuffer()/bytes()/blob()；而 text() 会把响应体按 UTF-8 解码，
原始二进制字节会被替换成 U+FFFD 而永久损坏。因此「整个响应体就是音频/图片」的接口
（OpenAI 兼容 /audio/speech、百度短文本合成 tsn.baidu.com/text2audio、阿里 CosyVoice 等）
在插件里无法直接读取。

本服务把这一步搬到 Python：Python 能无损拿到 response.content 原始字节，
转成 base64 放进 JSON 返回，插件读 JSON 就行。

设计要点
--------
- **原样转发**：method / headers / body 全部由插件给出并透传，本服务不掺和任何业务语义。
  这样百度（form-urlencoded + access_token 在 body）、OpenAI 兼容（JSON + Bearer 头）、
  各类自定义鉴权头都能用同一套逻辑。
- **两种返回**：供应商返回二进制 → `{"base64": ..., "contentType": ...}`；
  供应商本来就返回 JSON（如含 url 字段）→ 原样把 JSON 交回，让插件按 data_path 取数。
  这样插件无需区分，行为与直连时一致。
- 上游的 HTTP 状态码一并带回（`upstreamStatus`），401/402 这类鉴权/余额错误
  仍能被插件的错误分类逻辑识别。
"""

import base64
import json
import os

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

APP_HOST = os.environ.get("AIPLUGIN4_BACKEND_HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("AIPLUGIN4_BACKEND_TOKEN", "")

# 不设置总超时会让上游卡死时本服务一直挂着；(连接, 读取) 分开给，读取留足慢合成的时间
CONNECT_TIMEOUT = float(os.environ.get("MEDIA_FETCH_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.environ.get("MEDIA_FETCH_READ_TIMEOUT", "300"))

# 逐跳头（hop-by-hop）：由本次连接自己协商，不能原样转发给上游
HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


if AUTH_TOKEN:
    @app.before_request
    def _require_token():
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {AUTH_TOKEN}" or request.headers.get("X-Token") == AUTH_TOKEN:
            return None
        return jsonify({"error": "unauthorized"}), 401


def _clean_headers(raw):
    """过滤掉逐跳头，返回 {str: str}。"""
    out = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or key.lower() in HOP_BY_HOP:
                continue
            out[key] = "" if value is None else str(value)
    return out


def _is_json_response(content_type, body):
    """上游返回的是 JSON 吗：先看 Content-Type，其次看正文首字符（部分服务端不给准确类型）。"""
    if "json" in (content_type or "").lower():
        return True
    head = body.lstrip()[:1]
    return head in (b"{", b"[")


@app.route("/fetch-as-base64", methods=["POST"])
def fetch_as_base64():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url or not isinstance(url, str):
        return jsonify({"error": "url is required"}), 400

    method = str(data.get("method") or "POST").upper()
    headers = _clean_headers(data.get("headers"))
    body = data.get("body")
    if body is not None and not isinstance(body, str):
        # 插件侧应当已按 content_type 编码成字符串；这里兜底，避免把 dict 原样塞给 requests
        body = json.dumps(body, ensure_ascii=False)

    try:
        upstream = requests.request(
            method,
            url,
            headers=headers,
            data=None if body is None else body.encode("utf-8"),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return jsonify({"error": f"请求上游超时（读取上限 {READ_TIMEOUT:.0f}s）"}), 504
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"请求上游失败：{exc}"}), 502

    content_type = upstream.headers.get("Content-Type", "")
    raw = upstream.content
    status = upstream.status_code

    # 上游返回 JSON：原样交回，插件按 [response].data_path 取数（与直连行为一致）
    if _is_json_response(content_type, raw):
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        return jsonify({
            "json": payload,
            "contentType": content_type,
            "upstreamStatus": status,
            "bytes": len(raw),
        })

    # 上游返回二进制：这里是整个服务存在的理由 —— base64 无损带回
    payload = {
        "base64": base64.b64encode(raw).decode("ascii"),
        "contentType": content_type,
        "upstreamStatus": status,
        "bytes": len(raw),
    }
    if status >= 400:
        # 供插件构造 ApiError（错误分类/展示上游原文）；老插件忽略该字段
        payload["bodyText"] = raw.decode("utf-8", errors="replace")[:2000]
    return jsonify(payload)


@app.route("/health", methods=["GET"])
def health():
    """供 launcher / 人工探活，不触碰上游。"""
    return jsonify({"ok": True, "service": "media-fetch"})


if __name__ == "__main__":
    port = int(os.environ.get("AIPLUGIN4_BACKEND_PORT", "46679"))
    app.run(host=APP_HOST, port=port)
