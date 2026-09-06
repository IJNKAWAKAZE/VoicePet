import QtQuick

Rectangle {
    required property QtObject theme
    property string accessibleName

    radius: 14
    color: theme.surface
    border.width: 1
    border.color: theme.border
    Accessible.role: Accessible.Pane
    Accessible.name: accessibleName
}
