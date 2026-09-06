import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property QtObject theme
    required property bool processing
    signal submitRequested(string text)
    signal stopRequested

    implicitHeight: 92
    radius: 14
    color: theme.surface
    border.color: composer.activeFocus ? theme.focus : theme.border
    border.width: composer.activeFocus ? 2 : 1

    TextArea {
        id: composer
        anchors.left: parent.left
        anchors.right: actionButton.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.margins: 10
        placeholderText: "输入消息，Enter 发送，Shift+Enter 换行"
        wrapMode: TextEdit.Wrap
        color: root.theme.text
        placeholderTextColor: root.theme.textMuted
        background: null
        Accessible.name: "消息输入"
        Keys.onPressed: event => {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                if (!(event.modifiers & Qt.ShiftModifier) && !root.processing) {
                    root.submitRequested(text)
                    text = ""
                    event.accepted = true
                }
            }
        }
    }

    AppButton {
        id: actionButton
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 10
        theme: root.theme
        text: root.processing ? "停止" : "发送"
        kind: root.processing ? "secondary" : "primary"
        onClicked: root.processing ? root.stopRequested() : root.submitRequested(composer.text)
    }
}
