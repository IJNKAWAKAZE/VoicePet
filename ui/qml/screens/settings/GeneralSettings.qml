import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../components"

ScrollView {
    id: root
    required property QtObject theme
    required property QtObject settings
    Connections {
        target: root.settings
        function onDraftRestored() {
            startupToggle.checked = root.settings.draft.ui.start_at_login
            motionToggle.checked = root.settings.draft.ui.reduce_motion
            hotkeyField.text = root.settings.draft.ui.global_hotkey
            hotkeyToggle.checked = root.settings.draft.ui.global_hotkey_enabled
        }
    }
    clip: true
    rightPadding: 12
    ScrollBar.vertical.active: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

    ColumnLayout {
        width: root.availableWidth
        spacing: 16

        Text {
            text: "外观与启动"
            color: root.theme.text
            font.pixelSize: 24
            font.bold: true
        }

        GridLayout {
            columns: width >= 720 ? 3 : 1
            Layout.fillWidth: true
            columnSpacing: 12
            rowSpacing: 12

            Repeater {
                model: [
                    { "id": "sunny_sea", "name": "晴海微风", "canvas": "#F6FBFF", "primary": "#4BA8D1", "accent": "#69D6C5" },
                    { "id": "deep_night", "name": "深海夜航", "canvas": "#111827", "primary": "#69C7F2", "accent": "#83DFCF" },
                    { "id": "sakura_coral", "name": "樱花珊瑚", "canvas": "#FFF8F8", "primary": "#D95E7B", "accent": "#F3A06B" }
                ]
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.preferredHeight: 128
                    radius: 14
                    color: modelData.canvas
                    border.width: root.settings.draft.ui.theme_id === modelData.id ? 3 : 1
                    border.color: modelData.primary

                    Rectangle {
                        anchors.left: parent.left
                        anchors.bottom: parent.bottom
                        anchors.margins: 14
                        width: 58
                        height: 24
                        radius: 10
                        color: modelData.primary
                    }
                    Rectangle {
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 14
                        width: 28
                        height: 28
                        radius: 14
                        color: modelData.accent
                    }
                    Text {
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.margins: 14
                        text: modelData.name
                        color: modelData.id === "deep_night" ? "#F3F7FB" : "#44313E"
                        font.bold: true
                    }
                    TapHandler {
                        onTapped: root.theme.set_theme(modelData.id)
                    }
                }
            }
        }

        AppToggle {
            id: startupToggle
            theme: root.theme
            text: "开机时启动 VoicePet"
            checked: root.settings.draft.ui.start_at_login
            onToggled: root.settings.set_field("ui", "start_at_login", checked)
        }
        AppToggle {
            id: motionToggle
            theme: root.theme
            text: "减少动态效果"
            checked: root.settings.draft.ui.reduce_motion
            onToggled: root.theme.set_reduce_motion(checked)
        }
        FieldLabel { theme: root.theme; text: "全局快捷键" }
        AppTextField {
            id: hotkeyField
            Layout.preferredWidth: 320
            theme: root.theme
            text: root.settings.draft.ui.global_hotkey
            placeholderText: "全局快捷键"
            onEditingFinished: root.settings.set_field("ui", "global_hotkey", text)
        }
        AppToggle {
            id: hotkeyToggle
            theme: root.theme
            text: "启用全局快捷键"
            checked: root.settings.draft.ui.global_hotkey_enabled
            onToggled: root.settings.set_field("ui", "global_hotkey_enabled", checked)
        }
    }
}
