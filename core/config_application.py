"""分类不可变配置快照的即时与重启生效边界"""

from __future__ import annotations

from dataclasses import replace
from enum import Enum

from .config import AppConfig


class ConfigApplicationMode(str, Enum):
    """设置保存后实际采用的稳定生效模式"""

    NO_CHANGE = "no_change"
    IMMEDIATE_ONLY = "immediate_only"
    RESTART_REQUIRED = "restart_required"


def classify_config_change(
    previous: AppConfig,
    current: AppConfig,
) -> ConfigApplicationMode:
    """区分已支持即时应用与需要重启的设置变化"""

    if previous == current:
        return ConfigApplicationMode.NO_CHANGE
    without_immediate_changes = replace(
        current,
        wake_word=previous.wake_word,
        privacy=previous.privacy,
        tts=replace(
            current.tts,
            enabled=previous.tts.enabled,
            manual_input_enabled=previous.tts.manual_input_enabled,
        ),
        ui=replace(
            current.ui,
            active_skin=previous.ui.active_skin,
            always_on_top=previous.ui.always_on_top,
            start_at_login=previous.ui.start_at_login,
            theme_id=previous.ui.theme_id,
            reduce_motion=previous.ui.reduce_motion,
            pet_scale=previous.ui.pet_scale,
            pet_click_through=previous.ui.pet_click_through,
            preferred_screen=previous.ui.preferred_screen,
            global_hotkey_enabled=previous.ui.global_hotkey_enabled,
            global_hotkey=previous.ui.global_hotkey,
        ),
    )
    if without_immediate_changes == previous:
        return ConfigApplicationMode.IMMEDIATE_ONLY
    return ConfigApplicationMode.RESTART_REQUIRED
