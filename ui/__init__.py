"""VoicePet 的 PySide6 桌面界面组件"""

from .event_bridge import QtEventBridge
from .qml_resources import (
    QmlResourceError,
    qml_root,
    ui_assets_root,
    validated_asset_url,
)
from .qml_runtime import QmlRuntime, QmlRuntimeError
from .tray import TrayController

__all__ = [
    "QmlResourceError",
    "QmlRuntime",
    "QmlRuntimeError",
    "QtEventBridge",
    "TrayController",
    "qml_root",
    "ui_assets_root",
    "validated_asset_url",
]
