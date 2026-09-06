import QtQuick
import QtQuick.Controls

TextArea {
    id: control
    required property QtObject theme

    color: enabled ? theme.text : theme.disabledText
    placeholderTextColor: theme.textMuted
    selectionColor: theme.primary
    selectedTextColor: theme.inverseText
    wrapMode: TextEdit.Wrap
    font.pixelSize: 14
    font.family: "Microsoft YaHei UI"
    Accessible.name: placeholderText

    background: Rectangle {
        radius: 10
        color: control.theme.surface
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? control.theme.focus : control.theme.border
    }
}
