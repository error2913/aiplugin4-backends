"""image-url-to-base64 1.0.2 单测：webp 检测、max_side 缩放、bmp 转 png、响应字段。

GIF（D5）：``max_side > 0`` 时取首帧导出（有 alpha → png，无 alpha → webp）；
``max_side <= 0`` 保持旧行为（静态 gif→png、动图原样）。

不依赖真实外网：起一个本地 HTTP 服务器返回构造好的图片字节。

运行：
  python -m unittest discover -s backends/image-url-to-base64/tests -p "test_max_side.py" -v
"""

import base64
import io
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from PIL import Image  # noqa: E402

import urltobase64  # noqa: E402


def make_image(fmt="PNG", size=(100, 100), mode="RGB", color=None, **kwargs):
    color = color if color is not None else ((10, 20, 30, 255) if "A" in mode
                                             else (10, 20, 30))
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def open_image(data):
    return Image.open(io.BytesIO(data))


def animated_gif(size=(300, 100), frames=3):
    images = [Image.new("RGB", size, (i * 60, 20, 30)) for i in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:],
                   duration=100, loop=0)
    return buffer.getvalue()


def transparent_gif(size=(300, 100)):
    """上半透明、下半棋盘格实色的 GIF（P 模式 + transparency）。"""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1] // 2, size[1]):
        for x in range(0, size[0], 2):
            im.putpixel((x, y), (255, 0, 0, 255))
    buffer = io.BytesIO()
    im.save(buffer, "GIF")
    return buffer.getvalue()


class _TestServer:
    """极简 HTTP 服务器：responder(path) -> (status, body, headers)。"""

    def __init__(self):
        self.paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.paths.append(self.path)
                status, body, headers = self.server.responder(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, fmt, *args):      # 静音测试日志
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.paths = self.paths
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class TestCheckImageFormat(unittest.TestCase):
    def test_webp_detected(self):
        # 旧实现 buffer.read(4)[8:12] 恒为空 → webp 永远 unknown，这里锁住修复
        self.assertEqual(
            urltobase64.check_image_format(io.BytesIO(make_image("WEBP", (20, 20)))),
            "webp")

    def test_other_formats(self):
        cases = (("JPEG", "jpg"), ("PNG", "png"), ("GIF", "gif"), ("BMP", "bmp"))
        for fmt, expected in cases:
            self.assertEqual(
                urltobase64.check_image_format(io.BytesIO(make_image(fmt, (8, 8)))),
                expected, fmt)

    def test_unknown(self):
        self.assertEqual(
            urltobase64.check_image_format(io.BytesIO(b"not an image")), "unknown")

    def test_parse_max_side(self):
        self.assertEqual(urltobase64.parse_max_side(None), 0)
        self.assertEqual(urltobase64.parse_max_side(""), 0)
        self.assertEqual(urltobase64.parse_max_side("2048"), 2048)
        self.assertEqual(urltobase64.parse_max_side(0), 0)
        self.assertEqual(urltobase64.parse_max_side(-5), 0)
        self.assertEqual(urltobase64.parse_max_side("abc"), 0)
        self.assertEqual(urltobase64.parse_max_side(999999), urltobase64.MAX_SIDE_LIMIT)


class EndpointBase(unittest.TestCase):
    def post(self, image_bytes, content_type="image/png", **body):
        with _TestServer() as srv:
            srv.httpd.responder = lambda path: (
                200, image_bytes, {"Content-Type": content_type})
            body.setdefault("url", f"{srv.base}/img")
            with urltobase64.app.test_client() as client:
                return client.post("/image-to-base64", json=body)


class TestMaxSideEndpoint(EndpointBase):
    def test_no_max_side_keeps_legacy_behaviour(self):
        data = make_image("PNG", (300, 100))
        res = self.post(data)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        # 旧字段不变
        self.assertEqual(base64.b64decode(body["base64"]), data)
        self.assertEqual(body["format"], "png")
        # 新字段
        self.assertEqual(body["mime"], "image/png")
        self.assertEqual((body["width"], body["height"]), (300, 100))
        self.assertFalse(body["resized"])

    def test_jpeg_scaled(self):
        data = make_image("JPEG", (300, 100))
        res = self.post(data, content_type="image/jpeg", max_side=150)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        self.assertEqual(body["format"], "jpg")
        self.assertEqual(body["mime"], "image/jpeg")
        self.assertEqual((body["width"], body["height"]), (150, 50))
        self.assertTrue(body["resized"])
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(image.size, (150, 50))

    def test_png_alpha_preserved_when_scaled(self):
        data = make_image("PNG", (300, 100), "RGBA", (5, 6, 7, 128))
        res = self.post(data, max_side=100)
        body = res.get_json()
        self.assertEqual(body["format"], "png")
        self.assertEqual(body["mime"], "image/png")
        self.assertEqual(body["width"], 100)
        self.assertTrue(body["resized"])
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(max(image.size), 100)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((5, 5))[3], 128)

    def test_webp_scaled(self):
        data = make_image("WEBP", (300, 100))
        res = self.post(data, content_type="image/webp", max_side=150)
        body = res.get_json()
        self.assertEqual(body["format"], "webp")
        self.assertEqual(body["mime"], "image/webp")
        self.assertEqual((body["width"], body["height"]), (150, 50))
        self.assertTrue(body["resized"])
        self.assertEqual(open_image(base64.b64decode(body["base64"])).size, (150, 50))

    def test_bmp_converted_to_png(self):
        data = make_image("BMP", (200, 100))
        res = self.post(data, content_type="image/bmp", max_side=100)
        body = res.get_json()
        self.assertEqual(body["format"], "png")
        self.assertEqual(body["mime"], "image/png")
        self.assertEqual((body["width"], body["height"]), (100, 50))
        self.assertTrue(body["resized"])
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(image.format, "PNG")

    def test_bmp_converted_even_when_no_scaling_needed(self):
        data = make_image("BMP", (200, 100))
        res = self.post(data, content_type="image/bmp", max_side=1000)
        body = res.get_json()
        self.assertEqual(body["format"], "png")
        self.assertEqual((body["width"], body["height"]), (200, 100))
        self.assertFalse(body["resized"])
        self.assertEqual(open_image(base64.b64decode(body["base64"])).format, "PNG")

    def test_animated_gif_first_frame_scaled_to_webp(self):
        # D5：动图不再原样返回，取首帧导出并缩放
        data = animated_gif((300, 100))
        res = self.post(data, content_type="image/gif", max_side=100)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        self.assertEqual(body["format"], "webp")
        self.assertEqual(body["mime"], "image/webp")
        self.assertTrue(body["resized"])
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(image.format, "WEBP")
        self.assertEqual(max(image.size), 100)
        self.assertEqual((body["width"], body["height"]), image.size)
        self.assertFalse(getattr(image, "is_animated", False))
        # 首帧颜色（webp 有损，允许少量误差）
        pixel = image.getpixel((5, 5))
        for got, want in zip(pixel, (0, 20, 30)):
            self.assertLessEqual(abs(got - want), 25, pixel)
        # 输出不是原 gif 字节
        self.assertNotEqual(base64.b64decode(body["base64"]), data)

    def test_static_gif_first_frame_without_scaling_to_webp(self):
        # 无 alpha 且尺寸达标：仍导出首帧（webp），但 resized=false
        data = make_image("GIF", (120, 80))
        res = self.post(data, content_type="image/gif", max_side=1000)
        body = res.get_json()
        self.assertEqual(body["format"], "webp")
        self.assertEqual(body["mime"], "image/webp")
        self.assertEqual((body["width"], body["height"]), (120, 80))
        self.assertFalse(body["resized"])
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(image.format, "WEBP")
        self.assertEqual(image.size, (120, 80))

    def test_transparent_gif_first_frame_scaled_to_png(self):
        # 有 alpha → png（无损透明），尺寸/宽高/mime 反映最终结果
        data = transparent_gif((300, 100))
        res = self.post(data, content_type="image/gif", max_side=100)
        body = res.get_json()
        self.assertEqual(body["format"], "png")
        self.assertEqual(body["mime"], "image/png")
        self.assertTrue(body["resized"])
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(max(image.size), 100)
        self.assertEqual((body["width"], body["height"]), image.size)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((5, 5))[3], 0)       # 首帧上半部透明

    def test_animated_gif_no_max_side_keeps_legacy_passthrough(self):
        # max_side<=0 保持旧行为：动图原样返回
        data = animated_gif((300, 100))
        res = self.post(data, content_type="image/gif")
        body = res.get_json()
        self.assertEqual(body["format"], "gif")
        self.assertEqual(body["mime"], "image/gif")
        self.assertEqual((body["width"], body["height"]), (300, 100))
        self.assertFalse(body["resized"])
        self.assertEqual(base64.b64decode(body["base64"]), data)

    def test_static_gif_no_max_side_converted_to_png(self):
        data = make_image("GIF", (120, 80))
        res = self.post(data, content_type="image/gif")
        body = res.get_json()
        self.assertEqual(body["format"], "png")
        self.assertEqual(body["mime"], "image/png")
        self.assertEqual((body["width"], body["height"]), (120, 80))

    def test_invalid_max_side_does_not_scale(self):
        data = make_image("PNG", (300, 100))
        res = self.post(data, max_side="abc")
        body = res.get_json()
        self.assertEqual(base64.b64decode(body["base64"]), data)
        self.assertFalse(body["resized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
