import QtQuick
import QtQuick.Controls

Control {
    id: control
    required property QtObject theme
    property string text
    property string kind: "info"

    implicitWidth: label.implicitWidth + 16
    implicitHeight: 26
    Accessible.name: text

    contentItem: Text {
        id: label
        text: control.text
        color: control.kind === "danger" ? control.theme.danger
            : control.kind === "success" ? control.theme.success
            : control.kind === "warning" ? control.theme.warning
            : control.theme.info
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        font.pixelSize: 12
        font.family: "Microsoft YaHei UI"
    }

    background: Rectangle {
        radius: 10
        color: control.kind === "danger" ? control.theme.dangerSurface : control.theme.surfaceAlt
        border.width: 1
        border.color: control.theme.border
    }
}
