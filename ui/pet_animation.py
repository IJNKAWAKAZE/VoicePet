"""Codex v2 桌宠图集加载、切片与状态行映射"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QPixmap

from core.events import ConversationPhase

CELL_WIDTH = 192
CELL_HEIGHT = 208
ATLAS_COLUMNS = 8
ATLAS_ROWS = 11
ATLAS_WIDTH = CELL_WIDTH * ATLAS_COLUMNS
ATLAS_HEIGHT = CELL_HEIGHT * ATLAS_ROWS
STANDARD_ANIMATION_DURATIONS = (
    (280, 110, 110, 140, 140, 320),
    (120, 120, 120, 120, 120, 120, 120, 220),
    (120, 120, 120, 120, 120, 120, 120, 220),
    (140, 140, 140, 280),
    (140, 140, 140, 140, 280),
    (140, 140, 140, 140, 140, 140, 140, 240),
    (150, 150, 150, 150, 150, 260),
    (120, 120, 120, 120, 120, 220),
    (150, 150, 150, 150, 150, 280),
)


class PetAssetError(RuntimeError):
    """桌宠清单、图集路径或像素尺寸无效"""

    code = "pet.asset"


@dataclass(frozen=True, slots=True)
class PetSpriteAtlas:
    """已验证尺寸且可按固定单元格读取的 v2 图集"""

    pet_id: str
    display_name: str
    description: str
    sheet_path: Path
    _pixmap: QPixmap

    @classmethod
    def load(cls, directory: str | Path) -> PetSpriteAtlas:
        root = Path(directory).expanduser().resolve()
        manifest_path = root / "pet.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PetAssetError("桌宠清单无法读取") from error
        if not isinstance(data, dict):
            raise PetAssetError("桌宠清单顶层必须是对象")
        required = {
            "id",
            "displayName",
            "description",
            "spritesheetPath",
        }
        missing = required.difference(data)
        if missing:
            raise PetAssetError("桌宠清单缺少必要字段：" + "、".join(sorted(missing)))
        text_values = (
            data["id"],
            data["displayName"],
            data["description"],
            data["spritesheetPath"],
        )
        if any(not isinstance(value, str) or not value.strip() for value in text_values):
            raise PetAssetError("桌宠清单文本字段无效")
        sheet_path = (root / data["spritesheetPath"]).resolve()
        try:
            sheet_path.relative_to(root)
        except ValueError as error:
            raise PetAssetError("桌宠图集路径超出形象目录") from error
        if not sheet_path.is_file():
            raise PetAssetError("桌宠图集文件不存在")
        image = QImage(str(sheet_path))
        if image.isNull():
            raise PetAssetError("桌宠图集无法解码")
        if image.width() != ATLAS_WIDTH or image.height() not in {CELL_HEIGHT * 9, ATLAS_HEIGHT}:
            raise PetAssetError("桌宠图集尺寸必须为 1536x1872 或 1536x2288")
        if not image.hasAlphaChannel():
            raise PetAssetError("桌宠图集必须包含透明通道")
        return cls(
            data["id"],
            data["displayName"],
            data["description"],
            sheet_path,
            QPixmap.fromImage(image),
        )

    def frame(self, row: int, column: int) -> QPixmap:
        """返回指定 v2 行列的独立 192x208 帧"""

        # 按当前图集的实际行数裁切，九行包不能读取不存在的尾部行
        if row not in range(self._pixmap.height() // CELL_HEIGHT) or column not in range(ATLAS_COLUMNS):
            raise PetAssetError("桌宠帧索引超出图集范围")
        return self._pixmap.copy(
            QRect(
                column * CELL_WIDTH,
                row * CELL_HEIGHT,
                CELL_WIDTH,
                CELL_HEIGHT,
            )
        )


def animation_row_for_phase(phase: ConversationPhase) -> int:
    """把对话阶段映射为设计文档规定的标准动画行"""

    return {
        ConversationPhase.IDLE: 0,
        ConversationPhase.LISTENING: 6,
        ConversationPhase.TRANSCRIBING: 7,
        ConversationPhase.THINKING: 7,
        ConversationPhase.AWAITING_APPROVAL: 6,
        ConversationPhase.EXECUTING_TOOL: 4,
        ConversationPhase.SYNTHESIZING: 0,
        ConversationPhase.SPEAKING: 0,
        ConversationPhase.RECOVERING: 5,
    }[phase]
