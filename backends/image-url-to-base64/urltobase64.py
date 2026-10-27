from flask import Flask, request, jsonify
import requests
from io import BytesIO
from PIL import Image
import imageio
import base64
import os
import re
from urllib.parse import urlparse

app = Flask(__name__)

APP_HOST = os.environ.get("AIPLUGIN4_BACKEND_HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("AIPLUGIN4_BACKEND_TOKEN", "")

if AUTH_TOKEN:
    @app.before_request
    def _require_token():
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {AUTH_TOKEN}" or request.headers.get("X-Token") == AUTH_TOKEN:
            return None
        return jsonify({"error": "unauthorized"}), 401

# 下载超时：连接 10s / 读取 30s（可用模块常量调整）
DOWNLOAD_CONNECT_TIMEOUT = 10
DOWNLOAD_READ_TIMEOUT = 30
DOWNLOAD_TIMEOUT = (DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)
# 只对网络异常与 5xx 重试，最多 3 次；4xx（含 429）不重试
DOWNLOAD_RETRIES = 3

# QQ 官方 bot 的图片 URL 来自 JSON/HTML 上下文，& 常被转义成 &amp; 等实体。
# 这里只还原 & 的几种实体写法，不能用 html.unescape：它会把合法的
# `&notify=1` 解成 `¬ify=1`，破坏 URL。
_HTML_AMP_RE = re.compile(r"&(?:amp|#0*38|#x0*26);", re.IGNORECASE)
# 错误文本脱敏：不回显完整 URL（rkey 敏感）
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_URL_IN_MSG_RE = re.compile(r"(url:\s*)\S+", re.IGNORECASE)


def decode_url_html_entities(url):
    """还原 URL 中 HTML 转义的 &（&amp; / &#38; / &#x26;，支持嵌套）。"""
    s = str(url or "").strip()
    for _ in range(3):
        n = _HTML_AMP_RE.sub("&", s)
        if n == s:
            break
        s = n
    return s


def _redact(text):
    """去掉异常文本中的完整 URL / 查询串，避免泄露 rkey。"""
    out = _URL_RE.sub("<url>", str(text))
    return _URL_IN_MSG_RE.sub(r"\1<url>", out)


class UpstreamHTTPError(Exception):
    """上游返回了 >= 400 的 HTTP 状态。"""

    def __init__(self, status, host):
        super().__init__(f"HTTP {status}")
        self.status = int(status or 0)
        self.host = host or ""


class UpstreamNetworkError(Exception):
    """连接失败 / 超时 / 读取响应中断等网络异常。"""

    def __init__(self, exc, host):
        super().__init__(str(exc))
        self.exc = exc
        self.host = host or ""


def _download_once(url, host):
    try:
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        raise UpstreamHTTPError(status, host) from exc
    except requests.RequestException as exc:
        raise UpstreamNetworkError(exc, host) from exc

    try:
        return response.content
    except requests.RequestException as exc:
        raise UpstreamNetworkError(exc, host) from exc


def download_image_with_retry(url, retries=DOWNLOAD_RETRIES):
    host = urlparse(url).hostname or ""
    attempts = max(1, int(retries))
    last_error = None
    for attempt in range(attempts):
        try:
            return BytesIO(_download_once(url, host))
        except UpstreamNetworkError as exc:
            last_error = exc
        except UpstreamHTTPError as exc:
            if exc.status < 500:
                # 4xx（含 429）是确定性失败，重试没有意义
                raise
            last_error = exc
        print(f"Attempt {attempt + 1} failed: {_redact(last_error)}")
    raise last_error


def check_image_format(buffer):
    """按 magic bytes 判定格式（webp 需读足 12 字节判 RIFF....WEBP）。"""
    buffer.seek(0)
    header = buffer.read(12)
    buffer.seek(0)

    if header.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    elif header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    elif header.startswith(b'GIF89a') or header.startswith(b'GIF87a'):
        return 'gif'
    elif header.startswith(b'BM'):
        return 'bmp'
    elif header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    else:
        return 'unknown'


MIME_BY_FORMAT = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'webp': 'image/webp',
    'gif': 'image/gif',
    'bmp': 'image/bmp',
}

# max_side 上限（防御异常入参；正常插件只会传 2048 之类的小值）
MAX_SIDE_LIMIT = 8192


def mime_for(image_format):
    return MIME_BY_FORMAT.get(image_format, 'application/octet-stream')


def parse_max_side(value):
    """``max_side`` 可选参数：>0 才缩放；非法/缺省/≤0 视为 0（不缩放）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return min(number, MAX_SIDE_LIMIT)


def _image_dims(buffer):
    """PIL 读尺寸（不改变 buffer 位置）；失败返回 (None, None)。"""
    try:
        buffer.seek(0)
        with Image.open(buffer) as im:
            w, h = im.size
        return int(w), int(h)
    except Exception as exc:
        print(f"Error reading image size: {exc}")
        return None, None
    finally:
        try:
            buffer.seek(0)
        except Exception:
            pass


def _buffer_bytes(buffer):
    buffer.seek(0)
    data = buffer.read()
    buffer.seek(0)
    return data


def _encode_image(im, image_format):
    out = BytesIO()
    if image_format == 'jpg':
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        im.save(out, format='JPEG', quality=85, optimize=True)
    elif image_format == 'webp':
        if im.mode not in ('RGB', 'RGBA'):
            im = im.convert('RGBA' if 'A' in im.mode else 'RGB')
        im.save(out, format='WEBP', quality=85, method=4)
    else:
        if im.mode not in ('RGB', 'RGBA', 'P', 'L', 'LA'):
            im = im.convert('RGBA' if 'A' in im.mode else 'RGB')
        im.save(out, format='PNG')
    out.seek(0)
    return out


def _resize_to(im, max_side, image_format):
    """LANCZOS 缩放到长边 ``max_side``，按格式正确保留/丢弃 alpha。"""
    if im.mode == 'P':
        im = im.convert('RGBA' if 'transparency' in im.info else 'RGB')
    if image_format == 'jpg' and im.mode not in ('RGB', 'L'):
        im = im.convert('RGB')
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def gif_first_frame_format(im):
    """GIF 首帧导出格式：有 alpha → png（无损透明），否则 webp（体积更小）。"""
    if im.mode in ('RGBA', 'LA'):
        return 'png'
    if 'transparency' in getattr(im, 'info', {}):
        return 'png'
    return 'webp'


def process_gif_first_frame(buffer, max_side):
    """GIF 取首帧并缩放，返回 ``(BytesIO, format, width, height, resized)``。

    视觉模型只看一帧（D5）：静态/动图 gif 都只导出 ``seek(0)`` 的首帧；
    有 alpha → png，无 alpha → webp；长边超过 ``max_side`` 时 LANCZOS 缩放。
    失败返回 ``(None, 'gif', None, None, False)``（调用方按 500 处理）。
    """
    try:
        buffer.seek(0)
        with Image.open(buffer) as opened:
            try:
                opened.seek(0)          # 显式首帧；PIL 打开时通常已是第 0 帧
            except Exception:
                pass
            target_format = gif_first_frame_format(opened)
            mode = 'RGBA' if target_format == 'png' else 'RGB'
            im = opened.convert(mode)   # 调色板/透明统一转 RGBA/RGB
    except Exception as e:
        print(f"Error reading GIF first frame: {e}")
        return None, 'gif', None, None, False

    try:
        resized = max(im.size) > max_side
        if resized:
            im = _resize_to(im, max_side, target_format)
        out = _encode_image(im, target_format)
        w, h = im.size
        return out, target_format, int(w), int(h), bool(resized)
    finally:
        try:
            im.close()
        except Exception:
            pass


def process_image(buffer, image_format, max_side=0):
    """返回 ``(BytesIO, format, width, height, resized)``。

    - ``max_side <= 0``：旧行为（原样返回；静态 gif 转 png、动图原样）；
    - ``max_side > 0``：长边超限时缩放；bmp 统一转 png；gif 取**首帧**导出
      静态图（有 alpha → png，无 alpha → webp）再缩放（D5，视觉模型只看一帧）。
    """
    if image_format == 'gif':
        if max_side > 0:
            return process_gif_first_frame(buffer, max_side)
        if is_animated_gif(buffer):
            data = _buffer_bytes(buffer)
            w, h = _image_dims(BytesIO(data))
            return BytesIO(data), 'gif', w, h, False
        converted = convert_static_gif(buffer)
        if converted is None:
            return None, 'gif', None, None, False
        w, h = _image_dims(converted)
        return converted, 'png', w, h, False

    if max_side <= 0:
        # 旧行为：原字节返回（bmp 也原样），不重新编码
        data = _buffer_bytes(buffer)
        w, h = _image_dims(BytesIO(data))
        return BytesIO(data), image_format, w, h, False

    buffer.seek(0)
    with Image.open(buffer) as opened:
        im = opened.copy()
        w0, h0 = im.size
    try:
        if image_format == 'bmp':
            target_format = 'png'
        else:
            target_format = image_format
        if max(int(w0), int(h0)) <= max_side:
            # 尺寸已达标：不重编码（bmp 仍需转 png）
            if target_format == image_format:
                data = _buffer_bytes(buffer)
                return BytesIO(data), image_format, int(w0), int(h0), False
            resized = False
        else:
            im = _resize_to(im, max_side, target_format)
            resized = True
        out = _encode_image(im, target_format)
        w, h = im.size
        return out, target_format, int(w), int(h), bool(resized)
    finally:
        try:
            im.close()
        except Exception:
            pass


def is_animated_gif(buffer):
    try:
        buffer.seek(0)
        buffer_copy = BytesIO(buffer.read())
        gif = imageio.get_reader(buffer_copy)
        return gif.get_length() > 1 
    except Exception as e:
        print(f"Error checking animated GIF: {e}")
        return False

def convert_static_gif(buffer):
    try:
        buffer.seek(0) 
        img = Image.open(buffer)
        png_buffer = BytesIO()
        img.save(png_buffer, format='PNG')
        png_buffer.seek(0)
        return png_buffer
    except Exception as e:
        print(f"Error converting GIF to PNG: {e}")
        return None

@app.route('/image-to-base64', methods=['POST'])
def image_to_base64():
    data = request.get_json(silent=True) or {}
    url = decode_url_html_entities(data.get('url'))
    # 可选：长边缩放目标（0/缺省 = 不缩放，保持旧行为）
    max_side = parse_max_side(data.get('max_side'))

    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not url.lower().startswith(('http://', 'https://')):
        return jsonify({'error': 'URL 必须以 http:// 或 https:// 开头'}), 400

    try:
        buffer = download_image_with_retry(url)
    except UpstreamHTTPError as exc:
        host = exc.host or ''
        if 400 <= exc.status < 500 and exc.status != 429:
            # 保持 "4xx Client Error" 字样，插件侧 isPermanentUrlError 依赖它判永久失败
            error = f'下载失败：上游返回 {exc.status} Client Error'
        else:
            error = f'下载失败：上游返回 {exc.status}'
        print(f"Upstream HTTP error: {exc.status} ({host})")
        return jsonify({'error': error, 'upstream_status': exc.status, 'host': host}), 502
    except UpstreamNetworkError as exc:
        host = exc.host or ''
        print(f"Download failed: {_redact(exc.exc)}")
        return jsonify({
            'error': f'下载失败：网络错误：{_redact(exc.exc)}',
            'host': host,
        }), 502
    except Exception as exc:
        print(f"Error downloading image: {_redact(exc)}")
        return jsonify({
            'error': f'An error occurred while processing the image: {_redact(exc)}',
        }), 500

    try:
        image_format = check_image_format(buffer)
        if image_format == 'unknown':
            return jsonify({'error': 'Unsupported image format'}), 400

        buffer.seek(0)
        final_buffer, final_format, width, height, resized = process_image(
            buffer, image_format, max_side)
        if final_buffer is None:
            return jsonify({'error': 'Failed to convert GIF to PNG'}), 500

        final_buffer.seek(0)
        base64_data = base64.b64encode(final_buffer.read()).decode('utf-8')

        # 旧字段 base64/format 保持不变；新增 mime/width/height/resized
        return jsonify({
            'base64': base64_data,
            'format': final_format,
            'mime': mime_for(final_format),
            'width': width,
            'height': height,
            'resized': bool(resized),
        })
    except Exception as e:
        print(f"Error processing image: {_redact(e)}")
        return jsonify({'error': f'An error occurred while processing the image: {_redact(e)}'}), 500

if __name__ == '__main__':
    port = int(os.environ.get("AIPLUGIN4_BACKEND_PORT", "46678"))
    app.run(host=APP_HOST, port=port)
