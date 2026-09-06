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
    flags: Qt.Popup | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    onVisibleChanged: if (visible) quickMenu.forceActiveFocus()
    // 点击菜单外部或切换到其他应用时收起托盘菜单
    onActiveChanged: if (!active && visible) hide()

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
