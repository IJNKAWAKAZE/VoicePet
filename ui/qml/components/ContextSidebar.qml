import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property QtObject theme
    required property QtObject shellModel
    required property QtObject chatModel
    property bool overlayMode: false
    property bool drawerOpen: false

    width: overlayMode ? 0 : (root.shellModel.currentSection === "chat" ? 236 : 0)
    z: 20

    Rectangle {
        width: 236
        height: parent.height
        visible: root.visible && (!root.overlayMode || root.drawerOpen)
        color: root.theme.surfaceAlt
        border.color: root.theme.border
        border.width: root.overlayMode ? 1 : 0

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 10

            RowLayout {
                Layout.fillWidth: true
                visible: root.shellModel.currentSection === "chat"

                Text {
                    Layout.fillWidth: true
                    text: "最近会话"
                    color: root.theme.text
                    font.pixelSize: 15
                    font.bold: true
                    font.family: "Microsoft YaHei UI"
                }

                AppButton {
                    visible: root.overlayMode
                    theme: root.theme
                    text: "收起"
                    kind: "ghost"
                    onClicked: root.drawerOpen = false
                }
            }

            AppButton {
                objectName: "newSessionButton"
                Layout.fillWidth: true
                theme: root.theme
                text: "＋ 新建会话"
                enabled: !root.chatModel.sessionLoading
                kind: "primary"
                onClicked: root.chatModel.new_session()
            }

            SessionList {
                objectName: "sessionList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: root.shellModel.currentSection === "chat"
                theme: root.theme
                sessionModel: root.chatModel.sessionModel
                enabled: !root.chatModel.sessionLoading
                onDeleteRequested: sessionId => root.chatModel.request_delete_session(sessionId)
                onSessionRequested: sessionId => {
                    root.chatModel.activate_session(sessionId)
                    if (root.overlayMode)
                        root.drawerOpen = false
                }
            }

        }
    }
}
