import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../components"

ScrollView {
    id: root
    required property QtObject theme
    required property QtObject settings
    property bool componentReady: false
    Connections {
        target: root.settings
        function onDraftRestored() {
            wakeEnabled.checked = root.settings.draft.wake_word.enabled
            keywordField.text = root.settings.draft.wake_word.keyword
            sensitivity.value = root.settings.draft.wake_word.sensitivity
            ttsEnabled.checked = root.settings.draft.tts.enabled
            manualTtsEnabled.checked = root.settings.draft.tts.manual_input_enabled
            asrSelector.currentIndex = Qt.binding(function() {
                return asrSelector.model.indexOf(root.settings.draft.asr.model)
            })
            voiceSelector.currentIndex = Qt.binding(function() {
                return voiceSelector.selectedIndex()
            })
        }
    }
    clip: true
    rightPadding: 12
    ScrollBar.vertical.active: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    Component.onCompleted: {
        componentReady = true
        if (visible)
            root.settings.refresh_voices()
    }
    onVisibleChanged: {
        if (componentReady && visible)
            root.settings.refresh_voices()
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: 14
        Text { text: "语音与唤醒"; color: root.theme.text; font.pixelSize: 24; font.bold: true }
        AppToggle {
            id: wakeEnabled
            theme: root.theme
            text: "启用中文唤醒"
            checked: root.settings.draft.wake_word.enabled
            onToggled: root.settings.set_field("wake_word", "enabled", checked)
        }
        FieldLabel { theme: root.theme; text: "唤醒词" }
        AppTextField {
            id: keywordField
            Layout.preferredWidth: 360
            theme: root.theme
            text: root.settings.draft.wake_word.keyword
            placeholderText: "唤醒词"
            Accessible.name: "唤醒词"
            onTextEdited: root.settings.set_field("wake_word", "keyword", text)
        }
        FieldLabel { theme: root.theme; text: "唤醒灵敏度 · " + sensitivity.value.toFixed(2) }
        AppSlider {
            id: sensitivity
            Layout.preferredWidth: 360
            theme: root.theme
            from: 0
            to: 1
            value: root.settings.draft.wake_word.sensitivity
            onMoved: root.settings.set_field("wake_word", "sensitivity", value)
        }
        FieldLabel { theme: root.theme; text: "语音识别模型" }
        AppComboBox {
            id: asrSelector
            objectName: "asrModelSelector"
            Layout.preferredWidth: Math.min(360, root.availableWidth)
            theme: root.theme
            model: root.settings.asrModelOptions
            currentIndex: model.indexOf(root.settings.draft.asr.model)
            Accessible.name: "语音识别模型"
            onActivated: root.settings.set_field("asr", "model", currentText)
        }
        Text {
            Layout.fillWidth: true
            text: "tiny / base 速度更快；small / medium 较均衡；large 系列准确率更高、资源占用更多；turbo 兼顾速度与准确率"
            color: root.theme.textMuted
            wrapMode: Text.Wrap
        }
        Text {
            Layout.fillWidth: true
            text: root.settings.asrDownloadNote
            color: root.theme.textMuted
            wrapMode: Text.Wrap
        }
        AppButton {
            theme: root.theme
            text: "下载语音识别模型"
            kind: "secondary"
            enabled: !root.settings.operationBusy
            onClicked: root.settings.download_asr_model()
        }
        AppButton {
            theme: root.theme
            text: "下载中文唤醒模型"
            kind: "secondary"
            enabled: !root.settings.operationBusy
            onClicked: root.settings.download_wake_model()
        }
        AppToggle {
            id: ttsEnabled
            theme: root.theme
            text: "回复时播放语音"
            checked: root.settings.draft.tts.enabled
            onToggled: root.settings.set_field("tts", "enabled", checked)
        }
        AppToggle {
            id: manualTtsEnabled
            theme: root.theme
            text: "手动输入时播放语音"
            checked: root.settings.draft.tts.manual_input_enabled
            onToggled: root.settings.set_field("tts", "manual_input_enabled", checked)
        }
        FieldLabel { theme: root.theme; text: "回复声音" }
        AppComboBox {
            id: voiceSelector
            objectName: "voiceSelector"
            Layout.preferredWidth: Math.min(420, root.availableWidth)
            theme: root.theme
            model: root.settings.voiceOptions
            textRole: "label"
            valueRole: "short_name"
            function selectedIndex() {
                const selected = root.settings.draft.tts.voice
                const options = voiceSelector.model
                for (let index = 0; index < options.length; index++) {
                    if (options[index].short_name === selected)
                        return index
                }
                return -1
            }
            currentIndex: selectedIndex()
            onActivated: root.settings.set_field("tts", "voice", currentValue)
        }
        AppButton {
            theme: root.theme
            text: root.settings.voicesLoading ? "正在加载声音…" : "刷新声音列表"
            kind: "secondary"
            enabled: !root.settings.voicesLoading
            onClicked: root.settings.refresh_voices()
        }
        Text {
            Layout.fillWidth: true
            text: root.settings.voicesError
            visible: text.length > 0
            color: root.theme.textMuted
            wrapMode: Text.Wrap
        }
        AppButton {
            theme: root.theme
            text: "试听当前声音"
            kind: "secondary"
            enabled: !root.settings.operationBusy && voiceSelector.currentIndex >= 0
            onClicked: root.settings.preview_voice(voiceSelector.currentValue)
        }
    }
}
