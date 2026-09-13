import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QSG_RHI_BACKEND", "software")

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def wait_for():
    """轮询等待条件成立，避免断言依赖固定的墙钟时间。"""

    def _wait_for(predicate, timeout_ms: int = 2000, step_ms: int = 10) -> bool:
        elapsed = 0
        while elapsed < timeout_ms:
            if predicate():
                return True
            QTest.qWait(step_ms)
            elapsed += step_ms
        return predicate()

    return _wait_for
