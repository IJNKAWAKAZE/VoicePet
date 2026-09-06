import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property QtObject theme
    required property Window appWindow
    signal closeRequested

    height: 44
    color: "transparent"

    component WindowGlyph: Canvas {
        objectName: "windowGlyph"
        required property string kind
        property color strokeColor

        antialiasing: true
        onKindChanged: requestPaint()
        onStrokeColorChanged: requestPaint()
        onPaint: {
            const context = getContext("2d")
            context.reset()
            context.strokeStyle = strokeColor
            context.lineWidth = 1.5
            context.lineCap = "round"
            if (kind === "minimize") {
                context.beginPath()
                context.moveTo(6, 12)
                context.lineTo(18, 12)
                context.stroke()
            } else if (kind === "maximize") {
                context.strokeRect(6.5, 6.5, 11, 11)
            } else if (kind === "restore") {
                context.strokeRect(8.5, 5.5, 10, 10)
                context.strokeRect(5.5, 8.5, 10, 10)
            } else if (kind === "close") {
                context.beginPath()
                context.moveTo(6, 6)
                context.lineTo(18, 18)
                context.moveTo(18, 6)
                context.lineTo(6, 18)
                context.stroke()
            }
        }
    }

    component WindowControl: Button {
        id: control
        required property string glyphKind
        property bool destructive: false

        width: 40
        height: 32
        flat: true
        hoverEnabled: true
        padding: 0
        // 固定图标尺寸并居中，避免继承按钮内边距后绘制偏向左上角
        contentItem: Item {
            WindowGlyph {
                anchors.centerIn: parent
                width: 24
                height: 24
                kind: control.glyphKind
                strokeColor: control.destructive && control.down ? root.theme.inverseText
                    : control.destructive && control.hovered ? root.theme.danger : root.theme.text
            }
        }
        background: Rectangle {
            radius: 8
            color: control.destructive
                ? (control.down ? root.theme.danger : control.hovered ? root.theme.dangerSurface : "transparent")
                : (control.down ? root.theme.userBubble : control.hovered ? root.theme.surfaceAlt : "transparent")
            border.width: control.visualFocus ? 2 : 0
            border.color: root.theme.focus
        }
    }

    Image {
        id: brandMark
        objectName: "brandMark"
        anchors.left: parent.left
        anchors.leftMargin: 14
        anchors.verticalCenter: parent.verticalCenter
        width: 24
        height: 24
        source: root.theme.brandMark
        fillMode: Image.PreserveAspectFit
    }

    Text {
        anchors.left: parent.left
        anchors.leftMargin: 46
        anchors.verticalCenter: parent.verticalCenter
        text: "VoicePet"
        color: root.theme.text
        font.pixelSize: 14
        font.weight: Font.DemiBold
        font.family: "Microsoft YaHei UI"
    }

    Row {
        id: windowControls
        anchors.right: parent.right
        anchors.rightMargin: 8
        anchors.verticalCenter: parent.verticalCenter
        spacing: 4

        WindowControl {
            id: minimizeButton
            Accessible.name: "最小化"
            glyphKind: "minimize"
            onClicked: root.appWindow.showMinimized()
        }
        WindowControl {
            id: maximizeButton
            Accessible.name: "最大化或还原"
            glyphKind: root.appWindow.visibility === Window.Maximized ? "restore" : "maximize"
            onClicked: root.appWindow.visibility === Window.Maximized
                ? root.appWindow.showNormal() : root.appWindow.showMaximized()
        }
        WindowControl {
            id: closeButton
            Accessible.name: "关闭"
            glyphKind: "close"
            destructive: true
            onClicked: root.closeRequested()
        }
    }

    // 窗口拖动与双击手势只覆盖标题空白区域，不抢占右侧按钮点击
    Item {
        anchors.left: parent.left
        anchors.right: windowControls.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom

        DragHandler {
            target: null
            onActiveChanged: if (active) root.appWindow.startSystemMove()
        }

        TapHandler {
            acceptedButtons: Qt.LeftButton
            onDoubleTapped: root.appWindow.visibility === Window.Maximized
                ? root.appWindow.showNormal() : root.appWindow.showMaximized()
        }
    }
}
