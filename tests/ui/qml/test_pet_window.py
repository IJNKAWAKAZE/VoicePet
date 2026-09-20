import json
import shutil
from pathlib import Path

from PySide6.QtTest import QSignalSpy

from core.events import ConversationPhase
from ui.pet_animation_model import PetAnimationModel, PetInteractionController


def test_animation_model_publishes_v2_sheet_and_phase_rows(qapp):
    model = PetAnimationModel(Path("assets/pet/kawakaze").resolve())

    assert model.cell_width == 192
    assert model.cell_height == 208
    assert model.row == 0
    assert model.column == 0
    assert model.sheet_url.isLocalFile()

    model.set_phase(ConversationPhase.LISTENING)

    assert model.row == 6
    assert model.column == 0
    assert model.frame_duration == 150


def test_animation_model_uses_variable_durations_and_execution_followup(qapp):
    model = PetAnimationModel(Path("assets/pet/kawakaze").resolve())
    model.advance_frame()
    assert model.column == 1
    assert model.frame_duration == 110

    model.set_phase(ConversationPhase.EXECUTING_TOOL)
    for _ in range(5):
        model.advance_frame()

    assert model.row == 7
    assert model.column == 0


def test_single_click_waits_then_only_toggles_listening(qapp):
    controller = PetInteractionController(double_click_interval=5, drag_distance=10)
    listen = QSignalSpy(controller.listenToggleRequested)
    show = QSignalSpy(controller.showMainRequested)

    controller.pointer_press(10, 10, 1)
    controller.pointer_release(10, 10, 1)
    assert listen.count() == 0
    listen.wait(30)

    assert listen.count() == 1
    assert show.count() == 0


def test_double_click_cancels_pending_single_click(qapp):
    controller = PetInteractionController(double_click_interval=50, drag_distance=10)
    listen = QSignalSpy(controller.listenToggleRequested)
    show = QSignalSpy(controller.showMainRequested)

    controller.pointer_press(10, 10, 1)
    controller.pointer_release(10, 10, 1)
    controller.double_click(10, 10, 1)
    qapp.processEvents()

    assert listen.count() == 0
    assert show.count() == 1


def test_release_after_double_click_does_not_restart_single_click(qapp):
    controller = PetInteractionController(double_click_interval=20, drag_distance=10)
    listen = QSignalSpy(controller.listenToggleRequested)
    show = QSignalSpy(controller.showMainRequested)

    controller.pointer_press(10, 10, 1)
    controller.pointer_release(10, 10, 1)
    controller.double_click(10, 10, 1)
    controller.pointer_release(10, 10, 1)
    QSignalSpy(controller.listenToggleRequested).wait(40)

    assert show.count() == 1
    assert listen.count() == 0


def test_right_click_only_requests_quick_menu():
    controller = PetInteractionController(double_click_interval=5, drag_distance=10)
    listen = QSignalSpy(controller.listenToggleRequested)
    menu = QSignalSpy(controller.quickMenuRequested)

    controller.pointer_press(2, 3, 2)
    controller.pointer_release(2, 3, 2)

    assert menu.count() == 1
    assert listen.count() == 0


def test_drag_emits_position_without_click(qapp):
    controller = PetInteractionController(double_click_interval=5, drag_distance=8)
    listen = QSignalSpy(controller.listenToggleRequested)
    moved = QSignalSpy(controller.positionChanged)

    controller.pointer_press(10, 10, 1)
    controller.pointer_move(30, 25)
    controller.pointer_release(30, 25, 1)
    qapp.processEvents()

    assert moved.count() == 1
    assert moved.at(0) == [20.0, 15.0]
    assert listen.count() == 0


def test_drag_reports_incremental_deltas_after_threshold(qapp):
    controller = PetInteractionController(double_click_interval=5, drag_distance=8)
    moved = QSignalSpy(controller.positionChanged)

    controller.pointer_press(10, 10, 1)
    controller.pointer_move(30, 25)
    controller.pointer_move(35, 27)
    controller.pointer_release(35, 27, 1)

    assert moved.count() == 2
    assert moved.at(0) == [20.0, 15.0]
    assert moved.at(1) == [5.0, 2.0]


def test_animation_model_can_switch_to_valid_pet_directory(qapp, tmp_path):
    source = Path("assets/pet/kawakaze").resolve()
    target = tmp_path / "new-pet"
    target.mkdir()
    shutil.copy2(source / "spritesheet.webp", target / "spritesheet.webp")
    manifest = json.loads((source / "pet.json").read_text(encoding="utf-8"))
    manifest["id"] = "new-pet"
    (target / "pet.json").write_text(json.dumps(manifest), encoding="utf-8")
    animation = PetAnimationModel(source)
    assert hasattr(animation, "sheetChanged")
    changed = QSignalSpy(animation.sheetChanged)

    animation.load_directory(target)

    assert changed.count() == 1
    assert Path(animation.sheet_url.toLocalFile()) == (target / "spritesheet.webp").resolve()
    assert animation.row == 0
    assert animation.column == 0
