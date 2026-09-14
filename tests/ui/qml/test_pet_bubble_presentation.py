import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest
from test_qml_application import build_controller

from core.events import CorrelationId, TranscriptReady, TurnId


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)


def visible_speech_item(pet, name):
    """Repeater 生成的控件没有 QObject 父级，只能沿可视树查找"""

    speech = pet.findChild(QObject, "petSpeechWindow")
    return next(
        item for item in visual_items(speech.contentItem())
        if item.objectName() == name and item.property("visible")
    )


def test_pet_bubble_is_compact_and_adapts_to_short_reply(qapp):
    controller, _service, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        speech = pet.findChild(QObject, "petSpeechWindow")
        pet.setProperty("speech", "在呢")
        QTest.qWait(30)
        short_width = speech.width()
        assert short_width <= 180
        pet.setProperty("speech", "这是一段完整的助手回复，需要自然换行。" * 10)
        QTest.qWait(30)
        assert short_width < speech.width() <= 320
    finally:
        controller.close()


def test_long_pet_reply_renders_markdown_and_exposes_scrollbar(qapp):
    controller, _service, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        speech = pet.findChild(QObject, "petSpeechWindow")
        scrollbar = pet.findChild(QObject, "petSpeechScrollBar")
        markdown = "# 菜单初始化\n\n" + "- **父菜单**和 `route`\n" * 80

        pet.setProperty("speech", markdown)
        QTest.qWait(50)

        text = visible_speech_item(pet, "petSpeechText")
        rendered = text.property("text")
        assert "父菜单" in rendered
        assert "**" not in rendered
        assert speech.height() <= 260
        assert scrollbar.property("visible")
        assert scrollbar.property("size") < 1
    finally:
        controller.close()


def test_user_transcript_stays_in_chat_not_desktop_bubble(qapp):
    controller, _service, _tray, qml, _shell, chat, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        controller.handle_runtime_event(TranscriptReady(TurnId.new(), CorrelationId.new(), "用户说的话"))
        assert pet.property("speech") == ""
        assert chat.messageModel.data(chat.messageModel.index(0), Qt.UserRole + 3) == "用户说的话"
    finally:
        controller.close()


def test_code_block_wraps_inside_pet_speech_bubble(qapp):
    controller, _service, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        speech = pet.findChild(QObject, "petSpeechWindow")
        long_line = "Left 60%: " + "a flat opaque dark charcoal panel, " * 4

        pet.setProperty("speech", "概念图提示词\n```\n" + long_line.strip() + "\n```\n底图放 assets/ui\n")
        QTest.qWait(80)

        # Markdown 控件里的代码行不会折行，必须换成纯文本控件才能收在气泡内
        code = visible_speech_item(pet, "petSpeechCodeText")
        assert code.property("contentWidth") <= code.width() + 1
        assert code.height() == pytest.approx(code.property("implicitHeight"))
        assert code.mapToItem(speech.contentItem(), 0, 0).x() + code.width() <= speech.width()
        assert visible_speech_item(pet, "petSpeechText").property("text")
    finally:
        controller.close()
