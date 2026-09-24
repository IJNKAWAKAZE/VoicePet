import QtQuick
import QtQuick.Controls
import "../js/markdown.js" as MarkdownBlocks

Rectangle {
    id: root
    required property QtObject theme
    property string message
    property int timeoutMs: 8000
    property real availableScreenWidth: 1280
    property real availableScreenHeight: 720
    signal dismissRequested

    visible: message.length > 0
    implicitWidth: Math.min(320, availableScreenWidth - 32, Math.max(120, messageMetrics.advanceWidth + 28))
    implicitHeight: Math.min(260, availableScreenHeight - 16, speechColumn.implicitHeight + 28)
    width: implicitWidth
    height: implicitHeight
    radius: 16
    color: theme.surface
    border.color: theme.border
    clip: true
    onMessageChanged: {
        if (message.length > 0 && !hover.hovered)
            hideTimer.restart()
        else if (message.length === 0)
            hideTimer.stop()
    }

    // 短回复按内容收窄，长回复限制宽度并自然换行
    TextMetrics {
        id: messageMetrics
        // 只量开头一小段：长回复会被钳到最大宽度，没必要为测宽对全文排一次版
        text: root.message.slice(0, 80)
        font.pixelSize: 14
        font.family: "Microsoft YaHei UI"
    }

    ScrollView {
        id: speechScroll
        objectName: "petSpeechScroll"
        anchors.fill: parent
        anchors.margins: 10
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical: ScrollBar {
            objectName: "petSpeechScrollBar"
            policy: ScrollBar.AsNeeded
        }

        // 代码块必须交给纯文本控件换行，Markdown 控件里的代码行不会按宽度折行
        Column {
            id: speechColumn
            width: speechScroll.availableWidth
            spacing: 4

            Repeater {
                model: MarkdownBlocks.splitBlocks(root.message)

                delegate: Item {
                    required property var modelData
                    width: speechColumn.width
                    implicitHeight: modelData.kind === "code" ? speechCode.implicitHeight : bubbleText.implicitHeight
                    height: implicitHeight

                    TextArea {
                        id: bubbleText
                        objectName: "petSpeechText"
                        visible: modelData.kind !== "code"
                        width: parent.width
                        height: implicitHeight
                        text: modelData.text
                        textFormat: TextEdit.MarkdownText
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        color: root.theme.text
                        font.pixelSize: 14
                        font.family: "Microsoft YaHei UI"
                        leftPadding: 4
                        rightPadding: 4
                        topPadding: 4
                        bottomPadding: 4
                        background: null
                    }

                    Rectangle {
                        id: speechCode
                        objectName: "petSpeechCodeBlock"
                        visible: modelData.kind === "code"
                        width: parent.width
                        implicitHeight: speechCodeText.implicitHeight + 12
                        height: implicitHeight
                        radius: 6
                        color: root.theme.codeBlock

                        TextEdit {
                            id: speechCodeText
                            objectName: "petSpeechCodeText"
                            x: 6
                            y: 6
                            width: parent.width - 12
                            height: implicitHeight
                            text: modelData.text
                            textFormat: TextEdit.PlainText
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.Wrap
                            color: root.theme.text
                            font.pixelSize: 13
                            font.family: "Consolas"
                        }
                    }
                }
            }
        }
    }

    HoverHandler {
        id: hover
        onHoveredChanged: {
            if (hovered)
                hideTimer.stop()
            else if (root.message.length > 0)
                hideTimer.restart()
        }
    }
    Timer {
        id: hideTimer
        interval: root.timeoutMs
        onTriggered: root.dismissRequested()
    }
}
