import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: root
    required property QtObject theme
    required property var request
    signal resolved(string requestId, bool approved)

    modal: true
    focus: true
    closePolicy: Popup.NoAutoClose
    width: Math.min(520, parent ? parent.width - 48 : 520)
    padding: 24

    background: Rectangle {
        radius: 16
        color: root.theme.surface
        border.color: root.theme.border
    }

    contentItem: ColumnLayout {
        spacing: 12
        Text { Layout.fillWidth: true; text: root.request.title || "需要确认"; color: root.theme.text; font.family: "Microsoft YaHei UI"; font.pixelSize: 20; font.bold: true; wrapMode: Text.Wrap }
        Text { Layout.fillWidth: true; text: root.request.summary || ""; color: root.theme.text; font.family: "Microsoft YaHei UI"; wrapMode: Text.Wrap }
        Text {
            objectName: "confirmationImpact"
            Layout.fillWidth: true
            visible: root.request.showDetails !== false
            text: root.request.impact || ""
            color: root.theme.textMuted
            font.family: "Microsoft YaHei UI"
            wrapMode: Text.Wrap
        }
        Text {
            objectName: "confirmationRisk"
            Layout.fillWidth: true
            visible: root.request.showDetails !== false
            text: (root.request.reversible ? "可以撤销" : "不可撤销") + " · 风险：" + (root.request.risk || "未知")
            color: root.request.reversible ? root.theme.textMuted : root.theme.danger
            font.family: "Microsoft YaHei UI"
            wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            AppButton {
                theme: root.theme
                text: "取消"
                kind: "secondary"
                focus: true
                onClicked: root.resolved(root.request.requestId, false)
            }
            AppButton {
                objectName: "confirmationApprove"
                theme: root.theme
                text: root.request.confirmLabel || "确认"
                kind: "danger"
                onClicked: root.resolved(root.request.requestId, true)
            }
        }
    }

    Keys.onEscapePressed: event => {
        root.resolved(root.request.requestId, false)
        event.accepted = true
    }
}
