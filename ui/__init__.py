"""VoicePet 的 PySide6 桌面界面组件"""

from .event_bridge import QtEventBridge
from .pet_shell import ConversationBubble, PetShellWindow, SonarStatusWidget
from .qml_resources import (
    QmlResourceError,
    qml_root,
    ui_assets_root,
    validated_asset_url,
)
from .qml_runtime import QmlRuntime, QmlRuntimeError
from .settings_window import SettingsWindow
from .tray import TrayController

__all__ = [
    "ConversationBubble",
    "PetShellWindow",
    "QmlResourceError",
    "QmlRuntime",
    "QmlRuntimeError",
    "QtEventBridge",
    "SettingsWindow",
    "SonarStatusWidget",
    "TrayController",
    "qml_root",
    "ui_assets_root",
    "validated_asset_url",
]
