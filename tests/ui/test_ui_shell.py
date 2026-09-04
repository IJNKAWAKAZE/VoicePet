import json
import os
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSize, Qt, QUrl
from PySide6.QtGui import QEnterEvent, QPalette, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QScrollArea, QToolButton

from core.config import AppConfig
from core.event_bus import EventBus
from core.events import ConversationPhase, CorrelationId, TextDelta, TurnId
from core.tts import TtsVoiceOption
from ui.confirm_dialog import ConfirmationDialog
from ui.event_bridge import QtEventBridge
from ui.manual_input import ManualInputDialog
from ui.pet_shell import (
    ConversationBubble,
    PetChoice,
    PetShellWindow,
    clamp_window_top_left,
    discover_pet_choices,
    resolve_active_pet_directory,
)
from ui.settings_window import SettingsWindow
from ui.tray import TrayController


def app():
    return QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.001)
    assert predicate()


def write_pet_choice(
    parent,
    directory_name,
    *,
    pet_id=None,
    display_name="测试形象",
    version=2,
    sprite_path="spritesheet.webp",
):
    directory = parent / directory_name
    directory.mkdir(parents=True)
    (directory / "pet.json").write_text(
        json.dumps(
            {
                "id": pet_id or directory_name,
                "displayName": display_name,
                "description": "测试说明",
                "spriteVersionNumber": version,
                "spritesheetPath": sprite_path,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if sprite_path == "spritesheet.webp":
        (directory / sprite_path).write_bytes(b"sprite")
    return directory


def test_event_bridge_queues_runtime_events_from_background_thread():
    app()
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta,))
    received = []
    threads = []

    def receive(event):
        received.append(event)
        threads.append(threading.get_ident())

    bridge.event_received.connect(receive)
    event = TextDelta(TurnId.new(), CorrelationId.new(), "你好")
    worker = threading.Thread(target=lambda: __import__("asyncio").run(bus.publish(event)))
    worker.start()
    worker.join()
    spin_until(lambda: received == [event])

    assert threads == [threading.get_ident()]
    bridge.close()
    __import__("asyncio").run(bus.publish(event))
    QCoreApplication.processEvents()
    assert received == [event]


def test_settings_window_has_deep_sea_sections_and_emits_config():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)

    assert window.windowTitle() == "VoicePet 设置"
    assert window.navigation_labels == (
        "语音",
        "AI",
        "会话",
        "桌宠",
        "隐私",
        "诊断",
    )
    assert "#5FD3C7" in window.styleSheet()
    assert window.findChild(type(window.save_button), "save_settings") is window.save_button

    window.save_button.click()

    assert saved == [AppConfig()]
    window.close()
    assert window.isVisible() is False


def test_settings_window_shows_explicit_select_and_spin_controls():
    app()
    window = SettingsWindow(AppConfig())
    select_buttons = window.findChildren(QToolButton, "select_toggle")
    up_buttons = window.findChildren(QToolButton, "spin_up")
    down_buttons = window.findChildren(QToolButton, "spin_down")

    assert len(select_buttons) == 4
    assert all(button.accessibleName() == "展开选项" for button in select_buttons)
    assert all(not button.toolTip() for button in select_buttons)
    assert len(up_buttons) == 2
    assert len(down_buttons) == 2
    voice_toggle = window.voice_control.findChild(QToolButton, "select_toggle")
    QCoreApplication.sendEvent(voice_toggle, QEvent(QEvent.Type.Enter))
    assert window.voice_control.property("interactionActive") is True
    assert voice_toggle._hovered is True
    debounce_up = window.debounce_control.findChild(QToolButton, "spin_up")
    debounce_down = window.debounce_control.findChild(QToolButton, "spin_down")
    previous = window.debounce_spin.value()
    debounce_up.click()
    assert window.debounce_spin.value() == previous + 0.1
    debounce_down.click()
    assert window.debounce_spin.value() == previous
    window.pet_combo.showPopup()
    QCoreApplication.processEvents()
    popup = window.pet_combo.view().window()
    popup_image = popup.grab().toImage()
    assert popup.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    assert popup.palette().color(QPalette.ColorRole.Window).name() == "#1a212a"
    assert popup_image.pixelColor(popup_image.width() // 2, 0).name() != "#ffffff"
    assert (
        popup_image.pixelColor(
            popup_image.width() // 2,
            popup_image.height() - 1,
        ).name()
        != "#ffffff"
    )
    window.pet_combo.hidePopup()
    window.close()


def test_settings_window_uses_scrollable_cards_and_fixed_footer():
    app()
    window = SettingsWindow(AppConfig())

    assert window.width() >= 860
    assert window.height() >= 640
    assert window._pages.count() == 6
    assert all(
        isinstance(window._pages.widget(index), QScrollArea)
        and window._pages.widget(index).widgetResizable()
        for index in range(window._pages.count())
    )
    assert len(window.findChildren(QFrame, "settings_card")) >= 5
    assert window.footer.objectName() == "settings_footer"
    assert window.persona_edit.minimumHeight() >= 180
    assert (
        window.persona_edit.verticalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    window.close()


def test_settings_window_builds_new_config_from_edited_controls():
    app()
    window = SettingsWindow(AppConfig())
    window.set_pet_choices(
        (
            PetChoice(
                "dpsk-girl-night",
                "夜间形象",
                Path("assets/pet/dpsk-girl-night"),
                False,
            ),
        ),
        "dpsk-girl-night",
    )
    saved = []
    window.save_requested.connect(saved.append)

    window.sensitivity_slider.setValue(72)
    window.debounce_spin.setValue(2.4)
    window.asr_model_edit.setText("medium")
    window.voice_combo.setCurrentIndex(
        window.voice_combo.findData("zh-CN-YunxiNeural")
    )
    window.llm_model_edit.setText("gpt-test")
    window.llm_api_combo.setCurrentIndex(
        window.llm_api_combo.findData("chat_completions")
    )
    window.llm_base_url_edit.setText("  http://localhost:11434/v1  ")
    window.reasoning_combo.setCurrentText("high")
    window.store_checkbox.setChecked(True)
    window.persona_edit.setPlainText("  你是一只沉稳的蓝色猫娘  ")
    window.always_on_top_checkbox.setChecked(False)
    window.hot_reload_checkbox.setChecked(False)
    window.memory_checkbox.setChecked(False)
    window.short_term_retention_spin.setValue(14)
    window.diagnostic_recording_checkbox.setChecked(True)
    window.save_button.click()

    config = saved[-1]
    assert config.wake_word.sensitivity == 0.72
    assert config.wake_word.debounce_sec == 2.4
    assert config.asr.model == "medium"
    assert config.tts.voice == "zh-CN-YunxiNeural"
    assert config.llm.model == "gpt-test"
    assert config.llm.api == "chat_completions"
    assert config.llm.base_url == "http://localhost:11434/v1"
    assert config.llm.reasoning_effort == "high"
    assert config.llm.system_prompt == "你是一只沉稳的蓝色猫娘"
    assert config.llm.store is True
    assert config.ui.active_skin == "dpsk-girl-night"
    assert config.ui.always_on_top is False
    assert config.ui.hot_reload_skin is False
    assert config.privacy.memory_enabled is False
    assert config.privacy.short_term_retention_days == 14
    assert config.privacy.diagnostic_recording is True
    assert window.sensitivity_value.text() == "72%"
    window.close()


def test_settings_window_lists_dynamic_chinese_voices_and_requests_preview():
    app()
    window = SettingsWindow(AppConfig())
    previews = []
    window.voice_preview_requested.connect(previews.append)
    voices = (
        TtsVoiceOption("zh-CN-XiaoxiaoNeural", "zh-CN", "Female"),
        TtsVoiceOption("zh-TW-YunJheNeural", "zh-TW", "Male"),
    )

    window.set_voice_options(voices)
    window.voice_combo.setCurrentIndex(1)
    window.voice_preview_button.click()

    assert window.voice_combo.count() == 2
    assert window.voice_combo.itemData(1) == "zh-TW-YunJheNeural"
    assert "中国台湾" in window.voice_combo.itemText(1)
    assert previews == ["zh-TW-YunJheNeural"]
    assert "2 个" in window.voice_catalog_status.text()
    window.close()


def test_settings_window_loads_llm_protocol_and_base_url():
    app()
    config = replace(
        AppConfig(),
        llm=replace(
            AppConfig().llm,
            api="chat_completions",
            base_url="https://gateway.example/v1",
            system_prompt="称呼用户为主人",
        ),
    )
    window = SettingsWindow(config)

    assert window.llm_api_combo.currentData() == "chat_completions"
    assert window.llm_base_url_edit.text() == "https://gateway.example/v1"
    assert window.persona_edit.toPlainText() == "称呼用户为主人"
    window.close()


def test_settings_window_selects_and_saves_pet_choice(tmp_path):
    app()
    window = SettingsWindow(AppConfig())
    choices = (
        PetChoice("dpsk-girl", "迪普斯伊克", tmp_path / "built-in", True),
        PetChoice("forest-cat", "森林猫", tmp_path / "forest-cat", False),
    )
    selected = []
    saved = []
    window.pet_selection_changed.connect(selected.append)
    window.save_requested.connect(saved.append)

    window.set_pet_choices(choices, "dpsk-girl")

    assert window.pet_combo.isEditable() is False
    assert window.pet_combo.currentText() == "迪普斯伊克（dpsk-girl）"
    assert window.selected_pet_id() == "dpsk-girl"
    window.pet_combo.setCurrentIndex(1)
    window.save_button.click()

    assert selected == ["forest-cat"]
    assert saved[-1].ui.active_skin == "forest-cat"
    window.close()


def test_settings_window_loads_and_saves_voice_toggle():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)

    assert window.speech_enabled_checkbox.isChecked() is True
    window.speech_enabled_checkbox.setChecked(False)
    window.save_button.click()

    assert saved[-1].tts.enabled is False
    window.close()


def test_settings_window_edits_chinese_wake_configuration():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)

    assert window.wake_enabled_checkbox.isChecked() is True
    assert window.wake_keyword_edit.text() == "你好，小蓝"
    assert not hasattr(window, "wake_model_import_button")
    window.wake_keyword_edit.setText("你好海蓝")
    window.wake_enabled_checkbox.setChecked(False)
    window.save_button.click()

    assert saved[-1].wake_word.keyword == "你好海蓝"
    assert saved[-1].wake_word.enabled is False
    window.close()


def test_settings_window_rejects_invalid_wake_keyword():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)
    window.wake_keyword_edit.setText("hello")

    window.save_button.click()

    assert saved == []
    assert "唤醒词" in window.validation_message.text()
    window.close()


def test_settings_window_shows_wake_download_status_and_progress():
    app()
    window = SettingsWindow(AppConfig())
    downloads = []
    cancellations = []
    window.wake_model_download_requested.connect(lambda: downloads.append(True))
    window.wake_model_download_cancel_requested.connect(
        lambda: cancellations.append(True)
    )

    window.set_wake_model_status("missing", "中文唤醒模型尚未下载")
    window.wake_model_download_button.click()
    window.set_wake_model_status("downloading", "正在下载中文唤醒模型")
    window.set_wake_download_progress(16_327_433, 32_654_866)

    assert downloads == [True]
    assert window.wake_model_progress.isHidden() is False
    assert window.wake_model_progress.value() == 50
    assert "QProgressBar::chunk" in window.styleSheet()
    assert window.wake_model_cancel_button.isHidden() is False
    window.wake_model_cancel_button.click()
    assert cancellations == [True]
    window.close()


def test_settings_window_rejects_oversized_persona():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)
    window.persona_edit.setPlainText("角色" * 2001)

    window.save_button.click()

    assert saved == []
    assert "4000" in window.validation_message.text()
    window.close()


def test_settings_window_rejects_public_http_llm_endpoint():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)
    window.llm_base_url_edit.setText("http://example.com/v1")

    window.save_button.click()

    assert saved == []
    assert "HTTPS" in window.validation_message.text()
    window.close()


def test_settings_window_keeps_invalid_text_local_and_explains_correction():
    app()
    window = SettingsWindow(AppConfig())
    saved = []
    window.save_requested.connect(saved.append)
    window.llm_model_edit.clear()

    window.save_button.click()

    assert saved == []
    assert "不能为空" in window.validation_message.text()
    window.close()


def test_tray_actions_emit_clear_user_intents():
    app()
    tray = TrayController()
    intents = []
    tray.wake_requested.connect(lambda: intents.append("wake"))
    tray.manual_input_requested.connect(lambda: intents.append("manual"))
    tray.settings_requested.connect(lambda: intents.append("settings"))
    tray.quit_requested.connect(lambda: intents.append("quit"))

    tray.wake_action.trigger()
    tray.manual_input_action.trigger()
    tray.settings_action.trigger()
    tray.quit_action.trigger()

    assert intents == ["wake", "manual", "settings", "quit"]
    assert tray.wake_action.text() == "开始聆听"
    assert tray.manual_input_action.text() == "手动输入"
    assert tray.settings_action.text() == "打开设置"
    assert tray.quit_action.text() == "退出 VoicePet"
    tray.close()


def test_manual_input_dialog_submits_with_enter_and_keeps_errors_local():
    app()
    dialog = ManualInputDialog()
    submitted = []
    dialog.submitted.connect(submitted.append)
    dialog.open_for_input()

    QTest.keyClick(dialog.text_edit, Qt.Key.Key_Return)
    assert submitted == []
    assert "请输入" in dialog.error_label.text()

    dialog.text_edit.setText("  帮我看看天气  ")
    QTest.keyClick(dialog.text_edit, Qt.Key.Key_Return)
    assert submitted == ["帮我看看天气"]
    assert dialog.isVisible()

    dialog.set_submitting(True)
    assert dialog.send_button.isEnabled() is False
    dialog.submission_succeeded()
    assert dialog.text_edit.text() == ""
    assert dialog.isVisible()
    assert dialog.send_button.text() == "回复中…"
    assert dialog.text_edit.isEnabled() is False
    dialog.set_processing(False)
    assert dialog.text_edit.isEnabled() is True
    dialog.append_user_message("帮我看看天气")
    dialog.append_assistant_delta("今天晴朗")
    assert "帮我看看天气" in dialog.conversation_text
    assert "今天晴朗" in dialog.conversation_text
    assert not hasattr(dialog, "cancel_button")
    dialog.close()
    assert dialog.isHidden()
    dialog.open_for_input()
    assert "今天晴朗" in dialog.conversation_text
    dialog.close()


def test_chat_window_sidebar_switches_sessions_without_reopening():
    app()
    window = ManualInputDialog()
    switched = []
    deleted = []
    window.session_activate_requested.connect(switched.append)
    window.session_delete_requested.connect(deleted.append)
    window.set_session("session-1", (), "当前会话")
    records = (
        SimpleNamespace(
            id="session-1",
            title="当前会话",
            turn_count=2,
            updated_at=datetime(2026, 9, 4, 10, tzinfo=UTC),
            is_active=True,
        ),
        SimpleNamespace(
            id="session-2",
            title="另一个会话",
            turn_count=3,
            updated_at=datetime(2026, 9, 4, 9, tzinfo=UTC),
            is_active=False,
        ),
    )

    window.set_session_records(records)
    window.open_for_input()
    second_card = window.session_list.itemWidget(window.session_list.item(1))
    second_card.activated.emit("session-2")
    delete_button = second_card.findChild(QToolButton, "delete_session")
    delete_button.click()

    assert window.session_list.count() == 2
    assert switched == ["session-2"]
    assert deleted == ["session-2"]
    assert window.isVisible()
    window.close()


def test_chat_window_uses_wide_bubbles_typing_status_and_scrolls_to_bottom():
    application = app()
    window = ManualInputDialog()
    window.set_session("session-1", (), "测试会话")
    window.open_for_input()
    window.set_assistant_typing(True)
    application.processEvents()

    typing_labels = window.findChildren(QLabel, "typing_body")
    assert any(label.isVisible() and "正在输入" in label.text() for label in typing_labels)

    window.append_user_message("继续")
    window.append_assistant_delta("这是一条")
    application.processEvents()
    first_bubble = window.findChildren(QFrame, "message_assistant")[-1]
    initial_width = first_bubble.width()
    window.append_assistant_delta(
        "较长的回复，用来确认聊天气泡会随着流式回复持续加宽，"
        "不会再把每句话挤成一条很窄的文字柱。"
    )
    application.processEvents()
    assert first_bubble.width() > initial_width
    for index in range(18):
        window.append_user_message(f"第 {index} 条消息")
        window.append_assistant_delta(f"第 {index} 条回复")
        window.finish_assistant_message()
    spin_until(
        lambda: window.history_view.verticalScrollBar().maximum() > 0
        and window.history_view.verticalScrollBar().value()
        == window.history_view.verticalScrollBar().maximum()
    )

    assistant_bubbles = window.findChildren(QFrame, "message_assistant")
    assert any(bubble.width() >= 500 for bubble in assistant_bubbles)
    assert window.history_view.verticalScrollBar().value() > 0
    assert not any(label.isVisible() for label in window.findChildren(QLabel, "typing_body"))
    window.close()


def test_chat_window_first_open_scrolls_existing_history_to_bottom():
    app()
    window = ManualInputDialog()
    turns = tuple(
        SimpleNamespace(
            user_text=f"历史问题 {index}",
            assistant_text=f"历史回答 {index}",
        )
        for index in range(20)
    )
    window.set_session("session-1", turns, "历史会话")

    window.open_for_input()

    spin_until(
        lambda: window.history_view.verticalScrollBar().maximum() > 0
        and window.history_view.verticalScrollBar().value()
        == window.history_view.verticalScrollBar().maximum()
    )
    window.close()


def test_pet_shell_is_transparent_topmost_and_bubble_renders_safe_markdown():
    app()
    window = PetShellWindow()
    bubble = ConversationBubble()

    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowFlags() & Qt.WindowType.Tool
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    bubble.set_message("我是 **VoicePet**\n\n- 回答问题\n- 整理信息")
    assert bubble.text() == "我是 VoicePet\n回答问题\n整理信息"
    assert "**" not in bubble.text()
    assert bubble.openExternalLinks() is False
    assert bubble.openLinks() is False
    assert (
        bubble.horizontalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )

    bubble.set_message("<b>这不是 HTML</b>")
    assert "<b>" in bubble.text()
    assert (
        bubble.loadResource(
            QTextDocument.ResourceType.ImageResource,
            QUrl("https://example.com/private.png"),
        )
        is None
    )
    window.close()


def test_conversation_bubble_bounds_long_content_with_vertical_scroll():
    app()
    bubble = ConversationBubble(max_chars=400)

    bubble.set_message("内容" * 250)
    bubble.show()
    QCoreApplication.processEvents()

    assert bubble.height() <= 260
    assert bubble.verticalScrollBar().maximum() > 0
    assert bubble.text().endswith("…")
    bubble.close()


def test_pet_shell_grows_upward_without_clipping_renderer():
    app()
    window = PetShellWindow()
    window.show()
    QCoreApplication.processEvents()
    window.move(500, 600)
    previous_bottom = window.frameGeometry().bottom()
    renderer_size = window._renderer.size()

    window.show_message(
        "第一行\n\n" + "很长的内容" * 80,
        (QRect(0, 0, 4000, 4000),),
    )
    QCoreApplication.processEvents()

    assert renderer_size == QSize(192, 208)
    assert window._renderer.size() == renderer_size
    assert window.frameGeometry().bottom() == previous_bottom
    assert window.bubble.height() <= 260
    window.close()


def test_pet_shell_hides_scheduled_message_without_moving_bottom():
    app()
    window = PetShellWindow()
    window.show()
    window.move(500, 600)
    window.show_message("稍后隐藏", (QRect(0, 0, 4000, 4000),))
    previous_bottom = window.frameGeometry().bottom()

    window.schedule_message_hide(20)
    spin_until(lambda: not window.bubble.isVisible())

    assert window.frameGeometry().bottom() == previous_bottom
    window.close()


def test_pet_shell_pauses_and_resumes_message_hide_while_hovered():
    app()
    window = PetShellWindow()
    window.show()
    window.show_message("悬停阅读", (QRect(0, 0, 4000, 4000),))
    window.schedule_message_hide(80)
    QTest.qWait(20)

    window.bubble.enterEvent(
        QEnterEvent(QPoint(1, 1), QPoint(1, 1), QPoint(1, 1))
    )
    QTest.qWait(100)
    QCoreApplication.processEvents()
    assert window.bubble.isVisible()

    window.bubble.leaveEvent(QEvent(QEvent.Type.Leave))
    spin_until(lambda: not window.bubble.isVisible())
    window.close()


def test_pet_shell_replacement_message_cancels_previous_hide_schedule():
    app()
    window = PetShellWindow()
    window.show()
    window.show_message("旧消息", (QRect(0, 0, 4000, 4000),))
    window.schedule_message_hide(40)
    QTest.qWait(10)

    window.show_message("新消息", (QRect(0, 0, 4000, 4000),))
    QTest.qWait(60)
    QCoreApplication.processEvents()

    assert window.bubble.isVisible()
    assert window.bubble.text() == "新消息"
    window.close()


def test_window_position_clamps_to_nearest_screen_in_logical_coordinates():
    screens = (
        QRect(-1920, 0, 1920, 1080),
        QRect(0, 0, 2560, 1440),
    )
    size = QSize(300, 400)

    assert clamp_window_top_left(QPoint(-2100, 900), size, screens) == QPoint(
        -1920,
        680,
    )
    assert clamp_window_top_left(QPoint(2500, 1300), size, screens) == QPoint(
        2260,
        1040,
    )
    assert clamp_window_top_left(QPoint(5000, -500), size, screens) == QPoint(
        2260,
        0,
    )


def test_pet_shell_applies_topmost_setting_and_safe_programmatic_move():
    app()
    window = PetShellWindow(always_on_top=False)
    window.resize(300, 400)
    screens = (QRect(-1920, 0, 1920, 1080), QRect(0, 0, 2560, 1440))

    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    window.move_within_screens(QPoint(-2100, 900), screens)
    assert window.pos() == QPoint(-1920, 680)

    window.set_always_on_top(True)
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    window.close()


def test_pet_shell_reclamps_after_screen_configuration_change(monkeypatch):
    app()
    window = PetShellWindow(always_on_top=False)
    window.resize(300, 400)
    window.move(5000, 2000)
    scheduled = []
    monkeypatch.setattr(
        "ui.pet_shell.QTimer.singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    window._screen_configuration_changed()

    assert scheduled[0][0] == 0
    scheduled[0][1]((QRect(0, 0, 1920, 1080),))
    assert window.pos() == QPoint(1620, 680)
    window.close()


def test_pet_shell_emits_activation_for_short_click_but_not_drag():
    app()
    window = PetShellWindow()
    window.show()
    activated = []
    window.activation_requested.connect(lambda: activated.append(True))
    center = window.rect().center()

    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=center)
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseMove(window, center + QPoint(40, 0))
    QTest.mouseRelease(
        window,
        Qt.MouseButton.LeftButton,
        pos=center + QPoint(40, 0),
    )

    assert activated == [True]
    window.close()


def test_pet_shell_uses_v2_animation_and_falls_back_for_invalid_asset(tmp_path):
    app()
    animated = PetShellWindow(
        pet_directory=Path("assets/pet/dpsk-girl").resolve(),
    )
    fallback = PetShellWindow(pet_directory=tmp_path / "missing")

    animated.set_phase(ConversationPhase.THINKING)

    assert animated.using_pet_asset is True
    assert animated.pet_animation is not None
    assert animated.pet_animation.current_row == 7
    assert fallback.using_pet_asset is False
    assert fallback.pet_animation is None
    assert fallback.sonar is not None
    assert fallback.load_pet_directory(
        Path("assets/pet/dpsk-girl").resolve()
    ) is True
    assert fallback.using_pet_asset is True
    assert fallback.pet_animation is not None
    animated.close()
    fallback.close()


def test_pet_discovery_returns_safe_built_in_first_choices(tmp_path):
    built_in_root = tmp_path / "built-in"
    user_root = tmp_path / "data" / "pets"
    built_in = write_pet_choice(
        built_in_root,
        "ocean-girl",
        display_name="海洋少女",
    )
    write_pet_choice(
        user_root,
        "forest-cat",
        display_name="森林猫",
    )
    write_pet_choice(
        user_root,
        "ocean-girl",
        display_name="重复形象",
    )
    write_pet_choice(
        user_root,
        "wrong-directory",
        pet_id="different-id",
    )
    write_pet_choice(user_root, "old-version", version=1)
    escaping = write_pet_choice(
        user_root,
        "escaping",
        sprite_path="../outside.webp",
    )
    (escaping.parent / "outside.webp").write_bytes(b"outside")
    malformed = user_root / "malformed"
    malformed.mkdir()
    (malformed / "pet.json").write_text("{", encoding="utf-8")

    choices = discover_pet_choices(
        tmp_path / "data",
        built_in_root=built_in_root,
    )

    assert choices == (
        PetChoice("ocean-girl", "海洋少女", built_in.resolve(), True),
        PetChoice(
            "forest-cat",
            "森林猫",
            (user_root / "forest-cat").resolve(),
            False,
        ),
    )
    assert [choice.label for choice in choices] == [
        "海洋少女（ocean-girl）",
        "森林猫（forest-cat）",
    ]


def test_confirmation_dialog_timeout_rejects_by_default():
    app()
    dialog = ConfirmationDialog(timeout_ms=20)
    rejected = []
    dialog.rejected.connect(lambda: rejected.append(True))

    dialog.show_request("移动文件", "R2")
    spin_until(lambda: rejected == [True])

    assert not dialog.isVisible()
    dialog.close()


def test_settings_window_lists_memory_and_emits_data_management_intents():
    app()
    window = SettingsWindow(AppConfig())
    refreshed = []
    deleted = []
    confirmed = []
    resolved = []
    exported = []
    window.memory_refresh_requested.connect(lambda: refreshed.append(True))
    window.memory_delete_requested.connect(deleted.append)
    window.memory_confirm_requested.connect(confirmed.append)
    window.memory_resolve_requested.connect(resolved.append)
    window.memory_export_requested.connect(lambda: exported.append(True))
    records = (
        SimpleNamespace(
            id="memory-1",
            category="preference",
            content="喜欢低音量播报",
            status=SimpleNamespace(value="candidate"),
        ),
        SimpleNamespace(
            id="memory-2",
            category="preference",
            content="偏好耳机播报",
            status=SimpleNamespace(value="conflicted"),
        ),
    )

    window.set_memory_records(records)
    window.memory_list.setCurrentRow(0)
    window.memory_refresh_button.click()
    window.memory_confirm_button.click()
    window.memory_delete_button.click()
    window.memory_list.setCurrentRow(1)
    window.memory_resolve_button.click()
    window.memory_export_button.click()

    assert window.memory_list.count() == 2
    assert "喜欢低音量播报" in window.memory_list.item(0).text()
    assert refreshed == [True]
    assert deleted == ["memory-1"]
    assert confirmed == ["memory-1"]
    assert resolved == ["memory-2"]
    assert exported == [True]
    window.close()


def test_settings_window_lists_sessions_and_emits_management_intents():
    app()
    window = SettingsWindow(AppConfig())
    refreshed = []
    deleted = []
    cleared = []
    selected = []
    activated = []
    created = []
    window.session_refresh_requested.connect(lambda: refreshed.append(True))
    window.session_delete_requested.connect(deleted.append)
    window.session_clear_requested.connect(lambda: cleared.append(True))
    window.session_selection_changed.connect(selected.append)
    window.session_activate_requested.connect(activated.append)
    window.session_new_requested.connect(lambda: created.append(True))
    records = (
        SimpleNamespace(
            id="session-1",
            title="帮我规划今天的工作",
            turn_count=2,
            last_assistant_text="先整理待办，再安排优先级",
            created_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
            updated_at=datetime(2026, 9, 4, 9, 35, tzinfo=UTC),
            is_active=True,
        ),
    )

    window.set_session_records(records)
    window.session_list.setCurrentRow(0)
    window.session_refresh_button.click()
    window.session_activate_button.click()
    window.session_new_button.click()
    window.session_delete_button.click()
    window.session_clear_button.click()
    window.set_session_detail(
        (
            SimpleNamespace(
                user_text="第一问",
                assistant_text="第一答",
                created_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
            ),
        )
    )

    assert window.session_list.count() == 1
    assert "帮我规划今天的工作" in window.session_list.item(0).text()
    assert refreshed == [True]
    assert selected == ["session-1"]
    assert activated == ["session-1"]
    assert created == [True]
    assert deleted == ["session-1"]
    assert cleared == [True]
    assert "第一问" in window.session_detail.toPlainText()
    window.close()


def test_settings_window_emits_credential_intents_without_config_persistence():
    app()
    window = SettingsWindow(AppConfig())
    saved_keys = []
    deleted = []
    window.credential_save_requested.connect(saved_keys.append)
    window.credential_delete_requested.connect(lambda: deleted.append(True))

    window.api_key_edit.setText("sk-private-value")
    window.api_key_save_button.click()
    window.api_key_delete_button.click()

    assert window.api_key_edit.echoMode() is window.api_key_edit.EchoMode.Password
    assert saved_keys == ["sk-private-value"]
    assert deleted == [True]
    window.close()


def test_settings_window_emits_pet_file_and_directory_import_intents():
    app()
    window = SettingsWindow(AppConfig())
    intents = []
    window.pet_file_import_requested.connect(lambda: intents.append("file"))
    window.pet_directory_import_requested.connect(
        lambda: intents.append("directory")
    )

    window.pet_file_import_button.click()
    window.pet_directory_import_button.click()

    assert intents == ["file", "directory"]
    window.close()


def test_active_pet_resolution_prefers_user_install_and_rejects_traversal(tmp_path):
    user_pet = tmp_path / "pets" / "custom"
    user_pet.mkdir(parents=True)

    assert resolve_active_pet_directory("custom", tmp_path) == user_pet.resolve()
    assert resolve_active_pet_directory("../outside", tmp_path) == Path(
        "assets/pet/dpsk-girl"
    ).resolve()


def test_settings_window_emits_diagnostic_intents_and_renders_report():
    app()
    window = SettingsWindow(AppConfig())
    intents = []
    window.diagnostics_run_requested.connect(lambda: intents.append("run"))
    window.diagnostics_export_requested.connect(lambda: intents.append("export"))
    report = SimpleNamespace(
        overall_status=SimpleNamespace(value="degraded"),
        results=(
            SimpleNamespace(
                component="audio",
                status=SimpleNamespace(value="healthy"),
                safe_message="麦克风采集运行中",
                duration_ms=12,
            ),
        ),
    )

    window.diagnostics_run_button.click()
    window.diagnostics_export_button.click()
    window.set_diagnostic_report(report)

    assert intents == ["run", "export"]
    assert window.diagnostics_list.count() == 1
    assert "audio" in window.diagnostics_list.item(0).text()
    assert "degraded" in window.diagnostics_summary.text()
    window.close()
