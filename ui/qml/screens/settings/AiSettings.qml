import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../components"

ScrollView {
    id: root
    required property QtObject theme
    required property QtObject settings
    required property QtObject diagnostics
    property bool componentReady: false
    Component.onCompleted: componentReady = true
    Connections {
        target: root.settings
        function onDraftRestored() {
            apiSelector.currentIndex = Qt.binding(function() {
                return apiSelector.model.indexOf(root.settings.draft.llm.api)
            })
            baseUrlField.text = root.settings.draft.llm.base_url
            modelField.text = root.settings.draft.llm.model
            reasoningEffortSelector.currentIndex = Qt.binding(function() {
                return reasoningEffortSelector.values.indexOf(root.settings.draft.llm.reasoning_effort)
            })
            rolePrompt.text = root.settings.draft.llm.system_prompt
            apiKeyField.clear()
        }
    }
    clip: true
    rightPadding: 12
    ScrollBar.vertical.active: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

    ColumnLayout {
        width: root.availableWidth
        spacing: 14
        Text { text: "AI 服务"; color: root.theme.text; font.pixelSize: 24; font.bold: true }
        FieldLabel { theme: root.theme; text: "Codex Agent" }
        FieldLabel { theme: root.theme; text: "API 协议" }
        AppComboBox {
            id: apiSelector
            Layout.preferredWidth: 360
            theme: root.theme
            model: ["responses", "chat_completions"]
            currentIndex: model.indexOf(root.settings.draft.llm.api)
            onActivated: root.settings.set_field("llm", "api", currentText)
        }
        FieldLabel { theme: root.theme; text: "API 地址（留空使用官方地址）" }
        AppTextField {
            id: baseUrlField
            Layout.preferredWidth: Math.min(480, root.availableWidth)
            theme: root.theme
            text: root.settings.draft.llm.base_url
            placeholderText: "API Base URL，留空使用官方地址"
            onTextEdited: root.settings.set_field("llm", "base_url", text)
        }
        FieldLabel { theme: root.theme; text: "模型名称" }
        AppTextField {
            id: modelField
            Layout.preferredWidth: Math.min(360, root.availableWidth)
            theme: root.theme
            text: root.settings.draft.llm.model
            placeholderText: "模型名称"
            onTextEdited: root.settings.set_field("llm", "model", text)
        }
        FieldLabel { theme: root.theme; text: "思考强度" }
        AppComboBox {
            id: reasoningEffortSelector
            objectName: "reasoningEffortSelector"
            Layout.preferredWidth: 360
            theme: root.theme
            model: ["自动", "最小", "低", "中", "高", "极高"]
            property var values: ["auto", "minimal", "low", "medium", "high", "xhigh"]
            currentIndex: Math.max(0, values.indexOf(root.settings.draft.llm.reasoning_effort))
            onActivated: root.settings.set_field("llm", "reasoning_effort", values[currentIndex])
        }
        Text {
            text: "API Key"
            color: root.theme.text
            font.pixelSize: 14
            font.bold: true
        }
        AppTextField {
            id: apiKeyField
            objectName: "apiKeyField"
            Layout.preferredWidth: Math.min(480, root.availableWidth)
            theme: root.theme
            echoMode: TextInput.Password
            placeholderText: "输入后加密保存，不会写入 config.json"
        }
        RowLayout {
            AppButton {
                theme: root.theme
                text: "加密保存"
                kind: "secondary"
                onClicked: {
                    root.settings.save_api_key(apiKeyField.text)
                    apiKeyField.clear()
                }
            }
            AppButton {
                theme: root.theme
                text: "移除密钥"
                kind: "ghost"
                onClicked: root.settings.delete_api_key()
            }
        }
        FieldLabel { theme: root.theme; text: "角色设定" }
        ScrollView {
            objectName: "rolePromptScroll"
            Layout.fillWidth: true
            Layout.preferredHeight: 180
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            AppTextArea {
                id: rolePrompt
                objectName: "rolePromptEditor"
                theme: root.theme
                text: root.settings.draft.llm.system_prompt
                placeholderText: "角色设定"
                onTextChanged: {
                    if (root.componentReady && text !== root.settings.draft.llm.system_prompt)
                        root.settings.set_field("llm", "system_prompt", text)
                }
            }
        }
        Text {
            objectName: "aiConnectionStatus"
            Layout.fillWidth: true
            text: root.diagnostics.connectionStatus
            visible: text.length > 0
            color: root.theme.text
            wrapMode: Text.Wrap
        }
        RowLayout {
            AppButton {
                theme: root.theme
                objectName: "testAiConnectionButton"
                text: root.diagnostics.connectionRunning ? "正在测试连接…" : "测试连接"
                kind: "secondary"
                enabled: !root.diagnostics.connectionRunning
                onClicked: root.diagnostics.test_connection()
            }
        }
        Text {
            Layout.fillWidth: true
            text: "测试当前运行配置；修改 AI 设置后请先保存并重启，再测试新配置"
            color: root.theme.textMuted
            wrapMode: Text.Wrap
        }
    }
}
