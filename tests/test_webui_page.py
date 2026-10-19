# -*- coding: utf-8 -*-
"""WebUI 内嵌脚本的语法回归测试。

`webui.PAGE` 是 Python 三引号字符串：JS 里写 `'\\n'` 时，Python 会在运行期
把 `\\n` 转义成真实换行，导致整段脚本语法错误（登录按钮失效）。
这里直接取**运行期**的 PAGE / SAFE_PAGE（而不是源码文本）交给 node --check 校验。
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

import launcher
import webui


def _scripts(html: str) -> list:
    return re.findall(r"<script>(.*?)</script>", html or "", re.S)


class WebUIPageScriptTest(unittest.TestCase):
    def _node_check(self, chunks: list) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过内嵌 JS 语法检查")
        fd, path = tempfile.mkstemp(suffix=".js", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(chunks))
            proc = subprocess.run(
                [node, "--check", path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, f"内嵌 JS 语法错误：\n{proc.stderr}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_embedded_script_syntax(self):
        chunks = _scripts(webui.PAGE)
        self.assertTrue(chunks, "PAGE 中应至少有一段 <script>")
        self._node_check(chunks)

    def test_safe_page_script_syntax(self):
        chunks = _scripts(webui.SAFE_PAGE)
        self.assertTrue(chunks, "SAFE_PAGE 中应至少有一段 <script>")
        self._node_check(chunks)

    def test_no_raw_newline_in_js_string_literals(self):
        """兜底检查：避免 Python 转义漏洞再次出现（不依赖 node）。"""
        self.assertNotIn("join('\n')", webui.PAGE, "join('\\n') 会被 Python 提前转义成真实换行")
        self.assertNotIn("join('\n')", webui.SAFE_PAGE, "join('\\n') 会被 Python 提前转义成真实换行")

    def test_package_validator_passes(self):
        """发布前校验函数本身必须能通过当前页面；它会在 package 前执行。"""
        launcher._validate_webui_scripts()


if __name__ == "__main__":
    unittest.main()
