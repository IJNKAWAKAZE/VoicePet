import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property QtObject theme
    property var entries: []
    signal actionRequested(string action)

    width: 188
    implicitHeight: actions.implicitHeight + 20
    height: implicitHeight
    radius: 16
    color: theme.surface
    border.color: theme.border

    Column {
        id: actions
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 10

        Repeater {
            model: root.entries
            delegate: Button {
                id: menuAction
                required property var modelData
                objectName: "quickMenuAction_" + modelData.id
                width: actions.width
                height: 38
                text: modelData.label
                enabled: modelData.enabled !== false
                opacity: enabled ? 1.0 : 0.5
                flat: true
                hoverEnabled: true
                contentItem: Text {
                    text: menuAction.text
                    color: menuAction.modelData.id === "quit" ? root.theme.danger : root.theme.text
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 13
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 8
                }
                background: Rectangle {
                    radius: 8
                    color: menuAction.down ? root.theme.userBubble
                        : menuAction.hovered || menuAction.visualFocus ? root.theme.surfaceAlt : "transparent"
                    border.width: menuAction.visualFocus ? 1 : 0
                    border.color: root.theme.focus
                }
                onClicked: root.actionRequested(modelData.id)
            }
        }
    }
}
