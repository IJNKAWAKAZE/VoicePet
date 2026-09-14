import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../../components"

ScrollView {
    id: root
    required property QtObject theme
    required property QtObject settings
    required property QtObject memories
    required property QtObject chat
    Connections {
        target: root.settings
        function onDraftRestored() {
            memoryEnabled.checked = root.settings.draft.privacy.memory_enabled
            autoMemoryEnabled.checked = root.settings.draft.privacy.auto_memory_enabled
            chatHistoryEnabled.checked = root.settings.draft.privacy.chat_history_enabled
            summaryRetentionField.text = root.settings.draft.privacy.summary_retention_days
            chatRetentionField.text = root.settings.draft.privacy.chat_retention_days
            recordingEnabled.checked = root.settings.draft.privacy.diagnostic_recording
        }
    }
    clip: true
    rightPadding: 12
    ScrollBar.vertical.active: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

    FileDialog {
        id: exportDialog
        objectName: "memoriesExportDialog"
        title: "导出长期记忆"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "json"
        nameFilters: ["JSON 文件 (*.json)"]
        onAccepted: root.memories.export_url(selectedFile)
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: 14
        Text { text: "隐私与本地数据"; color: root.theme.text; font.pixelSize: 24; font.bold: true }
        AppToggle {
            id: memoryEnabled
            objectName: "memoryEnabled"
            theme: root.theme
            text: "使用记忆"
            checked: root.settings.draft.privacy.memory_enabled
            onToggled: root.settings.set_field("privacy", "memory_enabled", checked)
        }
        AppToggle {
            id: chatHistoryEnabled
            objectName: "chatHistoryEnabled"
            theme: root.theme
            text: "保存聊天历史"
            checked: root.settings.draft.privacy.chat_history_enabled
            onToggled: root.settings.set_field("privacy", "chat_history_enabled", checked)
        }
        AppToggle {
            id: autoMemoryEnabled
            objectName: "autoMemoryEnabled"
            theme: root.theme
            text: "自动整理摘要与稳定偏好"
            enabled: memoryEnabled.checked && chatHistoryEnabled.checked
            checked: root.settings.draft.privacy.auto_memory_enabled
            onToggled: root.settings.set_field("privacy", "auto_memory_enabled", checked)
        }
        Text {
            Layout.fillWidth: true
            text: "数据保存在本机；相关内容会发送至你配置的 AI 服务，后台整理会产生额外用量。关闭使用记忆后仍可在记忆页管理已存数据"
            color: root.theme.textMuted
            wrapMode: Text.Wrap
        }
        FieldLabel { theme: root.theme; text: "聊天记录保留天数（1–365 天）" }
        AppTextField {
            id: chatRetentionField
            Layout.preferredWidth: 240
            theme: root.theme
            text: root.settings.draft.privacy.chat_retention_days
            placeholderText: "聊天记录保留天数"
            inputMethodHints: Qt.ImhDigitsOnly
            onTextEdited: root.settings.set_field("privacy", "chat_retention_days", text === "" ? "" : Number(text))
        }
        FieldLabel { theme: root.theme; text: "近期摘要保留天数（1–365 天）" }
        AppTextField {
            id: summaryRetentionField
            Layout.preferredWidth: 240
            theme: root.theme
            // 编辑中保留空值和光标位置，放弃时由 draftRestored 恢复
            text: root.settings.draft.privacy.summary_retention_days
            placeholderText: "近期摘要保留天数"
            inputMethodHints: Qt.ImhDigitsOnly
            onTextEdited: root.settings.set_field("privacy", "summary_retention_days", text === "" ? "" : Number(text))
        }
        AppToggle {
            id: recordingEnabled
            theme: root.theme
            text: "允许诊断录音"
            checked: root.settings.draft.privacy.diagnostic_recording
            onToggled: root.settings.set_field("privacy", "diagnostic_recording", checked)
        }
        RowLayout {
            AppButton { theme: root.theme; text: "导出长期记忆"; kind: "secondary"; onClicked: exportDialog.open() }
            AppButton { theme: root.theme; text: "清空会话历史"; kind: "danger"; onClicked: root.chat.request_clear_sessions() }
        }
        Text { Layout.fillWidth: true; text: "清空会话历史会删除会话和相关摘要，保留长期记忆、设置和形象"; color: root.theme.textMuted; wrapMode: Text.Wrap }
        Text { Layout.fillWidth: true; text: root.memories.error; visible: text.length > 0; color: root.theme.danger; wrapMode: Text.Wrap }
    }
}
