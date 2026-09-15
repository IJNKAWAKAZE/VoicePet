import QtQuick
import QtQuick.Controls

ListView {
    id: root
    required property QtObject theme
    required property var sessionModel
    signal sessionRequested(string sessionId)
    signal deleteRequested(string sessionId)

    model: sessionModel
    spacing: 6
    clip: true

    delegate: Item {
        id: sessionRow
        required property string sessionId
        required property string title
        required property int turnCount
        required property bool active
        required property bool running
        required property string updatedLabel
        width: root.width
        height: 66
        Rectangle {
            anchors.fill: parent
            radius: 10
            color: active || sessionMouse.pressed ? root.theme.userBubble
                : sessionMouse.containsMouse ? root.theme.surfaceAlt : root.theme.surface
            border.width: active ? 2 : 1
            border.color: active ? root.theme.interactionPrimary : root.theme.border
        }
        Column {
            anchors.left: parent.left
            anchors.leftMargin: 12
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - 66
            spacing: 2
            Row {
                spacing: 6
                width: parent.width
                Text {
                    // 运行标识和标题同行，给下面的时间留出完整宽度
                    width: Math.max(0, parent.width - (sessionRow.running
                        ? runningRowLabel.implicitWidth + 18 : 0))
                    text: title
                    color: root.theme.text
                    font.pixelSize: 13
                    font.family: "Microsoft YaHei UI"
                    elide: Text.ElideRight
                }
                Rectangle {
                    objectName: "sessionRunningDot"
                    visible: sessionRow.running
                    anchors.verticalCenter: parent.verticalCenter
                    width: 6
                    height: 6
                    radius: 3
                    color: root.theme.warning
                }
                Text {
                    id: runningRowLabel
                    objectName: "sessionRunningLabel"
                    visible: sessionRow.running
                    anchors.verticalCenter: parent.verticalCenter
                    text: "运行中"
                    color: root.theme.warning
                    font.pixelSize: 11
                    font.family: "Microsoft YaHei UI"
                }
            }
            Text {
                objectName: "sessionUpdatedLabel"
                width: parent.width
                text: updatedLabel + " · " + turnCount + " 轮对话"
                elide: Text.ElideRight
                color: root.theme.textMuted
                font.pixelSize: 11
                font.family: "Microsoft YaHei UI"
            }
        }
        MouseArea {
            id: sessionMouse
            anchors.fill: parent
            anchors.rightMargin: 56
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.sessionRequested(sessionRow.sessionId)
        }
        AppButton {
            property string accessibleLabel: "删除会话"
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.rightMargin: 6
            width: 44
            height: 32
            leftPadding: 2
            rightPadding: 2
            theme: root.theme
            text: "×"
            Accessible.name: accessibleLabel
            ToolTip.visible: hovered
            ToolTip.text: accessibleLabel
            kind: "ghost"
            onClicked: root.deleteRequested(sessionRow.sessionId)
        }
        Accessible.description: (updatedLabel ? updatedLabel + "，" : "")
            + turnCount + " 轮对话" + (running ? "，运行中" : "")
    }
}
