"""MCP（Streamable HTTP）：只暴露 3 个工具 —— 搜索 / 重命名 / 打标签。

不做 ``image_delete``：删除属于人的决定（AI 误删代价不对称），留在 WebUI。
"""

from __future__ import annotations

import logging
import time

from flask import jsonify, request

log = logging.getLogger("image-host.mcp")


def _fmt_time(ms) -> str:
    if not ms:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (ValueError, OSError):
        return "-"


TOOLS = [
    {
        "name": "image_search",
        "description": ("搜索私人图库中的图片（按名称、标签、来源元数据）。"
                        "默认只搜长期图库（手动上传/收进图库）；插件自动抓取的图片要传 scope=plugin/all。"
                        "返回 id / 名称 / 标签 / 来源 / 时间 / URL。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "名称/标签/来源模糊匹配"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "platform": {"type": "string"},
                "group_id": {"type": "string"},
                "since": {"type": "string", "description": "ISO 时间，只返回此后的"},
                "scope": {"type": "string", "enum": ["gallery", "plugin", "all"],
                          "default": "gallery",
                          "description": "gallery=只搜长期图库（默认）；plugin=只搜插件自动抓取；all=全部"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_rename",
        "description": "给图库中的图片重命名",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "图片 id（sha256）"},
                "name": {"type": "string"},
            },
            "required": ["id", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "image_tag",
        "description": "给图片增删标签（支持批量：用 ids 一次改多张）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "单张图片 id（与 ids 二选一）"},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "批量图片 id（与 id 二选一，最多 200）"},
                "add": {"type": "array", "items": {"type": "string"}},
                "remove": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_keep",
        "description": "把图片收进图库（标记为手动收藏，之后不会被自动清理）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "图片 id 列表"},
                "id": {"type": "string", "description": "单张图片 id（与 ids 二选一）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_tags",
        "description": ("列出图库的标签清单（按使用次数降序，支持关键字过滤与分页）。"
                        "标签太多时用 offset 翻页。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "标签名模糊过滤"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
    },
]


def _text(value: str):
    return {"content": [{"type": "text", "text": value}]}


SCOPES = {"gallery": "manual", "plugin": "plugin", "all": ""}


def _tool_search(store, args: dict):
    scope = str(args.get("scope") or "gallery").strip().lower()
    if scope not in SCOPES:
        return _text("scope 不合法：可选 gallery（默认，长期图库）/ plugin（插件抓取）/ all（全部）。")
    origin = SCOPES[scope]
    tags = args.get("tags") or []
    limit = int(args.get("limit") or 20)
    offset = int(args.get("offset") or 0)
    total, items = store.list(
        q=(args.get("query") or "").strip(),
        tag=(tags[0] if tags else ""),
        origin=origin,
        limit=min(50, max(1, limit)),
        offset=max(0, offset),
    )
    platform = (args.get("platform") or "").strip()
    group_id = (args.get("group_id") or "").strip()
    if platform or group_id:
        items = [it for it in items
                 if any((not platform or (s.get("platform") or "") == platform)
                        and (not group_id or (s.get("group_id") or "") == group_id)
                        for s in it["sources"])]

    if not items:
        if scope == "gallery":
            return _text("长期图库里没有匹配的图片。插件自动抓取的图片需传 scope=plugin 或 scope=all 才会搜。")
        return _text("没有匹配的图片。")

    lines = []
    for it in items:
        src = it["sources"][0] if it["sources"] else {}
        extra = f"（另有 {len(it['sources']) - 1} 条来源）" if len(it["sources"]) > 1 else ""
        lines.append(
            f"{it['id']} | {it['name'] or '(未命名)'} | "
            f"标签: {','.join(it['tags']) or '-'} | "
            f"{src.get('platform') or '-'} 群{src.get('group_id') or '-'} "
            f"用户{src.get('user_id') or '-'} {_fmt_time(src.get('at') or it['created_at'])}{extra} | "
            f"/i/{it['id']}"
        )
    return _text(f"共 {total} 条（scope={scope}），返回 {len(items)} 条（offset={offset}）：\n" + "\n".join(lines))


def _tool_rename(store, args: dict):
    bid = str(args.get("id") or "")
    name = str(args.get("name") or "").strip()
    if not bid or not name:
        return _text("参数不合法：需要 id 与 name。")
    if not store.patch(bid, name=name):
        return _text(f"未找到图片 {bid}。")
    return _text(f"已重命名 {bid} → {name}")


def _ids_of(args: dict, cap: int = 200):
    raw = args.get("ids") or []
    if isinstance(raw, str):
        raw = [raw]
    one = args.get("id")
    out = [str(x) for x in (([one] if one else []) + list(raw)) if x]
    return list(dict.fromkeys(out))[:cap]


def _tool_tag(store, args: dict):
    ids = _ids_of(args)
    if not ids:
        return _text("参数不合法：需要 id 或 ids。")
    add = [str(t).strip() for t in (args.get("add") or []) if str(t).strip()]
    remove = [str(t).strip() for t in (args.get("remove") or []) if str(t).strip()]
    if not add and not remove:
        return _text("没有要增删的标签。")
    affected = 0
    if add:
        affected += store.batch("tag", ids, {"add_tags": add})["affected"]
    if remove:
        affected += store.batch("untag", ids, {"remove_tags": remove})["affected"]
    parts = []
    if add:
        parts.append("添加 " + ",".join(add))
    if remove:
        parts.append("移除 " + ",".join(remove))
    return _text(f"已更新 {affected} 张图的标签：{'；'.join(parts)}")


def _tool_tags(store, args: dict):
    q = (args.get("q") or "").strip()
    try:
        limit = min(200, max(1, int(args.get("limit") or 50)))
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return _text("参数不合法：limit/offset 必须是整数。")
    total, tags = store.tags(q=q, limit=limit, offset=offset)
    if not tags:
        return _text("没有匹配的标签。" if q else "图库还没有任何标签。")
    lines = [f"{t['tag']} ×{t['count']}" for t in tags]
    head = f"共 {total} 个标签，返回 {len(tags)} 个（offset={offset}）"
    if offset + len(tags) < total:
        head += f"，下一页 offset={offset + len(tags)}"
    return _text(head + "：\n" + "\n".join(lines))


def _tool_keep(store, args: dict):
    ids = _ids_of(args)
    if not ids:
        return _text("参数不合法：需要 id 或 ids。")
    res = store.batch("keep", ids, {})
    msg = f"已把 {res['affected']} 张图收进图库（不再被自动清理）"
    if res["failed"]:
        msg += f"；{len(res['failed'])} 张未找到"
    return _text(msg)


def _call(store, params: dict):
    name = params.get("name") or ""
    args = params.get("arguments") or {}
    if name == "image_search":
        return _tool_search(store, args)
    if name == "image_rename":
        return _tool_rename(store, args)
    if name == "image_tag":
        return _tool_tag(store, args)
    if name == "image_keep":
        return _tool_keep(store, args)
    if name == "image_tags":
        return _tool_tags(store, args)
    raise ValueError(f"unknown tool: {name}")


def register(app, store, token_ok, version: str) -> None:
    @app.post("/mcp")
    def mcp_endpoint():
        if not token_ok():
            return jsonify({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32001, "message": "unauthorized"}}), 401

        payload = request.get_json(silent=True) or {}
        method = payload.get("method") or ""
        rid = payload.get("id")

        if method.startswith("notifications/"):
            return "", 202

        try:
            if method == "initialize":
                params = payload.get("params") or {}
                result = {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "image-host", "version": version},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = _call(store, payload.get("params") or {})
            else:
                return jsonify({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32601,
                                          "message": f"unknown method: {method}"}})
        except Exception as exc:  # 工具异常按 JSON-RPC error 返回，不 500
            log.exception("MCP 调用失败")
            return jsonify({"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32603, "message": str(exc)}})

        return jsonify({"jsonrpc": "2.0", "id": rid, "result": result})
