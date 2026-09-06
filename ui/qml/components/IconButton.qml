import QtQuick
import QtQuick.Controls

Button {
    id: control
    required property QtObject theme
    required property string accessibleName
    property url iconSource

    implicitWidth: 40
    implicitHeight: 40
    Accessible.name: accessibleName

    contentItem: Image {
        source: control.iconSource
        sourceSize.width: 20
        sourceSize.height: 20
        fillMode: Image.PreserveAspectFit
    }

    background: Rectangle {
        radius: 10
        color: control.hovered ? control.theme.surfaceAlt : "transparent"
        border.width: control.visualFocus ? 2 : 0
        border.color: control.theme.focus
    }
}
