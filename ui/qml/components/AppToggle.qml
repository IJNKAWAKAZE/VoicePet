import QtQuick
import QtQuick.Controls

Switch {
    id: control
    required property QtObject theme

    implicitHeight: 38
    Accessible.name: text

    indicator: Rectangle {
        implicitWidth: 44
        implicitHeight: 24
        x: control.leftPadding
        y: parent.height / 2 - height / 2
        radius: 12
        color: control.checked ? control.theme.interactionPrimary : control.theme.surfaceAlt
        border.color: control.activeFocus ? control.theme.focus : control.theme.border
        border.width: control.activeFocus ? 2 : 1

        Rectangle {
            x: control.checked ? parent.width - width - 3 : 3
            y: 3
            width: 18
            height: 18
            radius: 9
            color: control.checked ? control.theme.inverseText : control.theme.textMuted
            Behavior on x {
                NumberAnimation { duration: control.theme.reduceMotion ? 0 : control.theme.motionFast }
            }
        }
    }

    contentItem: Text {
        leftPadding: control.indicator.width + control.spacing
        text: control.text
        color: control.enabled ? control.theme.text : control.theme.disabledText
        verticalAlignment: Text.AlignVCenter
        font.pixelSize: 14
        font.family: "Microsoft YaHei UI"
    }
}
