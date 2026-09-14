import QtQuick
import QtQuick.Controls
import "../components"

Item {
    id: root
    required property QtObject theme
    required property QtObject chat
    property bool showSidebarButton: false
    signal linkRequested(string link)
    signal sidebarRequested

    AppButton {
        objectName: "contextSidebarButton"
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: 24
        z: 2
        visible: root.showSidebarButton
        theme: root.theme
        text: "会话"
        kind: "secondary"
        onClicked: root.sidebarRequested()
    }

    ListView {
        id: messages
        objectName: "chatMessageList"
        property bool followTail: true
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: memoryChanges.visible ? memoryChanges.top : composer.top
        anchors.margins: 24
        anchors.topMargin: root.showSidebarButton ? 72 : 24
        model: root.chat.messageModel
        spacing: 12
        clip: true
        visible: count > 0
        ScrollBar.vertical: ScrollBar {}
        onCountChanged: if (followTail) tailLayout.restart()
        onContentHeightChanged: if (followTail) tailLayout.restart()
        onOriginYChanged: if (followTail) tailLayout.restart()
        onHeightChanged: if (followTail) tailLayout.restart()
        onMovementStarted: followTail = false
        onMovementEnded: followTail = atYEnd

        // 历史替换与流式换行都在布局完成后定位，避免停在最后一条用户消息
        Timer {
            id: tailLayout
            interval: 0
            onTriggered: {
                if (messages.followTail) {
                    messages.forceLayout()
                    messages.positionViewAtEnd()
                }
            }
        }
        Connections {
            target: root.chat.messageModel
            function onModelReset() {
                messages.followTail = true
                tailLayout.restart()
            }
            function onDataChanged() { if (messages.followTail) tailLayout.restart() }
        }

        delegate: Item {
            required property string messageId
            required property string role
            required property string markdown
            required property string status
            required property var attachments
            width: messages.width
            height: role !== "tool" && (markdown.length > 0 || attachments.length > 0)
                ? bubble.implicitHeight + 30 : 0
            MessageBubble {
                id: bubble
                width: Math.min(720, messages.width * 0.78)
                theme: root.theme
                role: parent.role
                markdown: parent.markdown
                status: parent.status
                attachments: parent.attachments
                playing: root.chat.messagePlaying
                visible: parent.role !== "tool"
                    && (parent.markdown.length > 0 || parent.attachments.length > 0)
                viewportWidth: messages.width
                anchors.right: parent.role === "user" ? parent.right : undefined
                anchors.left: parent.role === "user" ? undefined : parent.left
                onLinkRequested: link => root.linkRequested(link)
                onCopyRequested: text => root.chat.copy_message(text)
                onPlayRequested: text => root.chat.play_message(text)
                onFileRequested: path => root.chat.open_attachment(path)
            }
        }
    }

    AppCard {
        id: memoryChanges
        objectName: "chatMemoryChanges"
        visible: root.chat.memoryChangeCount > 0 || root.chat.memoryActionResult.length > 0
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: composer.top
        anchors.leftMargin: 24
        anchors.rightMargin: 24
        anchors.bottomMargin: 10
        height: 76
        theme: root.theme
        Row {
            anchors.fill: parent
            anchors.margins: 10
            spacing: 10
            Text {
                width: 120
                anchors.verticalCenter: parent.verticalCenter
                text: "已记住 " + root.chat.memoryChangeCount + " 项"
                color: root.theme.text
                elide: Text.ElideRight
            }
            AppButton {
                theme: root.theme
                text: "查看"
                kind: "secondary"
                anchors.verticalCenter: parent.verticalCenter
                onClicked: root.chat.open_memory()
            }
            Text { visible: root.chat.memoryActionResult.length > 0; text: root.chat.memoryActionResult; color: root.theme.textMuted; anchors.verticalCenter: parent.verticalCenter }
            ListView {
                width: Math.max(100, memoryChanges.width - 420)
                height: 40
                anchors.verticalCenter: parent.verticalCenter
                orientation: ListView.Horizontal
                spacing: 6
                clip: true
                model: root.chat.memoryChangesModel
                delegate: AppButton {
                    required property string changeId
                    required property bool undone
                    theme: root.theme
                    text: undone ? "已撤销" : "撤销"
                    enabled: !undone && !root.chat.memoryActionBusy
                    onClicked: root.chat.undo_memory_change(changeId)
                }
            }
        }
    }

    Column {
        anchors.centerIn: messages
        visible: messages.count === 0
        width: Math.min(620, Math.max(320, messages.width - 64))
        spacing: 10
        Image {
            id: welcomeIllustration
            objectName: "welcomeIllustration"
            anchors.horizontalCenter: parent.horizontalCenter
            width: parent.width
            height: Math.min(260, width * 0.5625)
            source: root.theme.welcomeImage
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            cache: true
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "今天想聊点什么？"
            color: root.theme.text
            font.pixelSize: 24
            font.family: "Microsoft YaHei UI"
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "也可以单击桌宠，直接开始聆听"
            color: root.theme.textMuted
            font.pixelSize: 14
            font.family: "Microsoft YaHei UI"
        }
    }

    ChatComposer {
        id: composer
        objectName: "chatComposer"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 24
        theme: root.theme
        processing: root.chat.processing
        agentMode: root.chat.agentMode
        agentModeLabel: root.chat.agentModeLabel
        agentModePending: root.chat.agentModePending
        agentModeOptions: root.chat.agentModeOptions
        enabled: !root.chat.sessionLoading
        onSubmitRequested: (text, attachments) => root.chat.submit(text, attachments)
        onStopRequested: root.chat.stop_generation()
        onVoiceInputRequested: root.chat.start_voice_input()
        recording: root.chat.voiceRecording
        pasteAttachments: count => root.chat.paste_attachments(count)
        onModeRequested: mode => root.chat.set_agent_mode(mode)
    }

    Connections {
        target: root.chat
        function onScrollToLatestRequested() {
            messages.cancelFlick()
            messages.followTail = true
            tailLayout.restart()
        }
        function onTranscriptReady(text) { composer.appendDraftText(text) }
        function onSubmissionAccepted() {
            composer.setDraftText("")
            composer.clearAttachments()
        }
        function onAttachmentPasted(url, kind) { composer.addAttachment(url, kind) }
        function onFolderPathPasted(path) { composer.appendDraftText(path) }
    }
}
