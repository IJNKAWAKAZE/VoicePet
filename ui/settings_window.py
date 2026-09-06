"""深海声波风格的 VoicePet 设置窗口"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from PySide6.QtCore import QEvent, QPointF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig, ConfigError

from .pet_shell import PetChoice

_STYLE = """
QWidget { color: #F1F5F7; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QWidget#settings_root { background: #0D1117; }
QWidget#settings_sidebar { background: #111720; border-right: 1px solid #28313C; }
QLabel#brand_mark { color: #5FD3C7; font-size: 20px; font-weight: 700; padding: 24px 20px 2px 20px; }
QLabel#brand_caption { color: #7F8C99; font-family: Consolas; font-size: 10px; letter-spacing: 1px; padding: 0 20px 18px 20px; }
QLabel#local_mark { color: #687582; font-family: Consolas; font-size: 10px; padding: 16px 20px; }
QListWidget#settings_navigation { background: transparent; border: 0; padding: 8px 10px; outline: 0; }
QListWidget#settings_navigation::item { color: #A5B0BA; padding: 12px 16px; margin: 3px 0; border: 1px solid transparent; border-left: 3px solid transparent; border-radius: 8px; }
QListWidget#settings_navigation::item:hover { color: #F1F5F7; background: #1A222D; }
QListWidget#settings_navigation::item:selected { color: #74DED3; background: #1B2B31; border-left: 3px solid #5FD3C7; }
QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: #0D1117; border: 0; }
QLabel#section_title { color: #F7FAFB; font-size: 24px; font-weight: 700; }
QLabel#section_description { color: #8E9AA6; font-size: 12px; padding-bottom: 8px; }
QFrame#settings_card { background: #161C24; border: 1px solid #2A3440; border-radius: 12px; }
QLabel#card_title { color: #F7FAFB; font-size: 16px; font-weight: 600; }
QLabel#card_description { color: #8E9AA6; font-size: 11px; }
QFrame#settings_footer { background: #121820; border-top: 1px solid #28313C; }
QPushButton { background: #202A35; color: #EDF2F4; border: 1px solid #3A4653; border-radius: 8px; padding: 8px 13px; min-height: 20px; }
QPushButton:hover { background: #293541; border-color: #5FD3C7; color: #FFFFFF; }
QPushButton:pressed { background: #182129; }
QPushButton:disabled { color: #687582; background: #171D24; border-color: #2A333D; }
QPushButton#save_settings { background: #5FD3C7; color: #0D1719; border: 1px solid #5FD3C7; border-radius: 8px; padding: 9px 24px; font-weight: 700; }
QPushButton#save_settings:hover { background: #7BE0D6; }
QPushButton#save_settings:pressed { background: #49BDB2; }
QPushButton#memory_action { background: #202A35; color: #EDF2F4; border: 1px solid #3A4653; border-radius: 8px; padding: 8px 12px; }
QPushButton#memory_action:hover { border-color: #5FD3C7; color: #74DED3; }
QComboBox, QLineEdit, QPlainTextEdit, QDoubleSpinBox, QSpinBox { background: #0F151C; border: 1px solid #374451; border-radius: 8px; padding: 6px 10px; min-height: 28px; selection-background-color: #285A5A; }
QPlainTextEdit { min-height: 180px; }
QComboBox:hover, QLineEdit:hover, QPlainTextEdit:hover, QDoubleSpinBox:hover, QSpinBox:hover { border-color: #536171; }
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus { border-color: #5FD3C7; }
QComboBox QAbstractItemView { background: #1A212A; color: #F1F5F7; border: 0; padding: 4px; outline: 0; selection-background-color: #285A5A; }
QComboBox QAbstractItemView::item { min-height: 32px; padding: 0 10px; border-radius: 5px; }
QComboBox::drop-down { border: 0; width: 0; }
QComboBox::down-arrow { image: none; }
QComboBox QLineEdit { background: transparent; border: 0; padding: 0; }
QFrame#select_control, QFrame#spin_control { background: #0F151C; border: 1px solid #374451; border-radius: 8px; }
QFrame#select_control[interactionActive="true"], QFrame#spin_control[interactionActive="true"] { background: #151E27; border-color: #5FD3C7; }
QFrame#select_control QComboBox, QFrame#spin_control QDoubleSpinBox, QFrame#spin_control QSpinBox { background: transparent; border: 0; border-radius: 7px; }
QToolButton#select_toggle { background: transparent; color: #8FE6DE; border: 0; border-top-right-radius: 7px; border-bottom-right-radius: 7px; }
QToolButton#select_toggle:hover { background: transparent; }
QToolButton#spin_up, QToolButton#spin_down { background: transparent; color: #8FE6DE; border: 0; }
QToolButton#spin_up { border-top-right-radius: 7px; }
QToolButton#spin_down { border-bottom-right-radius: 7px; }
QToolButton#spin_up:hover, QToolButton#spin_down:hover { background: transparent; }
QPushButton#voice_preview { background: #1A242E; color: #DCE7EA; border: 1px solid #3A4653; border-radius: 8px; padding: 7px 16px; min-height: 28px; }
QPushButton#voice_preview:hover { background: #203139; color: #FFFFFF; border-color: #5FD3C7; }
QPushButton#voice_preview:pressed { background: #18272E; }
QListWidget#memory_list, QListWidget#session_list, QListWidget#diagnostics_list { background: #0F151C; color: #DDE5E9; border: 1px solid #374451; border-radius: 8px; padding: 5px; outline: 0; }
QListWidget#memory_list::item, QListWidget#session_list::item, QListWidget#diagnostics_list::item { padding: 8px 10px; border-radius: 5px; }
QListWidget#memory_list::item:hover, QListWidget#session_list::item:hover, QListWidget#diagnostics_list::item:hover { background: #202A35; }
QListWidget#memory_list::item:selected, QListWidget#session_list::item:selected, QListWidget#diagnostics_list::item:selected { background: #285A5A; color: #FFFFFF; }
QLabel#voice_catalog_status { color: #8E9AA6; font-size: 11px; padding-top: 3px; }
QSlider::groove:horizontal { height: 4px; background: #34404C; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5FD3C7; border-radius: 2px; }
QSlider::handle:horizontal { background: #F4FBFA; border: 2px solid #5FD3C7; width: 14px; margin: -6px 0; border-radius: 8px; }
QProgressBar { background: #0F151C; border: 1px solid #374451; border-radius: 7px; min-height: 20px; text-align: center; color: #F1F5F7; }
QProgressBar::chunk { background: #5FD3C7; border-radius: 6px; }
QLabel#sonar_value { color: #74DED3; font-family: Consolas; font-weight: 700; min-width: 42px; }
QLabel#validation_message { color: #FFB3A7; padding: 4px 0; }
QCheckBox { min-height: 28px; spacing: 9px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px; border: 1px solid #536171; background: #0F151C; }
QCheckBox::indicator:hover { border-color: #5FD3C7; }
QCheckBox::indicator:checked { background: #5FD3C7; border: 4px solid #27413F; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 6px 3px; }
QScrollBar::handle:vertical { background: #4A5663; border-radius: 5px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: #6B7A89; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px 6px; }
QScrollBar::handle:horizontal { background: #4A5663; border-radius: 4px; min-width: 36px; }
QScrollBar::handle:horizontal:hover { background: #6B7A89; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
"""


class _ChevronButton(QToolButton):
    """不依赖字体绘制清晰方向箭头"""

    def __init__(self, direction: str) -> None:
        super().__init__()
        self.direction = direction
        self._hovered = False

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        control = self.parentWidget()
        while control is not None and not isinstance(control, _ControlFrame):
            control = control.parentWidget()
        active = control is not None and bool(
            control.property("interactionActive")
        )
        if self._hovered:
            hover_color = "#314750" if self.isDown() else "#263A42"
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(hover_color)))
            background = self.rect().adjusted(5, 3, -5, -3)
            painter.drawRoundedRect(background, 6, 6)
        color = QColor(
            "#FFFFFF"
            if self._hovered
            else "#8FE6DE"
            if active
            else "#8FA1AC"
        )
        pen = QPen(color, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center_x = self.width() / 2
        center_y = self.height() / 2
        half_width = 5.0 if self.direction == "down" else 4.0
        half_height = 2.5
        if self.direction == "up":
            points = (
                QPointF(center_x - half_width, center_y + half_height),
                QPointF(center_x, center_y - half_height),
                QPointF(center_x + half_width, center_y + half_height),
            )
        else:
            points = (
                QPointF(center_x - half_width, center_y - half_height),
                QPointF(center_x, center_y + half_height),
                QPointF(center_x + half_width, center_y - half_height),
            )
        painter.drawPolyline(points)


class _ModernComboBox(QComboBox):
    """统一下拉弹层背景并移除系统白边"""

    def showPopup(self) -> None:
        popup = self.view().window()
        popup.setObjectName("voicepet_combo_popup")
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        popup.setAutoFillBackground(True)
        palette = popup.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#1A212A"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#1A212A"))
        popup.setPalette(palette)
        popup.setStyleSheet(
            "QFrame#voicepet_combo_popup {"
            "background: #1A212A;"
            "border: 1px solid #465361;"
            "}"
        )
        super().showPopup()


class _ControlFrame(QFrame):
    """让组合控件共享统一的悬停与聚焦反馈"""

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("interactionActive", False)
        self._tracked_widgets: list[QWidget] = []

    def track(self, *widgets: QWidget) -> None:
        for widget in widgets:
            widget.installEventFilter(self)
            self._tracked_widgets.append(widget)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in {QEvent.Type.Enter, QEvent.Type.FocusIn}:
            self._set_interaction_active(True)
        elif event.type() in {QEvent.Type.Leave, QEvent.Type.FocusOut}:
            QTimer.singleShot(0, self._sync_interaction_state)
        return super().eventFilter(watched, event)

    def enterEvent(self, event) -> None:
        self._set_interaction_active(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        QTimer.singleShot(0, self._sync_interaction_state)
        super().leaveEvent(event)

    def _sync_interaction_state(self) -> None:
        active = self.underMouse() or any(
            widget.underMouse() or widget.hasFocus()
            for widget in self._tracked_widgets
        )
        self._set_interaction_active(active)

    def _set_interaction_active(self, active: bool) -> None:
        if bool(self.property("interactionActive")) == active:
            return
        self.setProperty("interactionActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class SettingsWindow(QWidget):
    """按用户可识别概念组织的设置窗口"""

    save_requested = Signal(object)
    memory_refresh_requested = Signal()
    memory_delete_requested = Signal(str)
    memory_confirm_requested = Signal(str)
    memory_resolve_requested = Signal(str)
    memory_export_requested = Signal()
    session_refresh_requested = Signal()
    session_delete_requested = Signal(str)
    session_clear_requested = Signal()
    session_selection_changed = Signal(str)
    session_activate_requested = Signal(str)
    session_new_requested = Signal()
    credential_save_requested = Signal(str)
    credential_delete_requested = Signal()
    pet_file_import_requested = Signal()
    pet_directory_import_requested = Signal()
    wake_model_download_requested = Signal()
    wake_model_download_cancel_requested = Signal()
    voice_preview_requested = Signal(str)
    diagnostics_run_requested = Signal()
    diagnostics_export_requested = Signal()
    pet_selection_changed = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self.setObjectName("settings_root")
        self.setWindowTitle("VoicePet 设置")
        self.resize(860, 640)
        self.setMinimumSize(760, 520)
        self.setStyleSheet(_STYLE)
        self._navigation = QListWidget()
        self._navigation.setObjectName("settings_navigation")
        self._pages = QStackedWidget()
        for label, page in self._build_pages(config):
            self._navigation.addItem(label)
            self._pages.addWidget(page)
        self._navigation.currentRowChanged.connect(self._pages.setCurrentIndex)
        self._navigation.setCurrentRow(0)
        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("save_settings")
        self.save_button.clicked.connect(self._save)
        self.validation_message = QLabel()
        self.validation_message.setObjectName("validation_message")
        self.validation_message.setWordWrap(True)

        self.footer = QFrame()
        self.footer.setObjectName("settings_footer")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(20, 12, 20, 12)
        footer_layout.addWidget(self.validation_message, 1)
        footer_layout.addWidget(self.save_button)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self._pages, 1)
        content.addWidget(self.footer)

        sidebar = QWidget()
        sidebar.setObjectName("settings_sidebar")
        sidebar.setFixedWidth(184)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        brand = QLabel("◉  VOICEPET")
        brand.setObjectName("brand_mark")
        caption = QLabel("DEEP-SEA COMPANION")
        caption.setObjectName("brand_caption")
        local_mark = QLabel("LOCAL  •  PRIVATE")
        local_mark.setObjectName("local_mark")
        sidebar_layout.addWidget(brand)
        sidebar_layout.addWidget(caption)
        sidebar_layout.addWidget(self._navigation, 1)
        sidebar_layout.addWidget(local_mark)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        layout.addLayout(content, 1)

    @property
    def navigation_labels(self) -> tuple[str, ...]:
        return tuple(
            self._navigation.item(index).text()
            for index in range(self._navigation.count())
        )

    def _build_pages(self, config: AppConfig):
        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity_slider.setRange(0, 100)
        self.sensitivity_slider.setValue(round(config.wake_word.sensitivity * 100))
        self.sensitivity_value = QLabel(
            f"{self.sensitivity_slider.value()}%"
        )
        self.sensitivity_value.setObjectName("sonar_value")
        self.sensitivity_slider.valueChanged.connect(
            lambda value: self.sensitivity_value.setText(f"{value}%")
        )
        sensitivity = QWidget()
        sensitivity_layout = QHBoxLayout(sensitivity)
        sensitivity_layout.setContentsMargins(0, 0, 0, 0)
        sensitivity_layout.addWidget(self.sensitivity_slider, 1)
        sensitivity_layout.addWidget(self.sensitivity_value)

        self.debounce_spin = QDoubleSpinBox()
        self.debounce_spin.setRange(0.1, 10.0)
        self.debounce_spin.setSingleStep(0.1)
        self.debounce_spin.setSuffix(" 秒")
        self.debounce_spin.setValue(config.wake_word.debounce_sec)
        self.debounce_control = self._spin_control(self.debounce_spin)
        self.asr_model_edit = QLineEdit(config.asr.model)
        self.wake_enabled_checkbox = QCheckBox("启用语音唤醒")
        self.wake_enabled_checkbox.setChecked(config.wake_word.enabled)
        self.wake_keyword_edit = QLineEdit(config.wake_word.keyword)
        self.wake_keyword_edit.setPlaceholderText("例如：你好，小蓝")
        self.wake_model_status = QLabel("正在检查中文唤醒模型")
        self.wake_model_status.setObjectName("wake_model_status")
        self.wake_model_download_button = QPushButton("下载模型")
        self.wake_model_cancel_button = QPushButton("取消下载")
        for button in (
            self.wake_model_download_button,
            self.wake_model_cancel_button,
        ):
            button.setObjectName("memory_action")
        self.wake_model_download_button.clicked.connect(
            self.wake_model_download_requested.emit
        )
        self.wake_model_cancel_button.clicked.connect(
            self.wake_model_download_cancel_requested.emit
        )
        self.wake_model_progress = QProgressBar()
        self.wake_model_progress.setRange(0, 100)
        self.wake_model_progress.setValue(0)
        self.wake_model_progress.setTextVisible(True)
        self.wake_model_progress.hide()
        self.wake_model_cancel_button.hide()
        wake_model_actions = QHBoxLayout()
        wake_model_actions.setContentsMargins(0, 0, 0, 0)
        wake_model_actions.addWidget(self.wake_model_download_button)
        wake_model_actions.addWidget(self.wake_model_cancel_button)
        wake_model_actions.addStretch(1)
        wake_model_panel = QWidget()
        wake_model_layout = QVBoxLayout(wake_model_panel)
        wake_model_layout.setContentsMargins(0, 0, 0, 0)
        wake_model_layout.addWidget(self.wake_model_status)
        wake_model_layout.addWidget(self.wake_model_progress)
        wake_model_layout.addLayout(wake_model_actions)
        self.voice_combo = _ModernComboBox()
        self.voice_combo.setEditable(False)
        for voice_name in dict.fromkeys(
            (
                config.tts.voice,
                "zh-CN-XiaoxiaoNeural",
                "zh-CN-YunxiNeural",
            )
        ):
            self.voice_combo.addItem(voice_name, voice_name)
        self.voice_combo.setCurrentIndex(
            self.voice_combo.findData(config.tts.voice)
        )
        self.voice_control = self._select_control(self.voice_combo)
        self.voice_preview_button = QPushButton("试听")
        self.voice_preview_button.setObjectName("voice_preview")
        self.voice_preview_button.setToolTip("试听当前选择的中文声音")
        self.voice_preview_button.clicked.connect(self._preview_voice)
        voice_selector = QWidget()
        voice_selector_layout = QHBoxLayout(voice_selector)
        voice_selector_layout.setContentsMargins(0, 0, 0, 0)
        voice_selector_layout.setSpacing(8)
        voice_selector_layout.addWidget(self.voice_control, 1)
        voice_selector_layout.addWidget(self.voice_preview_button)
        self.voice_catalog_status = QLabel("打开设置后获取全部中文声音")
        self.voice_catalog_status.setObjectName("voice_catalog_status")
        voice_panel = QWidget()
        voice_panel_layout = QVBoxLayout(voice_panel)
        voice_panel_layout.setContentsMargins(0, 0, 0, 0)
        voice_panel_layout.setSpacing(2)
        voice_panel_layout.addWidget(voice_selector)
        voice_panel_layout.addWidget(self.voice_catalog_status)
        self.speech_enabled_checkbox = QCheckBox("语音播报")
        self.speech_enabled_checkbox.setChecked(config.tts.enabled)
        self.manual_speech_enabled_checkbox = QCheckBox("手动输入时播报回复")
        self.manual_speech_enabled_checkbox.setChecked(
            config.tts.manual_input_enabled
        )
        voice = self._page(
            "语音",
            "管理唤醒、语音识别与回复播报",
            self._card(
                "聆听与识别",
                "本地唤醒模型只在设备上运行",
                "语音唤醒",
                self.wake_enabled_checkbox,
                "唤醒词",
                self.wake_keyword_edit,
                "唤醒灵敏度",
                sensitivity,
                "唤醒防抖",
                self.debounce_control,
                "中文唤醒模型",
                wake_model_panel,
                "转写模型",
                self.asr_model_edit,
            ),
            self._card(
                "语音输出",
                "控制回复是否朗读以及使用的声音",
                "播报开关",
                self.speech_enabled_checkbox,
                "手动输入",
                self.manual_speech_enabled_checkbox,
                "播报声音",
                voice_panel,
            ),
        )

        self.llm_model_edit = QLineEdit(config.llm.model)
        self.llm_api_combo = _ModernComboBox()
        self.llm_api_combo.addItem("Responses API", "responses")
        self.llm_api_combo.addItem(
            "Chat Completions API",
            "chat_completions",
        )
        self.llm_api_combo.setCurrentIndex(
            self.llm_api_combo.findData(config.llm.api)
        )
        self.llm_api_control = self._select_control(self.llm_api_combo)
        self.llm_base_url_edit = QLineEdit(config.llm.base_url)
        self.llm_base_url_edit.setPlaceholderText(
            "留空时使用 OpenAI 官方默认地址"
        )
        self.reasoning_combo = _ModernComboBox()
        self.reasoning_combo.addItems(["low", "medium", "high"])
        self.reasoning_combo.setCurrentText(config.llm.reasoning_effort)
        self.reasoning_control = self._select_control(self.reasoning_combo)
        self.store_checkbox = QCheckBox("允许服务端保存响应")
        self.store_checkbox.setChecked(config.llm.store)
        self.persona_edit = QPlainTextEdit(config.llm.system_prompt)
        self.persona_edit.setPlaceholderText(
            "例如：你是一只沉稳的蓝色猫娘，称呼用户为主人"
        )
        self.persona_edit.setMinimumHeight(180)
        self.persona_edit.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("输入后加密保存，不写入 config.json")
        self.api_key_save_button = QPushButton("加密保存")
        self.api_key_delete_button = QPushButton("移除密钥")
        for button in (self.api_key_save_button, self.api_key_delete_button):
            button.setObjectName("memory_action")
        self.api_key_save_button.clicked.connect(self._save_api_key)
        self.api_key_delete_button.clicked.connect(
            self.credential_delete_requested.emit
        )
        credential_actions = QHBoxLayout()
        credential_actions.addWidget(self.api_key_edit, 1)
        credential_actions.addWidget(self.api_key_save_button)
        credential_actions.addWidget(self.api_key_delete_button)
        credential_panel = QWidget()
        credential_panel.setLayout(credential_actions)
        ai = self._page(
            "AI",
            "配置模型连接、角色性格与服务凭据",
            self._card(
                "模型连接",
                "兼容 OpenAI 与自定义 API 端点",
                "服务提供方",
                QLabel(config.llm.provider),
                "API 协议",
                self.llm_api_control,
                "API Base URL",
                self.llm_base_url_edit,
                "模型",
                self.llm_model_edit,
                "推理强度",
                self.reasoning_control,
            ),
            self._card(
                "角色设定",
                "写下称呼、性格和表达习惯，安全规则不会被覆盖",
                "系统提示词",
                self.persona_edit,
            ),
            self._card(
                "响应与凭据",
                "API Key 使用 Windows 加密存储，不写入配置文件",
                "云端留存",
                self.store_checkbox,
                "API Key",
                credential_panel,
            ),
        )

        self.session_list = QListWidget()
        self.session_list.setObjectName("session_list")
        self.session_list.setMinimumHeight(170)
        self.session_detail = QPlainTextEdit()
        self.session_detail.setObjectName("session_detail")
        self.session_detail.setReadOnly(True)
        self.session_detail.setPlaceholderText("选择会话后查看完整聊天记录")
        self.session_detail.setMinimumHeight(190)
        self.session_new_button = QPushButton("新建会话")
        self.session_activate_button = QPushButton("切换到选中会话")
        self.session_refresh_button = QPushButton("刷新")
        self.session_delete_button = QPushButton("删除选中")
        self.session_clear_button = QPushButton("清空全部")
        for button in (
            self.session_new_button,
            self.session_activate_button,
            self.session_refresh_button,
            self.session_delete_button,
            self.session_clear_button,
        ):
            button.setObjectName("memory_action")
        self.session_refresh_button.clicked.connect(
            self.session_refresh_requested.emit
        )
        self.session_new_button.clicked.connect(
            self.session_new_requested.emit
        )
        self.session_activate_button.clicked.connect(
            self._activate_selected_session
        )
        self.session_delete_button.clicked.connect(
            self._delete_selected_session
        )
        self.session_clear_button.clicked.connect(
            self.session_clear_requested.emit
        )
        self.session_list.currentItemChanged.connect(
            self._selected_session_changed
        )
        session_actions = QHBoxLayout()
        session_actions.addWidget(self.session_new_button)
        session_actions.addWidget(self.session_activate_button)
        session_actions.addWidget(self.session_refresh_button)
        session_actions.addWidget(self.session_delete_button)
        session_actions.addStretch(1)
        session_actions.addWidget(self.session_clear_button)
        session_panel = QWidget()
        session_layout = QVBoxLayout(session_panel)
        session_layout.setContentsMargins(0, 0, 0, 0)
        session_layout.addWidget(self.session_list)
        session_layout.addWidget(self.session_detail)
        session_layout.addLayout(session_actions)
        sessions = self._page(
            "会话",
            "查看和管理保存在本机的近期问答",
            self._card(
                "近期会话",
                "删除后相关内容不会再参与后续对话上下文",
                "会话记录",
                session_panel,
            ),
        )

        self.pet_combo = _ModernComboBox()
        self.pet_combo.setEditable(False)
        self.pet_combo.addItem(config.ui.active_skin, config.ui.active_skin)
        self.pet_combo.currentIndexChanged.connect(
            self._emit_pet_selection_changed
        )
        self.pet_control = self._select_control(self.pet_combo)
        self.always_on_top_checkbox = QCheckBox("让桌宠保持在其他窗口上方")
        self.always_on_top_checkbox.setChecked(config.ui.always_on_top)
        self.start_at_login_checkbox = QCheckBox("开机启动 VoicePet")
        self.start_at_login_checkbox.setChecked(config.ui.start_at_login)
        self.hot_reload_checkbox = QCheckBox("形象文件变化时自动刷新")
        self.hot_reload_checkbox.setChecked(config.ui.hot_reload_skin)
        self.pet_file_import_button = QPushButton("导入形象包")
        self.pet_directory_import_button = QPushButton("导入形象目录")
        for button in (
            self.pet_file_import_button,
            self.pet_directory_import_button,
        ):
            button.setObjectName("memory_action")
        self.pet_file_import_button.clicked.connect(
            self.pet_file_import_requested.emit
        )
        self.pet_directory_import_button.clicked.connect(
            self.pet_directory_import_requested.emit
        )
        pet_import_actions = QHBoxLayout()
        pet_import_actions.addWidget(self.pet_file_import_button)
        pet_import_actions.addWidget(self.pet_directory_import_button)
        pet_import_panel = QWidget()
        pet_import_panel.setLayout(pet_import_actions)
        pet = self._page(
            "桌宠",
            "选择外观并控制桌宠窗口行为",
            self._card(
                "形象",
                "选择后立即预览，保存后下次启动继续使用",
                "当前形象",
                self.pet_control,
                "导入形象",
                pet_import_panel,
            ),
            self._card(
                "窗口行为",
                "控制桌宠的显示层级与开发预览",
                "窗口层级",
                self.always_on_top_checkbox,
                "开机启动",
                self.start_at_login_checkbox,
                "开发预览",
                self.hot_reload_checkbox,
            ),
        )

        self.memory_checkbox = QCheckBox("保存已确认的偏好与长期记忆")
        self.memory_checkbox.setChecked(config.privacy.memory_enabled)
        self.auto_memory_checkbox = QCheckBox("自动整理摘要与稳定偏好")
        self.auto_memory_checkbox.setChecked(config.privacy.auto_memory_enabled)
        self.chat_history_checkbox = QCheckBox("保存聊天历史")
        self.chat_history_checkbox.setChecked(config.privacy.chat_history_enabled)
        self.summary_retention_spin = QSpinBox()
        self.chat_retention_spin = QSpinBox()
        for spin, value in (
            (self.summary_retention_spin, config.privacy.summary_retention_days),
            (self.chat_retention_spin, config.privacy.chat_retention_days),
        ):
            spin.setRange(1, 365)
            spin.setSuffix(" 天")
            spin.setValue(value)
        self.summary_retention_control = self._spin_control(self.summary_retention_spin)
        self.chat_retention_control = self._spin_control(self.chat_retention_spin)
        self.diagnostic_recording_checkbox = QCheckBox(
            "仅在主动诊断时保留录音"
        )
        self.diagnostic_recording_checkbox.setChecked(
            config.privacy.diagnostic_recording
        )
        self.memory_list = QListWidget()
        self.memory_list.setObjectName("memory_list")
        self.memory_list.setMaximumHeight(150)
        self.memory_refresh_button = QPushButton("刷新")
        self.memory_confirm_button = QPushButton("确认候选")
        self.memory_resolve_button = QPushButton("采用冲突项")
        self.memory_delete_button = QPushButton("删除选中")
        self.memory_export_button = QPushButton("导出 JSON")
        for button in (
            self.memory_refresh_button,
            self.memory_confirm_button,
            self.memory_resolve_button,
            self.memory_delete_button,
            self.memory_export_button,
        ):
            button.setObjectName("memory_action")
        self.memory_refresh_button.clicked.connect(
            self.memory_refresh_requested.emit
        )
        self.memory_delete_button.clicked.connect(self._delete_selected_memory)
        self.memory_confirm_button.clicked.connect(self._confirm_selected_memory)
        self.memory_resolve_button.clicked.connect(self._resolve_selected_memory)
        self.memory_export_button.clicked.connect(
            self.memory_export_requested.emit
        )
        memory_actions = QHBoxLayout()
        memory_actions.addWidget(self.memory_refresh_button)
        memory_actions.addWidget(self.memory_confirm_button)
        memory_actions.addWidget(self.memory_resolve_button)
        memory_actions.addWidget(self.memory_delete_button)
        memory_actions.addWidget(self.memory_export_button)
        memory_panel = QWidget()
        memory_layout = QVBoxLayout(memory_panel)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.addWidget(self.memory_list)
        memory_layout.addLayout(memory_actions)
        privacy = self._page(
            "隐私",
            "管理本地记忆、保留时间与诊断数据",
            self._card(
                "数据保留",
                "本机保存；自动整理会使用所配置的 AI 服务并产生额外用量",
                "记忆",
                self.memory_checkbox,
                "保存聊天历史",
                self.chat_history_checkbox,
                "自动整理",
                self.auto_memory_checkbox,
                "聊天记录保留",
                self.chat_retention_control,
                "近期摘要保留",
                self.summary_retention_control,
                "诊断录音",
                self.diagnostic_recording_checkbox,
            ),
            self._card(
                "本地记忆",
                "查看、确认、替换或导出保存在设备上的内容",
                "记忆记录",
                memory_panel,
            ),
        )
        self.diagnostics_summary = QLabel("尚未运行诊断")
        self.diagnostics_summary.setObjectName("sonar_value")
        self.diagnostics_list = QListWidget()
        self.diagnostics_list.setObjectName("diagnostics_list")
        self.diagnostics_list.setMinimumHeight(220)
        self.diagnostics_run_button = QPushButton("运行诊断")
        self.diagnostics_export_button = QPushButton("导出脱敏 ZIP")
        for button in (
            self.diagnostics_run_button,
            self.diagnostics_export_button,
        ):
            button.setObjectName("memory_action")
        self.diagnostics_run_button.clicked.connect(
            self.diagnostics_run_requested.emit
        )
        self.diagnostics_export_button.clicked.connect(
            self.diagnostics_export_requested.emit
        )
        diagnostic_actions = QHBoxLayout()
        diagnostic_actions.addWidget(self.diagnostics_run_button)
        diagnostic_actions.addWidget(self.diagnostics_export_button)
        diagnostic_panel = QWidget()
        diagnostic_layout = QVBoxLayout(diagnostic_panel)
        diagnostic_layout.setContentsMargins(0, 0, 0, 0)
        diagnostic_layout.addWidget(self.diagnostics_summary)
        diagnostic_layout.addWidget(self.diagnostics_list)
        diagnostic_layout.addLayout(diagnostic_actions)
        diagnostics = self._page(
            "诊断",
            "检查本地组件并导出不含敏感内容的诊断包",
            self._card(
                "本地检查",
                "按需检查音频、模型、数据库与桌宠资源",
                "检查结果",
                diagnostic_panel,
            ),
        )
        return (
            ("语音", voice),
            ("AI", ai),
            ("会话", sessions),
            ("桌宠", pet),
            ("隐私", privacy),
            ("诊断", diagnostics),
        )

    @staticmethod
    def _select_control(combo: QComboBox) -> QFrame:
        control = _ControlFrame("select_control")
        control.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        combo.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle = _ChevronButton("down")
        toggle.setObjectName("select_toggle")
        toggle.setAccessibleName("展开选项")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        toggle.setFixedWidth(34)
        toggle.clicked.connect(lambda checked=False: combo.showPopup())
        layout.addWidget(combo, 1)
        layout.addWidget(toggle)
        control.track(combo, toggle)
        return control

    @staticmethod
    def _spin_control(spin: QDoubleSpinBox | QSpinBox) -> QFrame:
        control = _ControlFrame("spin_control")
        control.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        buttons = QWidget()
        buttons.setFixedWidth(34)
        button_layout = QVBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)
        up_button = _ChevronButton("up")
        up_button.setObjectName("spin_up")
        up_button.setAccessibleName("增加")
        down_button = _ChevronButton("down")
        down_button.setObjectName("spin_down")
        down_button.setAccessibleName("减少")
        for button in (up_button, down_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoRepeat(True)
        up_button.clicked.connect(lambda checked=False: spin.stepUp())
        down_button.clicked.connect(lambda checked=False: spin.stepDown())
        button_layout.addWidget(up_button)
        button_layout.addWidget(down_button)
        layout.addWidget(spin, 1)
        layout.addWidget(buttons)
        control.track(spin, up_button, down_button)
        return control

    @staticmethod
    def _page(title: str, description: str, *cards: QFrame) -> QScrollArea:
        page = QScrollArea()
        page.setObjectName("settings_page")
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("section_title")
        layout.addWidget(heading)
        subtitle = QLabel(description)
        subtitle.setObjectName("section_description")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        for card in cards:
            layout.addWidget(card)
        layout.addStretch(1)
        page.setWidget(content)
        return page

    @staticmethod
    def _card(title: str, description: str, *items) -> QFrame:
        card = QFrame()
        card.setObjectName("settings_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("card_title")
        layout.addWidget(heading)
        caption = QLabel(description)
        caption.setObjectName("card_description")
        caption.setWordWrap(True)
        layout.addWidget(caption)
        form = QFormLayout()
        form.setContentsMargins(0, 8, 0, 0)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        for index in range(0, len(items), 2):
            form.addRow(items[index], items[index + 1])
        layout.addLayout(form)
        return card

    def _save(self) -> None:
        try:
            updated = replace(
                self._config,
                wake_word=replace(
                    self._config.wake_word,
                    enabled=self.wake_enabled_checkbox.isChecked(),
                    keyword=self.wake_keyword_edit.text().strip(),
                    sensitivity=self.sensitivity_slider.value() / 100,
                    debounce_sec=self.debounce_spin.value(),
                ),
                asr=replace(
                    self._config.asr,
                    model=self.asr_model_edit.text().strip(),
                ),
                llm=replace(
                    self._config.llm,
                    api=str(self.llm_api_combo.currentData()),
                    base_url=self.llm_base_url_edit.text().strip(),
                    model=self.llm_model_edit.text().strip(),
                    reasoning_effort=self.reasoning_combo.currentText(),
                    system_prompt=(
                        self.persona_edit.toPlainText().strip()
                    ),
                    store=self.store_checkbox.isChecked(),
                ),
                tts=replace(
                    self._config.tts,
                    enabled=self.speech_enabled_checkbox.isChecked(),
                    manual_input_enabled=(
                        self.manual_speech_enabled_checkbox.isChecked()
                    ),
                    voice=self.selected_voice_name(),
                ),
                ui=replace(
                    self._config.ui,
                    active_skin=self.selected_pet_id(),
                    always_on_top=self.always_on_top_checkbox.isChecked(),
                    hot_reload_skin=self.hot_reload_checkbox.isChecked(),
                    start_at_login=self.start_at_login_checkbox.isChecked(),
                ),
                privacy=replace(
                    self._config.privacy,
                    memory_enabled=self.memory_checkbox.isChecked(),
                    auto_memory_enabled=self.auto_memory_checkbox.isChecked(),
                    chat_history_enabled=self.chat_history_checkbox.isChecked(),
                    summary_retention_days=self.summary_retention_spin.value(),
                    chat_retention_days=self.chat_retention_spin.value(),
                    diagnostic_recording=(
                        self.diagnostic_recording_checkbox.isChecked()
                    ),
                ),
            )
        except ConfigError as error:
            self.validation_message.setText(str(error))
            return
        self.validation_message.clear()
        self._config = updated
        self.save_requested.emit(self._config)

    def set_wake_model_status(self, status: str, message: str) -> None:
        """更新中文唤醒模型状态与可用操作"""

        self.wake_model_status.setProperty("status", status)
        self.wake_model_status.setText(message)
        downloading = status == "downloading"
        retryable = status in {"missing", "failed"}
        self.wake_model_progress.setVisible(downloading)
        self.wake_model_cancel_button.setVisible(downloading)
        self.wake_model_download_button.setVisible(retryable)
        self.wake_model_download_button.setEnabled(retryable)
        self.wake_model_download_button.setText(
            "重试下载" if status == "failed" else "下载模型"
        )
        self.wake_model_status.style().unpolish(self.wake_model_status)
        self.wake_model_status.style().polish(self.wake_model_status)

    def set_wake_download_progress(
        self,
        downloaded: int,
        total: int,
    ) -> None:
        """显示中文唤醒模型的确定下载百分比"""

        percent = 0 if total <= 0 else min(100, downloaded * 100 // total)
        self.wake_model_progress.setValue(percent)
        self.wake_model_progress.setFormat(f"{percent}%")

    def set_pet_choices(
        self,
        choices: Sequence[PetChoice],
        selected_id: str,
    ) -> None:
        """整体刷新形象下拉并静默恢复选中项"""

        blocker = QSignalBlocker(self.pet_combo)
        self.pet_combo.clear()
        for choice in choices:
            self.pet_combo.addItem(choice.label, choice.pet_id)
        selected_index = self.pet_combo.findData(selected_id)
        if selected_index < 0 and self.pet_combo.count():
            selected_index = 0
        self.pet_combo.setCurrentIndex(selected_index)
        del blocker

    def selected_pet_id(self) -> str:
        """返回当前下拉项保存的稳定形象 ID"""

        pet_id = self.pet_combo.currentData()
        return pet_id if isinstance(pet_id, str) else self._config.ui.active_skin

    def selected_voice_name(self) -> str:
        """返回当前中文声音的服务端短名称"""

        voice_name = self.voice_combo.currentData()
        if isinstance(voice_name, str) and voice_name.strip():
            return voice_name.strip()
        return self.voice_combo.currentText().strip()

    def set_voice_options(self, voices: Sequence[object]) -> None:
        """用动态中文声音目录刷新下拉框并保留当前选择"""

        selected = self.selected_voice_name()
        blocker = QSignalBlocker(self.voice_combo)
        self.voice_combo.clear()
        for voice in voices:
            short_name = getattr(voice, "short_name", None)
            label = getattr(voice, "label", None)
            if isinstance(short_name, str) and isinstance(label, str):
                self.voice_combo.addItem(label, short_name)
        selected_index = self.voice_combo.findData(selected)
        if selected_index < 0 and selected:
            self.voice_combo.insertItem(0, selected, selected)
            selected_index = 0
        if selected_index < 0 and self.voice_combo.count():
            selected_index = 0
        self.voice_combo.setCurrentIndex(selected_index)
        del blocker
        self.voice_catalog_status.setText(
            f"已获取 {len(voices)} 个在线中文声音"
        )
        self.voice_preview_button.setEnabled(bool(self.voice_combo.count()))

    def set_voice_catalog_status(self, message: str) -> None:
        """显示中文声音目录加载状态"""

        self.voice_catalog_status.setText(message)

    def set_voice_preview_active(self, active: bool, message: str) -> None:
        """切换试听按钮状态并显示安全结果"""

        self.voice_preview_button.setEnabled(not active)
        self.voice_preview_button.setText("试听中…" if active else "试听")
        self.voice_catalog_status.setText(message)

    def _preview_voice(self) -> None:
        voice_name = self.selected_voice_name()
        if voice_name:
            self.voice_preview_requested.emit(voice_name)

    def select_pet_id(self, pet_id: str) -> bool:
        """静默选择指定形象并返回是否存在"""

        index = self.pet_combo.findData(pet_id)
        if index < 0:
            return False
        blocker = QSignalBlocker(self.pet_combo)
        self.pet_combo.setCurrentIndex(index)
        del blocker
        return True

    def _emit_pet_selection_changed(self, index: int) -> None:
        if index < 0:
            return
        pet_id = self.pet_combo.itemData(index)
        if isinstance(pet_id, str):
            self.pet_selection_changed.emit(pet_id)

    def set_memory_records(self, records) -> None:
        """用 Runtime 返回的不可变记录刷新本地记忆列表"""

        self.memory_list.clear()
        for record in records:
            item_text = f"[{record.status.value}] {record.category} · {record.content}"
            self.memory_list.addItem(item_text)
            item = self.memory_list.item(self.memory_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, record.id)

    def set_session_records(self, records) -> None:
        """刷新多轮会话摘要并标记当前会话"""

        selected = self._selected_session_id()
        self.session_list.clear()
        for record in records:
            timestamp = record.updated_at.astimezone().strftime("%m-%d %H:%M")
            title = self._compact_text(record.title, 48)
            preview = self._compact_text(record.last_assistant_text, 72)
            active_mark = "● 当前  " if record.is_active else ""
            self.session_list.addItem(
                f"{active_mark}{timestamp}  {title}  ·  {record.turn_count} 轮\n"
                f"VoicePet：{preview}"
            )
            item = self.session_list.item(self.session_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            if record.id == selected or (selected is None and record.is_active):
                self.session_list.setCurrentItem(item)

    def set_session_detail(self, turns) -> None:
        """显示选中会话的完整多轮问答"""

        blocks: list[str] = []
        for index, turn in enumerate(turns, start=1):
            timestamp = turn.created_at.astimezone().strftime("%m-%d %H:%M")
            blocks.append(
                f"第 {index} 轮 · {timestamp}\n"
                f"你：{turn.user_text}\n\n"
                f"VoicePet：{turn.assistant_text}"
            )
        self.session_detail.setPlainText("\n\n──────────\n\n".join(blocks))

    def _delete_selected_session(self) -> None:
        item = self.session_list.currentItem()
        if item is None:
            self.validation_message.setText("请先选择要删除的会话")
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(session_id, str):
            self.session_delete_requested.emit(session_id)

    def _activate_selected_session(self) -> None:
        session_id = self._selected_session_id()
        if session_id is None:
            self.validation_message.setText("请先选择要切换的会话")
            return
        self.session_activate_requested.emit(session_id)

    def _selected_session_changed(self, current, previous) -> None:
        del previous
        if current is None:
            self.session_detail.clear()
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(session_id, str):
            self.session_selection_changed.emit(session_id)

    def _selected_session_id(self) -> str | None:
        item = self.session_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) else None

    @staticmethod
    def _compact_text(text: str, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 1]}…"

    def _delete_selected_memory(self) -> None:
        item = self.memory_list.currentItem()
        if item is None:
            self.validation_message.setText("请先选择要删除的记忆")
            return
        memory_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(memory_id, str):
            self.memory_delete_requested.emit(memory_id)

    def _confirm_selected_memory(self) -> None:
        item = self.memory_list.currentItem()
        if item is None:
            self.validation_message.setText("请先选择要确认的记忆候选")
            return
        memory_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(memory_id, str):
            self.memory_confirm_requested.emit(memory_id)

    def _resolve_selected_memory(self) -> None:
        item = self.memory_list.currentItem()
        if item is None:
            self.validation_message.setText("请先选择要采用的冲突记忆")
            return
        memory_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(memory_id, str):
            self.memory_resolve_requested.emit(memory_id)

    def _save_api_key(self) -> None:
        value = self.api_key_edit.text().strip()
        if not value:
            self.validation_message.setText("API Key 不能为空")
            return
        self.credential_save_requested.emit(value)
        self.api_key_edit.clear()

    def set_diagnostic_report(self, report) -> None:
        """显示 Runtime 返回的安全诊断摘要"""

        self.diagnostics_summary.setText(
            f"总体状态：{report.overall_status.value}"
        )
        self.diagnostics_list.clear()
        for result in report.results:
            self.diagnostics_list.addItem(
                f"{result.component} · {result.status.value} · "
                f"{result.safe_message} · {result.duration_ms}ms"
            )

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
