"""REST /api/upload 单测：tags（数组/JSON 字符串/逗号串）、source.tool/server/via、multipart 兼容。"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

TMP_ROOT = tempfile.mkdtemp(prefix="ih-upload-v3-")
os.environ["AIPLUGIN4_IMAGE_HOST_DIR"] = TMP_ROOT
os.environ["AIPLUGIN4_BACKEND_TOKEN"] = "test-token"
os.environ["AIPLUGIN4_IMAGE_HOST_WEBUI"] = "off"

import imagehost  # noqa: E402

PNG_1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_2 = PNG_1 + b"\x00upload-2"      # 不同字节，避免与其它用例去重成同一 blob
PNG_3 = PNG_1 + b"\x00upload-3"
PNG_4 = PNG_1 + b"\x00upload-4"
HEADERS = {"Authorization": "Bearer test-token"}


def setUpModule():
    imagehost.store.init()


def tearDownModule():
    store = imagehost.store
    try:
        con = getattr(store._local, "con", None)
        if con is not None:
            con.close()
            store._local.con = None
    except Exception:
        pass
    while True:
        try:
            store._pool.get_nowait().close()
        except Exception:
            break
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    for key in ("AIPLUGIN4_IMAGE_HOST_DIR", "AIPLUGIN4_BACKEND_TOKEN",
                "AIPLUGIN4_IMAGE_HOST_WEBUI"):
        os.environ.pop(key, None)


class UploadBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = imagehost.app.test_client()

    def upload_json(self, png: bytes, ref: str, **extra):
        body = {"base64": base64.b64encode(png).decode("ascii"), "ref": ref}
        body.update(extra)
        return self.client.post("/api/upload", json=body, headers=HEADERS)

    def list_items(self, **query):
        query.setdefault("limit", 100)
        res = self.client.get("/api/list", query_string=query, headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.data)
        return res.get_json()

    def find(self, bid: str):
        for item in self.list_items()["items"]:
            if item["id"] == bid:
                return item
        return None


class TestUpload(UploadBase):
    def test_health_version_is_1_0_16(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["version"], "1.0.16")

    def test_json_tags_and_source_metadata(self):
        source = {"platform": "QQ", "group_id": "123456", "user_id": "789",
                  "message_id": "m1", "session_id": "s1", "at": 1700000000000,
                  "tool": "text_to_image", "server": "image-host", "via": "tool"}
        res = self.upload_json(PNG_1, "up-json-1", source=source,
                               tags=["ai-generated", "猫", "来源:QQ", "群:123456"])
        self.assertEqual(res.status_code, 200, res.data)
        bid = res.get_json()["id"]
        item = self.find(bid)
        self.assertIsNotNone(item)
        self.assertEqual(item["tags"], ["ai-generated", "猫"])
        src = item["sources"][0]
        self.assertEqual(src["tool"], "text_to_image")
        self.assertEqual(src["server"], "image-host")
        self.assertEqual(src["via"], "tool")
        self.assertEqual(src["platform"], "QQ")
        self.assertEqual(src["group_id"], "123456")
        self.assertEqual(src["user_id"], "789")

        # 搜索过滤下推到 SQL
        data = self.list_items(tool="text_to_image")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["id"], bid)
        self.assertEqual(self.list_items(server="image-host")["total"], 1)
        self.assertEqual(self.list_items(platform="QQ")["total"], 1)

    def test_multipart_tags_and_source_compat(self):
        form = {
            "file": (io.BytesIO(PNG_2), "up.png"),
            "tags": json.dumps(["meme", "群:123"], ensure_ascii=False),
            "source": json.dumps({"tool": "meme_generator",
                                  "server": "image-host", "via": "tool"},
                                 ensure_ascii=False),
        }
        res = self.client.post("/api/upload", data=form,
                               content_type="multipart/form-data", headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.data)
        bid = res.get_json()["id"]
        item = self.find(bid)
        self.assertIsNotNone(item)
        self.assertEqual(item["origin"], "manual")          # multipart 强制 manual
        self.assertEqual(item["tags"], ["meme"])
        self.assertEqual(item["sources"][0]["tool"], "meme_generator")
        self.assertEqual(item["sources"][0]["via"], "tool")

    def test_tags_accept_comma_string_and_top_level_tool(self):
        res = self.upload_json(PNG_3, "up-csv-1", tags="cat,dog",
                               tool="tool-image", server="mcp-x")
        self.assertEqual(res.status_code, 200, res.data)
        item = self.find(res.get_json()["id"])
        self.assertEqual(item["tags"], ["cat", "dog"])
        # 顶层 tool/server 等价于 source.tool/server
        self.assertEqual(item["sources"][0]["tool"], "tool-image")
        self.assertEqual(item["sources"][0]["server"], "mcp-x")

    def test_upload_requires_token(self):
        res = self.client.post("/api/upload", json={"base64": base64.b64encode(PNG_4).decode()})
        self.assertEqual(res.status_code, 401)

    def test_tags_api_all_origins_by_default(self):
        self.upload_json(PNG_4, "up-tags-1", tags=["长期图库标签"])
        res = self.client.get("/api/tags", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        names = [t["tag"] for t in res.get_json()["tags"]]
        self.assertIn("长期图库标签", names)
        self.assertIn("cat", names)            # 插件图的标签（默认全部来源，兼容 WebUI）


if __name__ == "__main__":
    unittest.main(verbosity=2)
