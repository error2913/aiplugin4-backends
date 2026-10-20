"""MCP（Streamable HTTP）：共 6 个工具 —— 搜索 / 收进图库 / 移出图库 / 重命名 / 打标签 / 标签清单。

不做 ``image_delete``：删除属于人的决定（AI 误删代价不对称），留在 WebUI。

图片引用统一走 CIR（docs/17 §2）：``[img:<ref>]`` / ``h<sha256>`` / 裸 sha256 /
``/i/<sha256>`` / 插件 ref。``image_keep`` / ``image_unkeep`` / ``image_tag`` /
``image_rename`` 都接受这些形态。

来源/群/工具/服务器/时间是 **元数据**（写 ref 列，搜索用 tool/server/platform/
group_id 过滤），永不写成标签；标签只放类型/内容（自动类型标签为英文）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from flask import jsonify, request

from store import clean_tags

log = logging.getLogger("image-host.mcp")

# 单次批量上限（AI 误操作代价控制）；add/remove 每组标签上限
MAX_IDS = 50
MAX_TAGS_PER_GROUP = 10
MAX_NAME_LEN = 80
SCOPES = {"gallery": "manual", "plugin": "plugin", "all": ""}
SOURCE_GALLERY = "长期图库（人工上传）"
SOURCE_PLUGIN = "插件抓取（历史数据）"
TOOL_NAMES = ("image_search", "image_keep", "image_unkeep", "image_rename",
              "image_tag", "image_tags")
CIR_HINT = "支持 [img:<ref>] / h<sha> / 裸 sha256 / /i/<sha> / 插件 ref"

TOOLS = [
    {
        "name": "image_search",
        "description": ("搜索私人图库（按名称/标签/来源元数据）。"
                        "scope=gallery（默认）只搜长期图库：人工上传或确认收藏的图片，来源可信、"
                        "不会被自动清理；scope=plugin 搜插件自动抓取的图片（来源混杂，使用/发送/收藏前"
                        "必须核对来源、名称与标签）；scope=all 两者都搜。"
                        "每行带 [图库·可信]/[抓取·待验证]、来源、工具/服务器与类型/内容标签；"
                        "来源/工具/服务器是元数据（用 scope/platform/group_id/tool/server 过滤），"
                        "tags 表示图片类型/内容（自动类型标签为英文 ai-generated/meme/mcp-image/tool-image）。"
                        "返回的 id 可直接用 [img:ID]/[img:h<ID>] 在回复里发送。limit≤50。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "名称/标签/群号/用户号/消息号模糊匹配"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "类型/内容标签，多标签 AND"},
                "tool": {"type": "string", "description": "产出工具名（精确）"},
                "server": {"type": "string", "description": "MCP 服务器名（精确）"},
                "platform": {"type": "string", "description": "来源平台（精确，如 QQ）"},
                "group_id": {"type": "string", "description": "来源群号（精确）"},
                "since": {"type": "string",
                          "description": "ISO 8601 时间，只返回此时间之后入库的图片"},
                "scope": {"type": "string", "enum": ["gallery", "plugin", "all"],
                          "default": "gallery",
                          "description": "gallery=长期图库（默认）；plugin=插件自动抓取；all=全部"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_keep",
        "description": ("把图片收进长期图库（origin → manual），此后不会被自动清理。"
                        "只对插件自动抓取的图有意义；请先核对来源与用途，确认适宜后再收藏。"
                        f"单次最多 {MAX_IDS} 张。参数 id 或 ids，{CIR_HINT}。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "单张图片引用（与 ids 二选一）"},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": f"图片引用列表（与 id 二选一，最多 {MAX_IDS}）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_unkeep",
        "description": ("把图片移出长期图库（origin → plugin），之后重新进入自动淘汰候选。"
                        f"可逆操作；单次最多 {MAX_IDS} 张。参数 id 或 ids，{CIR_HINT}。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "单张图片引用（与 ids 二选一）"},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": f"图片引用列表（与 id 二选一，最多 {MAX_IDS}）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_rename",
        "description": ("给单张图片重命名（不支持批量同名）。回收站中的图片不能重命名。"
                        f"参数 id（{CIR_HINT}）与 name（1–{MAX_NAME_LEN} 字符）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "图片引用"},
                "name": {"type": "string",
                         "description": f"新名称（1–{MAX_NAME_LEN} 字符）"},
            },
            "required": ["id", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "image_tag",
        "description": ("给图片增删内容标签（语言不限），支持批量 ids（≤"
                        f"{MAX_IDS}）与 add[]/remove[]（每组 ≤{MAX_TAGS_PER_GROUP} 个）。"
                        "自动类型标签（ai-generated/meme/mcp-image/tool-image）由系统维护，不要重复添加；"
                        "不要写来源/群/工具/服务器/时间等元数据（这些用搜索过滤）。"
                        "幂等；返回真正变化的图片数与未找到项。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "单张图片引用（与 ids 二选一）"},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": f"图片引用列表（与 id 二选一，最多 {MAX_IDS}）"},
                "add": {"type": "array", "items": {"type": "string"},
                        "description": f"要添加的内容标签（≤{MAX_TAGS_PER_GROUP} 个）"},
                "remove": {"type": "array", "items": {"type": "string"},
                           "description": f"要移除的内容标签（≤{MAX_TAGS_PER_GROUP} 个）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "image_tags",
        "description": ("列出长期图库的标签清单（按使用次数降序，只统计类型/内容标签，"
                        "不含回收站与插件抓取图；来源/群/工具不是标签）。支持关键字过滤与分页。"),
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


class ToolArgError(ValueError):
    """工具参数错误：tools/call 返回 isError 文本。"""


class UnknownTool(ValueError):
    """未知工具：tools/call 返回 JSON-RPC -32602。"""


def _fmt_time(ms) -> str:
    if not ms:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (ValueError, OSError):
        return ""


def _text(value: str):
    return {"content": [{"type": "text", "text": value}]}


def _result(value: str, *, changed_ids=None, skipped_ids=None, failed=None):
    """人类可读文本 + 机器可读 structuredContent（changed_ids / skipped_ids / failed）。"""
    out = _text(value)
    data = {}
    if changed_ids is not None:
        data["changed_ids"] = list(changed_ids)
    if skipped_ids is not None:
        data["skipped_ids"] = list(skipped_ids)
    if failed is not None:
        data["failed"] = list(failed)
    if data:
        out["structuredContent"] = data
    return out


def _err(value: str):
    return {"content": [{"type": "text", "text": value}], "isError": True}


def _source_text(src: dict, origin: str) -> str:
    """来源字段永远有值：有 ref 显示原来源；无 ref 按 origin 兜底；绝不输出“未知”/“-”。

    WebUI 上传写入的合成来源 ``platform=manual`` 不是真来源，按长期图库口径显示。
    """
    src = src or {}
    platform = str(src.get("platform") or "").strip()
    group_id = str(src.get("group_id") or "").strip()
    user_id = str(src.get("user_id") or "").strip()
    session_id = str(src.get("session_id") or "").strip()
    at = _fmt_time(src.get("at"))
    if platform.lower() in ("manual", "webui"):
        platform = ""
    if platform or group_id or user_id:
        parts = []
        if platform:
            parts.append(platform)
        if group_id:
            parts.append(f"群{group_id}")
        elif user_id:
            parts.append(f"私聊{user_id}")
        if group_id and user_id:
            parts.append(f"用户{user_id}")
        if at:
            parts.append(at)
        if parts:
            return " ".join(parts)
    if session_id:
        return f"会话 {session_id}"
    if origin == "manual":
        return SOURCE_GALLERY
    if at:
        return f"{SOURCE_PLUGIN} {at}"
    return SOURCE_PLUGIN


def _as_list(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    raise ToolArgError("参数必须是字符串或字符串数组")


def _int_arg(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ToolArgError(f"{key} 必须是整数")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ToolArgError(f"{key} 必须是整数")
    return max(lo, min(hi, value))


def _since_ms(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ToolArgError("since 需要 ISO 8601 时间字符串")
    if isinstance(raw, (int, float)):
        value = float(raw)
        return int(value if value > 1e12 else value * 1000)
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        raise ToolArgError("since 需要 ISO 8601 时间，如 2025-01-01 或 2025-01-01T12:00:00Z")


def _refs_of(args: dict) -> list:
    raws: list = []
    one = args.get("id")
    if one is not None and one != "":
        if not isinstance(one, str):
            raise ToolArgError("id 必须是字符串")
        raws.append(one)
    ids = args.get("ids")
    if ids is not None:
        if isinstance(ids, str):
            if ids.strip():
                raws.append(ids)
        elif isinstance(ids, (list, tuple)):
            raws.extend(ids)
        else:
            raise ToolArgError("ids 必须是数组")
    if not raws:
        raise ToolArgError(f"需要 id 或 ids（{CIR_HINT}）")
    return raws


def _targets(store, args: dict):
    """CIR/ref → (blob ids, failed)。"""
    refs = store.resolve_refs(_refs_of(args))
    if len(refs) > MAX_IDS:
        raise ToolArgError(f"单次最多 {MAX_IDS} 张（本次 {len(refs)} 张），请分批调用")
    ids, unmatched = store.lookup_ids(refs)
    failed = [{"id": str(ref), "reason": "not_found"} for ref in unmatched]
    return ids, failed


def _failed_text(prefix: str, failed: list) -> str:
    ids = [str(item.get("id")) for item in failed if item.get("id")]
    if not ids:
        return prefix
    return f"{prefix}：{','.join(ids)}"


def _tag_values(raw, label: str) -> list:
    values, ignored = clean_tags(_as_list(raw))
    if ignored:
        shown = "、".join(str(x) for x in ignored[:5])
        raise ToolArgError(
            f"{label}含不合法标签：{shown}。只接受类型/内容标签，不接受 "
            "来源:/群:/工具:/MCP:/类型: 前缀，以及纯群号/工具名/时间/文件名等元数据式标签。")
    if len(values) > MAX_TAGS_PER_GROUP:
        raise ToolArgError(f"{label}最多 {MAX_TAGS_PER_GROUP} 个标签")
    return values


def _validate_args(name: str, args: dict) -> None:
    """inputSchema 运行时校验：未知参数拒绝、required 缺失拒绝。"""
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    allowed = set((schema.get("properties") or {}).keys())
    unknown = [key for key in args if key not in allowed]
    if unknown:
        raise ToolArgError(f"未知参数：{', '.join(str(k) for k in unknown)}")
    for key in schema.get("required") or []:
        if key not in args:
            raise ToolArgError(f"缺少参数：{key}")


# ---------------------------------------------------------------- 工具实现

def _tool_search(store, args: dict):
    scope = str(args.get("scope") or "gallery").strip().lower()
    if scope not in SCOPES:
        return _err("scope 不合法：可选 gallery（默认，长期图库）/ plugin（插件抓取）/ all（全部）。")
    q = str(args.get("query") or "").strip()
    tags = [str(t).strip() for t in _as_list(args.get("tags")) if str(t).strip()]
    tool = str(args.get("tool") or "").strip()
    server = str(args.get("server") or "").strip()
    platform = str(args.get("platform") or "").strip()
    group_id = str(args.get("group_id") or "").strip()
    since = _since_ms(args.get("since"))
    limit = _int_arg(args, "limit", 20, 1, 50)
    offset = _int_arg(args, "offset", 0, 0, 10 ** 9)

    total, items = store.list(
        q=q, tags=tags, origin=SCOPES[scope], platform=platform,
        group_id=group_id, tool=tool, server=server, since=since,
        limit=limit, offset=offset)

    if not items:
        if total:
            return _text(f"共 {total} 条匹配，offset={offset} 已超出返回范围。"
                        f"可减小 offset 或加大 limit（≤50）。")
        if scope == "gallery":
            return _text("长期图库里没有匹配的图片。插件自动抓取的图片需传 scope=plugin 或 scope=all 才会搜。")
        return _text("没有匹配的图片。")

    lines = []
    for it in items:
        origin = str(it.get("origin") or "")
        sources = it.get("sources") or []
        src = sources[0] if sources else {}
        trust = "[图库·可信]" if origin == "manual" else "[抓取·待验证]"
        tool_txt = str((src or {}).get("tool") or "").strip()
        server_txt = str((src or {}).get("server") or "").strip()
        tags_txt = ",".join(it.get("tags") or []) or "-"
        lines.append(
            f"{it['id']} | {trust} | {it.get('name') or '(未命名)'} | "
            f"标签:{tags_txt} | 来源: {_source_text(src, origin)} | "
            f"工具: {tool_txt or '-'} | 服务器: {server_txt or '-'} | "
            f"/i/{it['id']} | [img:{it['id']}]"
        )
    head = f"共 {total} 条（scope={scope}），返回 {len(items)} 条（offset={offset}）："
    return _text(head + "\n" + "\n".join(lines))


def _flip_tool(store, args: dict, action: str):
    ids, failed = _targets(store, args)
    if not ids:
        return _text(_failed_text("未找到图片", failed))
    res = store.batch(action, ids, {})
    failed = failed + list(res.get("failed") or [])
    changed = res.get("changed_ids") or []
    if action == "keep":
        ok_text = f"已把 {len(changed)} 张图收进长期图库（此后不会被自动清理）"
        skip_reason = "已是长期图库或在回收站"
    else:
        ok_text = f"已把 {len(changed)} 张图移出长期图库（重新进入自动淘汰候选）"
        skip_reason = "已不在长期图库或在回收站"
    parts = [ok_text]
    if res.get("skipped"):
        parts.append(f"跳过 {res['skipped']} 张（{skip_reason}）")
    if failed:
        parts.append(_failed_text("未找到", failed))
    return _result("；".join(parts), changed_ids=changed,
                   skipped_ids=res.get("skipped_ids") or [], failed=failed)


def _tool_keep(store, args: dict):
    return _flip_tool(store, args, "keep")


def _tool_unkeep(store, args: dict):
    return _flip_tool(store, args, "unkeep")


def _tool_rename(store, args: dict):
    raw_name = args.get("name")
    if not isinstance(raw_name, str):
        raise ToolArgError("name 必须是字符串")
    name = raw_name.strip()
    if not name:
        raise ToolArgError("name 不能为空")
    if len(name) > MAX_NAME_LEN:
        raise ToolArgError(f"name 太长（最多 {MAX_NAME_LEN} 字符）")
    if any(ord(ch) < 32 for ch in name):
        raise ToolArgError("name 不能包含控制字符")

    ids, failed = _targets(store, args)
    if not ids:
        return _text(_failed_text("未找到图片", failed))
    if len(ids) > 1:
        raise ToolArgError("一次只能重命名 1 张（不支持批量同名）")
    bid = ids[0]
    row = store.lookup(bid)
    if not row:
        return _text(f"未找到图片 {bid}。")
    if row.get("deleted_at") is not None:
        return _text(f"图片 {bid} 在回收站中，不能重命名。")
    if not store.patch(bid, name=name):
        return _text(f"未找到图片 {bid}。")
    return _result(f"已重命名 {bid} → {name}", changed_ids=[bid],
                   skipped_ids=[], failed=[])


def _tool_tag(store, args: dict):
    add = _tag_values(args.get("add"), "add ")
    rm = _tag_values(args.get("remove"), "remove ")
    if not add and not rm:
        raise ToolArgError("没有要增删的标签：请提供 add 或 remove。")
    ids, failed = _targets(store, args)
    if not ids:
        return _text(_failed_text("未找到图片", failed))
    res = store.batch("tag", ids, {"add_tags": add, "remove_tags": rm})
    failed = failed + list(res.get("failed") or [])
    changed = res.get("changed_ids") or []
    parts = [f"已更新 {len(changed)} 张图的标签"]
    ops = []
    if add:
        ops.append("添加 " + ",".join(add))
    if rm:
        ops.append("移除 " + ",".join(rm))
    if ops:
        parts.append("；".join(ops))
    if res.get("skipped"):
        parts.append(f"跳过 {res['skipped']} 张（回收站或标签已符合）")
    if failed:
        parts.append(_failed_text("未找到", failed))
    return _result("；".join(parts), changed_ids=changed,
                   skipped_ids=res.get("skipped_ids") or [], failed=failed)


def _tool_tags(store, args: dict):
    q = str(args.get("q") or "").strip()
    limit = _int_arg(args, "limit", 50, 1, 200)
    offset = _int_arg(args, "offset", 0, 0, 10 ** 9)
    total, tags = store.tags(q=q, limit=limit, offset=offset, origin="manual")
    if not tags:
        return _text("长期图库没有匹配的标签。" if q else "长期图库还没有任何标签。")
    lines = [f"{t['tag']} ×{t['count']}" for t in tags]
    head = f"长期图库共 {total} 个标签，返回 {len(tags)} 个（offset={offset}）"
    if offset + len(tags) < total:
        head += f"，下一页 offset={offset + len(tags)}"
    return _text(head + "：\n" + "\n".join(lines))


def _call(store, params: dict):
    try:
        if not isinstance(params, dict):
            raise ToolArgError("params 必须是对象")
        name = str(params.get("name") or "")
        if name not in TOOL_NAMES:
            raise UnknownTool(name)
        raw_args = params.get("arguments")
        if raw_args is None:
            raw_args = {}
        if not isinstance(raw_args, dict):
            raise ToolArgError("arguments 必须是对象")
        _validate_args(name, raw_args)
        if name == "image_search":
            return _tool_search(store, raw_args)
        if name == "image_keep":
            return _tool_keep(store, raw_args)
        if name == "image_unkeep":
            return _tool_unkeep(store, raw_args)
        if name == "image_rename":
            return _tool_rename(store, raw_args)
        if name == "image_tag":
            return _tool_tag(store, raw_args)
        return _tool_tags(store, raw_args)
    except ToolArgError as exc:
        return _err(str(exc))


def register(app, store, token_ok, version: str) -> None:
    @app.post("/mcp")
    def mcp_endpoint():
        if not token_ok():
            return jsonify({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32001, "message": "unauthorized"}}), 401

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}
        method = str(payload.get("method") or "")
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
                try:
                    result = _call(store, payload.get("params") or {})
                except UnknownTool as exc:
                    return jsonify({"jsonrpc": "2.0", "id": rid,
                                    "error": {"code": -32602,
                                              "message": f"unknown tool: {exc}"}})
            else:
                return jsonify({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32601,
                                          "message": f"unknown method: {method}"}})
        except Exception as exc:  # 工具异常按 JSON-RPC error 返回，不 500
            log.exception("MCP 调用失败")
            return jsonify({"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32603, "message": str(exc)}})

        return jsonify({"jsonrpc": "2.0", "id": rid, "result": result})
