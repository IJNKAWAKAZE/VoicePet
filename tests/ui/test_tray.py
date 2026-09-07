import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.tray import TrayController


def app():
    return QApplication.instance() or QApplication([])


def test_tray_actions_emit_clear_user_intents():
    app()
    tray = TrayController()
    intents = []
    tray.wake_requested.connect(lambda: intents.append("wake"))
    tray.manual_input_requested.connect(lambda: intents.append("manual"))
    tray.settings_requested.connect(lambda: intents.append("settings"))
    tray.quit_requested.connect(lambda: intents.append("quit"))

    tray.wake_action.trigger()
    tray.manual_input_action.trigger()
    tray.settings_action.trigger()
    tray.quit_action.trigger()

    assert intents == ["wake", "manual", "settings", "quit"]
    assert tray.wake_action.text() == "开始聆听"
    assert tray.manual_input_action.text() == "手动输入"
    assert tray.settings_action.text() == "打开设置"
    assert tray.quit_action.text() == "退出 VoicePet"
    tray.close()
