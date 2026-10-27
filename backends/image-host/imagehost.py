"""image-host：私人图床 + 图库。

一个服务三个面（共享同一份 SQLite）：

  REST   /api/*  /i/*  /t/*   ← 插件（抓取、取图、取 b64、释放引用、对账、批量）
  MCP    /mcp                 ← AI（搜索 / 收进图库 / 移出图库 / 重命名 / 打标签 / 标签清单）
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

from flask import Flask, Response, abort, jsonify, request, send_file

import fetch as fetchmod
import mcp as mcpmod
import model_variant
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
PASSWORD = os.environ.get("AIPLUGIN4_IMAGE_HOST_PASSWORD") or "123456"
WEBUI_ENABLED = (os.environ.get("AIPLUGIN4_IMAGE_HOST_WEBUI") or "on").strip().lower() \
    not in ("", "0", "off", "false", "no")
DATA_DIR = os.environ.get("AIPLUGIN4_IMAGE_HOST_DIR") or os.path.join(ROOT, "data")
CAP_MB = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_CAP_MB") or "2048")
MAX_FILE_MB = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_MAX_FILE_MB") or "8")
THUMB_MAX = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_THUMB_MAX") or "512")
FETCH_CONCURRENCY = max(1, int(os.environ.get("AIPLUGIN4_IMAGE_HOST_FETCH_CONCURRENCY") or "4"))
TRASH_DAYS = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_TRASH_DAYS") or "7")
CHANGE_KEEP_DAYS = int(os.environ.get("AIPLUGIN4_IMAGE_HOST_CHANGE_KEEP_DAYS") or "30")
DEV_ALLOW_PRIVATE = (os.environ.get("AIPLUGIN4_IMAGE_HOST_DEV_ALLOW_PRIVATE") or "") == "1"

# 模型变体图：默认目标长边（插件 /m?max= 可覆盖，clamp 到 [512,4096]）
MODEL_MAX_SIDE = model_variant.clamp_max(
    os.environ.get("AIPLUGIN4_IMAGE_HOST_MODEL_MAX"), model_variant.DEFAULT_MAX_SIDE)

MAX_BATCH_IDS = 500
MAX_FILTER_MATCH = 2000
MAX_META_IDS = 200


def _parse_timeout(raw: str):
    try:
        parts = [float(x) for x in raw.split(",")]
        if len(parts) == 2:
            return (parts[0], parts[1])
    except ValueError:
        pass
    return (5, 20)


FETCH_TIMEOUT = _parse_timeout(os.environ.get("AIPLUGIN4_IMAGE_HOST_FETCH_TIMEOUT") or "5,20")


def _parse_tags(raw) -> list:
    """解析 upload 的 tags：JSON 数组、JSON 字符串或逗号分隔字符串；其它一律忽略。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def _normalize_source(raw) -> dict:
    """source 必须是对象；工具图片的来源元数据（tool/server/via）只写 ref 列。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    source = {k: raw[k] for k in ("platform", "group_id", "user_id", "message_id",
                                  "session_id", "at", "raw_url",
                                  "tool", "server", "via") if raw.get(k) is not None}
    for key in ("tool", "server", "via"):
        if key in source:
            value = str(source[key]).strip()
            if value:
                source[key] = value
            else:
                source.pop(key)
    return source

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
    model_format = model_variant.canonical_format(row.get("format") or fmt)
    variant = store.variant_lookup(bid, MODEL_MAX_SIDE)
    if variant:
        model_format = variant["fmt"]
    return {
        "id": bid,
        "url": f"/i/{bid}",
        "b64_url": f"/i/{bid}/b64",
        "model_url": f"/m/{bid}",
        "model_format": model_format,
        "thumb_url": f"/t/{bid}" if row.get("thumb_path") else None,
        "format": fmt,
        "size": size,
        "w": row.get("w"),
        "h": row.get("h"),
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


def _probe_size(path):
    """PIL 读尺寸；失败返回 (None, None)，不阻断上传。"""
    if not path:
        return None, None
    try:
        return model_variant.image_size(path)
    except Exception:
        return None, None


def _ensure_variant(bid: str, max_side: int) -> dict:
    """生成/复用变体，返回含 ``bytes`` 的 dict。

    缓存写失败（磁盘满 / 并发删除）不影响本次响应；真正无法解码时抛 400。
    """
    row = store.lookup(bid)
    if not row:
        raise ApiError("not_found", 404)
    cached = store.variant_lookup(bid, max_side)
    if cached and os.path.exists(cached["path"]):
        return dict(cached, cached=True)
    path = store.blob_path(bid, row["format"])
    if not os.path.exists(path):
        raise ApiError("not_found", 404)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        result = model_variant.render(data, max_side, fmt_hint=row["format"])
    except model_variant.VariantError as exc:
        log.warning("模型变体生成失败 id=%s max=%s: %s", bid, max_side, exc)
        raise ApiError("unsupported_for_model", 400, reason="render_failed",
                       detail=str(exc))
    saved = store.save_variant(bid, max_side, result["fmt"], result["mime"],
                               result["w"], result["h"], result["bytes"])
    if saved:
        return dict(saved, bytes=result["bytes"], cached=False)
    return {"blob_id": bid, "max_side": int(max_side), "fmt": result["fmt"],
            "mime": result["mime"], "w": result["w"], "h": result["h"],
            "size": len(result["bytes"]), "path": None,
            "bytes": result["bytes"], "cached": False}


def _ensure_row_size(bid: str, row: dict) -> dict:
    """旧数据 w/h 为 NULL：首次请求时惰性回填（回填失败则维持 NULL）。"""
    if row.get("w") and row.get("h"):
        return row
    w, h = _probe_size(store.blob_path(bid, row["format"]))
    if w and h:
        store.set_size(bid, w, h)
        row = dict(row)
        row["w"], row["h"] = w, h
    return row


def _store_put(tmp, sha, size, fmt, mime, ref, source, origin, name=None,
               file_ready=False, tags=None):
    w = h = None
    # multipart / URL 抓取路径：tmp 即将被 store.put 搬走，先取尺寸
    if tmp:
        w, h = _probe_size(tmp)
    try:
        bid, dedup = store.put(tmp, sha, size, fmt, mime, ref=ref, source=source,
                               origin=origin, name=name, file_ready=file_ready,
                               tags=tags, w=w, h=h)
    except DiskFull as exc:
        if tmp:
            _unlink(tmp)
        raise ApiError("disk_full", 507,
                       used_mb=round(exc.used / 1048576, 1),
                       manual_mb=round(exc.manual / 1048576, 1))
    if w is None or h is None:
        # base64 快路径（file_ready）与旧图去重：从终态文件补尺寸
        w, h = _probe_size(store.blob_path(bid, fmt))
        if w and h:
            store.set_size(bid, w, h)
    log.info("落库 id=%s size=%d fmt=%s w=%s h=%s ref=%s origin=%s tool=%s dedup=%s tags=%s",
             bid, size, fmt, w, h, ref, origin, (source or {}).get("tool"), dedup, tags)
    # 入库预热默认模型变体：超默认目标且可为模型出图（GIF_MODE=first_frame 时
    # gif 也预热首帧；original 旧行为仍跳过 gif）；失败只记日志，不阻断入库
    prefetchable = (model_variant.canonical_format(fmt) != "gif"
                    or model_variant.gif_first_frame_enabled())
    if w and h and prefetchable and max(w, h) > MODEL_MAX_SIDE:
        try:
            _ensure_variant(bid, MODEL_MAX_SIDE)
        except Exception as exc:
            log.warning("入库预生成模型变体失败 id=%s: %s", bid, exc)
    return bid, dedup


# ---------------------------------------------------------------- 公开读

@app.get("/api/health")
def api_health():
    stats = store.stats()
    gif_mode = model_variant.gif_mode()
    capabilities = ["model-variant", "meta", "variant-max"]
    if gif_mode == model_variant.GIF_MODE_FIRST_FRAME:
        capabilities.append("gif-first-frame")
    return jsonify({
        "ok": True,
        "image_host": True,
        "capabilities": capabilities,
        "gif_mode": gif_mode,
        "model_max_side": MODEL_MAX_SIDE,
        "cap_mb": CAP_MB,
        "used_mb": round(stats["used_bytes"] / 1048576, 2),
        "manual_mb": round(stats["manual_bytes"] / 1048576, 2),
        "count": stats["count"],
        "trash": stats["trash"],
        "variants": stats.get("variant_count", 0),
        "variant_mb": round(stats.get("variant_bytes", 0) / 1048576, 2),
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


# ---------------------------------------------------------------- 模型变体图（/m）

def _lookup_public(bid: str):
    """与 /i 一致：不存在 → 404；回收站 → 非登录态 404。"""
    row = store.lookup(bid)
    if not row:
        abort(404)
    if row["deleted_at"] is not None and not _authed():
        abort(404)
    return row


def _model_headers(resp, *, in_trash: bool, variant: bool, w, h):
    resp.headers["X-Model-Variant"] = "1" if variant else "0"
    if w and h:
        resp.headers["X-Image-Size"] = f"{int(w)}x{int(h)}"
    resp.headers["Cache-Control"] = ("private, max-age=60" if in_trash
                                     else "public, max-age=31536000, immutable")
    return resp


def _model_plan(bid: str, row: dict, max_side: int):
    """统一的 /m 判定：原样 / 变体 / 不支持。"""
    row = _ensure_row_size(bid, row)
    plan = model_variant.decide(row["format"], row.get("w"), row.get("h"), max_side)
    if plan["mode"] == model_variant.MODE_UNSUPPORTED:
        raise ApiError("unsupported_for_model", 400,
                       format=model_variant.canonical_format(row["format"]),
                       w=row.get("w"), h=row.get("h"), max=max_side,
                       reason=plan.get("reason"))
    return row, plan


@app.get("/m/<bid>")
def serve_model(bid: str):
    """给模型的图片出口：原图够小 → 原字节；否则生成/命中 PNG/JPEG/WEBP 变体。

    GIF_MODE=first_frame 时超限 gif 返回首帧变体（webp/jpeg），原图不受影响。
    """
    if not _valid_id(bid):
        abort(404)
    row = _lookup_public(bid)
    in_trash = row["deleted_at"] is not None
    max_side = model_variant.clamp_max(request.args.get("max"), MODEL_MAX_SIDE)
    row, plan = _model_plan(bid, row, max_side)
    store.touch(bid, row)            # 1 小时节流的 LRU 信号

    if plan["mode"] == model_variant.MODE_PASSTHROUGH:
        path = store.blob_path(bid, row["format"])
        if not os.path.exists(path):
            abort(404)
        resp = send_file(path, mimetype=row["mime"], conditional=True,
                         max_age=31536000)
        return _model_headers(resp, in_trash=in_trash, variant=False,
                              w=row.get("w"), h=row.get("h"))

    cached = store.variant_lookup(bid, max_side)
    if cached and os.path.exists(cached["path"]):
        resp = send_file(cached["path"], mimetype=cached["mime"],
                         conditional=True, max_age=31536000)
        return _model_headers(resp, in_trash=in_trash, variant=True,
                              w=cached["w"], h=cached["h"])

    variant = _ensure_variant(bid, max_side)
    resp = Response(variant["bytes"], mimetype=variant["mime"])
    return _model_headers(resp, in_trash=in_trash, variant=True,
                          w=variant["w"], h=variant["h"])


@app.get("/m/<bid>/b64")
def serve_model_b64(bid: str):
    """同 /m，但回 base64 JSON：``{base64, format, mime, w, h, id, variant}``。"""
    if not _valid_id(bid):
        abort(404)
    row = _lookup_public(bid)
    max_side = model_variant.clamp_max(request.args.get("max"), MODEL_MAX_SIDE)
    row, plan = _model_plan(bid, row, max_side)
    store.touch(bid, row)

    is_variant = plan["mode"] == model_variant.MODE_VARIANT
    if not is_variant:
        path = store.blob_path(bid, row["format"])
        if not os.path.exists(path):
            abort(404)
        with open(path, "rb") as fh:
            data = fh.read()
        fmt = model_variant.canonical_format(row["format"])
        mime = row["mime"]
        w, h = row.get("w"), row.get("h")
        if not (w and h):
            w, h = _probe_size(path)
    else:
        cached = store.variant_lookup(bid, max_side)
        if cached and os.path.exists(cached["path"]):
            with open(cached["path"], "rb") as fh:
                data = fh.read()
            fmt, mime, w, h = cached["fmt"], cached["mime"], cached["w"], cached["h"]
        else:
            variant = _ensure_variant(bid, max_side)
            data = variant["bytes"]
            fmt, mime, w, h = (variant["fmt"], variant["mime"],
                               variant["w"], variant["h"])

    payload = base64.b64encode(data).decode("ascii")
    resp = jsonify({"base64": payload, "format": fmt, "mime": mime,
                    "w": w, "h": h, "id": bid, "variant": bool(is_variant)})
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/api/meta")
def api_meta():
    """批量查询图片元数据（后端 token 鉴权），供插件选源；单次 ≤200 个 id。"""
    _require_auth()
    raw = (request.args.get("ids") or "").strip()
    ids = list(dict.fromkeys(x.strip().lower() for x in raw.split(",") if x.strip()))
    if not ids:
        raise ApiError("bad_request", 400, reason="need_ids")
    if len(ids) > MAX_META_IDS:
        raise ApiError("too_many", 413, count=len(ids), max=MAX_META_IDS)

    items = []
    for bid in ids:
        row = store.lookup(bid) if _valid_id(bid) else None
        if not row:
            items.append({"id": bid, "found": False, "deleted": False,
                          "format": None, "mime": None, "w": None, "h": None,
                          "size": 0, "variant_max": []})
            continue
        row = _ensure_row_size(bid, row)
        items.append({
            "id": bid,
            "found": True,
            "deleted": row["deleted_at"] is not None,
            "format": row["format"],
            "mime": row["mime"],
            "w": row.get("w"),
            "h": row.get("h"),
            "size": row["size"],
            "variant_max": store.variant_max(bid),
        })
    return jsonify(items)


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
        source = _normalize_source(raw_source)
    elif request.form:
        # multipart 也允许直接带 tool/server/via 字段
        source = _normalize_source({key: request.form.get(key)
                                    for key in ("platform", "group_id", "user_id",
                                                "message_id", "session_id", "at",
                                                "raw_url", "tool", "server", "via")
                                    if request.form.get(key)})
    tags = _parse_tags(request.form.get("tags")) if request.form else []

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
                                name=name or None, tags=tags)
        return jsonify(_item(bid, fmt, size, dedup, ref))

    data = request.get_json(silent=True) or {}
    ref = data.get("ref") or uuid.uuid4().hex
    source = _normalize_source(data.get("source") or source)
    # 兼容顶层 tool/server/via（等价于 source.tool/server/via）
    for key in ("tool", "server", "via"):
        if not source.get(key) and data.get(key):
            value = str(data[key]).strip()
            if value:
                source[key] = value
    tags = _parse_tags(data.get("tags")) or tags
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
                    tmp_dir=store.tmp,
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
        bid, dedup = _store_put(tmp, sha, size, fmt, mime, ref, source, origin,
                                name=name, tags=tags)
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
                                    name=name, tags=tags)
        else:
            store.write_blob(sha, fmt, blob)
            bid, dedup = _store_put(None, sha, size, fmt, mime, ref, source, origin,
                                    name=name, file_ready=True, tags=tags)
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
    tags_raw = (request.args.get("tags") or "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    total, items = store.list(
        q=(request.args.get("q") or "").strip(),
        tag=(request.args.get("tag") or "").strip(),
        tags=tags,
        origin=(request.args.get("origin") or "").strip(),
        platform=(request.args.get("platform") or "").strip(),
        group_id=(request.args.get("group_id") or "").strip(),
        tool=(request.args.get("tool") or "").strip(),
        server=(request.args.get("server") or "").strip(),
        since=(request.args.get("since") or None),
        limit=limit, offset=offset, trash=trash)
    for item in items:
        item["url"] = f"/i/{item['id']}"
        item["thumb_url"] = f"/t/{item['id']}" if item.pop("has_thumb") else None
        item["b64_url"] = f"/i/{item['id']}/b64"
    return jsonify({"total": total, "limit": limit, "offset": offset, "items": items})


@app.get("/api/tags")
def api_tags():
    """标签清单（分页）。WebUI 标签视图用全部来源；MCP image_tags 默认长期图库。"""
    _require_auth()
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        raise ApiError("bad_request", 400, reason="bad_pagination")
    total, tags = store.tags(q=(request.args.get("q") or "").strip(),
                              limit=limit, offset=offset,
                              origin=(request.args.get("origin") or "").strip())
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
    return jsonify({"authed": _authed(), "webui": WEBUI_ENABLED and bool(PASSWORD),
                    "webui_enabled": WEBUI_ENABLED, "version": VERSION})


@app.post("/api/login")
def api_login():
    if not (WEBUI_ENABLED and PASSWORD):
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
    if not (WEBUI_ENABLED and PASSWORD):
        # 开关关掉 = WebUI 关闭：直接说明白，别让人对着一个永远登不进的登录页
        return (
            "<!doctype html><meta charset='utf-8'><title>image-host</title>"
            "<body style='margin:0;min-height:100vh;display:grid;place-items:center;"
            "font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#222;background:#f6f7f9'>"
            "<div style='max-width:460px;padding:28px 32px;background:#fff;border:1px solid #e3e6ea;"
            "border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.06)'>"
            "<h2 style='margin:0 0 10px;font-size:18px'>WebUI 已关闭</h2>"
            "<p style='margin:0 0 10px;line-height:1.6;color:#444'>后端配置里「WebUI 开关」现在是 off。"
            "把它改成 on（并确认设有访问密码）后重启本后端即可打开。</p>"
            "<p style='margin:0 0 10px;line-height:1.6;color:#444'>在 aiplugin4 的后端管理里"
            "选 image-host 点「⚙ 配置」即可修改。</p>"
            "<p style='margin:0;line-height:1.6;color:#666;font-size:13px'>"
            "API 仍可用：<code>/api/*</code> · <code>/i/&lt;id&gt;</code> · <code>/i/&lt;id&gt;/b64</code> · "
            "<code>/t/&lt;id&gt;</code> · <code>/mcp</code></p>"
            "</div></body>"
        ), 200
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
    if not WEBUI_ENABLED:
        log.info("AIPLUGIN4_IMAGE_HOST_WEBUI=off：WebUI 已关闭（API 与图片分发不受影响）")
    log.info("image-host %s 监听 http://%s:%d  数据目录=%s  容量=%dMB 回收站=%s 天",
             VERSION, HOST, PORT, DATA_DIR, CAP_MB, TRASH_DAYS or "关闭")
    if DEV_ALLOW_PRIVATE:
        log.warning("AIPLUGIN4_IMAGE_HOST_DEV_ALLOW_PRIVATE=1：已放开私网抓取，仅限本地测试！")
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
