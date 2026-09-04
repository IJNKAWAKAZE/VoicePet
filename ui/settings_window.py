"""深海声波风格的 VoicePet 设置窗口"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from PySide6.QtCore import QSignalBlocker, Qt, Signal
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
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig, ConfigError

from .pet_shell import PetChoice

_STYLE = """
QWidget { color: #EAF6F5; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QWidget#settings_root { background: #071521; }
QWidget#settings_sidebar { background: #081B29; border-right: 1px solid #17384A; }
QLabel#brand_mark { color: #65D6D0; font-family: DengXian; font-size: 20px; font-weight: 700; padding: 24px 20px 2px 20px; }
QLabel#brand_caption { color: #7294A5; font-family: Consolas; font-size: 10px; letter-spacing: 1px; padding: 0 20px 18px 20px; }
QLabel#local_mark { color: #52758A; font-family: Consolas; font-size: 10px; padding: 16px 20px; }
QListWidget#settings_navigation { background: transparent; border: 0; padding: 8px 10px; outline: 0; }
QListWidget#settings_navigation::item { color: #9BB3BE; padding: 12px 16px; margin: 3px 0; border: 1px solid transparent; border-left: 3px solid transparent; border-radius: 7px; }
QListWidget#settings_navigation::item:hover { color: #EAF6F5; background: #0D2637; }
QListWidget#settings_navigation::item:selected { color: #65D6D0; background: #102B3F; border-left: 3px solid #65D6D0; }
QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: #071521; border: 0; }
QLabel#section_title { color: #EAF6F5; font-family: DengXian; font-size: 25px; font-weight: 700; }
QLabel#section_description { color: #7294A5; font-size: 12px; padding-bottom: 8px; }
QFrame#settings_card { background: #0B2030; border: 1px solid #173E52; border-radius: 12px; }
QLabel#card_title { color: #EAF6F5; font-family: DengXian; font-size: 16px; font-weight: 600; }
QLabel#card_description { color: #7294A5; font-size: 11px; }
QFrame#settings_footer { background: #091C2A; border-top: 1px solid #17384A; }
QPushButton { background: #102B3F; color: #EAF6F5; border: 1px solid #315C70; border-radius: 8px; padding: 8px 13px; min-height: 20px; }
QPushButton:hover { background: #15364B; border-color: #65D6D0; color: #FFFFFF; }
QPushButton:pressed { background: #0A2131; }
QPushButton:disabled { color: #527080; background: #0A1B28; border-color: #183849; }
QPushButton#save_settings { background: #65D6D0; color: #071521; border: 1px solid #65D6D0; border-radius: 7px; padding: 9px 24px; font-weight: 700; }
QPushButton#save_settings:hover { background: #86E5DF; }
QPushButton#save_settings:pressed { background: #4DBBB7; }
QPushButton#memory_action { background: #102B3F; color: #EAF6F5; border: 1px solid #315C70; border-radius: 8px; padding: 8px 12px; }
QPushButton#memory_action:hover { border-color: #65D6D0; color: #65D6D0; }
QComboBox, QLineEdit, QPlainTextEdit, QDoubleSpinBox, QSpinBox { background: #071A28; border: 1px solid #285064; border-radius: 7px; padding: 6px 9px; min-height: 26px; selection-background-color: #1F4B60; }
QPlainTextEdit { min-height: 180px; }
QComboBox:hover, QLineEdit:hover, QPlainTextEdit:hover, QDoubleSpinBox:hover, QSpinBox:hover { border-color: #3E7186; }
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus { border-color: #65D6D0; }
QComboBox QAbstractItemView { background: #0B2030; color: #EAF6F5; border: 1px solid #315C70; selection-background-color: #1F4B60; }
QComboBox::drop-down { border: 0; width: 30px; }
QComboBox::down-arrow { width: 8px; height: 8px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { background: #102B3F; border: 0; border-left: 1px solid #285064; width: 22px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: #1A4055; }
QSlider::groove:horizontal { height: 4px; background: #1C455C; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #65D6D0; border-radius: 2px; }
QSlider::handle:horizontal { background: #EAF6F5; border: 2px solid #65D6D0; width: 14px; margin: -6px 0; border-radius: 8px; }
QProgressBar { background: #071A28; border: 1px solid #285064; border-radius: 7px; min-height: 20px; text-align: center; color: #EAF6F5; }
QProgressBar::chunk { background: #65D6D0; border-radius: 6px; }
QLabel#sonar_value { color: #65D6D0; font-family: Consolas; font-weight: 700; min-width: 42px; }
QLabel#validation_message { color: #FFB3A7; padding: 4px 0; }
QCheckBox { min-height: 28px; spacing: 9px; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px; border: 1px solid #3B687C; background: #071A28; }
QCheckBox::indicator:hover { border-color: #65D6D0; }
QCheckBox::indicator:checked { background: #65D6D0; border: 4px solid #173A4D; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 6px 2px; }
QScrollBar::handle:vertical { background: #315C70; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: #65D6D0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px 6px; }
QScrollBar::handle:horizontal { background: #315C70; border-radius: 4px; min-width: 36px; }
QScrollBar::handle:horizontal:hover { background: #65D6D0; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
"""


class SettingsWindow(QWidget):
    """按用户可识别概念组织的设置窗口"""

    save_requested = Signal(object)
    memory_refresh_requested = Signal()
    memory_delete_requested = Signal(str)
    memory_confirm_requested = Signal(str)
    memory_resolve_requested = Signal(str)
    memory_export_requested = Signal()
    credential_save_requested = Signal(str)
    credential_delete_requested = Signal()
    pet_file_import_requested = Signal()
    pet_directory_import_requested = Signal()
    wake_model_download_requested = Signal()
    wake_model_download_cancel_requested = Signal()
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
        self.voice_combo = QComboBox()
        self.voice_combo.setEditable(True)
        self.voice_combo.addItems(
            [
                config.tts.voice,
                "zh-CN-XiaoxiaoNeural",
                "zh-CN-YunxiNeural",
            ]
        )
        self.voice_combo.setCurrentText(config.tts.voice)
        self.speech_enabled_checkbox = QCheckBox("语音播报")
        self.speech_enabled_checkbox.setChecked(config.tts.enabled)
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
                self.debounce_spin,
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
                "播报声音",
                self.voice_combo,
            ),
        )

        self.llm_model_edit = QLineEdit(config.llm.model)
        self.llm_api_combo = QComboBox()
        self.llm_api_combo.addItem("Responses API", "responses")
        self.llm_api_combo.addItem(
            "Chat Completions API",
            "chat_completions",
        )
        self.llm_api_combo.setCurrentIndex(
            self.llm_api_combo.findData(config.llm.api)
        )
        self.llm_base_url_edit = QLineEdit(config.llm.base_url)
        self.llm_base_url_edit.setPlaceholderText(
            "留空时使用 OpenAI 官方默认地址"
        )
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.addItems(["low", "medium", "high"])
        self.reasoning_combo.setCurrentText(config.llm.reasoning_effort)
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
                self.llm_api_combo,
                "API Base URL",
                self.llm_base_url_edit,
                "模型",
                self.llm_model_edit,
                "推理强度",
                self.reasoning_combo,
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

        self.pet_combo = QComboBox()
        self.pet_combo.setEditable(False)
        self.pet_combo.addItem(config.ui.active_skin, config.ui.active_skin)
        self.pet_combo.currentIndexChanged.connect(
            self._emit_pet_selection_changed
        )
        self.always_on_top_checkbox = QCheckBox("让桌宠保持在其他窗口上方")
        self.always_on_top_checkbox.setChecked(config.ui.always_on_top)
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
                self.pet_combo,
                "导入形象",
                pet_import_panel,
            ),
            self._card(
                "窗口行为",
                "控制桌宠的显示层级与开发预览",
                "窗口层级",
                self.always_on_top_checkbox,
                "开发预览",
                self.hot_reload_checkbox,
            ),
        )

        self.memory_checkbox = QCheckBox("保存已确认的偏好与长期记忆")
        self.memory_checkbox.setChecked(config.privacy.memory_enabled)
        self.short_term_retention_spin = QSpinBox()
        self.short_term_retention_spin.setRange(1, 365)
        self.short_term_retention_spin.setSuffix(" 天")
        self.short_term_retention_spin.setValue(
            config.privacy.short_term_retention_days
        )
        self.diagnostic_recording_checkbox = QCheckBox(
            "仅在主动诊断时保留录音"
        )
        self.diagnostic_recording_checkbox.setChecked(
            config.privacy.diagnostic_recording
        )
        self.memory_list = QListWidget()
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
                "所有长期记忆由你确认后才会保存",
                "记忆",
                self.memory_checkbox,
                "短期摘要保留",
                self.short_term_retention_spin,
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
            ("桌宠", pet),
            ("隐私", privacy),
            ("诊断", diagnostics),
        )

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
                    voice=self.voice_combo.currentText().strip(),
                ),
                ui=replace(
                    self._config.ui,
                    active_skin=self.selected_pet_id(),
                    always_on_top=self.always_on_top_checkbox.isChecked(),
                    hot_reload_skin=self.hot_reload_checkbox.isChecked(),
                ),
                privacy=replace(
                    self._config.privacy,
                    memory_enabled=self.memory_checkbox.isChecked(),
                    short_term_retention_days=(
                        self.short_term_retention_spin.value()
                    ),
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
