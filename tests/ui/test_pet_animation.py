import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

import ui.pet_animation as pet_animation_module
from core.events import ConversationPhase
from ui.pet_animation import (
    PetAssetError,
    PetSpriteAtlas,
    animation_row_for_phase,
)
from ui.pet_animation_model import PetAnimationModel


def app():
    return QApplication.instance() or QApplication([])


def test_default_v2_atlas_loads_manifest_and_exact_cells():
    app()
    directory = Path("assets/pet/dpsk-girl").resolve()

    atlas = PetSpriteAtlas.load(directory)

    assert atlas.pet_id == "dpsk-girl"
    assert atlas.display_name == "鲸鱼娘"
    assert atlas.frame(0, 0).size() == QSize(192, 208)
    assert atlas.frame(10, 7).size() == QSize(192, 208)
    with pytest.raises(PetAssetError):
        atlas.frame(11, 0)
    with pytest.raises(PetAssetError):
        atlas.frame(0, 8)


@pytest.mark.parametrize(
    ("phase", "row"),
    [
        (ConversationPhase.IDLE, 0),
        (ConversationPhase.LISTENING, 6),
        (ConversationPhase.AWAITING_APPROVAL, 6),
        (ConversationPhase.TRANSCRIBING, 7),
        (ConversationPhase.THINKING, 7),
        (ConversationPhase.EXECUTING_TOOL, 4),
        (ConversationPhase.RECOVERING, 5),
        (ConversationPhase.SPEAKING, 0),
    ],
)
def test_animation_rows_follow_conversation_semantics(phase, row):
    assert animation_row_for_phase(phase) == row


@pytest.mark.parametrize(
    ("row", "durations"),
    [
        (0, (280, 110, 110, 140, 140, 320)),
        (1, (120, 120, 120, 120, 120, 120, 120, 220)),
        (2, (120, 120, 120, 120, 120, 120, 120, 220)),
        (3, (140, 140, 140, 280)),
        (4, (140, 140, 140, 140, 280)),
        (5, (140, 140, 140, 140, 140, 140, 140, 240)),
        (6, (150, 150, 150, 150, 150, 260)),
        (7, (120, 120, 120, 120, 120, 220)),
        (8, (150, 150, 150, 150, 150, 280)),
    ],
)
def test_standard_animation_rows_publish_exact_v2_durations(row, durations):
    assert pet_animation_module.STANDARD_ANIMATION_DURATIONS[row] == durations


@pytest.mark.parametrize(
    ("phase", "frame_count", "first_duration"),
    [
        (ConversationPhase.IDLE, 6, 280),
        (ConversationPhase.LISTENING, 6, 150),
        (ConversationPhase.TRANSCRIBING, 6, 120),
        (ConversationPhase.RECOVERING, 8, 140),
    ],
)
def test_animation_model_wraps_at_valid_frame_count(
    phase,
    frame_count,
    first_duration,
):
    app()
    model = PetAnimationModel(Path("assets/pet/dpsk-girl").resolve())

    model.set_phase(phase)
    assert model.frame_duration == first_duration
    for _ in range(frame_count):
        model.advance_frame()

    assert model.column == 0
    assert model.frame_duration == first_duration


def test_animation_model_resets_on_phase_and_runs_execution_reaction_once():
    app()
    model = PetAnimationModel(Path("assets/pet/dpsk-girl").resolve())

    model.advance_frame()
    assert (model.row, model.column) == (0, 1)

    model.set_phase(ConversationPhase.LISTENING)
    assert (model.row, model.column) == (6, 0)

    model.set_phase(ConversationPhase.EXECUTING_TOOL)
    for _ in range(5):
        model.advance_frame()
    assert (model.row, model.column) == (7, 0)
