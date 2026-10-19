# -*- coding: utf-8 -*-
"""aibackend webui-stop / webui-restart 的部署形态分支。

- systemd 托管：交给 systemctl restart/stop；
- 无 pid 文件（前台/手动运行）：按端口清理旧 launcher/webui 进程后再启动；
- 由 systemd 直接调起（INVOCATION_ID）：等待 systemd 拉起，不抢端口。
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import aibackend


class WebUIRestartTest(unittest.TestCase):
    def _args(self):
        return SimpleNamespace()

    def test_restart_uses_systemd_when_active(self):
        with mock.patch.object(aibackend, "restart_webui_service", return_value=True) as svc, \
                mock.patch.object(aibackend, "stop_webui") as stop, \
                mock.patch.object(aibackend, "start_webui_background") as start:
            aibackend.cmd_webui_restart(self._args())
        svc.assert_called_once_with()
        stop.assert_not_called()
        start.assert_not_called()

    def test_restart_kills_stale_when_no_pid(self):
        with mock.patch.object(aibackend, "restart_webui_service", return_value=False), \
                mock.patch.object(aibackend, "stop_webui", return_value=False), \
                mock.patch.object(aibackend, "effective_webui_port", return_value=56210), \
                mock.patch.object(aibackend, "_kill_stale_webui_on_port", return_value=True) as kill, \
                mock.patch.object(aibackend, "start_webui_background") as start:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("INVOCATION_ID", None)
                aibackend.cmd_webui_restart(self._args())
        kill.assert_called_once_with(56210)
        start.assert_called_once_with(open_browser=False)

    def test_restart_systemd_invocation_does_not_spawn(self):
        with mock.patch.object(aibackend, "restart_webui_service", return_value=False), \
                mock.patch.object(aibackend, "stop_webui", return_value=False), \
                mock.patch.object(aibackend, "effective_webui_port", return_value=56210), \
                mock.patch.object(aibackend, "_kill_stale_webui_on_port", return_value=True), \
                mock.patch.object(aibackend, "start_webui_background") as start, \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}, clear=False):
            aibackend.cmd_webui_restart(self._args())
        start.assert_not_called()

    def test_stop_uses_systemd_when_active(self):
        with mock.patch.object(aibackend, "stop_webui_service", return_value=True) as svc, \
                mock.patch.object(aibackend, "stop_webui") as stop:
            aibackend.cmd_webui_stop(self._args())
        svc.assert_called_once_with()
        stop.assert_not_called()

    def test_stop_kills_stale_when_no_pid(self):
        with mock.patch.object(aibackend, "stop_webui_service", return_value=False), \
                mock.patch.object(aibackend, "stop_webui", return_value=False), \
                mock.patch.object(aibackend, "effective_webui_port", return_value=56210), \
                mock.patch.object(aibackend, "_kill_stale_webui_on_port", return_value=True) as kill:
            aibackend.cmd_webui_stop(self._args())
        kill.assert_called_once_with(56210)


if __name__ == "__main__":
    unittest.main()
