import json

from core.window_state import WindowPosition, WindowPositionStore


def test_window_position_store_round_trips_multi_screen_coordinates(tmp_path):
    store = WindowPositionStore(tmp_path / "window-state.json")
    position = WindowPosition(-1200, 640)

    store.save(position)

    assert store.load() == position
    assert json.loads((tmp_path / "window-state.json").read_text("utf-8")) == {
        "pet_x": -1200,
        "pet_y": 640,
    }


def test_window_position_store_ignores_missing_or_invalid_state(tmp_path):
    path = tmp_path / "window-state.json"
    store = WindowPositionStore(path)

    assert store.load() is None
    path.write_text('{"pet_x": true, "pet_y": 2}', encoding="utf-8")
    assert store.load() is None
