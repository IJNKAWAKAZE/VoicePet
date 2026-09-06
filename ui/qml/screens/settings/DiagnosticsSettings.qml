import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../../components"

Item {
    id: root
    required property QtObject theme
    required property QtObject diagnostics

    FileDialog {
        id: exportDialog
        objectName: "diagnosticsExportDialog"
        title: "导出脱敏诊断包"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "zip"
        nameFilters: ["诊断包 (*.zip)"]
        onAccepted: root.diagnostics.export_url(selectedFile)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 14
        Text {
            objectName: "diagnosticsHeading"
            text: "组件诊断"
            color: root.theme.text
            font.pixelSize: 24
            font.bold: true
            Layout.minimumHeight: implicitHeight
        }
        GridView {
            id: resultGrid
            Layout.fillWidth: true
            Layout.fillHeight: true
            objectName: "diagnosticsGrid"
            clip: true
            ScrollBar.vertical: ScrollBar { active: true }
            cellWidth: width / Math.max(1, Math.floor(width / 220))
            cellHeight: 156
            model: root.diagnostics.diagnosticModel
            delegate: AppCard {
                required property string component
                required property string status
                required property string message
                required property int durationMs
                required property bool retryable
                width: resultGrid.cellWidth - 10
                height: resultGrid.cellHeight - 10
                theme: root.theme
                accessibleName: component + "诊断"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 6
                    Text { text: component; color: root.theme.text; font.bold: true }
                    StatusBadge { theme: root.theme; text: status; kind: status === "failed" ? "danger" : status === "degraded" ? "warning" : "success" }
                    Text { width: parent.width; text: message + " · " + durationMs + "ms"; color: root.theme.textMuted; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight }
                }
                TapHandler { enabled: retryable; onTapped: root.diagnostics.retry(component) }
            }
        }
        RowLayout {
            AppButton { theme: root.theme; text: root.diagnostics.running ? "检查中…" : "全部运行"; enabled: !root.diagnostics.running; onClicked: root.diagnostics.run_all() }
            AppButton { theme: root.theme; text: "导出脱敏诊断包"; kind: "secondary"; onClicked: exportDialog.open() }
        }
        Text { Layout.fillWidth: true; text: root.diagnostics.statusMessage; visible: text.length > 0; color: root.theme.textMuted; wrapMode: Text.Wrap }
    }
}
