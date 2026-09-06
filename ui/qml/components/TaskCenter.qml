import QtQuick
import QtQuick.Controls

Column {
    id: root
    required property QtObject theme
    required property var taskModel
    signal cancelRequested(string taskId)

    spacing: 8
    width: 320

    Repeater {
        model: root.taskModel
        delegate: Rectangle {
            required property string taskId
            required property string title
            required property real progress
            required property bool cancellable
            width: root.width
            height: 64
            radius: 14
            color: root.theme.surface
            border.color: root.theme.border

            Text {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.margins: 12
                text: title
                color: root.theme.text
            }
            ProgressBar {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 12
                from: 0
                to: 1
                value: progress < 0 ? 0 : progress
                indeterminate: progress < 0
            }
            Button {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 8
                visible: cancellable
                text: "取消"
                flat: true
                onClicked: root.cancelRequested(taskId)
            }
        }
    }
}
