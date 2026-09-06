import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    required property QtObject theme

    implicitHeight: 40
    Accessible.name: currentText

    indicator: Canvas {
        id: chevron
        objectName: "comboDownIndicator"
        width: 14
        height: 9
        x: control.width - width - 14
        y: (control.height - height) / 2
        property color strokeColor: control.enabled ? control.theme.text : control.theme.disabledText
        rotation: control.popup.visible ? 180 : 0
        onStrokeColorChanged: requestPaint()
        onPaint: {
            const context = getContext("2d")
            context.clearRect(0, 0, width, height)
            context.strokeStyle = strokeColor
            context.lineWidth = 2
            context.lineCap = "round"
            context.lineJoin = "round"
            context.beginPath()
            context.moveTo(2, 2)
            context.lineTo(7, 7)
            context.lineTo(12, 2)
            context.stroke()
        }
    }

    contentItem: Text {
        leftPadding: 12
        rightPadding: 28
        text: control.displayText
        color: control.enabled ? control.theme.text : control.theme.disabledText
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        font.pixelSize: 14
        font.family: "Microsoft YaHei UI"
    }

    background: Rectangle {
        radius: 10
        color: control.theme.surface
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? control.theme.focus : control.theme.border
    }
}
