"""VoicePet 的 PySide6 桌面界面组件"""

from .event_bridge import QtEventBridge
from .pet_shell import ConversationBubble, PetShellWindow, SonarStatusWidget
from .settings_window import SettingsWindow
from .tray import TrayController

__all__ = [
    "ConversationBubble",
    "PetShellWindow",
    "QtEventBridge",
    "SettingsWindow",
    "SonarStatusWidget",
    "TrayController",
]
