import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


class FakeFastMCP:
    def __init__(self, *_args, **_kwargs):
        pass

    def tool(self):
        return lambda fn: fn


class McpFilesExecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["MCP_SANDBOX_ROOTS"] = cls.temp_dir.name
        os.environ["MCP_MAX_EXPORT_BYTES"] = "1024"
        os.environ["MCP_EXPORT_TTL_SECONDS"] = "30"

        mcp_module = types.ModuleType("mcp")
        mcp_server_module = types.ModuleType("mcp.server")
        mcp_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        mcp_fastmcp_module.FastMCP = FakeFastMCP
        sys.modules["mcp"] = mcp_module
        sys.modules["mcp.server"] = mcp_server_module
        sys.modules["mcp.server.fastmcp"] = mcp_fastmcp_module

        source = Path(__file__).resolve().parents[1] / "mcp_files_exec.py"
        spec = importlib.util.spec_from_file_location("mcp_files_exec_test_module", source)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        for name in ("mcp", "mcp.server", "mcp.server.fastmcp", "mcp_files_exec_test_module"):
            sys.modules.pop(name, None)

    def setUp(self):
        self.file_path = Path(self.temp_dir.name) / "测试.txt"
        self.file_path.write_bytes("hello export".encode("utf-8"))
        self.module._EXPORTS.clear()
        self.module.DEFAULT_MAX_EXPORT = 1024

    @staticmethod
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    @staticmethod
    async def collect(scope):
        events = []

        async def send(event):
            events.append(event)

        await McpFilesExecTests.module._download_export(scope, McpFilesExecTests.receive, send)
        return events

    def test_relative_path_resolves_against_first_sandbox_root(self):
        self.assertEqual(str(self.file_path), self.module._resolve("测试.txt"))

    def test_export_file_returns_short_lived_download_url_and_downloads(self):
        result = json.loads(self.module.export_file("测试.txt"))
        self.assertTrue(result["downloadUrl"].startswith("/files/download/"))
        self.assertEqual(result["name"], "测试.txt")

        events = asyncio.run(self.collect({
            "type": "http",
            "method": "GET",
            "path": result["downloadUrl"],
        }))
        self.assertEqual(events[0]["status"], 200)
        self.assertEqual(events[1]["body"], "hello export".encode("utf-8"))
        headers = dict(events[0]["headers"])
        self.assertEqual(headers[b"content-length"], str(len("hello export".encode("utf-8"))).encode("ascii"))

    def test_head_and_expiry(self):
        result = json.loads(self.module.export_file("测试.txt"))
        head_events = asyncio.run(self.collect({
            "type": "http",
            "method": "HEAD",
            "path": result["downloadUrl"],
        }))
        self.assertEqual(head_events[0]["status"], 200)
        self.assertEqual(head_events[1]["body"], b"")
        head_headers = dict(head_events[0]["headers"])
        self.assertEqual(head_headers[b"content-length"], str(len("hello export".encode("utf-8"))).encode("ascii"))
        token = result["downloadUrl"].rsplit("/", 1)[-1]
        self.module._EXPORTS[token]["expires_at"] = 0
        expired_events = asyncio.run(self.collect({
            "type": "http",
            "method": "GET",
            "path": result["downloadUrl"],
        }))
        self.assertEqual(expired_events[0]["status"], 404)

    def test_export_size_limit(self):
        self.module.DEFAULT_MAX_EXPORT = 3
        with self.assertRaises(ValueError):
            self.module.export_file("测试.txt")


if __name__ == "__main__":
    unittest.main()
