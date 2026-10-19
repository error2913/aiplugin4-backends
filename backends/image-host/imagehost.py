"""image-host：私人图床 + 图库。

一个服务三个面（共享同一份 SQLite）：

  REST   /api/*  /i/*  /t/*   ← 插件（抓取、取图、取 b64、释放引用、对账、批量）
  MCP    /mcp                 ← AI（搜索 / 重命名 / 打标签）
  WebUI  /                    ← 人（鉴权：画廊 / 来源 / 删除 / 重命名 / 标签 / 上传 / 批量）

依赖 Flask + requests + Pillow（Pillow 缺失时只关闭缩略图）。
设计与规则见 docs/16-图片生命周期与图床方案.md。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import threading
import uuid

from flask import Flask, abort, jsonify, request, send_file

import fetch as fetchmod
import mcp as mcpmod
from store import ACTIONS, DiskFull, Store, now_ms

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read_version() -> str:
    """版本号以 backend.json 为唯一来源，避免运行期（health/MCP）与注册表不一致。"""
    try:
        with open(os.path.join(ROOT, "backend.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version") or "1.0.0")
    except Exception:
        return "1.0.0"


VERSION = _read_version()

HOST = os.environ.get("AIPLUGIN4_BACKEND_HOST") or "127.0.0.1"
PORT = int(os.environ.get("AIPLUGIN4_BACKEND_PORT") or "46680")
TOKEN = os.environ.get("AIPLUGIN4_BACKEND_TOKEN") or ""
PASSWORD = os.environ.get("AIPLUGIN4_IMAGE_HOST_PASSWORD") or ""
DATA_DIR = os.environ.get("AIPLUGIN4_IMAGE_HOST_DIR") or os.path.join(ROOT, "data")
PUBLIC_BASE_URL = (os.environ.get("AIPLUGIN4_IMAGE_HOST_PUBLIC_BASE_URL") or "").rstrip("/")
ALLOW_HOSTS = [h for h in (os.environ.get("AIPLUGIN4_IMAGE_HOST_ALLOW_HOSTS") or "").split(",")
               if h.strip()]
CAP_MB = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_CAP_MB") or "2048")
MAX_FILE_MB = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_MAX_FILE_MB") or "8")
THUMB_MAX = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_THUMB_MAX") or "512")
FETCH_CONCURRENCY = max(1, int(os.environ.get("AIPLUGIN4_IMAGE_HOST_FETCH_CONCURRENCY") or "4"))
TRASH_DAYS = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_TRASH_DAYS") or "7")
CHANGE_KEEP_DAYS = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_CHANGE_KEEP_DAYS") or "30")
DEV_ALLOW_PRIVATE = (os.environ.get("AIPLUGIN4_IMAGE_HOST_DEV_ALLOW_PRIVATE") or "") == "1"

MAX_BATCH_IDS = 500
MAX_FILTER_MATCH = 2000


def _parse_timeout(raw: str):
    try:
        parts = [float(x) for x in raw.split(",")]
        if len(parts) == 2:
            return (parts[0], parts[1])
    except ValueError:
        pass
    return (5, 20)


FETCH_TIMEOUT = _parse_timeout(os.environ.get("AIPLUGIN4_IMAGE_HOST_FETCH_TIMEOUT") or "5,20")

CAP_BYTES = CAP_MB * 1024 * 1024
MAX_BYTES = MAX_FILE_MB * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("image-host")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES * 2
store = Store(DATA_DIR, cap_bytes=CAP_BYTES, thumb_max=THUMB_MAX,
              trash_days=TRASH_DAYS, change_keep_days=CHANGE_KEEP_DAYS)
fetch_slots = threading.Semaphore(FETCH_CONCURRENCY)

COOKIE_NAME = "ih"
COOKIE_VALUE = (hmac.new(PASSWORD.encode("utf-8"), b"image-host", hashlib.sha256).hexdigest()
                if PASSWORD else "")


# ---------------------------------------------------------------- 基础设施

class ApiError(Exception):
    def __init__(self, error: str, status: int = 400, **extra):
        super().__init__(error)
        self.error = error
        self.status = status
        self.extra = extra


@app.errorhandler(ApiError)
def _handle_api_error(exc: ApiError):
    body = {"error": exc.error}
    body.update(exc.extra)
    return jsonify(body), exc.status


@app.teardown_request
def _release_store_conn(_exc):
    """把本请求占用的 SQLite 连接还回池（见 store.release_conn）。"""
    store.release_conn()


def _public(path: str) -> str:
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def _token_ok() -> bool:
    if not TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    return (hmac.compare_digest(bearer, TOKEN)
            or hmac.compare_digest(request.headers.get("X-Token", ""), TOKEN))


def _cookie_ok() -> bool:
    if not COOKIE_VALUE:
        return False
    return hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), COOKIE_VALUE)


def _authed() -> bool:
    return _token_ok() or _cookie_ok()


def _require_auth() -> None:
    if not _authed():
        raise ApiError("unauthorized", 401)


def _valid_id(bid: str) -> bool:
    return (len(bid) == 64 and all(c in "0123456789abcdef" for c in bid))


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _item(bid: str, fmt: str, size: int, dedup: bool, ref: str):
    row = store.get(bid) or {}
    return {
        "id": bid,
        "url": _public(f"/i/{bid}"),
        "b64_url": _public(f"/i/{bid}/b64"),
        "thumb_url": _public(f"/t/{bid}") if row.get("thumb_path") else None,
        "format": fmt,
        "size": size,
        "reachable": bool(PUBLIC_BASE_URL),
        "dedup": dedup,
        "ref": ref,
    }


def _write_stream(chunks, limit: int):
    fd, tmp = tempfile.mkstemp(dir=store.tmp, suffix=".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise ApiError("too_large", 413, size=total)
                digest.update(chunk)
                out.write(chunk)
        with open(tmp, "rb") as fh:
            fmt, mime = fetchmod.detect_format(fh.read(16))
        if not fmt:
            raise ApiError("unsupported_format", 400)
        return tmp, digest.hexdigest(), total, fmt, mime
    except ApiError:
        _unlink(tmp)
        raise
    except Exception as exc:
        _unlink(tmp)
        raise ApiError("internal", 500, detail=str(exc))


def _store_put(tmp, sha, size, fmt, mime, ref, source, origin, name=None,
               file_ready=False):
    try:
        bid, dedup = store.put(tmp, sha, size, fmt, mime, ref=ref, source=source,
                               origin=origin, name=name, file_ready=file_ready)
    except DiskFull as exc:
        if tmp:
            _unlink(tmp)
        raise ApiError("disk_full", 507,
                       used_mb=round(exc.used / 1048576, 1),
                       manual_mb=round(exc.manual / 1048576, 1))
    log.info("落库 id=%s size=%d fmt=%s ref=%s origin=%s dedup=%s",
             bid, size, fmt, ref, origin, dedup)
    return bid, dedup


# ---------------------------------------------------------------- 公开读

@app.get("/api/health")
def api_health():
    stats = store.stats()
    return jsonify({
        "ok": True,
        "image_host": True,
        "public_base_url_set": bool(PUBLIC_BASE_URL),
        "cap_mb": CAP_MB,
        "used_mb": round(stats["used_bytes"] / 1048576, 2),
        "manual_mb": round(stats["manual_bytes"] / 1048576, 2),
        "count": stats["count"],
        "trash": stats["trash"],
        "changes_head": stats["changes_head"],
        "webui": bool(PASSWORD),
        "version": VERSION,
    })


@app.get("/i/<bid>")
def serve_blob(bid: str):
    if not _valid_id(bid):
        abort(404)
    row = store.lookup(bid)          # 缓存命中时零查询
    if not row:
        abort(404)
    in_trash = row["deleted_at"] is not None
    # 回收站里的图不再对外公开（模型/协议端拿不到），但已登录的 WebUI 可以预览
    if in_trash and not _authed():
        abort(404)
    store.touch(bid, row)            # 1 小时节流的 LRU 信号
    path = store.blob_path(bid, row["format"])
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype=row["mime"], conditional=True, max_age=31536000)
    resp.headers["Cache-Control"] = ("private, max-age=60" if in_trash
                                     else "public, max-age=31536000, immutable")
    return resp


@app.get("/i/<bid>/b64")
def serve_b64(bid: str):
    if not _valid_id(bid):
        abort(404)
    row = store.lookup(bid)
    if not row:
        abort(404)
    if row["deleted_at"] is not None and not _authed():
        abort(404)
    store.touch(bid, row)
    path = store.blob_path(bid, row["format"])
    if not os.path.exists(path):
        abort(404)
    with open(path, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode("ascii")
    resp = jsonify({"base64": payload, "format": row["format"], "id": bid})
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/t/<bid>")
def serve_thumb(bid: str):
    if not _valid_id(bid):
        abort(404)
    row = store.lookup(bid)
    if not row or not row.get("thumb_path") or not os.path.exists(row["thumb_path"]):
        abort(404)
    # 回收站里的缩略图只给已登录的 WebUI 看（模型不需要缩略图）
    if row["deleted_at"] is not None and not _authed():
        abort(404)
    resp = send_file(row["thumb_path"], mimetype="image/webp",
                     conditional=True, max_age=31536000)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ---------------------------------------------------------------- 上传

@app.post("/api/upload")
def api_upload():
    _require_auth()

    source = {}
    raw_source = request.form.get("source") if request.form else None
    if raw_source:
        try:
            source = json.loads(raw_source)
        except Exception:
            source = {}

    # ① multipart（WebUI 手动上传）→ origin 强制 manual
    if request.files.get("file"):
        fh = request.files["file"]
        tmp, sha, size, fmt, mime = _write_stream(
            iter(lambda: fh.stream.read(64 * 1024), b""), MAX_BYTES)
        ref = request.form.get("ref") or uuid.uuid4().hex
        if not source:
            source = {"platform": "manual", "at": now_ms()}
        name = (request.form.get("name") or "").strip()
        if not name and fh.filename:
            name = os.path.splitext(os.path.basename(fh.filename))[0][:80]
        bid, dedup = _store_put(tmp, sha, size, fmt, mime, ref, source, "manual",
                                name=name or None)
        return jsonify(_item(bid, fmt, size, dedup, ref))

    data = request.get_json(silent=True) or {}
    ref = data.get("ref") or uuid.uuid4().hex
    source = data.get("source") or source
    # 只有「WebUI 会话 + via=webui」才允许把 JSON 路径判为手动上传
    manual = data.get("via") == "webui" and _cookie_ok()
    origin = "manual" if manual else "plugin"
    name = (data.get("name") or "").strip() or None

    # ② 按 URL 抓取
    if data.get("url"):
        url = str(data["url"])
        with fetch_slots:
            try:
                tmp, sha, size, fmt, mime = fetchmod.safe_fetch(
                    url, max_bytes=MAX_BYTES, timeout=FETCH_TIMEOUT,
                    tmp_dir=store.tmp, allow_hosts=ALLOW_HOSTS,
                    allow_private=DEV_ALLOW_PRIVATE)
            except fetchmod.FetchError as exc:
                if exc.kind == "bad_url":
                    raise ApiError("bad_url", 400, reason=exc.reason, detail=exc.detail)
                if exc.kind == "unsupported_format":
                    raise ApiError("unsupported_format", 400)
                if exc.kind == "too_large":
                    raise ApiError("too_large", 413, size=exc.size)
                raise ApiError("fetch_failed", 502,
                               upstream_status=exc.status, detail=exc.detail)
        bid, dedup = _store_put(tmp, sha, size, fmt, mime, ref, source, origin, name=name)
        return jsonify(_item(bid, fmt, size, dedup, ref))

    # ③ 已有字节（快路径：先算哈希，命中则不落任何文件；新图直接写终态）
    if data.get("base64"):
        try:
            blob = base64.b64decode(str(data["base64"]), validate=False)
        except Exception as exc:
            raise ApiError("bad_url", 400, reason="bad_base64", detail=str(exc))
        size = len(blob)
        if size > MAX_BYTES:
            raise ApiError("too_large", 413, size=size)
        fmt, mime = fetchmod.detect_format(blob[:16])
        if not fmt:
            raise ApiError("unsupported_format", 400)
        sha = hashlib.sha256(blob).hexdigest()
        if store.lookup(sha) is not None:
            bid, dedup = _store_put(None, sha, size, fmt, mime, ref, source, origin,
                                    name=name)
        else:
            store.write_blob(sha, fmt, blob)
            bid, dedup = _store_put(None, sha, size, fmt, mime, ref, source, origin,
                                    name=name, file_ready=True)
        return jsonify(_item(bid, fmt, size, dedup, ref))

    raise ApiError("bad_request", 400, reason="need_url_or_base64_or_file")


# ---------------------------------------------------------------- 引用管理

@app.post("/api/release")
def api_release():
    _require_auth()
    data = request.get_json(silent=True) or {}
    refs = [str(r) for r in (data.get("refs") or []) if r]
    released, freed = store.release(refs)
    return jsonify({"released": released, "blobs_freed": freed})


@app.post("/api/exists")
def api_exists():
    _require_auth()
    data = request.get_json(silent=True) or {}
    refs = [str(r) for r in (data.get("refs") or []) if r]
    return jsonify({"missing": store.missing(refs)})


@app.get("/api/changes")
def api_changes():
    """变更日志增量拉取（插件同步用）。"""
    _require_auth()
    try:
        since = max(0, int(request.args.get("since", 0)))
        limit = max(1, min(1000, int(request.args.get("limit", 500))))
    except ValueError:
        raise ApiError("bad_request", 400, reason="bad_params")
    return jsonify(store.changes(since, limit))


# ---------------------------------------------------------------- 批量

@app.post("/api/batch")
def api_batch():
    _require_auth()
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ACTIONS:
        raise ApiError("bad_request", 400, reason="bad_action")

    try:
        ids, flt = store.resolve(body)
    except ValueError as exc:
        raise ApiError("bad_request", 400, reason=str(exc))

    if flt is not None:
        expected = body.get("expected_count")
        if expected is None:
            raise ApiError("bad_request", 400, reason="expected_count_required")
        if int(expected) != len(ids):
            raise ApiError("count_mismatch", 409,
                           expected=int(expected), actual=len(ids))
        if len(ids) > MAX_FILTER_MATCH:
            raise ApiError("too_many", 413, count=len(ids), max=MAX_FILTER_MATCH)
    elif len(ids) > MAX_BATCH_IDS:
        raise ApiError("too_many", 413, count=len(ids), max=MAX_BATCH_IDS)

    if body.get("dry_run"):
        result = store.preview(action, ids, body)
        result.update({"ok": True, "dry_run": True, "action": action,
                       "cursor": store.change_head()})
        return jsonify(result)

    try:
        result = store.batch(action, ids, body)
    except ValueError as exc:
        raise ApiError("bad_request", 400, reason=str(exc))
    log.info("批量 %s：requested=%d affected=%d failed=%d",
             action, result["requested"], result["affected"], len(result["failed"]))
    return jsonify(result)


# ---------------------------------------------------------------- 管理

@app.get("/api/list")
def api_list():
    _require_auth()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 24))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        raise ApiError("bad_request", 400, reason="bad_pagination")
    trash = request.args.get("trash") in ("1", "true", "yes")
    total, items = store.list(
        q=(request.args.get("q") or "").strip(),
        tag=(request.args.get("tag") or "").strip(),
        origin=(request.args.get("origin") or "").strip(),
        limit=limit, offset=offset, trash=trash)
    for item in items:
        item["url"] = _public(f"/i/{item['id']}")
        item["thumb_url"] = _public(f"/t/{item['id']}") if item.pop("has_thumb") else None
        item["b64_url"] = _public(f"/i/{item['id']}/b64")
    return jsonify({"total": total, "limit": limit, "offset": offset, "items": items})


@app.get("/api/tags")
def api_tags():
    """标签清单（分页）。WebUI 标签视图与 MCP image_tags 共用。"""
    _require_auth()
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        raise ApiError("bad_request", 400, reason="bad_pagination")
    total, tags = store.tags(q=(request.args.get("q") or "").strip(),
                              limit=limit, offset=offset)
    return jsonify({"total": total, "limit": limit, "offset": offset, "tags": tags})


@app.patch("/api/image/<bid>")
def api_patch(bid: str):
    _require_auth()
    if not _valid_id(bid):
        raise ApiError("not_found", 404)
    data = request.get_json(silent=True) or {}
    ok = store.patch(bid, name=data.get("name"), add_tags=data.get("add_tags"),
                     remove_tags=data.get("remove_tags"), origin=data.get("origin"))
    if not ok:
        raise ApiError("not_found", 404)
    return jsonify({"ok": True, "id": bid})


@app.delete("/api/image/<bid>")
def api_delete(bid: str):
    _require_auth()
    if not _valid_id(bid) or not store.delete(bid):
        raise ApiError("not_found", 404)
    log.info("手动删除 id=%s", bid)
    return jsonify({"ok": True, "id": bid})


# ---------------------------------------------------------------- WebUI 会话

@app.get("/api/session")
def api_session():
    return jsonify({"authed": _authed(), "webui": bool(PASSWORD), "version": VERSION})


@app.post("/api/login")
def api_login():
    if not PASSWORD:
        raise ApiError("webui_disabled", 404)
    data = request.get_json(silent=True) or {}
    if not hmac.compare_digest(str(data.get("password") or ""), PASSWORD):
        raise ApiError("unauthorized", 401)
    resp = jsonify({"ok": True})
    resp.set_cookie(COOKIE_NAME, COOKIE_VALUE, httponly=True, samesite="Lax",
                    max_age=7 * 86400)
    return resp


@app.post("/api/logout")
def api_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/")
def webui():
    page = os.path.join(ROOT, "webui.html")
    if not os.path.exists(page):
        return "<h1>image-host</h1><p>webui.html 缺失</p>", 200
    resp = send_file(page, mimetype="text/html")
    # 前端改版后必须让浏览器拿到最新 HTML，不做缓存
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ---------------------------------------------------------------- 入口

def main():
    store.init()
    mcpmod.register(app, store, _token_ok, VERSION)
    if not TOKEN:
        log.warning("未设置 AIPLUGIN4_BACKEND_TOKEN：所有写操作将返回 401")
    if not PASSWORD:
        log.warning("未设置 AIPLUGIN4_IMAGE_HOST_PASSWORD：WebUI 已关闭")
    log.info("image-host %s 监听 http://%s:%d  数据目录=%s  容量=%dMB 回收站=%s 天",
             VERSION, HOST, PORT, DATA_DIR, CAP_MB, TRASH_DAYS or "关闭")
    log.info("PUBLIC_BASE_URL=%s（空 = 模型侧不可达，插件应改用 b64 出口）",
             PUBLIC_BASE_URL or "(未配置)")
    if DEV_ALLOW_PRIVATE:
        log.warning("AIPLUGIN4_IMAGE_HOST_DEV_ALLOW_PRIVATE=1：已放开私网抓取，仅限本地测试！")
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
