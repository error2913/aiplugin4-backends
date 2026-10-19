"""回归测试：image-host 抓取前还原 URL 中的 HTML & 实体（issue #3）。

运行：
  python -m unittest discover -s backends/image-host/tests -p "test_fetch_entities.py" -v
"""

import base64
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import fetch as fetchmod  # noqa: E402


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _TestServer:
    def __init__(self):
        self.paths = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.httpd.paths = self.paths
        self.httpd.responder = None
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _handler_class(self):
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

            def log_message(self, fmt, *args):
                pass

        return Handler

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class DecodeUrlHtmlEntitiesTest(unittest.TestCase):
    def test_amp(self):
        self.assertEqual(
            fetchmod.decode_url_html_entities("http://x/img?a=1&amp;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_numeric_decimal(self):
        self.assertEqual(
            fetchmod.decode_url_html_entities("http://x/img?a=1&#38;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_numeric_hex(self):
        self.assertEqual(
            fetchmod.decode_url_html_entities("http://x/img?a=1&#x26;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_nested(self):
        self.assertEqual(
            fetchmod.decode_url_html_entities("http://x/img?a=1&amp;amp;b=2"),
            "http://x/img?a=1&b=2",
        )

    def test_legal_amp_entity_not_mangled(self):
        url = "http://x/img?a=1&notify=1"
        self.assertEqual(fetchmod.decode_url_html_entities(url), url)

    def test_strip_and_none(self):
        self.assertEqual(
            fetchmod.decode_url_html_entities("  http://x/a?b=1&amp;c=2  "),
            "http://x/a?b=1&c=2",
        )
        self.assertEqual(fetchmod.decode_url_html_entities(None), "")


class SafeFetchEntitiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ih-fetch-entities-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _fetch(self, url):
        return fetchmod.safe_fetch(
            url,
            max_bytes=1024 * 1024,
            timeout=(5, 5),
            tmp_dir=self.tmp_dir,
            allow_private=True,  # 本机测试服务器
        )

    def test_escaped_amp_decoded_before_request(self):
        with _TestServer() as srv:
            srv.httpd.responder = lambda path: (
                200,
                PNG_BYTES,
                {"Content-Type": "image/png"},
            )
            url = f"{srv.base}/img?a=1&amp;b=2"

            tmp, sha, size, fmt, mime = self._fetch(url)
            try:
                self.assertEqual(fmt, "png")
                self.assertEqual(mime, "image/png")
                self.assertEqual(size, len(PNG_BYTES))
                self.assertEqual(sha, hashlib.sha256(PNG_BYTES).hexdigest())
                with open(tmp, "rb") as fh:
                    self.assertEqual(fh.read(), PNG_BYTES)
            finally:
                fetchmod._safe_unlink(tmp)

            self.assertEqual(srv.paths, ["/img?a=1&b=2"])

    def test_redirect_location_entities_decoded(self):
        with _TestServer() as srv:
            final_path = "/final?a=1&b=2"

            def responder(path):
                if path == "/redir":
                    return (
                        302,
                        b"",
                        {"Location": f"{srv.base}/final?a=1&amp;b=2"},
                    )
                return 200, PNG_BYTES, {"Content-Type": "image/png"}

            srv.httpd.responder = responder
            tmp, sha, size, fmt, mime = self._fetch(f"{srv.base}/redir")
            try:
                self.assertEqual(fmt, "png")
                self.assertEqual(sha, hashlib.sha256(PNG_BYTES).hexdigest())
            finally:
                fetchmod._safe_unlink(tmp)

            self.assertEqual(srv.paths, ["/redir", final_path])


if __name__ == "__main__":
    unittest.main(verbosity=2)
