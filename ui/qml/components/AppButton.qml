import QtQuick
import QtQuick.Controls

Button {
    id: control
    required property QtObject theme
    property url iconSource
    property string kind: "primary"
    property color backgroundColor: {
        if (kind === "danger")
            return down ? Qt.darker(theme.danger, 1.3)
                : hovered ? Qt.darker(theme.danger, 1.12) : theme.danger
        if (kind === "primary")
            return down ? theme.primaryPressed
                : hovered ? theme.primaryHover : theme.interactionPrimary
        if (kind === "selected")
            return down ? theme.primaryPressed
                : hovered ? theme.surfaceAlt : theme.userBubble
        if (down)
            return theme.userBubble
        if (hovered)
            return theme.surfaceAlt
        return kind === "ghost" ? "transparent" : theme.surface
    }
    property color foregroundColor: enabled
        ? (kind === "primary" || kind === "danger" ? theme.inverseText
            : kind === "selected" ? (down ? theme.inverseText : theme.selectedText)
            : theme.text)
        : theme.disabledText
    property int focusBorderWidth: activeFocus ? 2 : 0

    implicitHeight: 40
    hoverEnabled: true
    opacity: enabled ? 1.0 : 0.5
    leftPadding: 16
    rightPadding: 16
    Accessible.name: text

    contentItem: Text {
        text: control.text
        color: control.foregroundColor
        font.pixelSize: 14
        font.family: "Microsoft YaHei UI"
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }

    background: Rectangle {
        radius: 10
        color: control.backgroundColor
        border.width: control.focusBorderWidth > 0 ? control.focusBorderWidth
            : control.kind === "ghost" && !control.hovered && !control.down ? 0 : 1
        border.color: control.activeFocus || control.hovered || control.down
            ? control.theme.focus : control.theme.border
        Behavior on color {
            ColorAnimation { duration: control.theme.reduceMotion ? 0 : control.theme.motionStandard }
        }
    }
}
