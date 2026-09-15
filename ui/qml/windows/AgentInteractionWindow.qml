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
    width: 460
    // 详情长度随内容变化，长命令只滚动不截断
    height: Math.max(210, layout.implicitHeight + 36)
    visible: false
    flags: Qt.Dialog | Qt.WindowStaysOnTopHint
    title: "Agent 需要你的选择"
    color: theme.canvas

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: 18
        spacing: 10
        Text { Layout.fillWidth: true; text: window.request.title || "需要选择"; color: theme.text; font.pixelSize: 18; font.bold: true; wrapMode: Text.Wrap }
        Rectangle {
            objectName: "agentRequestDetailBox"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(260, Math.max(28, detailText.implicitHeight + 16))
            color: theme.surface
            radius: 10
            border.color: theme.border
            Flickable {
                id: detailFlick
                anchors.fill: parent
                anchors.margins: 8
                clip: true
                contentWidth: width
                contentHeight: detailText.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                Text {
                    id: detailText
                    objectName: "agentRequestDetail"
                    width: detailFlick.width
                    text: window.request.message || ""
                    color: window.theme.text
                    font.pixelSize: 13
                    font.family: "Microsoft YaHei UI"
                    wrapMode: Text.WrapAnywhere
                }
            }
        }
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
