import QtQuick

// Agent 执行过程中的单条工具调用：命令、工具名和实时输出
Rectangle {
    id: root
    required property QtObject theme
    required property string kind
    required property string title
    required property string output
    required property string status
    property bool expanded: false
    property real viewportWidth: 800

    readonly property bool isThinking: kind === "thinking"
    readonly property string kindLabel: kind === "command" ? "命令"
        : kind === "file" ? "文件"
        : isThinking ? "思考" : "工具"
    readonly property string statusLabel: status === "running" ? (isThinking ? "思考中" : "执行中")
        : status === "failed" ? "失败"
        : status === "stopped" ? "已中断"
        : isThinking ? "已思考" : "已完成"
    readonly property color statusColor: status === "running" ? theme.info
        : status === "failed" ? theme.danger
        : status === "stopped" ? theme.warning : theme.success
    readonly property int collapsedHeight: 160
    readonly property bool collapsible: outputText.implicitHeight > collapsedHeight

    width: Math.min(720, viewportWidth * 0.78)
    implicitHeight: activityColumn.implicitHeight + 20
    height: implicitHeight
    radius: 12
    color: theme.surfaceAlt
    border.width: 1
    border.color: status === "failed" ? theme.danger : theme.border
    clip: true
    Accessible.role: Accessible.Pane
    Accessible.name: kindLabel + statusLabel

    Column {
        id: activityColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 10
        spacing: 6

        Row {
            width: parent.width
            spacing: 8

            Text {
                id: kindText
                text: root.kindLabel
                color: root.theme.textMuted
                font.pixelSize: 12
                font.weight: Font.DemiBold
                font.family: "Microsoft YaHei UI"
            }

            Text {
                id: statusText
                text: root.statusLabel
                color: root.statusColor
                font.pixelSize: 12
                font.weight: Font.DemiBold
                font.family: "Microsoft YaHei UI"
            }

            Text {
                objectName: "agentActivityTitle"
                width: Math.max(parent.width - kindText.width - statusText.width - 32, 60)
                text: root.title
                color: root.theme.text
                font.pixelSize: 12
                font.family: "Consolas"
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
        }

        Rectangle {
            objectName: "agentActivityOutputBox"
            visible: root.output.length > 0
            width: parent.width
            height: visible
                ? Math.min(outputText.implicitHeight + 16, root.expanded ? 600 : root.collapsedHeight)
                : 0
            radius: 8
            color: root.isThinking ? "transparent" : root.theme.codeBlock
            clip: true

            TextEdit {
                id: outputText
                objectName: "agentActivityOutput"
                x: 8
                y: 8
                width: parent.width - 16
                text: root.output
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: root.theme.text
                font.pixelSize: root.isThinking ? 13 : 12
                font.family: root.isThinking ? "Microsoft YaHei UI" : "Consolas"
            }
        }

        AppButton {
            objectName: "agentActivityToggle"
            visible: root.collapsible
            theme: root.theme
            text: root.expanded ? "收起输出" : "展开输出"
            kind: "secondary"
            onClicked: root.expanded = !root.expanded
        }
    }
}
