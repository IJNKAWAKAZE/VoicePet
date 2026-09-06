import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "settings"
import "../components"

Item {
    id: root
    required property QtObject theme
    required property QtObject settings
    required property QtObject memories
    required property QtObject chat
    property QtObject pets: null
    property QtObject diagnostics: null
    property QtObject animationModel: null
    property string category: "general"
    Component.onCompleted: root.settings.set_status_context(visible ? category : "")
    onCategoryChanged: root.settings.set_status_context(visible ? category : "")
    onVisibleChanged: root.settings.set_status_context(visible ? category : "")

    RowLayout {
        anchors.fill: parent
        anchors.margins: 20
        anchors.bottomMargin: 76
        spacing: 20

        ListView {
            Layout.preferredWidth: 116
            Layout.fillHeight: true
            spacing: 6
            clip: true
            model: [
                { "value": "general", "label": "常规" },
                { "value": "voice", "label": "语音" },
                { "value": "ai", "label": "AI 服务" },
                { "value": "pets", "label": "桌宠" },
                { "value": "privacy", "label": "隐私" },
                { "value": "diagnostics", "label": "诊断" },
                { "value": "about", "label": "关于" }
            ]
            delegate: AppButton {
                required property var modelData
                width: ListView.view.width
                height: 42
                theme: root.theme
                text: modelData.label
                kind: root.category === modelData.value ? "selected" : "ghost"
                onClicked: root.category = modelData.value
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: ["general", "voice", "ai", "pets", "privacy", "diagnostics", "about"]
                .indexOf(root.category)

            GeneralSettings { theme: root.theme; settings: root.settings }
            VoiceSettings { theme: root.theme; settings: root.settings }
            AiSettings {
                theme: root.theme
                settings: root.settings
                diagnostics: root.diagnostics
            }
            PetSettings {
                theme: root.theme
                settings: root.settings
                pets: root.pets
                animationModel: root.animationModel
            }
            PrivacySettings { theme: root.theme; settings: root.settings; memories: root.memories; chat: root.chat }
            DiagnosticsSettings { theme: root.theme; diagnostics: root.diagnostics }
            AboutSettings { theme: root.theme; settings: root.settings }
        }
    }

    RowLayout {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 20
        spacing: 10
        Text {
            objectName: "settingsStatusMessage"
            Layout.fillWidth: true
            text: root.settings.statusMessage
            color: root.theme.textMuted
            elide: Text.ElideRight
        }
        AppButton {
            objectName: "discardSettingsButton"
            theme: root.theme
            text: "放弃未保存更改"
            enabled: root.settings.hasDraftChanges
            ToolTip.visible: hovered
            ToolTip.text: "仅放弃未保存更改；主题、启动与桌宠设置即时生效"
            kind: "ghost"
            onClicked: root.settings.discard_draft()
        }
        AppButton {
            objectName: "saveSettingsButton"
            theme: root.theme
            text: "保存设置"
            onClicked: root.settings.save_draft()
        }
    }
}
