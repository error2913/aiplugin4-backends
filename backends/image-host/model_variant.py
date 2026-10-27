"""模型变体图：尺寸判定与缩放规则（纯 PIL，不依赖 Flask）。

背景：模型侧拒绝任何单边 > 8192 的图（实测报 ``unsupported image``），
插件因此改为向图床请求 ``/m/<id>?max=<目标长边>``（默认 2048）。
本模块只负责「要不要变体 / 变体长什么样」，不碰 Flask 与 SQLite，
端点（imagehost.py）与入库预热共用这里的规则，单测可直接 import。

约定：
- 原图格式白名单 ``png/jpeg/webp/gif``；
- gif 需要变体时按 ``GIF_MODE`` 处理（视觉模型只看一帧，原图仍用于 QQ 发送）：
  - ``first_frame``（默认）：取首帧（``seek(0)``；调色板/透明转 RGBA/RGB），
    LANCZOS 缩到 ``max_side``，输出 webp（有 alpha）或 jpeg（无 alpha）；
  - ``original``：旧行为，gif 永不缩放，超限返回 unsupported；
- 变体输出为 ``png/jpeg/webp``，有 alpha → webp（失败回退 png），无 alpha → jpeg；
- 缩放统一 ``Image.LANCZOS``，长边压到 ``max_side``。
"""

from __future__ import annotations

import io
import os

DEFAULT_MAX_SIDE = 2048
MIN_MAX_SIDE = 512
MAX_MAX_SIDE = 4096
DEFAULT_QUALITY = 80

# ---- GIF 模型图源策略（D5）---------------------------------------------
# ``first_frame``：需要变体时取首帧导出静态图（默认）；
# ``original``：旧行为，gif 不缩放/不转码，超限返回 unsupported。
GIF_MODE_ENV = "AIPLUGIN4_IMAGE_HOST_GIF_MODE"
GIF_MODE_FIRST_FRAME = "first_frame"
GIF_MODE_ORIGINAL = "original"
GIF_MODES = (GIF_MODE_FIRST_FRAME, GIF_MODE_ORIGINAL)
DEFAULT_GIF_MODE = GIF_MODE_FIRST_FRAME
# 模块级默认值；运行期实际取值见 ``gif_mode()``（每次读 env，便于热切换/测试）
GIF_MODE = DEFAULT_GIF_MODE


def gif_mode() -> str:
    """读取 GIF 模型图源策略：``first_frame``（默认）或 ``original``。

    env ``AIPLUGIN4_IMAGE_HOST_GIF_MODE``；未知值按默认 ``first_frame`` 处理。
    """
    raw = str(os.environ.get(GIF_MODE_ENV) or "").strip().lower().replace("-", "_")
    if raw in ("original", "orig", "legacy"):
        return GIF_MODE_ORIGINAL
    return GIF_MODE_FIRST_FRAME


def gif_first_frame_enabled() -> bool:
    """当前是否按首帧导出 gif 变体。"""
    return gif_mode() == GIF_MODE_FIRST_FRAME


# 可以直接把原字节喂给模型的格式（尺寸不超限时）。gif 在极限内原样放行。
MODEL_FORMATS = frozenset({"png", "jpeg", "webp", "gif"})
# 变体允许落盘的格式
OUTPUT_FORMATS = ("png", "jpeg", "webp")

MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "jpe": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}

# decide() 的三种结果
MODE_PASSTHROUGH = "passthrough"   # 原字节直接可用
MODE_VARIANT = "variant"           # 需要缩放/转码（gif 首帧也走这里）
MODE_UNSUPPORTED = "unsupported"   # 无法为模型服务（GIF_MODE=original 时 gif 超限等）


class VariantError(Exception):
    """无法生成变体（损坏图 / gif 且 GIF_MODE=original / PIL 缺失等）。"""


def canonical_format(fmt) -> str:
    """统一格式名：``jpg`` → ``jpeg``，其余小写；未知返回空串。"""
    value = str(fmt or "").strip().lower()
    if value in ("jpg", "jpe"):
        return "jpeg"
    return value


def mime_of(fmt) -> str:
    return MIME.get(canonical_format(fmt), "application/octet-stream")


def clamp_max(value, default: int = DEFAULT_MAX_SIDE) -> int:
    """``max`` 参数 clamp 到 [512, 4096]；缺省/非法回落到 ``default``。"""
    for candidate in (value, default):
        try:
            number = int(candidate)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return max(MIN_MAX_SIDE, min(MAX_MAX_SIDE, number))
    return DEFAULT_MAX_SIDE


def image_size(source):
    """读取图片尺寸；``source`` 可为路径 / bytes / file-like。

    返回 ``(w, h)``；无法解析（PIL 缺失、损坏、非图片）返回 ``(None, None)``。
    """
    try:
        from PIL import Image
    except Exception:                     # pragma: no cover - 运行环境缺 pillow
        return None, None
    try:
        if isinstance(source, (bytes, bytearray)):
            with Image.open(io.BytesIO(bytes(source))) as im:
                w, h = im.size
                return int(w), int(h)
        if hasattr(source, "read"):
            try:
                source.seek(0)
            except Exception:
                pass
            with Image.open(source) as im:
                w, h = im.size
                return int(w), int(h)
        with Image.open(source) as im:
            w, h = im.size
            return int(w), int(h)
    except Exception:
        return None, None


def has_alpha(im) -> bool:
    """判断已打开的图片是否带透明通道（含调色板透明）。"""
    if im.mode in ("RGBA", "LA", "PA", "RGBa"):
        return True
    if im.mode in ("P", "L") and "transparency" in getattr(im, "info", {}):
        return True
    return False


def target_format(src_fmt, alpha: bool) -> str:
    """变体输出格式：有 alpha → webp；无 alpha → jpeg。

    webp 同时兼顾体积与 alpha；不支持 webp 的环境由 ``render`` 回退 png。
    """
    if alpha:
        return "webp"
    return "jpeg"


def scaled_size(w: int, h: int, max_side: int) -> tuple[int, int]:
    """按长边压到 ``max_side`` 计算目标尺寸（至少 1px，长宽比保持）。"""
    w, h = int(w), int(h)
    limit = max(1, int(max_side))
    longest = max(w, h)
    if longest <= limit:
        return w, h
    factor = limit / float(longest)
    return max(1, round(w * factor)), max(1, round(h * factor))


def decide(fmt, w, h, max_side) -> dict:
    """判定原图能否直接给模型用。

    返回 ``{"mode": passthrough|variant|unsupported, "fmt", "max_side",
    "w", "h", "gif_mode"?, "first_frame"?, "reason"?}``：

    - 单边 ≤ max 且格式在 png/jpeg/webp/gif → passthrough（回原字节）；
    - gif 需要变体：
      - ``GIF_MODE=first_frame``（默认）→ **variant**：取首帧导出静态图，
        实际输出格式由 ``render`` 决定（有 alpha → webp，无 alpha → jpeg），
        因此 ``mode=variant`` 时下游不得假设输出 fmt 等于源 fmt（gif）；
      - ``GIF_MODE=original`` → unsupported（旧行为：gif 不缩放），
        reason ``gif_not_resizable`` / ``unknown_size``；
    - 其余（超限 / bmp 等非白名单格式 / 尺寸未知）→ variant。
    """
    src = canonical_format(fmt)
    limit = clamp_max(max_side)
    size_known = bool(w) and bool(h)
    result = {"fmt": src, "max_side": limit,
              "w": int(w) if size_known else None,
              "h": int(h) if size_known else None}
    if src == "gif":
        mode = gif_mode()
        result["gif_mode"] = mode
        if size_known and max(int(w), int(h)) <= limit:
            result.update(mode=MODE_PASSTHROUGH)
        elif mode == GIF_MODE_FIRST_FRAME:
            result.update(mode=MODE_VARIANT, first_frame=True)
        else:
            result.update(mode=MODE_UNSUPPORTED,
                          reason="gif_not_resizable" if size_known else "unknown_size")
        return result
    if src in MODEL_FORMATS and size_known and max(int(w), int(h)) <= limit:
        result.update(mode=MODE_PASSTHROUGH)
        return result
    result.update(mode=MODE_VARIANT)
    return result


def render(source, max_side, *, fmt_hint: str | None = None,
           quality: int = DEFAULT_QUALITY) -> dict:
    """缩放/转码一张图，返回 ``{bytes, fmt, mime, w, h, scaled}``。

    - ``source``：路径 / bytes / file-like；
    - 长边 > ``max_side`` 时用 ``Image.LANCZOS`` 缩放；
    - 有 alpha → webp（保存失败回退 png），无 alpha → jpeg；
    - gif：``GIF_MODE=first_frame`` 时取首帧（``seek(0)``，调色板/透明转
      RGBA/RGB）再走同一缩放/编码流程，返回 ``first_frame=True``；
      ``GIF_MODE=original`` 时抛 ``VariantError("gif_not_resizable")``（旧行为）。
    """
    try:
        from PIL import Image
    except Exception as exc:              # pragma: no cover
        raise VariantError("pillow_unavailable") from exc

    im = None
    first_frame = False
    try:
        if isinstance(source, (bytes, bytearray)):
            holder = io.BytesIO(bytes(source))
        elif hasattr(source, "read"):
            try:
                source.seek(0)
            except Exception:
                pass
            holder = source
        else:
            holder = source
        with Image.open(holder) as opened:
            src_fmt = canonical_format(fmt_hint or opened.format or "")
            if src_fmt == "gif":
                if not gif_first_frame_enabled():
                    raise VariantError("gif_not_resizable")
                try:
                    opened.seek(0)        # 显式回到首帧（视觉模型只看一帧）
                except Exception:
                    pass
                # 调色板/透明统一转 RGBA，其余转 RGB，后续缩放/编码不再带 P 模式
                im = opened.convert("RGBA" if has_alpha(opened) else "RGB")
                first_frame = True
            else:
                im = opened.copy()
    except VariantError:
        raise
    except Exception as exc:
        raise VariantError(f"open_failed: {exc}") from exc

    try:
        alpha = has_alpha(im)
        out_fmt = target_format(src_fmt, alpha)
        limit = max(1, int(max_side))
        longest = max(im.size)
        scaled = longest > limit
        if scaled:
            im.thumbnail((limit, limit), Image.LANCZOS)
        w, h = im.size

        if out_fmt == "jpeg":
            if im.mode != "RGB":
                im = im.convert("RGB")
        elif out_fmt == "webp":
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if alpha else "RGB")
        elif out_fmt == "png":
            if im.mode not in ("RGB", "RGBA", "P", "L", "LA"):
                im = im.convert("RGBA" if alpha else "RGB")

        payload = _encode(im, out_fmt, quality, alpha)
        result = {"bytes": payload, "fmt": out_fmt, "mime": mime_of(out_fmt),
                  "w": int(w), "h": int(h), "scaled": bool(scaled)}
        if first_frame:
            result["first_frame"] = True
            result["gif_mode"] = GIF_MODE_FIRST_FRAME
        return result
    finally:
        try:
            if im is not None:
                im.close()
        except Exception:
            pass


def _encode(im, out_fmt: str, quality: int, alpha: bool) -> bytes:
    """编码为字节；webp 不可用时回退 png（保证 alpha 不丢）。"""
    buffer = io.BytesIO()
    if out_fmt == "jpeg":
        im.save(buffer, "JPEG", quality=quality, optimize=True)
        return buffer.getvalue()
    if out_fmt == "webp":
        try:
            im.save(buffer, "WEBP", quality=quality, method=4)
            return buffer.getvalue()
        except Exception:
            buffer = io.BytesIO()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if alpha else "RGB")
            im.save(buffer, "PNG", optimize=True)
            return buffer.getvalue()
    if out_fmt == "png":
        im.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()
    raise VariantError(f"bad_output_format: {out_fmt}")
