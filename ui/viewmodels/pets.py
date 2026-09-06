"""桌宠形象目录、导入、选择和安全删除 ViewModel"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, ClassVar, Protocol

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)

from core.pet_packages import PetPackageError
from ui.pet_shell import PetChoice
from ui.qml_resources import QmlResourceError, validated_asset_url

from .settings import SettingsViewModel

_INVALID_INDEX = QModelIndex()


class PetRuntimeProtocol(Protocol):
    def install_pet(self, source: str) -> Future[Path]: ...

    def remove_pet(self, pet_id: str) -> Future[bool]: ...


class PetCatalogModel(QAbstractListModel):
    """只向 QML 暴露经过验证的桌宠目录 URL"""

    _ROLES: ClassVar[dict[int, bytes]] = {
        Qt.UserRole + 1: b"petId",
        Qt.UserRole + 2: b"displayName",
        Qt.UserRole + 3: b"description",
        Qt.UserRole + 4: b"builtIn",
        Qt.UserRole + 5: b"active",
        Qt.UserRole + 6: b"directoryUrl",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, object]] = []

    def roleNames(self) -> dict[int, bytes]:
        return self._ROLES

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        name = self._ROLES.get(role)
        return None if name is None else self._items[index.row()].get(name.decode())

    def replace_items(self, items: list[dict[str, object]]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()


class PetViewModel(QObject):
    """协调桌宠目录与设置，保持主题选择完全独立"""

    previewRequested = Signal(str)
    searchChanged = Signal()
    errorOccurred = Signal(str)
    statusMessageChanged = Signal()
    importBusyChanged = Signal()
    _futureFinished = Signal(str, object, object)
    _STATUS_TIMEOUT_MS = 3000

    def __init__(
        self,
        runtime: PetRuntimeProtocol,
        settings: SettingsViewModel,
        catalog_loader: Callable[[], tuple[PetChoice, ...]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._settings = settings
        self._catalog_loader = catalog_loader
        self._model = PetCatalogModel(self)
        self._choices: dict[str, PetChoice] = {}
        self._search = ""
        self._status_message = ""
        self._import_busy = False
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.clear_status)
        self._futureFinished.connect(self._handle_future)

    @Property(QObject, constant=True)
    def catalog_model(self) -> PetCatalogModel:
        return self._model

    @Property(QObject, constant=True)
    def catalogModel(self) -> PetCatalogModel:
        return self._model

    @Property(str, notify=searchChanged)
    def search(self) -> str:
        return self._search

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(bool, notify=importBusyChanged)
    def importBusy(self) -> bool:
        return self._import_busy

    def _set_status(self, message: str) -> None:
        # 新状态重新计时，导入中的进度不因旧成功提示到期而消失
        self._status_timer.stop()
        self._status_message = message
        self.statusMessageChanged.emit()
        if message and not self._import_busy:
            self._status_timer.start(self._STATUS_TIMEOUT_MS)

    @Slot()
    def clear_status(self) -> None:
        if not self._import_busy:
            self._set_status("")

    def _set_import_busy(self, busy: bool) -> None:
        self._import_busy = busy
        self.importBusyChanged.emit()

    @Slot(QUrl)
    def import_pet_url(self, source: QUrl) -> None:
        """由 Python 转换选择器 URL，保留中文、空格及特殊字符"""

        if not source.isLocalFile():
            self._set_status("请选择本地桌宠包或形象目录")
            self.errorOccurred.emit(self._status_message)
            return
        self.import_pet(source.toLocalFile())

    @Slot()
    def refresh(self) -> None:
        try:
            choices = self._catalog_loader()
        except (OSError, RuntimeError):
            self.errorOccurred.emit("桌宠形象列表加载失败")
            return
        ordered = sorted(
            choices,
            key=lambda choice: (
                not choice.built_in,
                choice.display_name.casefold(),
                choice.pet_id,
            ),
        )
        self._choices = {choice.pet_id: choice for choice in ordered}
        self._apply_filter()

    @Slot(str)
    def set_search(self, query: str) -> None:
        normalized = query.strip().casefold() if isinstance(query, str) else ""
        if normalized == self._search:
            return
        self._search = normalized
        self.searchChanged.emit()
        self._apply_filter()

    @Slot(str)
    def select_pet(self, pet_id: str) -> None:
        if pet_id not in self._choices:
            self.errorOccurred.emit("桌宠形象不存在")
            return
        self._settings.set_field("ui", "active_skin", pet_id)
        if self._settings.config.ui.active_skin != pet_id:
            self.errorOccurred.emit("桌宠形象切换失败")
            return
        self._apply_filter()
        self.previewRequested.emit(pet_id)

    def directory_for(self, pet_id: str) -> Path | None:
        """只解析已加载目录中的桌宠形象"""

        choice = self._choices.get(pet_id)
        return None if choice is None else choice.directory

    @Slot(str)
    def import_pet(self, source: str) -> None:
        if self._import_busy or not isinstance(source, str) or not source:
            return
        self._set_import_busy(True)
        self._set_status("正在校验并导入形象…")
        try:
            self._watch("import", self._runtime.install_pet(source))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self._set_import_busy(False)
            self._set_status("桌宠导入当前不可用")
            self.errorOccurred.emit(self._status_message)

    @Slot(str)
    def delete_pet(self, pet_id: str) -> None:
        choice = self._choices.get(pet_id)
        if choice is None:
            self.errorOccurred.emit("桌宠形象不存在")
            return
        if choice.built_in:
            self.errorOccurred.emit("内置桌宠不能删除")
            return
        if self._settings.config.ui.active_skin == pet_id:
            fallback = next(
                (item.pet_id for item in self._choices.values() if item.built_in),
                "dpsk-girl",
            )
            self._settings.set_field("ui", "active_skin", fallback)
            if self._settings.config.ui.active_skin != fallback:
                self.errorOccurred.emit("切换回内置桌宠后才能删除当前形象")
                return
        try:
            self._watch("delete", self._runtime.remove_pet(pet_id))
        except RuntimeError:
            self.errorOccurred.emit("桌宠删除当前不可用")

    def _watch(self, operation: str, future: Future[Any]) -> None:
        def done(completed: Future[Any]) -> None:
            try:
                self._futureFinished.emit(operation, completed.result(), None)
            except Exception as error:  # noqa: BLE001
                self._futureFinished.emit(operation, None, error)

        future.add_done_callback(done)

    @Slot(str, object, object)
    def _handle_future(self, operation: str, result: object, error: object) -> None:
        if operation == "import":
            self._set_import_busy(False)
        if error is not None:
            # 包校验异常仅包含安装器定义的可展示说明
            message = str(error) if isinstance(error, PetPackageError) else "桌宠操作失败，请检查形象包"
            self._set_status(message)
            self.errorOccurred.emit(message)
            return
        self.refresh()
        if operation == "import":
            pet_id = Path(str(result)).name
            if pet_id in self._choices:
                self.set_search("")
                self.select_pet(pet_id)
                if self._settings.config.ui.active_skin == pet_id:
                    self._set_status("已导入并切换：" + self._choices[pet_id].display_name)
                else:
                    self._set_status("形象已导入，但切换失败，请重新选择")
            else:
                self._set_status("形象已安装，但未能加载到列表，请检查形象包")
                self.errorOccurred.emit(self._status_message)

    def _apply_filter(self) -> None:
        active = self._settings.config.ui.active_skin
        items = []
        for choice in self._choices.values():
            searchable = f"{choice.pet_id} {choice.display_name}".casefold()
            if self._search and self._search not in searchable:
                continue
            try:
                manifest_url = validated_asset_url(
                    choice.directory / "pet.json", (choice.directory,)
                )
            except QmlResourceError:
                continue
            directory_url = QUrl(manifest_url.toString().rsplit("/", 1)[0] + "/")
            items.append(
                {
                    "petId": choice.pet_id,
                    "displayName": choice.display_name,
                    "description": _read_description(choice.directory),
                    "builtIn": choice.built_in,
                    "active": choice.pet_id == active,
                    "directoryUrl": directory_url,
                }
            )
        self._model.replace_items(items)


def _read_description(directory: Path) -> str:
    try:
        data = json.loads((directory / "pet.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    value = data.get("description", "") if isinstance(data, dict) else ""
    return value[:240] if isinstance(value, str) else ""
