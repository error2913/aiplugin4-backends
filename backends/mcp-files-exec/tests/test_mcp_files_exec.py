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


class FakeContext:
    """FastMCP Context 的替身：仅用于让模块 import/注解可用，工具直调时传 None。"""


class McpFilesExecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.external_dir = tempfile.TemporaryDirectory()
        os.environ.pop("MCP_ALLOW_EXTERNAL_PATHS", None)
        os.environ.pop("MCP_ALLOW_DANGEROUS_COMMANDS", None)
        os.environ["MCP_SANDBOX_ROOTS"] = cls.temp_dir.name
        os.environ["MCP_MAX_EXPORT_BYTES"] = "1024"
        os.environ["MCP_EXPORT_TTL_SECONDS"] = "30"
        os.environ["MCP_LOG_FILE"] = os.path.join(cls.temp_dir.name, "audit.log")
        cls.audit_log = os.path.join(cls.temp_dir.name, "audit.log")

        mcp_module = types.ModuleType("mcp")
        mcp_server_module = types.ModuleType("mcp.server")
        mcp_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        mcp_fastmcp_module.FastMCP = FakeFastMCP
        mcp_fastmcp_module.Context = FakeContext
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
        cls.external_dir.cleanup()
        for name in ("mcp", "mcp.server", "mcp.server.fastmcp", "mcp_files_exec_test_module"):
            sys.modules.pop(name, None)

    def setUp(self):
        self.file_path = Path(self.temp_dir.name) / "测试.txt"
        self.file_path.write_bytes("hello export".encode("utf-8"))
        self._session_token = None
        self.module._EXPORTS.clear()
        self.module.DEFAULT_MAX_EXPORT = 1024

    def tearDown(self):
        self.clear_session()

    def set_session(self, session_id):
        """设置当前请求上下文会话 ID，返回 True（调用方负责 clear_session）。"""
        self._session_token = self.module._current_session.set(session_id)

    def clear_session(self):
        if self._session_token is not None:
            self.module._current_session.reset(self._session_token)
            self._session_token = None

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

    def test_external_absolute_path_is_readable_by_default_open_mode(self):
        external = Path(self.external_dir.name) / "outside.txt"
        external.write_text("outside content", encoding="utf-8")
        self.assertEqual(self.module.read_file(str(external)), "outside content")
        self.assertEqual(self.module._resolve(str(external)), str(external.resolve()))

    def test_download_file_accepts_url_and_external_destination(self):
        source = Path(self.external_dir.name) / "source.bin"
        source.write_bytes(b"downloaded content")
        destination = Path(self.external_dir.name) / "nested" / "target.bin"
        result = json.loads(self.module.download_file(source.as_uri(), str(destination)))
        self.assertEqual(result["path"], str(destination.resolve()))
        self.assertEqual(destination.read_bytes(), b"downloaded content")

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

    def test_parse_session_header(self):
        self.assertEqual(self.module._parse_session_header(None), "")
        self.assertEqual(self.module._parse_session_header(""), "")
        self.assertEqual(self.module._parse_session_header("  QQ-Group%3A123456  "), "QQ-Group:123456")
        self.assertEqual(self.module._parse_session_header("%E4%BC%9A%E8%AF%9D"), "会话")

    def test_sanitize_session_id(self):
        self.assertEqual(self.module._sanitize_session_id("QQ-Group:123456"), "QQ-Group-123456")
        self.assertEqual(self.module._sanitize_session_id("QQ:987654"), "QQ-987654")
        self.assertEqual(self.module._sanitize_session_id("DISCORD-Group:abc"), "DISCORD-Group-abc")
        self.assertEqual(self.module._sanitize_session_id("a/b\\c:d"), "a-b-c-d")
        self.assertEqual(self.module._sanitize_session_id(":::"), "")

    def test_default_workspace_falls_back_to_first_root_without_session(self):
        self.assertEqual(self.module._session_workspace(), None)
        self.assertEqual(self.module._default_workspace(), os.path.realpath(self.temp_dir.name))
        self.assertEqual(self.module._resolve("测试.txt"), os.path.realpath(self.file_path))

    def test_relative_path_resolves_into_session_workspace(self):
        self.set_session("QQ-Group:123456")
        try:
            expected = os.path.join(self.temp_dir.name, "sessions", "QQ-Group-123456", "dir", "文件.txt")
            self.assertEqual(self.module._resolve("dir/文件.txt"), os.path.realpath(expected))
            self.assertNotEqual(
                self.module._resolve("测试.txt"),
                os.path.realpath(os.path.join(self.temp_dir.name, "测试.txt")),
            )
        finally:
            self.clear_session()

    def test_sessions_are_isolated(self):
        self.set_session("QQ-Group:111")
        self.module.write_file("a.txt", "from A")
        first_dir = os.path.join(self.temp_dir.name, "sessions", "QQ-Group-111")
        self.assertTrue(os.path.isfile(os.path.join(first_dir, "a.txt")))

        self.set_session("QQ-Group:222")
        self.module.write_file("b.txt", "from B")
        second_dir = os.path.join(self.temp_dir.name, "sessions", "QQ-Group-222")
        self.assertTrue(os.path.isfile(os.path.join(second_dir, "b.txt")))
        with self.assertRaises(ValueError):
            self.module.read_file("a.txt")
        listing = self.module.list_dir(".")
        self.assertIn("b.txt", listing)
        self.assertNotIn("a.txt", listing)
        self.assertFalse(os.path.isfile(os.path.join(self.temp_dir.name, "a.txt")))
        self.assertFalse(os.path.isfile(os.path.join(self.temp_dir.name, "b.txt")))

        self.set_session("QQ-Group:111")
        self.assertEqual(self.module.read_file("a.txt"), "from A")
        with self.assertRaises(ValueError):
            self.module.read_file("b.txt")

    def test_session_workspace_is_stable_and_persists_across_context(self):
        self.set_session("QQ-Group:123456")
        target = self.module._resolve("note.txt")
        self.module.write_file("note.txt", "hello")
        self.clear_session()

        # 无会话回退共享根，读不到会话区文件
        self.assertNotEqual(self.module._resolve("note.txt"), target)
        with self.assertRaises(ValueError):
            self.module.read_file("note.txt")

        # 同一会话再次出现时仍指向同一目录且文件可见
        self.set_session("QQ-Group:123456")
        try:
            self.assertEqual(self.module._resolve("note.txt"), target)
            self.assertEqual(self.module.read_file("note.txt"), "hello")
        finally:
            self.clear_session()

    def test_absolute_path_still_allowed_with_session(self):
        external = Path(self.external_dir.name) / "outside-session.txt"
        external.write_text("outside content", encoding="utf-8")
        self.set_session("QQ-Group:123456")
        try:
            self.assertEqual(self.module.read_file(str(external)), "outside content")
            self.assertEqual(self.module._resolve(str(external)), str(external.resolve()))
        finally:
            self.clear_session()

    def test_hard_sandbox_still_enforced_with_session(self):
        external = Path(self.external_dir.name) / "secret.txt"
        external.write_text("secret", encoding="utf-8")
        old = self.module._ALLOW_EXTERNAL_PATHS
        self.module._ALLOW_EXTERNAL_PATHS = False
        self.set_session("QQ-Group:123456")
        try:
            with self.assertRaises(ValueError):
                self.module.read_file(str(external))
        finally:
            self.module._ALLOW_EXTERNAL_PATHS = old
            self.clear_session()

    def test_run_shell_default_cwd_uses_session_workspace(self):
        self.set_session("QQ-Group:123456")
        try:
            result = self.module.run_shell("echo ok")
            expected = os.path.realpath(os.path.join(self.temp_dir.name, "sessions", "QQ-Group-123456"))
            self.assertIn(f"(cwd: {expected}", result)
            self.assertTrue(os.path.isdir(expected))
        finally:
            self.clear_session()

    def test_download_file_relative_target_lands_in_session_workspace(self):
        source = Path(self.external_dir.name) / "session-source.bin"
        source.write_bytes(b"downloaded content")
        self.set_session("QQ-Group:123456")
        try:
            result = json.loads(self.module.download_file(source.as_uri(), "nested/target.bin"))
            expected_dir = os.path.realpath(os.path.join(self.temp_dir.name, "sessions", "QQ-Group-123456"))
            expected = os.path.join(expected_dir, "nested", "target.bin")
            self.assertEqual(result["path"], os.path.realpath(expected))
            self.assertTrue(os.path.isfile(expected))
            self.assertFalse(os.path.isfile(os.path.join(self.temp_dir.name, "nested", "target.bin")))
        finally:
            self.clear_session()

    def test_export_token_records_origin_session(self):
        self.set_session("QQ-Group:123456")
        try:
            self.module.write_file("export-session.txt", "hello")
            result = json.loads(self.module.export_file("export-session.txt"))
            token = result["downloadUrl"].rsplit("/", 1)[-1]
            self.assertEqual(self.module._EXPORTS[token]["session"], "QQ-Group:123456")
        finally:
            self.clear_session()

    def test_audit_log_records_session(self):
        self.set_session("QQ-Group:123456")
        try:
            self.module.write_file("audit.txt", "x")
        finally:
            self.clear_session()
        self.module.write_file("audit-shared.txt", "y")

        lines = Path(self.audit_log).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(json.loads(lines[-1])["session"], "")
        self.assertEqual(json.loads(lines[-2])["session"], "QQ-Group:123456")


if __name__ == "__main__":
    unittest.main()
