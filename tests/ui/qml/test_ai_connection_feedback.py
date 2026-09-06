from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtTest import QTest
from test_settings_interactions import Runtime, Store, page

from core.config import AppConfig
from ui.viewmodels.settings import SettingsViewModel


@pytest.mark.parametrize("succeeds", [True, False])
def test_ai_button_shows_pending_and_terminal_result_in_same_page(qapp, succeeds):
    settings = SettingsViewModel(AppConfig(), Store(), runtime=Runtime())
    with page(qapp, "AiSettings", settings) as root:
        diagnostics = root.property("diagnostics")
        pending = Future()
        calls = []
        diagnostics._runtime.test_llm_connection = lambda: calls.append("llm") or pending
        button = root.findChild(QObject, "testAiConnectionButton")
        status = root.findChild(QObject, "aiConnectionStatus")
        flickable = root.property("contentItem")
        flickable.setProperty("contentY", max(0, flickable.property("contentHeight") - root.height()))
        QTest.qWait(30)
        QTest.mouseClick(root.window(), Qt.LeftButton,
                        pos=button.mapToScene(QPointF(20, 20)).toPoint())
        assert calls == ["llm"]
        assert not button.property("enabled")
        assert "正在测试" in status.property("text")
        QTest.qWait(30)
        assert status.mapToScene(QPointF(0, 0)).y() + status.height() <= root.height()
        if succeeds:
            pending.set_result(SimpleNamespace(status="healthy", duration_ms=42))
        else:
            pending.set_exception(RuntimeError("secret"))
        qapp.processEvents()
        assert button.property("enabled")
        assert ("成功" if succeeds else "失败") in status.property("text")
        assert "secret" not in status.property("text")
        QTest.qWait(30)
        assert status.mapToScene(QPointF(0, 0)).y() + status.height() <= root.height()
