"""回归测试：image_search 默认只搜长期图库，插件抓取需 scope=plugin/all。

运行：
  python -m unittest discover -s backends/image-host/tests -p "test_mcp_search.py" -v
"""

import os
import sys
import types
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

# mcp.py 顶层 import flask（服务运行环境有）；单测只测纯逻辑，缺 flask 时用空模块替代
try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake = types.ModuleType("flask")
    fake.jsonify = lambda *a, **k: None
    fake.request = types.SimpleNamespace()
    sys.modules["flask"] = fake

import mcp  # noqa: E402


class FakeStore:
    def __init__(self, items=None):
        self.calls = []
        self._items = items or []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return len(self._items), self._items


def text_of(result):
    return result["content"][0]["text"]


class TestSearchScope(unittest.TestCase):
    def test_default_scope_is_gallery(self):
        store = FakeStore()
        out = text_of(mcp._tool_search(store, {}))
        self.assertEqual(store.calls[0]["origin"], "manual")
        self.assertIn("长期图库", out)
        self.assertIn("scope=plugin", out)

    def test_scope_plugin(self):
        store = FakeStore()
        text_of(mcp._tool_search(store, {"scope": "plugin"}))
        self.assertEqual(store.calls[0]["origin"], "plugin")

    def test_scope_all(self):
        store = FakeStore()
        text_of(mcp._tool_search(store, {"scope": "all"}))
        self.assertEqual(store.calls[0]["origin"], "")

    def test_invalid_scope_rejected(self):
        store = FakeStore()
        out = text_of(mcp._tool_search(store, {"scope": "everywhere"}))
        self.assertIn("scope 不合法", out)
        self.assertEqual(store.calls, [])

    def test_result_reports_scope(self):
        item = {
            "id": "abc",
            "name": "cat",
            "tags": ["猫"],
            "sources": [{"platform": "QQ", "group_id": "1", "user_id": "2", "at": 0}],
            "created_at": 0,
        }
        store = FakeStore([item])
        out = text_of(mcp._tool_search(store, {"query": "cat"}))
        self.assertIn("scope=gallery", out)
        self.assertIn("abc", out)

    def test_tool_schema_exposes_scope(self):
        search = next(t for t in mcp.TOOLS if t["name"] == "image_search")
        scope = search["inputSchema"]["properties"]["scope"]
        self.assertEqual(scope["enum"], ["gallery", "plugin", "all"])
        self.assertEqual(scope["default"], "gallery")


if __name__ == "__main__":
    unittest.main()
