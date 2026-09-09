from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPointF, Qt, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from core.config import AppConfig, UiConfig
from core.tts import TtsVoiceOption
from ui.viewmodels.diagnostics import DiagnosticsViewModel
from ui.viewmodels.settings import SettingsViewModel
from ui.viewmodels.theme import ThemeViewModel


class Store:
    def save(self, config):
        pass


class Runtime:
    def list_tts_voices(self):
        future = Future()
        future.set_result((TtsVoiceOption("zh-CN-YunxiNeural", "zh-CN", "Male"),))
        return future


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)


@contextmanager
def page(qapp, name, settings, *, visible=True):
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    diagnostics = DiagnosticsViewModel(Runtime())
    QQuickStyle.setStyle("Basic")
    engine = QQmlEngine()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(items))
    component = QQmlComponent(engine, QUrl.fromLocalFile(
        str(Path(f"ui/qml/screens/settings/{name}.qml").resolve()),
    ))
    properties = {"theme": theme, "settings": settings, "visible": visible}
    if name == "AiSettings":
        properties["diagnostics"] = diagnostics
    if name == "PrivacySettings":
        from ui.viewmodels.chat import ChatViewModel
        from ui.viewmodels.dialogs import DialogCoordinator
        from ui.viewmodels.memories import MemoryViewModel
        dialogs = DialogCoordinator()
        memories = MemoryViewModel(Runtime(), dialogs)
        chat = ChatViewModel(Runtime())
        properties.update(memories=memories, chat=chat)
    root = component.createWithInitialProperties(properties)
    window = QQuickWindow()
    try:
        assert root is not None, component.errors()
        root.setParentItem(window.contentItem())
        root.setWidth(600)
        root.setHeight(660)
        window.resize(600, 660)
        window.show()
        QTest.qWait(30)
        yield root
        assert not warnings, [item.toString() for item in warnings]
    finally:
        window.close()
        if root is not None:
            root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("name", ["GeneralSettings", "AiSettings", "VoiceSettings"])
def test_subpages_leave_save_feedback_to_common_footer(qapp, name):
    settings = SettingsViewModel(AppConfig(), Store(), runtime=Runtime())
    settings.save_draft()
    with page(qapp, name, settings) as root:
        assert not any(item.property("text") == "设置已保存" for item in visual_items(root))


def test_about_button_opens_config_data_directory(qapp, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("PySide6.QtGui.QDesktopServices.openUrl", lambda url: opened.append(url) or True)
    settings = SettingsViewModel(AppConfig(), Store(), data_directory=tmp_path)
    with page(qapp, "AboutSettings", settings) as root:
        button = next(item for item in visual_items(root)
                      if item.property("text") == "打开数据目录" and hasattr(item, "clicked"))
        button.clicked.emit()
        assert opened == [QUrl.fromLocalFile(str(tmp_path))]
        assert any(item.property("text") == str(tmp_path) for item in visual_items(root))


def test_voice_dropdown_preserves_selection_on_refresh_and_updates_draft_on_activation(qapp):
    settings = SettingsViewModel(AppConfig(), Store(), runtime=Runtime())
    saved_voice = settings.config.tts.voice
    with page(qapp, "VoiceSettings", settings) as root:
        combo = root.findChild(QObject, "voiceSelector")
        assert combo is not None
        assert combo.property("currentValue") == saved_voice
        settings.refresh_voices()
        qapp.processEvents()
        assert combo.property("currentValue") == saved_voice
        index = next(index for index, item in enumerate(settings.voiceOptions)
                     if item["short_name"] == "zh-CN-YunxiNeural")
        combo.setProperty("currentIndex", index)
        combo.activated.emit(index)
        assert settings.draft_value("tts", "voice") == "zh-CN-YunxiNeural"
        assert settings.config.tts.voice == saved_voice
        settings.refresh_voices()
        qapp.processEvents()
        assert combo.property("currentValue") == "zh-CN-YunxiNeural"


def test_voice_dropdown_tracks_catalog_reorder_after_discard(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    saved_voice = settings.config.tts.voice
    with page(qapp, "VoiceSettings", settings) as root:
        combo = root.findChild(QObject, "voiceSelector")
        combo.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_Down)
        assert settings.draft_value("tts", "voice") == "zh-CN-YunxiNeural"
        settings.discard_draft()
        assert combo.property("currentValue") == saved_voice
        future = Future()
        future.set_result((
            TtsVoiceOption("zh-CN-YunxiNeural", "zh-CN", "Male"),
            TtsVoiceOption(saved_voice, "zh-CN", "Female"),
        ))
        runtime.list_tts_voices = lambda: future
        settings.refresh_voices()
        qapp.processEvents()
        assert combo.property("currentValue") == saved_voice
        assert combo.property("currentIndex") == 1
        assert settings.draft_value("tts", "voice") == saved_voice


def test_ai_reasoning_effort_dropdown_tracks_draft_and_supports_six_levels(qapp):
    settings = SettingsViewModel(AppConfig(), Store())
    with page(qapp, "AiSettings", settings) as root:
        combo = root.findChild(QObject, "reasoningEffortSelector")
        assert combo is not None
        assert combo.property("model") == ["自动", "最小", "低", "中", "高", "极高"]
        assert combo.property("currentIndex") == 2
        combo.setProperty("currentIndex", 4)
        combo.activated.emit(4)
        assert settings.draft_value("llm", "reasoning_effort") == "high"


def test_asr_dropdown_tracks_changed_model_options_after_discard(qapp):
    settings = SettingsViewModel(AppConfig(), Store(), runtime=Runtime())
    with page(qapp, "VoiceSettings", settings) as root:
        combo = root.findChild(QObject, "asrModelSelector")
        combo.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_Down)
        settings.discard_draft()
        settings.set_field("asr", "model", "custom/model")
        qapp.processEvents()
        assert combo.property("currentValue") == "custom/model"
        settings.set_field("asr", "model", "small")
        qapp.processEvents()
        assert combo.property("currentValue") == "small"


@pytest.mark.parametrize("initially_visible", [False, True])
def test_voice_page_loads_catalog_when_shown_after_runtime_start(qapp, initially_visible):
    class DelayedRuntime(Runtime):
        def __init__(self):
            self.running = False
            self.requests = 0

        def list_tts_voices(self):
            self.requests += 1
            if not self.running:
                raise RuntimeError("运行时尚未启动")
            return super().list_tts_voices()

    runtime = DelayedRuntime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    with page(qapp, "VoiceSettings", settings, visible=initially_visible) as root:
        assert runtime.requests == int(initially_visible)
        root.setVisible(False)
        runtime.running = True
        root.setVisible(True)
        qapp.processEvents()

        assert runtime.requests == int(initially_visible) + 1
        assert settings.voicesError == ""
        assert "zh-CN-YunxiNeural" in [item["short_name"] for item in settings.voiceOptions]
        assert root.findChild(QObject, "voiceSelector").property("currentValue") == settings.config.tts.voice


def test_role_prompt_scrolls_within_bounded_editor_and_preserves_draft(qapp):
    settings = SettingsViewModel(AppConfig(), Store())
    prompt = "请保持角色设定并使用中文回答\n" * 70
    settings.set_field("llm", "system_prompt", prompt)
    with page(qapp, "AiSettings", settings) as root:
        scroll = root.findChild(QObject, "rolePromptScroll")
        assert scroll is not None
        editor = root.findChild(QObject, "rolePromptEditor")
        assert editor.property("text") == prompt
        assert scroll.property("height") == 180
        assert scroll.property("contentHeight") > scroll.property("availableHeight")
        flickable = scroll.property("contentItem")
        flickable.setProperty("contentY", 100)
        qapp.processEvents()
        assert flickable.property("contentY") > 0
        editor.setProperty("text", prompt + "新的草稿")
        assert settings.draft_value("llm", "system_prompt") == prompt + "新的草稿"


def test_discard_restores_focused_typed_ai_field_and_role_prompt(qapp):
    settings = SettingsViewModel(AppConfig(), Store())
    with page(qapp, "AiSettings", settings) as root:
        field = next(item for item in visual_items(root)
                     if item.property("placeholderText") == "模型名称")
        field.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_A, Qt.ControlModifier)
        for character in "unsaved-model":
            QTest.keyClick(root.window(), character)
        assert settings.draft_value("llm", "model") == "unsaved-model"
        settings.discard_draft()
        qapp.processEvents()
        assert field.property("text") == settings.config.llm.model
        editor = root.findChild(QObject, "rolePromptEditor")
        editor.setProperty("text", "changed prompt")
        settings.discard_draft()
        qapp.processEvents()
        assert editor.property("text") == settings.config.llm.system_prompt
        assert settings.draft_value("llm", "system_prompt") == settings.config.llm.system_prompt


def test_discard_restores_user_toggled_voice_and_model_selection(qapp):
    settings = SettingsViewModel(AppConfig(), Store(), runtime=Runtime())
    with page(qapp, "VoiceSettings", settings) as root:
        toggle = next(item for item in visual_items(root)
                      if item.property("text") == "启用中文唤醒" and hasattr(item, "toggled"))
        toggle.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_Space)
        assert settings.draft_value("wake_word", "enabled") != settings.config.wake_word.enabled
        combo = root.findChild(QObject, "asrModelSelector")
        assert combo is not None
        combo.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_Down)
        assert settings.draft_value("asr", "model") != settings.config.asr.model
        settings.discard_draft()
        qapp.processEvents()
        assert toggle.property("checked") == settings.config.wake_word.enabled
        assert combo.property("currentValue") == settings.config.asr.model


@pytest.mark.parametrize("key,placeholder", [
    ("summary_retention_days", "近期摘要保留天数"),
    ("chat_retention_days", "聊天记录保留天数"),
])
def test_privacy_retention_can_be_retyped_and_discarded_without_blur(qapp, key, placeholder):
    settings = SettingsViewModel(AppConfig(), Store())
    with page(qapp, "PrivacySettings", settings) as root:
        field = next(item for item in visual_items(root)
                     if item.property("placeholderText") == placeholder)
        field.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(root.window(), Qt.Key_Backspace)
        assert field.property("text") == ""
        QTest.keyClick(root.window(), "1")
        QTest.keyClick(root.window(), "4")
        assert settings.draft_value("privacy", key) == 14
        settings.discard_draft()
        assert field.property("text") == str(getattr(settings.config.privacy, key))


@pytest.mark.parametrize("key,placeholder", [
    ("summary_retention_days", "近期摘要保留天数"),
    ("chat_retention_days", "聊天记录保留天数"),
])
def test_privacy_retention_retyped_value_can_be_saved(qapp, key, placeholder):
    settings = SettingsViewModel(AppConfig(), Store())
    with page(qapp, "PrivacySettings", settings) as root:
        field = next(item for item in visual_items(root) if item.property("placeholderText") == placeholder)
        field.forceActiveFocus()
        QTest.keyClick(root.window(), Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(root.window(), "2")
        QTest.keyClick(root.window(), "1")
        settings.save_draft()
        assert getattr(settings.config.privacy, key) == 21


def test_privacy_switches_restore_and_auto_dependency_preserves_preference(qapp):
    settings = SettingsViewModel(AppConfig(), Store())
    with page(qapp, "PrivacySettings", settings) as root:
        memory = root.findChild(QObject, "memoryEnabled")
        history = root.findChild(QObject, "chatHistoryEnabled")
        automatic = root.findChild(QObject, "autoMemoryEnabled")
        assert memory is not None and history is not None and automatic is not None
        memory.setProperty("checked", False)
        memory.toggled.emit()
        assert not automatic.property("enabled")
        assert settings.draft_value("privacy", "auto_memory_enabled") is True
        history.setProperty("checked", False)
        history.toggled.emit()
        settings.discard_draft()
        assert memory.property("checked") and history.property("checked")
        assert automatic.property("enabled") and automatic.property("checked")


def test_settings_footer_clears_when_category_changes_or_page_hides(qapp):
    from test_qml_application import build_controller
    controller, _service, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        window = qml.root_objects[0]
        root = next(item for item in visual_items(window.contentItem())
                    if item.metaObject().indexOfProperty("category") >= 0)
        root.setVisible(True)
        root.setProperty("category", "ai")
        settings = root.property("settings")
        settings.save_draft()
        assert settings.statusMessage == "设置已保存"
        root.setProperty("category", "privacy")
        assert settings.statusMessage == ""
        settings.save_draft()
        root.setVisible(False)
        assert settings.statusMessage == ""
        discard = root.findChild(QObject, "discardSettingsButton")
        assert discard is not None and not discard.property("enabled")
    finally:
        controller.close()


def test_footer_discard_click_restores_last_focused_editor_and_disables_itself(qapp):
    from test_qml_application import build_controller
    controller, _service, _tray, qml, shell, *_ = build_controller(qapp)
    controller.start()
    try:
        window = qml.root_objects[0]
        root = next(item for item in visual_items(window.contentItem())
                    if item.metaObject().indexOfProperty("category") >= 0)
        shell.navigate("settings")
        root.setProperty("category", "ai")
        window.show()
        QTest.qWait(20)
        settings = root.property("settings")
        field = next(item for item in visual_items(root)
                     if item.property("placeholderText") == "模型名称")
        field.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        for character in "new-model":
            QTest.keyClick(window, character)
        button = root.findChild(QObject, "discardSettingsButton")
        assert button.property("enabled")
        position = button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, position)
        assert field.property("text") == settings.config.llm.model
        assert settings.draft_value("llm", "model") == settings.config.llm.model
        assert not button.property("enabled")
    finally:
        controller.close()


def test_role_prompt_keeps_cursor_when_draft_updates_during_typing(qapp):
    settings = SettingsViewModel(AppConfig(), Store())
    with page(qapp, "AiSettings", settings) as root:
        editor = root.findChild(QObject, "rolePromptEditor")
        editor.setProperty("text", "abcd")
        editor.forceActiveFocus()
        editor.setProperty("cursorPosition", 2)
        QTest.keyClick(root.window(), "x")
        assert editor.property("text") == "abxcd"
        assert editor.property("cursorPosition") == 3
        settings.set_field("llm", "model", "another-model")
        assert editor.property("cursorPosition") == 3
        QTest.keyClick(root.window(), "y")
        assert settings.draft_value("llm", "system_prompt") == "abxycd"
