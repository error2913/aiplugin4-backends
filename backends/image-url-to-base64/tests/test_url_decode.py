"""回归测试：QQ 官方图片 URL 的 HTML & 实体解码（issue #3）。

只覆盖 /image-to-base64 的 URL 预处理与错误语义，不依赖真实外网。

运行：
  python -m unittest discover -s backends/image-url-to-base64/tests -p "test_url_decode.py" -v
"""

import base64
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import urltobase64  # noqa: E402


# 1x1 PNG（合法 magic bytes，足以通过格式判定）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _TestServer:
    """极简 HTTP 服务器：记录请求 path，由 responder 决定响应。"""

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

            def log_message(self, fmt, *args):  # 静音测试日志
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


def _png_responder(path):
    return 200, PNG_BYTES, {"Content-Type": "image/png"}


class DecodeUrlHtmlEntitiesTest(unittest.TestCase):
    def test_amp(self):
        self.assertEqual(
            urltobase64.decode_url_html_entities(
                "http://example.com/img?appid=1407&amp;fileid=abc&amp;rkey=xyz"
            ),
            "http://example.com/img?appid=1407&fileid=abc&rkey=xyz",
        )

    def test_numeric_decimal(self):
        self.assertEqual(
            urltobase64.decode_url_html_entities("http://x/img?a=1&#38;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_numeric_hex(self):
        self.assertEqual(
            urltobase64.decode_url_html_entities("http://x/img?a=1&#x26;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_nested(self):
        self.assertEqual(
            urltobase64.decode_url_html_entities("http://x/img?a=1&amp;amp;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_legal_amp_entity_not_mangled(self):
        # 不能用 html.unescape：&notify=1 必须原样保留
        url = "http://x/img?a=1&notify=1"
        self.assertEqual(urltobase64.decode_url_html_entities(url), url)

    def test_strip_and_none(self):
        self.assertEqual(
            urltobase64.decode_url_html_entities("  http://x/a?b=1&amp;c=2  "),
            "http://x/a?b=1&c=2",
        )
        self.assertEqual(urltobase64.decode_url_html_entities(None), "")


class ImageToBase64EndpointTest(unittest.TestCase):
    def test_escaped_amp_is_decoded_before_request(self):
        with _TestServer() as srv:
            srv.httpd.responder = _png_responder
            url = f"{srv.base}/img?a=1&amp;b=2"

            with urltobase64.app.test_client() as client:
                resp = client.post("/image-to-base64", json={"url": url})

            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["format"], "png")
            self.assertEqual(base64.b64decode(body["base64"]), PNG_BYTES)
            self.assertEqual(srv.paths, ["/img?a=1&b=2"])

    def test_4xx_not_retried_and_reported_as_client_error(self):
        calls = []

        def responder(path):
            calls.append(path)
            return 400, b"bad request", {"Content-Type": "text/plain"}

        with _TestServer() as srv:
            srv.httpd.responder = responder
            url = f"{srv.base}/expired?appid=1407&amp;fileid=abc"

            with urltobase64.app.test_client() as client:
                resp = client.post("/image-to-base64", json={"url": url})

            self.assertEqual(resp.status_code, 502, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["upstream_status"], 400)
            self.assertEqual(body["host"], "127.0.0.1")
            self.assertIn("Client Error", body["error"])
            self.assertIn("400", body["error"])
            self.assertNotIn("http://", body["error"])
            # 4xx 不重试：只应有 1 次请求
            self.assertEqual(len(calls), 1)
            self.assertEqual(srv.paths, ["/expired?appid=1407&fileid=abc"])

    def test_5xx_retried_up_to_three_times(self):
        calls = []

        def responder(path):
            calls.append(path)
            return 500, b"upstream boom", {"Content-Type": "text/plain"}

        with _TestServer() as srv:
            srv.httpd.responder = responder

            with urltobase64.app.test_client() as client:
                resp = client.post(
                    "/image-to-base64", json={"url": f"{srv.base}/boom?a=1&amp;b=2"}
                )

            self.assertEqual(resp.status_code, 502, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["upstream_status"], 500)
            self.assertIn("500", body["error"])
            # 5xx 重试：共 3 次
            self.assertEqual(len(calls), 3)

    def test_missing_or_non_http_url_rejected(self):
        with urltobase64.app.test_client() as client:
            self.assertEqual(
                client.post("/image-to-base64", json={}).status_code, 400
            )
            self.assertEqual(
                client.post("/image-to-base64", json={"url": "file:///etc/passwd"}).status_code,
                400,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
