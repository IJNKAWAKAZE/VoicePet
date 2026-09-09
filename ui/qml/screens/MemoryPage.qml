import QtQuick
import QtQuick.Controls
import "../components"

Item {
    id: root
    required property QtObject theme
    required property QtObject memories
    property bool ready: false
    property string section: "long"

    function refreshWhenVisible() {
        if (!ready || !visible || memories.loading) return
        memories.refresh()
        memories.refreshChanges()
        memories.refreshSummaries()
    }
    function statusLabel(status) { return status === "confirmed" ? "已保存" : status === "conflicted" ? "待核实" : "待确认" }

    Component.onCompleted: { ready = true; refreshWhenVisible() }
    onVisibleChanged: refreshWhenVisible()

    Column {
        id: toolbar
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        anchors.margins: 20; spacing: 10
        Flow {
            width: parent.width; spacing: 8
            AppButton { theme: root.theme; text: "长期记忆"; kind: root.section === "long" ? "primary" : "ghost"; onClicked: root.section = "long" }
            AppButton { theme: root.theme; text: "近期摘要"; kind: root.section === "summary" ? "primary" : "ghost"; onClicked: root.section = "summary" }
            AppButton { objectName: "memoryRefreshButton"; theme: root.theme; text: "刷新"; kind: "secondary"; enabled: !root.memories.loading && !root.memories.actionBusy; onClicked: { root.refreshWhenVisible(); focus = false } }
        }
        Flow {
            visible: root.section === "long"; width: parent.width; spacing: 8
            Repeater {
                model: [{value:"all",label:"全部"},{value:"confirmed",label:"已保存"},{value:"candidate",label:"待确认"},{value:"conflicted",label:"待核实"}]
                delegate: AppButton { required property var modelData; theme: root.theme; text: modelData.label; kind: root.memories.filterStatus === modelData.value ? "primary" : "ghost"; onClicked: root.memories.set_filter_status(modelData.value) }
            }
        }
        Text { objectName: "memoryStatus"; width: parent.width; visible: text.length > 0; text: root.memories.loading ? "正在加载记忆…" : root.memories.error ? root.memories.error : root.memories.maintenanceStatus ? root.memories.maintenanceStatus : root.section === "long" && memoryList.count === 0 ? "暂无符合筛选条件的记忆" : root.section === "summary" && summaryList.count === 0 ? "暂无近期摘要" : ""; color: root.memories.error ? root.theme.danger : root.theme.textMuted; wrapMode: Text.Wrap }
        Text { width: parent.width; visible: root.memories.actionResult.length > 0; text: root.memories.actionResult; color: root.theme.textMuted; wrapMode: Text.Wrap }
    }

    ListView {
        id: memoryList
        objectName: "memoryList"
        visible: root.section === "long"
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: toolbar.bottom; anchors.bottom: parent.bottom
        anchors.margins: 20; anchors.topMargin: 12; spacing: 10; clip: true
        model: root.memories.memoryModel; ScrollBar.vertical: ScrollBar {}
        delegate: AppCard {
            id: card
            required property int index
            required property string memoryId; required property string category; required property string content
            required property string status; required property string createdAt; required property string source
            required property string originLabel; required property string sourceTitle; required property string sourceState
            required property string sourceTime; required property int version; required property string conflictContent
            readonly property bool selected: root.memories.selectedMemory.memoryId === memoryId
            objectName: "memoryCard-" + memoryId; width: memoryList.width; height: body.implicitHeight + 28
            theme: root.theme; border.color: selected ? root.theme.focus : root.theme.border
            Column {
                id: body; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 14; spacing: 8
                Text { width: parent.width; text: card.category + " · " + root.statusLabel(card.status) + " · " + card.originLabel; color: root.theme.textMuted; wrapMode: Text.Wrap }
                Text { width: parent.width; text: card.content; color: root.theme.text; wrapMode: Text.Wrap }
                Text { width: parent.width; visible: card.status === "conflicted"; text: "原内容：" + (card.conflictContent || "关联旧记录不可用") + "\n新内容：" + card.content; color: root.theme.textMuted; wrapMode: Text.Wrap }
                Text { objectName: "memoryDetails-" + card.memoryId; width: parent.width; visible: card.selected; text: "来源：" + (card.sourceTitle || "来源会话不可用") + " · " + (card.sourceTime || card.createdAt) + "\n记录 ID：" + card.memoryId + " · 来源轮次：" + (card.source || "未知"); color: root.theme.textMuted; wrapMode: Text.WrapAnywhere }
                Flow { width: parent.width; visible: card.selected; spacing: 8
                    AppButton { objectName: "memoryConfirmButton-" + card.memoryId; theme: root.theme; text: "确认保存"; visible: root.memories.canConfirm; enabled: !root.memories.actionBusy; onClicked: root.memories.confirm_selected() }
                    AppButton { objectName: "memoryResolveButton-" + card.memoryId; theme: root.theme; text: "采用新内容"; visible: root.memories.canResolve; enabled: !root.memories.actionBusy; onClicked: root.memories.resolve_selected() }
                    AppButton { objectName: "memoryEditButton-" + card.memoryId; theme: root.theme; text: "编辑"; enabled: !root.memories.actionBusy; onClicked: { root.memories.begin_edit_selected(); editor.text = card.content; editDialog.open() } }
                    AppButton { objectName: "memoryDeleteButton-" + card.memoryId; theme: root.theme; text: "删除"; kind: "danger"; enabled: !root.memories.actionBusy; onClicked: root.memories.delete_selected() }
                }
            }
            TapHandler { onTapped: { root.memories.select(card.memoryId); memoryList.positionViewAtIndex(card.index, ListView.Contain) } }
        }
    }

    ListView {
        id: summaryList; objectName: "summaryList"; visible: root.section === "summary"
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: toolbar.bottom; anchors.bottom: parent.bottom; anchors.margins: 20; spacing: 10; clip: true
        model: root.memories.summaryModel; ScrollBar.vertical: ScrollBar {}
        delegate: AppCard {
            required property string summaryId; required property string sourceTitle; required property string topic
            required property var decisions; required property var unfinishedItems; required property string createdAt; required property string expiresAt
            width: summaryList.width; height: summaryBody.implicitHeight + 28; theme: root.theme
            Column { id: summaryBody; anchors.fill: parent; anchors.margins: 14; spacing: 7
                Text { width: parent.width; text: topic || "未命名话题"; color: root.theme.text; font.pixelSize: 15; wrapMode: Text.Wrap }
                Text { width: parent.width; text: "决定：" + (decisions.length ? decisions.join("；") : "无") + "\n待办：" + (unfinishedItems.length ? unfinishedItems.join("；") : "无"); color: root.theme.textMuted; wrapMode: Text.Wrap }
                Text { width: parent.width; text: sourceTitle + " · " + createdAt + " · 到期 " + expiresAt; color: root.theme.textMuted; wrapMode: Text.Wrap }
                AppButton { theme: root.theme; text: "删除摘要"; kind: "danger"; enabled: !root.memories.actionBusy; onClicked: root.memories.delete_summary(summaryId) }
            }
        }
    }

    AppCard {
        id: changesPanel; objectName: "memoryChangesPanel"; visible: false
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 20
        height: 92; theme: root.theme
        Column { id: changesColumn; anchors.fill: parent; anchors.margins: 10; spacing: 6
            Text { text: "近期变更"; color: root.theme.text; font.pixelSize: 14 }
            ListView { width: parent.width; height: 48; model: root.memories.changeModel; spacing: 5; clip: true
                delegate: Row { required property string changeId; required property string content; required property string createdAt; required property bool canUndo; required property string undoReason; spacing: 8
                    Text { width: Math.max(180, changesPanel.width - 220); text: (content || "记录已删除") + " · " + createdAt; color: root.theme.textMuted; elide: Text.ElideRight }
                    AppButton { theme: root.theme; text: canUndo ? "撤销" : undoReason; enabled: canUndo && !root.memories.actionBusy; onClicked: root.memories.undo_change(changeId) }
                }
            }
        }
    }

    Dialog {
        id: editDialog; objectName: "memoryEditDialog"; title: "编辑记忆"; modal: true; anchors.centerIn: parent; width: Math.min(parent.width - 32, 520)
        palette.window: root.theme.surface
        palette.windowText: root.theme.text
        onAccepted: root.memories.save_edit(editor.text)
        contentItem: AppTextArea { id: editor; objectName: "memoryEditor"; theme: root.theme; width: parent.width; implicitHeight: 120; wrapMode: TextEdit.Wrap }
        footer: Item {
            implicitHeight: 64
            Row {
                anchors.right: parent.right
                anchors.rightMargin: 20
                anchors.verticalCenter: parent.verticalCenter
                spacing: 10
                AppButton { objectName: "memoryEditCancel"; theme: root.theme; text: "取消"; kind: "secondary"; onClicked: editDialog.reject() }
                AppButton { objectName: "memoryEditSave"; theme: root.theme; text: "保存"; onClicked: editDialog.accept() }
            }
        }
    }
}
