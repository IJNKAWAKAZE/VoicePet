import QtQuick
import QtQuick.Controls
import QtQuick.Window
import "../components"

ApplicationWindow {
    id: petWindow
    objectName: "petWindow"
    required property QtObject animation
    required property QtObject interaction
    required property QtObject theme
    property real petScale: 1.0
    property string speech: ""
    property bool alwaysOnTop: true
    property rect availableArea: Qt.rect(0, 0, 1280, 720)

    width: animation.cellWidth * petScale
    height: animation.cellHeight * petScale
    visible: false
    transientParent: null
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint
        | (alwaysOnTop ? Qt.WindowStaysOnTopHint : 0)

    PetSprite {
        anchors.fill: parent
        animation: petWindow.animation
        petScale: petWindow.petScale
    }

    MouseArea {
        id: petMouse
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        hoverEnabled: true
        // 全局坐标不会随桌宠窗口移动改变，避免局部坐标反馈造成来回抖动
        onPressed: mouse => {
            const point = petMouse.mapToGlobal(mouse.x, mouse.y)
            petWindow.interaction.pointer_press(point.x, point.y, mouse.button)
        }
        onPositionChanged: mouse => {
            if (pressedButtons & Qt.LeftButton) {
                const point = petMouse.mapToGlobal(mouse.x, mouse.y)
                petWindow.interaction.pointer_move(point.x, point.y)
            }
        }
        onReleased: mouse => {
            const point = petMouse.mapToGlobal(mouse.x, mouse.y)
            petWindow.interaction.pointer_release(point.x, point.y, mouse.button)
        }
        onDoubleClicked: mouse => petWindow.interaction.double_click(mouse.x, mouse.y, mouse.button)
    }

    Timer {
        interval: petWindow.animation.frameDuration
        repeat: true
        running: petWindow.visible
        onTriggered: petWindow.animation.advance_frame()
    }

    Window {
        id: speechWindow
        objectName: "petSpeechWindow"
        property rect availableArea: petWindow.availableArea
        width: speechBubble.implicitWidth
        height: speechBubble.implicitHeight
        visible: petWindow.visible && speechBubble.visible
        transientParent: null
        color: "transparent"
        flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
            | (petWindow.alwaysOnTop ? Qt.WindowStaysOnTopHint : 0)
        x: Math.max(
            availableArea.x + 8,
            Math.min(
                petWindow.x + (petWindow.width - width) / 2,
                availableArea.x + availableArea.width - width - 8
            )
        )
        y: {
            const above = petWindow.y - height - 10
            const preferred = above >= availableArea.y + 8
                ? above : petWindow.y + petWindow.height + 10
            return Math.max(
                availableArea.y + 8,
                Math.min(preferred, availableArea.y + availableArea.height - height - 8)
            )
        }

        PetSpeechBubble {
            id: speechBubble
            objectName: "petSpeechBubble"
            anchors.fill: parent
            theme: petWindow.theme
            message: petWindow.speech
            availableScreenWidth: speechWindow.availableArea.width
            availableScreenHeight: speechWindow.availableArea.height
            onDismissRequested: petWindow.speech = ""
        }
    }
}
