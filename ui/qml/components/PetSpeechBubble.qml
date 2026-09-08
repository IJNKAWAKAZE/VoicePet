import QtQuick
import QtQuick.Controls

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
    implicitHeight: Math.min(260, availableScreenHeight - 16, bubbleText.contentHeight + 28)
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
        text: root.message
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

        TextArea {
            id: bubbleText
            objectName: "petSpeechText"
            width: speechScroll.availableWidth
            text: root.message
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
