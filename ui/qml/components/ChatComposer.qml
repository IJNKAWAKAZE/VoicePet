import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs

Rectangle {
    id: root
    required property QtObject theme
    required property bool processing
    signal submitRequested(string text, var attachments)
    signal stopRequested
    signal voiceInputRequested
    signal modeRequested(string mode)
    property string agentMode: "auto_edit"
    property string agentModeLabel: "自动编辑"
    property bool agentModePending: false
    property var agentModeOptions: []
    property alias attachmentModel: attachments
    property bool menuOpen: attachmentMenu.visible
    property bool recording: false
    property var pasteAttachments: null

    function setDraftText(value) {
        composer.text = value
        composer.forceActiveFocus()
        composer.cursorPosition = composer.length
    }

    function appendDraftText(value) {
        const incoming = value === undefined || value === null ? "" : String(value).trim()
        if (!incoming)
            return
        // 保留草稿中的换行和空格，追加后将光标移到末尾
        const separator = composer.length > 0 && !/\s$/.test(composer.text) ? " " : ""
        composer.insert(composer.length, separator + incoming)
        composer.forceActiveFocus()
        composer.cursorPosition = composer.length
    }

    readonly property int toolbarHeight: 40
    readonly property int editorHeight: Math.max(56, Math.min(140, composer.implicitHeight))
    implicitHeight: editorHeight + toolbarHeight + 16
        + (attachments.count > 0 ? attachmentPreview.implicitHeight + 8 : 0)
        + (recording ? 22 : 0)
    radius: 14
    color: theme.surface
    border.color: composer.activeFocus ? theme.focus : theme.border
    border.width: composer.activeFocus ? 2 : 1

    ListModel {
        id: attachments
    }

    FileDialog {
        id: imageDialog
        title: "上传图片"
        nameFilters: ["图片 (*.png *.jpg *.jpeg *.webp)", "所有文件 (*)"]
        onAccepted: root.addAttachment(selectedFile, "image")
    }

    FileDialog {
        id: fileDialog
        title: "上传文件"
        nameFilters: ["所有文件 (*)"]
        onAccepted: root.addAttachment(selectedFile, "file")
    }

    function addAttachment(url, kind) {
        // 保留完整文件 URL，由 Python 统一处理 Windows 盘符和转义字符
        const path = String(url)
        if (!path)
            return
        attachments.append({
            path: path,
            name: decodeURIComponent(path.split(/[\\/]/).pop()),
            kind: kind
        })
        // 等待文件选择窗口关闭后恢复输入焦点，保留草稿并将光标移到末尾
        Qt.callLater(function() {
            composer.forceActiveFocus(Qt.OtherFocusReason)
            composer.cursorPosition = composer.length
        })
    }

    function removeAttachment(index) {
        attachments.remove(index)
    }

    function attachmentPayload() {
        const result = []
        for (let index = 0; index < attachments.count; index++)
        {
            const item = attachments.get(index)
            // ListModel.get 返回 QObject 包装，显式复制为 Python 可识别的普通对象
            result.push({path: String(item.path), name: String(item.name), kind: String(item.kind)})
        }
        return result
    }

    function clearAttachments() {
        attachments.clear()
    }

    Flow {
        id: attachmentPreview
        objectName: "attachmentPreview"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 14
        spacing: 6
        visible: attachments.count > 0
        Repeater {
            model: attachments
            delegate: Rectangle {
                required property string name
                required property int index
                width: Math.min(180, attachmentName.implicitWidth + 34)
                height: 26
                radius: 13
                color: root.theme.surfaceAlt
                border.color: root.theme.border
                Text {
                    id: attachmentName
                    anchors.left: parent.left
                    anchors.leftMargin: 10
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - 28
                    text: parent.name
                    color: root.theme.text
                    elide: Text.ElideMiddle
                    font.pixelSize: 12
                }
                ToolButton {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    width: 24
                    height: 24
                    text: "×"
                    onClicked: root.removeAttachment(parent.index)
                }
            }
        }
    }

    Item {
        id: toolbar
        objectName: "composerToolbar"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        anchors.bottomMargin: 6
        height: root.toolbarHeight
        // 工具栏共用外层面板，仅通过内缩分隔线区分输入区
        Rectangle {
            width: parent.width
            height: 1
            color: root.theme.border
            opacity: 0.55
        }
    }

    ScrollView {
        id: composerScroll
        objectName: "composerScroll"
        anchors.left: parent.left
        anchors.leftMargin: 14
        anchors.right: parent.right
        anchors.top: voiceStatus.visible ? voiceStatus.bottom
            : attachmentPreview.visible ? attachmentPreview.bottom : parent.top
        anchors.bottom: toolbar.top
        anchors.rightMargin: 14
        anchors.topMargin: voiceStatus.visible ? 2 : attachmentPreview.visible ? 4 : 10
        anchors.bottomMargin: 6
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical: ScrollBar {
            objectName: "composerScrollBar"
            policy: ScrollBar.AsNeeded
        }

        TextArea {
            id: composer
            objectName: "composerText"
            width: composerScroll.availableWidth
            placeholderText: "输入消息，Enter 发送，Shift+Enter 换行"
            wrapMode: TextEdit.Wrap
            color: root.theme.text
            placeholderTextColor: root.theme.textMuted
            background: null
            selectByMouse: true
            Accessible.name: "消息输入"
            Keys.onPressed: event => {
                if (event.matches(StandardKey.Paste) && root.pasteAttachments
                        && root.pasteAttachments(attachments.count)) {
                    event.accepted = true
                    return
                }
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                    if (!(event.modifiers & Qt.ShiftModifier)) {
                        // 普通 Enter 始终按发送键处理，忙碌时不落入 TextArea 的默认换行。
                        event.accepted = true
                        if (!root.processing && !root.recording)
                            root.submitRequested(text, attachmentPayload())
                    }
                }
            }
        }
    }

    AppButton {
        id: attachmentMenuButton
        objectName: "attachmentMenuButton"
        anchors.left: toolbar.left
        anchors.verticalCenter: toolbar.verticalCenter
        anchors.verticalCenterOffset: 2
        width: 28
        height: 28
        leftPadding: 0
        rightPadding: 0
        theme: root.theme
        text: "＋"
        kind: "ghost"
        onClicked: attachmentMenu.visible ? attachmentMenu.close() : attachmentMenu.open()
    }

    AppButton {
        id: agentModeButton
        objectName: "agentModeButton"
        anchors.left: attachmentMenuButton.right
        anchors.leftMargin: 4
        anchors.verticalCenter: toolbar.verticalCenter
        width: Math.max(112, implicitWidth)
        height: 28
        theme: root.theme
        text: ({suggest: "◉", auto_edit: "✎", full_auto: "⚡"}[root.agentMode] || "✎") + "  " + root.agentModeLabel + (root.agentModePending ? "（下一轮）" : "")
        kind: "ghost"
        Accessible.name: "Agent 审批模式"
        onClicked: agentModeMenu.visible ? agentModeMenu.close() : agentModeMenu.open()
    }

    Popup {
        id: agentModeMenu
        x: agentModeButton.x
        y: -height - 8
        width: 250
        padding: 8
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent
        background: Rectangle { color: root.theme.surface; radius: 12; border.color: root.theme.border }
        Column {
            spacing: 2
            Repeater {
                model: root.agentModeOptions
                delegate: Rectangle {
                    required property var modelData
                    width: 234
                    height: 56
                    radius: 8
                    color: modelData.value === root.agentMode ? root.theme.surfaceAlt : "transparent"
                    border.color: modelData.value === root.agentMode ? root.theme.focus : "transparent"
                    objectName: "agentModeOption_" + modelData.value
                    Accessible.name: modelData.label
                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: modelData.icon
                        color: root.theme.info
                        font.pixelSize: 17
                    }
                    Column {
                        anchors.left: parent.left
                        anchors.leftMargin: 38
                        anchors.verticalCenter: parent.verticalCenter
                        Text { text: modelData.label + (modelData.value === root.agentMode ? "  ✓" : ""); color: root.theme.text; font.pixelSize: 13 }
                        Text { text: modelData.description; color: root.theme.textMuted; font.pixelSize: 11 }
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: { root.modeRequested(modelData.value); agentModeMenu.close() }
                    }
                }
            }
        }
    }

    Text {
        id: voiceStatus
        objectName: "voiceStatus"
        anchors.left: parent.left
        anchors.leftMargin: 14
        anchors.top: attachmentPreview.visible ? attachmentPreview.bottom : parent.top
        anchors.topMargin: attachmentPreview.visible ? 4 : 8
        visible: root.recording
        text: "● 正在聆听，请说话…"
        color: root.theme.info
        font.pixelSize: 13
        font.family: "Microsoft YaHei UI"
    }

    Popup {
        id: attachmentMenu
        objectName: "attachmentMenu"
        x: 10
        y: -height - 8
        width: 180
        implicitHeight: menuItems.implicitHeight + topPadding + bottomPadding
        padding: 8
        z: 100
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent
        onClosed: {
            uploadImageButton.focus = false
            uploadFileButton.focus = false
            attachmentMenuButton.focus = false
        }
        background: Rectangle {
            color: root.theme.surface
            radius: 12
            border.color: root.theme.border
        }
        // 显式指定内容项，避免子按钮依赖父宽度时隐式尺寸归零
        contentItem: Column {
            id: menuItems
            spacing: 4
            AppButton {
                id: uploadImageButton
                objectName: "uploadImageButton"
                width: parent.width
                focusPolicy: Qt.NoFocus
                theme: root.theme
                text: ""
                kind: "ghost"
                Accessible.name: "上传图片"
                contentItem: Row {
                    spacing: 8
                    anchors.centerIn: parent
                    Canvas {
                        width: 18
                        height: 18
                        onPaint: {
                            const ctx = getContext("2d")
                            ctx.reset()
                            ctx.strokeStyle = root.theme.info
                            ctx.fillStyle = root.theme.surfaceAlt
                            ctx.lineWidth = 1.6
                            ctx.beginPath()
                            ctx.roundedRect(1, 2, 16, 14, 2, 2)
                            ctx.fill()
                            ctx.stroke()
                            ctx.beginPath()
                            ctx.moveTo(3, 13)
                            ctx.lineTo(7, 9)
                            ctx.lineTo(10, 12)
                            ctx.lineTo(13, 8)
                            ctx.lineTo(16, 13)
                            ctx.stroke()
                        }
                    }
                    Text {
                        text: "上传图片"
                        color: root.theme.text
                        font.pixelSize: 14
                        font.family: "Microsoft YaHei UI"
                    }
                }
                onClicked: { attachmentMenu.close(); imageDialog.open() }
            }
            AppButton {
                id: uploadFileButton
                objectName: "uploadFileButton"
                width: parent.width
                focusPolicy: Qt.NoFocus
                theme: root.theme
                text: ""
                kind: "ghost"
                Accessible.name: "上传文件"
                contentItem: Row {
                    spacing: 8
                    anchors.centerIn: parent
                    Canvas {
                        width: 18
                        height: 18
                        onPaint: {
                            const ctx = getContext("2d")
                            ctx.reset()
                            ctx.strokeStyle = root.theme.info
                            ctx.lineWidth = 1.8
                            ctx.lineCap = "round"
                            ctx.beginPath()
                            ctx.moveTo(11, 3)
                            ctx.lineTo(5, 9)
                            ctx.bezierCurveTo(2, 12, 6, 16, 9, 13)
                            ctx.lineTo(15, 7)
                            ctx.bezierCurveTo(18, 4, 14, 1, 11, 4)
                            ctx.lineTo(6, 9)
                            ctx.bezierCurveTo(5, 10, 7, 12, 8, 11)
                            ctx.lineTo(13, 6)
                            ctx.stroke()
                        }
                    }
                    Text {
                        text: "上传文件"
                        color: root.theme.text
                        font.pixelSize: 14
                        font.family: "Microsoft YaHei UI"
                    }
                }
                onClicked: { attachmentMenu.close(); fileDialog.open() }
            }
        }
    }

    AppButton {
        id: microphoneButton
        objectName: "microphoneButton"
        anchors.right: actionButton.left
        anchors.rightMargin: 8
        anchors.verticalCenter: toolbar.verticalCenter
        anchors.verticalCenterOffset: 2
        width: 28
        height: 28
        theme: root.theme
        text: ""
        implicitWidth: 40
        leftPadding: 2
        rightPadding: 2
        topPadding: 2
        bottomPadding: 2
        Accessible.name: root.recording ? "正在语音输入" : "语音输入"
        contentItem: Canvas {
            implicitWidth: 24
            implicitHeight: 24
            property color strokeColor: root.recording ? root.theme.info : root.theme.text
            onStrokeColorChanged: requestPaint()
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                ctx.translate((width - 24) / 2, (height - 24) / 2)
                ctx.strokeStyle = strokeColor
                ctx.lineWidth = 1.8
                ctx.lineCap = "round"
                ctx.lineJoin = "round"
                // 胶囊麦头、拾音弧和底座共用主题描边
                ctx.beginPath()
                ctx.moveTo(8.5, 6)
                ctx.bezierCurveTo(8.5, 1.5, 15.5, 1.5, 15.5, 6)
                ctx.lineTo(15.5, 11)
                ctx.bezierCurveTo(15.5, 15.5, 8.5, 15.5, 8.5, 11)
                ctx.closePath()
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(5, 10)
                ctx.lineTo(5, 11)
                ctx.bezierCurveTo(5, 20, 19, 20, 19, 11)
                ctx.lineTo(19, 10)
                ctx.moveTo(12, 18)
                ctx.lineTo(12, 21)
                ctx.moveTo(8, 21)
                ctx.lineTo(16, 21)
                ctx.stroke()
            }
        }
        kind: "ghost"
        enabled: !root.processing && !root.recording
        onClicked: root.voiceInputRequested()
    }

    AppButton {
        id: actionButton
        objectName: "composerSendButton"
        anchors.right: toolbar.right
        anchors.verticalCenter: toolbar.verticalCenter
        anchors.verticalCenterOffset: 2
        height: 28
        leftPadding: 12
        rightPadding: 12
        topPadding: 2
        bottomPadding: 2
        theme: root.theme
        text: root.processing ? "停止" : "发送"
        kind: root.processing ? "secondary" : "primary"
        onClicked: root.processing
            ? root.stopRequested()
            : root.submitRequested(composer.text, attachmentPayload())
    }
}
