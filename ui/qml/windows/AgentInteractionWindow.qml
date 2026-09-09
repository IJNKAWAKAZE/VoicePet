import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

ApplicationWindow {
    id: window
    objectName: "agentInteractionWindow"
    required property QtObject theme
    required property QtObject dialogs
    property var request: dialogs.currentAgentInteraction
    width: 420
    height: request.kind === "approval" ? 190 : 300
    visible: false
    flags: Qt.Dialog | Qt.WindowStaysOnTopHint
    title: "Agent 需要你的选择"
    color: theme.canvas

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 10
        Text { Layout.fillWidth: true; text: window.request.title || "需要选择"; color: theme.text; font.pixelSize: 18; font.bold: true; wrapMode: Text.Wrap }
        Text { Layout.fillWidth: true; text: window.request.message || ""; color: theme.text; wrapMode: Text.Wrap }
        Repeater {
            model: window.request.options || []
            delegate: AppButton {
                required property var modelData
                Layout.fillWidth: true
                theme: window.theme
                text: modelData.label || modelData.value || "选择"
                onClicked: {
                    window.dialogs.resolve_agent_interaction(String(window.request.requestId), String(modelData.value || modelData.label))
                    window.hide()
                }
            }
        }
        Item { Layout.fillHeight: true }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            AppButton {
                theme: window.theme
                kind: "secondary"
                text: "取消"
                onClicked: {
                    window.dialogs.resolve_agent_interaction(String(window.request.requestId), "cancel")
                    window.hide()
                }
            }
            AppButton {
                visible: window.request.kind === "approval"
                theme: window.theme
                text: "允许"
                onClicked: {
                    window.dialogs.resolve_agent_interaction(String(window.request.requestId), "accept")
                    window.hide()
                }
            }
        }
    }

    Connections {
        target: window.dialogs
        function onAgentInteractionChanged() {
            if (!window.request.requestId)
                window.hide()
            else
                window.show()
        }
    }
}
