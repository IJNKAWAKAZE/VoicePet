import QtQuick

Item {
    id: root
    required property QtObject animation
    property real petScale: 1.0

    width: animation.cellWidth * petScale
    height: animation.cellHeight * petScale
    clip: true

    Image {
        anchors.fill: parent
        source: root.animation.sheetUrl
        sourceClipRect: Qt.rect(
            root.animation.column * root.animation.cellWidth,
            root.animation.row * root.animation.cellHeight,
            root.animation.cellWidth,
            root.animation.cellHeight
        )
        fillMode: Image.Stretch
        smooth: true
        mipmap: true
    }
}
