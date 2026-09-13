import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "components"
import "screens"
import "windows"

ApplicationWindow {
    id: mainWindow
    objectName: "mainWindow"
    width: 1040
    height: 680
    minimumWidth: 820
    minimumHeight: 560
    visible: false
    title: "VoicePet"
    color: "transparent"
    font.family: "Microsoft YaHei UI"
    background: Rectangle {
        objectName: "windowFrame"
        color: themeViewModel.shellSurface
        radius: mainWindow.visibility === Window.Maximized ? 0 : 14
        border.width: 2
        border.color: themeViewModel.border
    }
    flags: Qt.Window | Qt.FramelessWindowHint

    component ResizeHandle: MouseArea {
        required property int resizeEdges
        acceptedButtons: Qt.LeftButton
        z: 1000
        onPressed: mainWindow.startSystemResize(resizeEdges)
    }

    header: AppTitleBar {
        theme: themeViewModel
        appWindow: mainWindow
        onCloseRequested: appShell.hide_main()
    }

    Row {
        anchors.fill: parent
        anchors.margins: 10

        NavigationRail {
            id: navigationRail
            objectName: "navigationRail"
            width: mainWindow.width <= 860 ? 64 : 168
            height: parent.height
            compact: mainWindow.width <= 860
            theme: themeViewModel
            currentSection: appShell.currentSection
            onNavigationRequested: section => appShell.navigate(section)
        }

        // 非聊天页面不显示会话侧栏，避免占用主内容宽度

        ContextSidebar {
            id: contextSidebar
            objectName: "contextSidebar"
            height: parent.height
            theme: themeViewModel
            shellModel: appShell
            chatModel: chatViewModel
            overlayMode: mainWindow.width < 960
            visible: appShell.currentSection === "chat"
        }

        Rectangle {
            id: mainContent
            objectName: "mainContent"
            width: parent.width - navigationRail.width
                - (contextSidebar.visible && !contextSidebar.overlayMode ? contextSidebar.width : 0)
            height: parent.height
            color: themeViewModel.canvas
            radius: 12
            clip: true

            Image {
                id: themeBackground
                objectName: "themeBackground"
                anchors.fill: parent
                source: themeViewModel.backgroundImage
                fillMode: Image.PreserveAspectCrop
                opacity: 0.09
                enabled: false
                asynchronous: true
                cache: true
            }

            StackLayout {
                z: 1
                anchors.fill: parent
                currentIndex: appShell.currentSection === "chat" ? 0
                    : appShell.currentSection === "memories" ? 1 : 2

                ChatPage {
                    objectName: "chatPage"
                    theme: themeViewModel
                    chat: chatViewModel
                    showSidebarButton: contextSidebar.overlayMode
                    onSidebarRequested: contextSidebar.drawerOpen = !contextSidebar.drawerOpen
                }
                MemoryPage {
                    objectName: "memoryPage"
                    theme: themeViewModel
                    memories: memoryViewModel
                }
                SettingsPage {
                    objectName: "settingsPage"
                    theme: themeViewModel
                    settings: settingsViewModel
                    memories: memoryViewModel
                    chat: chatViewModel
                    pets: petViewModel
                    diagnostics: diagnosticsViewModel
                    animationModel: petAnimation
                    onCategoryChanged: dialogCoordinator.clear_toasts()
                }
            }
        }
    }

    ToastHost {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 20
        theme: themeViewModel
        toastModel: dialogCoordinator.toastModel
        onDismissRequested: row => dialogCoordinator.dismiss_toast(row)
    }

    ConfirmSheet {
        id: mainConfirmationSheet
        objectName: "mainConfirmationSheet"
        parent: Overlay.overlay
        x: parent ? (parent.width - width) / 2 : 0
        y: parent ? (parent.height - height) / 2 : 0
        theme: themeViewModel
        request: dialogCoordinator.currentConfirmation
        onResolved: (requestId, approved) =>
            dialogCoordinator.resolve_confirmation(requestId, approved)
    }

    // 主面板隐藏时排队的确认在窗口重新显示后必须补弹，避免静默堆积
    function syncConfirmationSheet() {
        const request = dialogCoordinator.currentConfirmation
        if (!request.requestId || !mainWindow.visible)
            mainConfirmationSheet.close()
        else
            mainConfirmationSheet.open()
    }

    onVisibleChanged: syncConfirmationSheet()

    Connections {
        target: appShell
        function onCurrentSectionChanged() { dialogCoordinator.clear_toasts() }
        function onShowMainRequested(section) {
            if (!mainWindow.visible) petViewModel.clear_status()
            mainWindow.show()
            mainWindow.raise()
            mainWindow.requestActivate()
        }
        function onHideMainRequested() {
            dialogCoordinator.clear_toasts()
            petViewModel.clear_status()
            mainWindow.hide()
        }
    }

    Connections {
        target: dialogCoordinator
        function onConfirmationChanged() { mainWindow.syncConfirmationSheet() }
        function onWindowConfirmationChanged() { mainWindow.syncConfirmationSheet() }
    }

    ResizeHandle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 6
        resizeEdges: Qt.LeftEdge
        cursorShape: Qt.SizeHorCursor
    }
    ResizeHandle {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 6
        resizeEdges: Qt.RightEdge
        cursorShape: Qt.SizeHorCursor
    }
    ResizeHandle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 6
        resizeEdges: Qt.TopEdge
        cursorShape: Qt.SizeVerCursor
    }
    ResizeHandle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 6
        resizeEdges: Qt.BottomEdge
        cursorShape: Qt.SizeVerCursor
    }
    ResizeHandle {
        anchors.left: parent.left
        anchors.top: parent.top
        width: 10
        height: 10
        resizeEdges: Qt.LeftEdge | Qt.TopEdge
        cursorShape: Qt.SizeFDiagCursor
    }
    ResizeHandle {
        anchors.right: parent.right
        anchors.top: parent.top
        width: 10
        height: 10
        resizeEdges: Qt.RightEdge | Qt.TopEdge
        cursorShape: Qt.SizeBDiagCursor
    }
    ResizeHandle {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        width: 10
        height: 10
        resizeEdges: Qt.LeftEdge | Qt.BottomEdge
        cursorShape: Qt.SizeBDiagCursor
    }
    ResizeHandle {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        width: 10
        height: 10
        resizeEdges: Qt.RightEdge | Qt.BottomEdge
        cursorShape: Qt.SizeFDiagCursor
    }

    PetWindow {
        objectName: "petWindow"
        animation: petAnimation
        interaction: petInteraction
        theme: themeViewModel
        petScale: settingsViewModel.draft_value("ui", "pet_scale")
    }

    PetMenuWindow {
        id: trayMenuWindow
        objectName: "trayMenuWindow"
        theme: themeViewModel
    }

    ToolConfirmationWindow {
        objectName: "toolConfirmationWindow"
        theme: themeViewModel
        dialogs: dialogCoordinator
    }

    AgentInteractionWindow {
        objectName: "agentInteractionWindow"
        theme: themeViewModel
        dialogs: dialogCoordinator
    }
}
