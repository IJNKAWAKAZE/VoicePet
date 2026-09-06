import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../components"

ScrollView {
    id: root
    required property QtObject theme
    required property QtObject settings
    property string version: "0.1.0"
    property string projectHomepage: "VoicePet"
    property string licenseName: "项目许可见发行包"
    readonly property string dataDirectory: root.settings.dataDirectory

    ColumnLayout {
        width: root.availableWidth
        spacing: 14
        Text { text: "关于 VoicePet"; color: root.theme.text; font.pixelSize: 24; font.bold: true }
        Text { text: "版本 " + root.version; color: root.theme.text }
        Text { text: root.projectHomepage; color: root.theme.textMuted }
        Text { text: root.licenseName; color: root.theme.textMuted }
        Text { Layout.fillWidth: true; text: root.dataDirectory; color: root.theme.textMuted; wrapMode: Text.Wrap }
        AppButton {
            theme: root.theme
            text: "打开数据目录"
            kind: "secondary"
            onClicked: root.settings.open_data_directory()
        }
    }
}
