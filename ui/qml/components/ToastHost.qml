import QtQuick
import QtQuick.Controls

Column {
    id: root
    required property QtObject theme
    required property var toastModel
    signal dismissRequested(int row)

    spacing: 8
    width: 320

    Repeater {
        model: root.toastModel
        delegate: Rectangle {
            required property int index
            required property string message
            required property string kind
            width: root.width
            height: toastText.implicitHeight + 24
            radius: 14
            color: kind === "danger" ? root.theme.dangerSurface : root.theme.surface
            border.color: root.theme.border

            Text {
                id: toastText
                anchors.fill: parent
                anchors.margins: 12
                text: message
                color: root.theme.text
                wrapMode: Text.Wrap
            }

            TapHandler { onTapped: root.dismissRequested(index) }
        }
    }
}
