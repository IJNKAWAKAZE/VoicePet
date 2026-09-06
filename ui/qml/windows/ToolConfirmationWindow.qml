import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

ApplicationWindow {
    id: confirmationWindow
    objectName: "toolConfirmationWindow"
    required property QtObject theme
    required property QtObject dialogs
    property var request: dialogs.currentConfirmation

    width: 500
    height: request.showDetails === false ? 180 : 330
    minimumWidth: 420
    minimumHeight: request.showDetails === false ? 180 : 280
    visible: false
    transientParent: null
    title: "VoicePet 操作确认"
    color: theme.canvas
    flags: Qt.Dialog | Qt.WindowStaysOnTopHint

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 12
        Text { Layout.fillWidth: true; text: confirmationWindow.request.title || "需要确认"; color: confirmationWindow.theme.text; font.family: "Microsoft YaHei UI"; font.pixelSize: 22; font.bold: true; wrapMode: Text.Wrap }
        Text { Layout.fillWidth: true; text: confirmationWindow.request.summary || ""; color: confirmationWindow.theme.text; font.family: "Microsoft YaHei UI"; wrapMode: Text.Wrap }
        Text {
            objectName: "confirmationImpact"
            Layout.fillWidth: true
            visible: confirmationWindow.request.showDetails !== false
            text: confirmationWindow.request.impact || ""
            color: confirmationWindow.theme.textMuted
            font.family: "Microsoft YaHei UI"
            wrapMode: Text.Wrap
        }
        Text {
            objectName: "confirmationRisk"
            Layout.fillWidth: true
            visible: confirmationWindow.request.showDetails !== false
            text: (confirmationWindow.request.reversible ? "可以撤销" : "不可撤销")
                + " · 风险：" + (confirmationWindow.request.risk || "未知")
            color: confirmationWindow.request.reversible ? confirmationWindow.theme.textMuted : confirmationWindow.theme.danger
            font.family: "Microsoft YaHei UI"
            wrapMode: Text.Wrap
        }
        Item { Layout.fillHeight: true }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            AppButton {
                theme: confirmationWindow.theme
                text: "取消"
                kind: "secondary"
                focus: true
                onClicked: {
                    confirmationWindow.dialogs.resolve_confirmation(confirmationWindow.request.requestId, false)
                    confirmationWindow.hide()
                }
            }
            AppButton {
                objectName: "confirmationApprove"
                theme: confirmationWindow.theme
                text: confirmationWindow.request.confirmLabel || "确认"
                kind: "danger"
                onClicked: {
                    confirmationWindow.dialogs.resolve_confirmation(confirmationWindow.request.requestId, true)
                    confirmationWindow.hide()
                }
            }
        }
    }

    Shortcut {
        sequence: "Escape"
        onActivated: {
            confirmationWindow.dialogs.resolve_confirmation(confirmationWindow.request.requestId, false)
            confirmationWindow.hide()
        }
    }

    Connections {
        target: confirmationWindow.dialogs
        function onConfirmationChanged() {
            if (!confirmationWindow.request.requestId)
                confirmationWindow.hide()
        }
    }
}
