"""MCP 6 工具单测：搜索过滤/total/since/多标签/来源兜底、keep/unkeep、tag、CIR 入参、isError、-32602。"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

# mcp.py 顶层 import flask（服务运行环境有）；单测缺 flask 时用空模块替代
try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    import types
    fake = types.ModuleType("flask")
    fake.jsonify = lambda *a, **k: None
    fake.request = types.SimpleNamespace()
    sys.modules["flask"] = fake

import mcp  # noqa: E402
from store import Store  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_of(result) -> str:
    return result["content"][0]["text"]


class MCPBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ih-mcp-v3-")
        self.store = Store(self.root, cap_bytes=64 * 1024 * 1024)
        self.store.init()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, data: bytes, ref: str, origin: str = "plugin", tags=None,
            source=None):
        bid = sha(data)
        source = source if source is not None else {}
        tmp = None
        if self.store.lookup(bid) is None:
            tmp = os.path.join(self.store.tmp, bid + ".part")
            with open(tmp, "wb") as fh:
                fh.write(data)
        self.store.put(tmp, bid, len(data), "png", "image/png", ref, source,
                       origin, tags=tags or [])
        return bid

    def call(self, tool_name: str, **arguments):
        return mcp._call(self.store, {"name": tool_name, "arguments": arguments})

    def call_text(self, tool_name: str, /, **arguments) -> str:
        return text_of(self.call(tool_name, **arguments))


class TestToolRegistry(MCPBase):
    def test_six_tools_and_docstring(self):
        self.assertEqual(len(mcp.TOOLS), 6)
        names = {tool["name"] for tool in mcp.TOOLS}
        self.assertEqual(names, {"image_search", "image_keep", "image_unkeep",
                                 "image_rename", "image_tag", "image_tags"})
        self.assertIn("6 个工具", mcp.__doc__)
        keep = next(t for t in mcp.TOOLS if t["name"] == "image_keep")
        self.assertIn("50", keep["description"])

    def test_unknown_tool_is_32602_via_endpoint(self):
        app = flask.Flask(__name__)
        mcp.register(app, self.store, lambda: True, "1.0.15")
        client = app.test_client()
        res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/call",
                                        "params": {"name": "image_delete",
                                                   "arguments": {}}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["error"]["code"], -32602)
        # 未实现的方法仍是 -32601
        res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                        "method": "resources/list"})
        self.assertEqual(res.get_json()["error"]["code"], -32601)
        # 通知返回 202
        res = client.post("/mcp", json={"jsonrpc": "2.0",
                                        "method": "notifications/initialized"})
        self.assertEqual(res.status_code, 202)
        # tools/list = 6
        res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3,
                                        "method": "tools/list"})
        self.assertEqual(len(res.get_json()["result"]["tools"]), 6)

    def test_tools_call_unauthorized(self):
        app = flask.Flask(__name__)
        mcp.register(app, self.store, lambda: False, "1.0.15")
        res = app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                   "method": "tools/list"})
        self.assertEqual(res.status_code, 401)


class TestSearch(MCPBase):
    def setUp(self):
        super().setUp()
        self.manual = self.put(
            b"manual", "m1", origin="manual", tags=["cat", "ai-generated"],
            source={"platform": "QQ", "group_id": "g1", "user_id": "u1",
                    "at": 1700000000000, "tool": "text_to_image",
                    "server": "image-host", "via": "tool"})
        self.p1 = self.put(
            b"p1", "p1", origin="plugin", tags=["meme"],
            source={"platform": "QQ", "group_id": "g2", "user_id": "u2",
                    "at": 1700000001000, "tool": "meme_generator",
                    "server": "image-host", "via": "tool"})
        self.p2 = self.put(b"p2", "p2", origin="plugin")

    def test_tool_server_platform_group_filters_and_total(self):
        text = self.call_text("image_search", scope="all")
        self.assertIn("共 3 条（scope=all）", text)
        self.assertIn("返回 3 条", text)

        text = self.call_text("image_search", scope="all", tool="meme_generator")
        self.assertIn("共 1 条", text)
        self.assertIn(self.p1, text)
        text = self.call_text("image_search", scope="all", server="image-host")
        self.assertIn("共 2 条", text)
        text = self.call_text("image_search", scope="all", platform="QQ")
        self.assertIn("共 2 条", text)
        text = self.call_text("image_search", scope="all", group_id="g1")
        self.assertIn("共 1 条", text)
        # tool 过滤 + 分页：total 是过滤后的数，不是全库；按 created_at DESC
        page0 = self.call("image_search", scope="all", server="image-host",
                          limit=1, offset=0)
        self.assertIn("共 2 条", text_of(page0))
        self.assertIn(self.p1, text_of(page0))
        page1 = self.call("image_search", scope="all", server="image-host",
                          limit=1, offset=1)
        self.assertIn("共 2 条", text_of(page1))
        self.assertIn(self.manual, text_of(page1))
        beyond = self.call_text("image_search", scope="all", server="image-host",
                                limit=1, offset=5)
        self.assertIn("共 2 条", beyond)
        self.assertIn("offset=5 已超出", beyond)

    def test_scope_default_gallery(self):
        text = self.call_text("image_search")
        self.assertIn("共 1 条", text)
        self.assertIn(self.manual, text)
        self.assertNotIn(self.p1, text)
        text = self.call_text("image_search", scope="plugin")
        self.assertIn("共 2 条", text)
        self.assertNotIn(self.manual, text)

    def test_multi_tags_and(self):
        self.assertIn("共 1 条", self.call_text("image_search", tags=["cat"]))
        self.assertIn("共 1 条", self.call_text("image_search", tags=["cat", "ai-generated"]))
        self.assertIn("没有匹配的图片", self.call_text("image_search", scope="all",
                                                       tags=["cat", "meme"]))
        # 字符串按单值处理
        self.assertIn("共 1 条", self.call_text("image_search", tags="cat"))
        self.assertIn("共 1 条", self.call_text("image_search", tags=["cat"]))

    def test_since(self):
        with self.store._lock:
            con = self.store._conn()
            con.execute("UPDATE blob SET created_at=1000 WHERE id=?", (self.manual,))
            con.execute("UPDATE blob SET created_at=2000 WHERE id=?", (self.p1,))
            con.execute("UPDATE blob SET created_at=3000 WHERE id=?", (self.p2,))
        self.assertIn("共 2 条（scope=all）",
                      self.call_text("image_search", scope="all",
                                     since="1970-01-01T00:00:01.500000+00:00"))
        self.assertIn("共 1 条（scope=all）",
                      self.call_text("image_search", scope="all",
                                     since="1970-01-01T00:00:02.500000+00:00"))
        # ISO 形式（带时区）
        iso = "1970-01-01T00:00:02.500000+00:00"
        self.assertIn("共 1 条（scope=all）", self.call_text("image_search", scope="all",
                                                           since=iso))
        bad = self.call("image_search", since="not-a-date")
        self.assertTrue(bad.get("isError"))
        self.assertIn("ISO 8601", text_of(bad))

    def test_source_never_empty_and_no_unknown(self):
        text = self.call_text("image_search", scope="all")
        self.assertNotIn("未知", text)
        self.assertNotIn("来源: -", text)
        lines = [line for line in text.splitlines() if line and " | " in line]
        p2_line = next(line for line in lines if self.p2 in line)
        self.assertIn("插件抓取（历史数据）", p2_line)
        p1_line = next(line for line in lines if self.p1 in line)
        self.assertIn("群g2", p1_line)
        self.assertIn("工具: meme_generator", p1_line)
        self.assertIn("服务器: image-host", p1_line)
        self.assertIn(f"[img:{self.p1}]", p1_line)
        self.assertIn("[抓取·待验证]", p1_line)

    def test_manual_without_ref_uses_gallery_fallback(self):
        bid = self.put(b"no-ref-manual", "m2", origin="manual")
        text = self.call_text("image_search", scope="gallery")
        line = next(line for line in text.splitlines() if bid in line)
        self.assertIn("长期图库（人工上传）", line)
        self.assertIn("[图库·可信]", line)

    def test_webui_synthetic_manual_source_is_gallery(self):
        bid = self.put(b"webui-manual", "w1", origin="manual",
                       source={"platform": "manual", "at": 1700000000000})
        line = next(line for line in self.call_text("image_search", scope="gallery").splitlines()
                    if bid in line)
        self.assertIn("长期图库（人工上传）", line)
        self.assertNotIn("来源: manual", line)

    def test_bad_scope_and_unknown_param_are_iserror(self):
        res = self.call("image_search", scope="everywhere")
        self.assertTrue(res.get("isError"))
        self.assertIn("scope 不合法", text_of(res))
        res = self.call("image_search", bogus=1)
        self.assertTrue(res.get("isError"))
        self.assertIn("未知参数", text_of(res))


class TestKeepUnkeep(MCPBase):
    def test_keep_unkeep_and_idempotent(self):
        bid = self.put(b"x", "p1", origin="plugin")
        res = self.call("image_keep", ids=[bid])
        self.assertIn("已把 1 张图收进长期图库", text_of(res))
        self.assertEqual(res["structuredContent"]["changed_ids"], [bid])
        self.assertEqual(res["structuredContent"]["skipped_ids"], [])
        self.assertEqual(res["structuredContent"]["failed"], [])
        self.assertEqual(self.store.lookup(bid)["origin"], "manual")
        text = self.call_text("image_keep", ids=["h" + bid])
        self.assertIn("已把 0 张图", text)
        self.assertIn("跳过 1 张", text)
        self.assertIn("已把 1 张图移出长期图库",
                      self.call_text("image_unkeep", ids=["[img:" + bid + "]"]))
        self.assertEqual(self.store.lookup(bid)["origin"], "plugin")
        # CIR 的 /i/<sha> 形态
        self.assertIn("已把 1 张图收进长期图库",
                      self.call_text("image_keep", id=f"/i/{bid}"))
        self.assertEqual(self.store.lookup(bid)["origin"], "manual")

    def test_keep_accepts_plugin_ref(self):
        bid = self.put(b"x", "plugin-ref-abc", origin="plugin")
        text = self.call_text("image_keep", ids=["plugin-ref-abc"])
        self.assertIn("已把 1 张图", text)
        self.assertEqual(self.store.lookup(bid)["origin"], "manual")

    def test_keep_missing_and_skipped_reporting(self):
        missing = "f" * 64
        text = self.call_text("image_keep", ids=[missing])
        self.assertIn("未找到", text)
        self.assertIn(missing, text)

    def test_ids_over_50_is_iserror(self):
        res = self.call("image_keep", ids=[f"{i:064x}" for i in range(51)])
        self.assertTrue(res.get("isError"))
        self.assertIn("最多 50", text_of(res))

    def test_no_ids_is_iserror(self):
        res = self.call("image_keep")
        self.assertTrue(res.get("isError"))
        self.assertIn("需要 id 或 ids", text_of(res))
        res = self.call("image_keep", ids=123)
        self.assertTrue(res.get("isError"))


class TestTag(MCPBase):
    def test_affected_counts_images_and_failed_reported(self):
        a = self.put(b"a", "a")
        b = self.put(b"b", "b")
        missing = "e" * 64
        text = self.call_text("image_tag", ids=[a, b, missing], add=["猫"])
        self.assertIn("已更新 2 张图", text)
        self.assertIn("未找到", text)
        self.assertIn(missing, text)
        # 幂等
        text = self.call_text("image_tag", ids=[a, b], add=["猫"])
        self.assertIn("已更新 0 张图", text)
        self.assertIn("跳过 2 张", text)
        # add + remove 同一张只计 1 张
        self.call("image_tag", id=a, add=["旧"])
        text = self.call_text("image_tag", id=a, add=["新"], remove=["旧"])
        self.assertIn("已更新 1 张图", text)
        # 字符串按单值
        text = self.call_text("image_tag", id=b, add="单个")
        self.assertIn("已更新 1 张图", text)

    def test_metadata_tags_rejected(self):
        a = self.put(b"a", "a")
        for bad in ("来源:QQ", "群:123456", "工具:image_search", "MCP:image-host",
                    "类型:ai-generated", "123456", "2025-01-01", "a.png"):
            res = self.call("image_tag", id=a, add=[bad])
            self.assertTrue(res.get("isError"), bad)
            self.assertIn("不合法标签", text_of(res))
        res = self.call("image_tag", id=a, add=[f"t{i}" for i in range(11)])
        self.assertTrue(res.get("isError"))
        self.assertIn("最多 10", text_of(res))
        res = self.call("image_tag", id=a)
        self.assertTrue(res.get("isError"))

    def test_tag_tags_listing_gallery_only(self):
        a = self.put(b"a", "a", origin="manual", tags=["猫"])
        self.put(b"b", "b", origin="plugin", tags=["插件标签"])
        text = self.call_text("image_tags")
        self.assertIn("猫", text)
        self.assertNotIn("插件标签", text)
        self.assertIn("长期图库共 1 个标签", text)
        self.assertIn("猫 ×1", text)

    def test_tag_rename_accepts_cir_and_validates(self):
        a = self.put(b"a", "a")
        self.assertIn("已重命名",
                      self.call_text("image_rename", id=f"[img:{a}:旧名]", name="新名"))
        res = self.call("image_rename", id=a, name="   ")
        self.assertTrue(res.get("isError"))
        res = self.call("image_rename", id=a, name="x" * 81)
        self.assertTrue(res.get("isError"))
        self.assertIn("最多 80", text_of(res))
        text = self.call_text("image_rename", id="d" * 64, name="新名")
        self.assertIn("未找到", text)

    def test_rename_trash_refused(self):
        a = self.put(b"a", "a")
        self.store.batch("delete", [a])
        text = self.call_text("image_rename", id=a, name="新名")
        self.assertIn("回收站", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
