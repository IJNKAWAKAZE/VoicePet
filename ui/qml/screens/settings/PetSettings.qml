import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "../../components"

Item {
    id: root
    required property QtObject theme
    required property QtObject settings
    required property QtObject pets
    required property QtObject animationModel
    onVisibleChanged: {
        if (!pets) return
        // 离页和返回时都清理已结束的提示，后台导入不受影响
        pets.clear_status()
        if (visible) pets.refresh()
    }

    FileDialog {
        id: fileDialog
        objectName: "petFileDialog"
        title: "导入桌宠形象包"
        nameFilters: ["Codex Pet (*.codex-pet *.zip)", "所有文件 (*)"]
        onAccepted: root.pets.import_pet_url(fileDialog.selectedFile)
    }

    FolderDialog {
        id: folderDialog
        objectName: "petFolderDialog"
        title: "导入桌宠形象目录"
        onAccepted: root.pets.import_pet_url(folderDialog.selectedFolder)
    }

    RowLayout {
        anchors.fill: parent
        spacing: 16

        AppCard {
            Layout.preferredWidth: Math.min(240, root.width * 0.42)
            Layout.fillHeight: true
            theme: root.theme
            accessibleName: "桌宠动态预览"

            Column {
                anchors.fill: parent
                anchors.margins: 18
                spacing: 12
                Text {
                    text: "动态预览"
                    color: root.theme.text
                    font.pixelSize: 18
                    font.bold: true
                }
                Rectangle {
                    width: parent.width
                    height: Math.min(width * 1.2, parent.height - 130)
                    radius: 14
                    color: root.theme.surfaceAlt
                    PetSprite {
                        objectName: "settingsPetPreview"
                        anchors.centerIn: parent
                        animation: root.animationModel
                        petScale: Math.min(
                            1.0,
                            (parent.width - 24) / root.animationModel.cellWidth,
                            (parent.height - 24) / root.animationModel.cellHeight
                        )
                    }
                }
                Flow {
                    width: parent.width
                    spacing: 8
                    AppButton {
                        objectName: "importPetFileButton"
                        theme: root.theme
                        text: "导入形象包"
                        enabled: !root.pets.importBusy
                        kind: "secondary"
                        onClicked: fileDialog.open()
                    }
                    AppButton {
                        objectName: "importPetDirectoryButton"
                        theme: root.theme
                        text: "导入目录"
                        enabled: !root.pets.importBusy
                        kind: "ghost"
                        onClicked: folderDialog.open()
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 12

            AppButton {
                objectName: "refreshPetCatalogButton"
                theme: root.theme
                text: "刷新形象列表"
                kind: "secondary"
                enabled: !root.pets.importBusy
                onClicked: root.pets.refresh()
            }

            Text {
                objectName: "petImportStatus"
                Layout.fillWidth: true
                text: root.pets.statusMessage
                visible: text.length > 0
                color: root.theme.text
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }

            AppTextField {
                Layout.fillWidth: true
                visible: petGrid.count > 12
                theme: root.theme
                placeholderText: "搜索形象"
                onTextChanged: root.pets.set_search(text)
            }

            GridView {
                id: petGrid
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: root.pets.catalogModel
                cellWidth: 180
                cellHeight: 186
                clip: true
                ScrollBar.vertical: ScrollBar { active: true }

                delegate: AppCard {
                    required property string petId
                    required property string displayName
                    required property string description
                    required property bool builtIn
                    required property bool active
                    width: petGrid.cellWidth - 10
                    height: petGrid.cellHeight - 10
                    theme: root.theme
                    accessibleName: displayName
                    border.width: active ? 3 : 1
                    border.color: active ? root.theme.focus : root.theme.border

                    Column {
                        anchors.fill: parent
                        anchors.margins: 12
                        anchors.bottomMargin: builtIn ? 12 : 52
                        spacing: 5
                        Rectangle {
                            objectName: "activePetBadge_" + petId
                            visible: active
                            width: 68
                            height: 22
                            radius: 7
                            color: root.theme.userBubble
                            Text {
                                anchors.centerIn: parent
                                text: "当前使用"
                                color: root.theme.selectedText
                                font.pixelSize: 11
                            }
                        }
                        Text { width: parent.width; text: displayName; color: root.theme.text; font.bold: true; elide: Text.ElideRight }
                        Text {
                            width: parent.width
                            text: description
                            color: root.theme.textMuted
                            wrapMode: Text.Wrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                        Text {
                            width: parent.width
                            text: builtIn ? "内置形象" : petId
                            elide: Text.ElideMiddle
                            color: root.theme.textMuted
                            font.pixelSize: 11
                        }
                    }
                    AppButton {
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 12
                        visible: !builtIn
                        theme: root.theme
                        text: "删除"
                        kind: "danger"
                        height: 32
                        onClicked: root.pets.delete_pet(petId)
                    }
                    TapHandler { onTapped: root.pets.select_pet(petId) }
                }
            }

            Flow {
                Layout.fillWidth: true
                spacing: 12
                AppToggle {
                    theme: root.theme
                    text: "置顶"
                    checked: root.settings.draft_value("ui", "always_on_top")
                    onToggled: root.settings.set_field("ui", "always_on_top", checked)
                }
                AppToggle {
                    theme: root.theme
                    text: "鼠标穿透"
                    checked: root.settings.draft_value("ui", "pet_click_through")
                    onToggled: root.settings.set_field("ui", "pet_click_through", checked)
                }
                Column {
                    width: Math.min(180, parent.width)
                    spacing: 4
                    FieldLabel { theme: root.theme; text: "桌宠缩放 · " + Math.round(petScaleSlider.value * 100) + "%" }
                    AppSlider {
                        id: petScaleSlider
                        width: parent.width
                        theme: root.theme
                        from: 0.5
                        to: 2.0
                        value: root.settings.draft_value("ui", "pet_scale")
                        onMoved: root.settings.set_field("ui", "pet_scale", value)
                    }
                }
            }
        }
    }
}
