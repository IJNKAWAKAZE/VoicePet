import QtQuick
import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property QtObject theme
    required property string role
    required property string markdown
    required property string status
    property var attachments: []
    property bool playing: false
    property real viewportWidth: 800
    signal linkRequested(string link)
    signal copyRequested(string text)
    signal playRequested(string text)
    signal fileRequested(string path)
    property string previewSource: ""
    property bool actionsVisible: hoverHandler.hovered || copyMessageButton.visualFocus || playMessageButton.visualFocus

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

    // 悬停范围覆盖气泡、间隙和操作栏，按钮出现时不改变消息高度
    Item {
        z: 1
        width: root.width
        height: root.height + 30
        HoverHandler {
            id: hoverHandler
        }
    }

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

        Repeater {
            model: root.attachments
            delegate: Rectangle {
                required property string kind
                required property string name
                required property string mediaType
                required property string url
                required property string path
                property bool imageFailed: false
                width: messageColumn.width
                height: kind === "image" ? 168 : 38
                radius: 10
                color: root.theme.surfaceAlt
                border.color: root.theme.border

                Image {
                    objectName: "messageAttachmentImage"
                    visible: kind === "image"
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.margins: 4
                    width: parent.width - 8
                    height: parent.height - 8
                    source: url || path || ""
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    cache: false
                    onStatusChanged: imageFailed = status === Image.Error
                    MouseArea {
                        anchors.fill: parent
                        enabled: parent.visible && !imageFailed
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            root.previewSource = url || path
                            imagePreview.open()
                        }
                    }
                }

                Text {
                    objectName: "messageAttachmentFile"
                    visible: kind !== "image"
                    anchors.fill: parent
                    anchors.margins: 10
                    text: (name || "附件") + "  ·  " + (mediaType || "文件")
                    color: root.theme.text
                    font.pixelSize: 12
                    elide: Text.ElideMiddle
                    verticalAlignment: Text.AlignVCenter
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: kind !== "image"
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.fileRequested(path)
                }

                Text {
                    visible: kind === "image" && imageFailed
                    anchors.centerIn: parent
                    text: "图片加载失败：" + (name || "附件")
                    color: root.theme.textMuted
                    font.pixelSize: 12
                }
            }
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

    Row {
        id: messageActions
        anchors.right: parent.right
        anchors.top: parent.bottom
        anchors.topMargin: 2
        spacing: 2
        visible: root.role !== "tool" && root.actionsVisible

        Button {
            id: copyMessageButton
            objectName: "copyMessageButton"
            implicitWidth: 28
            implicitHeight: 28
            text: ""
            hoverEnabled: true
            focusPolicy: Qt.StrongFocus
            Accessible.name: "复制消息"
            contentItem: Text {
                text: "⧉"
                color: root.theme.textMuted
                font.pixelSize: 18
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 8
                color: copyMessageButton.hovered ? root.theme.surfaceAlt : "transparent"
                border.width: copyMessageButton.visualFocus ? 2 : 0
                border.color: root.theme.focus
            }
            onClicked: root.copyRequested(root.markdown)
        }

        Button {
            id: playMessageButton
            objectName: "playMessageButton"
            implicitWidth: 28
            implicitHeight: 28
            text: ""
            hoverEnabled: true
            focusPolicy: Qt.StrongFocus
            Accessible.name: "播放消息"
            contentItem: Text {
                text: root.playing ? "■" : "🔊"
                color: root.theme.textMuted
                font.pixelSize: 15
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 8
                color: playMessageButton.hovered ? root.theme.surfaceAlt : "transparent"
                border.width: playMessageButton.visualFocus ? 2 : 0
                border.color: root.theme.focus
            }
            onClicked: root.playRequested(root.markdown)
        }
    }

    Popup {
        id: imagePreview
        objectName: "imagePreviewPopup"
        anchors.centerIn: Overlay.overlay
        width: Math.min(960, Overlay.overlay ? Overlay.overlay.width - 48 : 960)
        height: Math.min(720, Overlay.overlay ? Overlay.overlay.height - 48 : 720)
        padding: 12
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle {
            color: root.theme.surface
            radius: 14
            border.color: root.theme.border
        }
        Image {
            anchors.fill: parent
            source: root.previewSource
            fillMode: Image.PreserveAspectFit
            asynchronous: true
        }
        Button {
            anchors.right: parent.right
            anchors.top: parent.top
            width: 30
            height: 30
            text: "×"
            onClicked: imagePreview.close()
        }
    }
}
