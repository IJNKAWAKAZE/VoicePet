import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property QtObject theme
    required property string role
    required property string markdown
    required property string status
    property real viewportWidth: 800
    signal linkRequested(string link)

    width: Math.min(720, viewportWidth * 0.78)
    implicitHeight: messageColumn.implicitHeight + 24
    height: implicitHeight
    radius: 14
    color: role === "user" ? theme.userBubble
        : role === "tool" ? theme.surfaceAlt : theme.assistantBubble
    border.width: 1
    border.color: role !== "tool" ? theme.border
        : status === "success" ? theme.success
        : status === "partial" ? theme.warning : theme.danger

    Column {
        id: messageColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 12
        spacing: 6

        Text {
            visible: root.role === "tool"
            text: root.status === "success" ? "工具操作完成"
                : root.status === "partial" ? "工具操作部分完成" : "工具操作未完成"
            color: root.status === "success" ? root.theme.success
                : root.status === "partial" ? root.theme.warning : root.theme.danger
            font.pixelSize: 12
            font.weight: Font.DemiBold
            font.family: "Microsoft YaHei UI"
        }

        TextEdit {
            id: messageText
            width: parent.width
            height: implicitHeight
            text: root.markdown
            textFormat: TextEdit.MarkdownText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.Wrap
            color: root.theme.text
            font.pixelSize: 14
            font.family: "Microsoft YaHei UI"
            onLinkActivated: link => root.linkRequested(link)
        }
    }
}
