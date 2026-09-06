import QtQuick
import QtQuick.Controls

Slider {
    id: control
    required property QtObject theme

    implicitHeight: 36
    Accessible.name: objectName

    background: Rectangle {
        x: control.leftPadding
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: control.availableWidth
        height: 4
        radius: 2
        color: control.theme.border

        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: 2
            color: control.theme.interactionPrimary
        }
    }

    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: 18
        height: 18
        radius: 9
        color: control.theme.surface
        border.width: control.activeFocus ? 3 : 2
        border.color: control.activeFocus ? control.theme.focus : control.theme.interactionPrimary
    }
}
