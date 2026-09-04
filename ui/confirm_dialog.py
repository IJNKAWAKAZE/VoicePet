"""高风险工具影响范围的非阻塞界面确认对话框"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_STYLE = """
QDialog { background: #081827; color: #EAF6F5; font-family: "Microsoft YaHei UI"; }
QLabel#risk_badge { color: #081827; background: #FFB86B; border-radius: 4px; padding: 4px 9px; font-weight: 700; }
QLabel#confirmation_title { color: #65D6D0; font-family: DengXian; font-size: 20px; font-weight: 600; }
QLabel#confirmation_summary { background: #102B3F; border: 1px solid #1C455C; border-radius: 6px; padding: 12px; }
QPushButton { min-width: 96px; padding: 8px 16px; border-radius: 4px; }
QPushButton#approve_action { color: #081827; background: #65D6D0; border: 0; font-weight: 700; }
QPushButton#reject_action { color: #EAF6F5; background: transparent; border: 1px solid #52758A; }
"""


class ConfirmationDialog(QDialog):
    """显示本地策略摘要并提供明确确认与取消动作"""

    def __init__(
        self,
        *,
        timeout_ms: int = 30_000,
        parent: QWidget | None = None,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("确认超时必须大于零")
        super().__init__(parent)
        self.setWindowTitle("确认电脑操作")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(440)
        self.setStyleSheet(_STYLE)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(timeout_ms)
        self._timeout.timeout.connect(self.reject)
        self.accepted.connect(self._timeout.stop)
        self.rejected.connect(self._timeout.stop)

        title = QLabel("确认电脑操作")
        title.setObjectName("confirmation_title")
        self.risk_label = QLabel()
        self.risk_label.setObjectName("risk_badge")
        heading = QHBoxLayout()
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(self.risk_label)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("confirmation_summary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)

        self.reject_button = QPushButton("取消")
        self.reject_button.setObjectName("reject_action")
        self.approve_button = QPushButton("确认执行")
        self.approve_button.setObjectName("approve_action")
        self.reject_button.clicked.connect(self.reject)
        self.approve_button.clicked.connect(self.accept)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.reject_button)
        actions.addWidget(self.approve_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addLayout(heading)
        layout.addWidget(QLabel("VoicePet 请求执行以下操作，请确认影响范围"))
        layout.addWidget(self.summary_label)
        layout.addLayout(actions)

    def show_request(self, summary: str, risk: str) -> None:
        """更新纯文本影响摘要并非阻塞显示"""

        self.summary_label.setText(summary)
        self.risk_label.setText(risk)
        self._timeout.start()
        self.show()
        self.raise_()
        self.activateWindow()
