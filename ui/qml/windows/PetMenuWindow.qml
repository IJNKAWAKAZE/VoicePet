import QtQuick
import QtQuick.Controls
import "../components"

ApplicationWindow {
    id: menuWindow
    objectName: "trayMenuWindow"
    required property QtObject theme
    property var entries: []
    signal actionRequested(string action)

    width: 188
    height: entries.length * 38 + 20
    visible: false
    transientParent: null
    color: "transparent"
    // 使用独立自绘工具窗口，避免 Qt.Popup 在 shell 切换时自动关闭。
    // 外部点击和键盘收起由 TrayMenuDismissal 统一处理。
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    onVisibleChanged: if (visible) quickMenu.forceActiveFocus()

    Shortcut {
        sequence: "Escape"
        enabled: menuWindow.visible
        onActivated: menuWindow.hide()
    }

    PetQuickMenu {
        id: quickMenu
        anchors.fill: parent
        theme: menuWindow.theme
        entries: menuWindow.entries
        Keys.onEscapePressed: menuWindow.hide()
        onActionRequested: action => {
            menuWindow.hide()
            menuWindow.actionRequested(action)
        }
    }
}
