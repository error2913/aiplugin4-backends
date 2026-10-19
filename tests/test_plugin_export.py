# -*- coding: utf-8 -*-
"""插件配置导出（/api/plugin-config、/api/mcp-config + CLI）的单元测试。

不启动 WebUI、不依赖真实安装：注册表用仓库真实 backends.json，
后端清单 / 依赖状态 / 运行时配置全部 mock。
"""

import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import launcher


def _backend(name: str):
    return SimpleNamespace(name=name)


class PluginExportTest(unittest.TestCase):
    def setUp(self):
        self.registry = launcher.load_registry()
        self.backends = [
            _backend(name)
            for name in (
                "image-url-to-base64",
                "media-fetch",
                "mcp-files-exec",
                "md-html-render",
                "stream-output",
                "usage-chart",
                "mcp-browser",
                "ob11-core-bridge",
                "image-host",
            )
        ]
        self.cfg = {
            "stream-output": {"port": 3010, "host": "0.0.0.0", "token": "", "options": {}},
            "image-url-to-base64": {"port": 46678, "host": "0.0.0.0", "token": "", "options": {}},
            "media-fetch": {"port": 46679, "host": "0.0.0.0", "token": "", "options": {}},
            "usage-chart": {"port": 3009, "host": "0.0.0.0", "token": "", "options": {}},
            "image-host": {"port": 46680, "host": "0.0.0.0", "token": "img-token", "options": {}},
            "ob11-core-bridge": {
                "port": 46880,
                "host": "127.0.0.1",
                "token": "unused-backend-token",
                "options": {"plugin_token": "bridge-token"},
            },
            "mcp-files-exec": {"port": 3910, "host": "0.0.0.0", "token": "mcp-token", "options": {}},
            "md-html-render": {"port": 37632, "host": "0.0.0.0", "token": "", "options": {}},
            "mcp-browser": {"port": 8921, "host": "0.0.0.0", "token": "", "options": {}},
        }

    def _patched(self, stack: ExitStack, deps_ready=True):
        stack.enter_context(mock.patch.object(launcher, "load_registry", return_value=self.registry))
        stack.enter_context(mock.patch.object(launcher, "discover_backends", return_value=self.backends))
        stack.enter_context(mock.patch.object(launcher, "deps_ready", return_value=deps_ready))
        stack.enter_context(
            mock.patch.object(launcher, "backend_config", side_effect=lambda b: dict(self.cfg[b.name]))
        )

    def test_backend_export(self):
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_backend_config()
        config = result["config"]
        self.assertEqual(config["version"], 1)
        self.assertEqual(
            list(config["backends"].keys()),
            ["image-url-to-base64", "media-fetch", "stream-output", "usage-chart",
             "ob11-core-bridge", "image-host"],
            "顺序应固定为注册表顺序",
        )
        self.assertEqual(config["backends"]["stream-output"], {"url": "http://127.0.0.1:3010"})
        self.assertEqual(config["backends"]["image-url-to-base64"], {"url": "http://127.0.0.1:46678"})
        self.assertEqual(
            config["backends"]["ob11-core-bridge"],
            {"url": "ws://127.0.0.1:46880/plugin", "token": "bridge-token"},
        )
        self.assertEqual(
            config["backends"]["image-host"],
            {"url": "http://127.0.0.1:46680", "token": "img-token"},
        )
        self.assertNotIn("token", config["backends"]["media-fetch"])
        self.assertEqual(result["text"], json.dumps(config, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(len(result["included"]), 6)
        self.assertEqual(result["warnings"], [])

    def test_backend_export_host_rules(self):
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_backend_config("192.168.1.10")
        config = result["config"]
        self.assertEqual(config["backends"]["stream-output"]["url"], "http://192.168.1.10:3010")
        # 后端监听在具体 IP 时以该 IP 为准
        self.assertEqual(config["backends"]["ob11-core-bridge"]["url"], "ws://127.0.0.1:46880/plugin")
        self.assertEqual(result["host"], "192.168.1.10")

    def test_backend_export_warns_for_required_token(self):
        self.cfg["image-host"]["token"] = ""
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_backend_config()
        self.assertNotIn("token", result["config"]["backends"]["image-host"])
        self.assertTrue(any("image-host" in w and "token" in w for w in result["warnings"]))

    def test_backend_export_uninstalled(self):
        self.backends = [_backend("stream-output")]
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_backend_config()
        self.assertEqual(list(result["config"]["backends"].keys()), ["stream-output"])
        excluded = {x["name"]: x["reason"] for x in result["excluded"]}
        self.assertEqual(excluded["image-host"], "未安装")

    def test_backend_export_deps_not_ready(self):
        with ExitStack() as stack:
            self._patched(stack, deps_ready=False)
            result = launcher.build_plugin_backend_config()
        self.assertEqual(result["config"]["backends"], {})
        self.assertEqual(len(result["included"]), 0)

    def test_mcp_export(self):
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_mcp_config()
        servers = result["config"]["mcpServers"]
        self.assertEqual(
            list(servers.keys()),
            ["mcp-files-exec", "md-html-render", "mcp-browser", "image-host"],
        )
        self.assertEqual(
            servers["mcp-files-exec"],
            {
                "type": "http",
                "url": "http://127.0.0.1:3910/mcp",
                "headers": {"Authorization": "Bearer mcp-token"},
            },
        )
        # 默认不写 platform：平台白名单由用户粘贴后自行决定，避免工具在其它平台不可见
        self.assertEqual(
            servers["md-html-render"],
            {"type": "http", "url": "http://127.0.0.1:37632/mcp"},
        )
        self.assertEqual(
            servers["mcp-browser"],
            {"type": "http", "url": "http://127.0.0.1:8921/mcp"},
        )
        # image-host 也提供 MCP（/mcp，需要访问 token）
        self.assertEqual(
            servers["image-host"],
            {
                "type": "http",
                "url": "http://127.0.0.1:46680/mcp",
                "headers": {"Authorization": "Bearer img-token"},
            },
        )
        self.assertNotIn("version", result["config"])
        self.assertEqual(result["text"], json.dumps(result["config"], ensure_ascii=False, separators=(",", ":")))

    def test_mcp_export_image_host_token_required_warning(self):
        self.cfg["image-host"]["token"] = ""
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_mcp_config()
        self.assertNotIn("headers", result["config"]["mcpServers"]["image-host"])
        self.assertTrue(any("image-host" in w and "token" in w for w in result["warnings"]))

    def test_mcp_export_empty_token_omits_headers(self):
        self.cfg["mcp-files-exec"]["token"] = ""
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_mcp_config()
        self.assertNotIn("headers", result["config"]["mcpServers"]["mcp-files-exec"])

    def test_mcp_export_empty(self):
        self.backends = []
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_mcp_config()
        self.assertEqual(result["config"]["mcpServers"], {})
        self.assertEqual(result["included"], [])

    def test_host_validation(self):
        self.assertEqual(launcher.normalize_export_host(""), "127.0.0.1")
        self.assertEqual(launcher.normalize_export_host("  192.168.1.10  "), "192.168.1.10")
        for bad in ("http://x", "a/b", "a b", "a?b", "a#b", "127.0.0.1:3010"):
            with self.assertRaises(ValueError):
                launcher.normalize_export_host(bad)

    def test_ipv6_url_brackets(self):
        with ExitStack() as stack:
            self._patched(stack)
            result = launcher.build_plugin_backend_config("::1")
        self.assertEqual(result["config"]["backends"]["stream-output"]["url"], "http://[::1]:3010")

    def test_registry_metadata_present(self):
        by_name = {item["name"]: item for item in self.registry}
        for name in ("stream-output", "image-url-to-base64", "media-fetch", "usage-chart",
                     "image-host", "ob11-core-bridge"):
            self.assertIn("backend", by_name[name]["pluginConfig"], name)
        for name in ("mcp-files-exec", "md-html-render", "mcp-browser", "image-host"):
            self.assertIn("mcp", by_name[name]["pluginConfig"], name)
        self.assertNotIn("platform", by_name["md-html-render"]["pluginConfig"]["mcp"])


if __name__ == "__main__":
    unittest.main()
