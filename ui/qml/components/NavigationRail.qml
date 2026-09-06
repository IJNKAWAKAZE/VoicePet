import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property QtObject theme
    required property string currentSection
    property bool compact: width <= 64
    signal navigationRequested(string section)

    color: theme.shellSurface
    border.color: theme.border
    border.width: 0

    Column {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 8

        Repeater {
            model: [
                { "section": "chat", "label": "聊天", "mark": "C" },
                { "section": "memories", "label": "记忆", "mark": "M" },
                { "section": "settings", "label": "设置", "mark": "S" }
            ]

            delegate: AppButton {
                required property var modelData
                width: root.width - 16
                height: 48
                theme: root.theme
                text: modelData.label
                kind: root.currentSection === modelData.section ? "selected" : "ghost"
                leftPadding: 6
                rightPadding: 6
                onClicked: root.navigationRequested(modelData.section)
                Accessible.name: modelData.label
                Accessible.description: root.currentSection === modelData.section
                    ? "当前页面" : "切换页面"
            }
        }
    }
}
