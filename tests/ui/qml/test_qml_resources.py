from pathlib import Path

import pytest

from ui.qml_resources import (
    QmlResourceError,
    qml_root,
    ui_assets_root,
    validated_asset_url,
)


def test_qml_root_resolves_source_tree():
    assert qml_root() == Path("ui/qml").resolve()
    assert (qml_root() / "Bootstrap.qml").is_file()


def test_qml_root_resolves_frozen_tree(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert qml_root() == (tmp_path / "ui" / "qml").resolve()


def test_ui_assets_root_resolves_source_tree():
    assert ui_assets_root() == Path("assets/ui").resolve()


def test_ui_assets_root_resolves_frozen_tree(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert ui_assets_root() == (tmp_path / "assets" / "ui").resolve()


def test_validated_asset_url_accepts_file_under_allowed_root(tmp_path):
    allowed = tmp_path / "pets" / "safe"
    allowed.mkdir(parents=True)
    asset = allowed / "sprite.webp"
    asset.write_bytes(b"asset")

    url = validated_asset_url(asset, (allowed,))

    assert url.isLocalFile()
    assert Path(url.toLocalFile()) == asset.resolve()


def test_validated_asset_url_rejects_escape(tmp_path):
    allowed = tmp_path / "pets" / "safe"
    allowed.mkdir(parents=True)
    outside = tmp_path / "private.png"
    outside.write_bytes(b"private")

    with pytest.raises(QmlResourceError):
        validated_asset_url(outside, (allowed,))


def test_validated_asset_url_requires_existing_file(tmp_path):
    allowed = tmp_path / "pets"
    allowed.mkdir()

    with pytest.raises(QmlResourceError):
        validated_asset_url(allowed / "missing.webp", (allowed,))
