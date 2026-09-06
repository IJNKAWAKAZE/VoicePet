"""QML 引擎的装配、加载和清理边界"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication


class QmlRuntimeError(RuntimeError):
    """QML 根组件无法安全加载"""


class QmlRuntime:
    """管理单个 QML 引擎及其上下文对象生命周期"""

    def __init__(
        self,
        application: QApplication,
        context_objects: Mapping[str, QObject],
        entrypoint: str | Path | QUrl,
    ) -> None:
        self._application = application
        self._context_objects = dict(context_objects)
        self._entrypoint = _as_url(entrypoint)
        self._engine: QQmlApplicationEngine | None = None
        self._warnings: list[QQmlError] = []

    @property
    def root_objects(self) -> tuple[QObject, ...]:
        if self._engine is None:
            return ()
        return tuple(self._engine.rootObjects())

    def load(self) -> None:
        """创建引擎、注入上下文并加载根组件"""

        if self._engine is not None:
            raise QmlRuntimeError("QML 运行时已经加载")
        QQuickStyle.setStyle("Basic")
        engine = QQmlApplicationEngine()
        self._engine = engine
        engine.warnings.connect(self._collect_warnings)
        context = engine.rootContext()
        for name, value in self._context_objects.items():
            context.setContextProperty(name, value)
        engine.load(self._entrypoint)
        self._application.processEvents()
        if self._warnings or not engine.rootObjects():
            details = "; ".join(warning.toString() for warning in self._warnings)
            self.close()
            suffix = f": {details}" if details else ""
            raise QmlRuntimeError(f"QML 根组件加载失败{suffix}")

    def close(self) -> None:
        """释放根对象并断开对 QML 引擎的引用"""

        engine = self._engine
        self._engine = None
        self._warnings.clear()
        if engine is None:
            return
        for root in engine.rootObjects():
            root.deleteLater()
        engine.clearComponentCache()
        engine.deleteLater()
        self._application.processEvents()

    def _collect_warnings(self, warnings: list[QQmlError]) -> None:
        self._warnings.extend(warnings)


def _as_url(entrypoint: str | Path | QUrl) -> QUrl:
    if isinstance(entrypoint, QUrl):
        return entrypoint
    return QUrl.fromLocalFile(str(Path(entrypoint).expanduser().resolve()))
