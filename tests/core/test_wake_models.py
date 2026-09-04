import asyncio
import hashlib
import io
import tarfile

import pytest

from core.cancellation import CancellationSource, CancelledError
from core.wake_models import (
    WakeDownloadProgress,
    WakeModelError,
    WakeModelState,
    WakeModelStore,
)

MODEL_NAME = "test-kws-model"
REQUIRED_FILES = {
    "tokens.txt": b"tokens",
    "encoder.onnx": b"encoder",
    "decoder.onnx": b"decoder",
    "joiner.onnx": b"joiner",
}


def make_archive(*, extra_members=()):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        for name, content in REQUIRED_FILES.items():
            info = tarfile.TarInfo(f"{MODEL_NAME}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for info, content in extra_members:
            archive.addfile(info, io.BytesIO(content) if content else None)
    return buffer.getvalue()


def build_store(tmp_path, package, **settings):
    return WakeModelStore(
        tmp_path / "wake",
        model_name=MODEL_NAME,
        required_files={
            "tokens": "tokens.txt",
            "encoder": "encoder.onnx",
            "decoder": "decoder.onnx",
            "joiner": "joiner.onnx",
        },
        expected_bytes=len(package),
        sha256=hashlib.sha256(package).hexdigest(),
        **settings,
    )


def test_wake_model_store_installs_fixed_package_atomically(tmp_path):
    package = make_archive()
    source = tmp_path / "model.tar.bz2"
    source.write_bytes(package)
    store = build_store(tmp_path, package)

    assert store.state() is WakeModelState.MISSING
    files = store.install_package(source)

    assert store.state() is WakeModelState.READY
    assert files.tokens.read_bytes() == b"tokens"
    assert files.encoder.read_bytes() == b"encoder"
    assert files.decoder.read_bytes() == b"decoder"
    assert files.joiner.read_bytes() == b"joiner"
    assert not tuple((tmp_path / "wake").glob(".wake-install-*"))


def test_wake_model_store_rejects_bad_hash_and_preserves_ready_model(tmp_path):
    package = make_archive()
    source = tmp_path / "model.tar.bz2"
    source.write_bytes(package)
    store = build_store(tmp_path, package)
    installed = store.install_package(source)
    old_encoder = installed.encoder.read_bytes()
    damaged = tmp_path / "damaged.tar.bz2"
    damaged.write_bytes(package + b"damage")

    with pytest.raises(WakeModelError, match="校验"):
        store.install_package(damaged)

    assert store.files().encoder.read_bytes() == old_encoder


@pytest.mark.parametrize("kind", ["traversal", "symlink", "duplicate"])
def test_wake_model_store_rejects_unsafe_archive_members(tmp_path, kind):
    if kind == "traversal":
        info = tarfile.TarInfo("../outside.txt")
        info.size = 1
        extras = ((info, b"x"),)
    elif kind == "symlink":
        info = tarfile.TarInfo(f"{MODEL_NAME}/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "tokens.txt"
        extras = ((info, b""),)
    else:
        info = tarfile.TarInfo(f"{MODEL_NAME}/tokens.txt")
        info.size = 3
        extras = ((info, b"dup"),)
    package = make_archive(extra_members=extras)
    source = tmp_path / f"{kind}.tar.bz2"
    source.write_bytes(package)
    store = build_store(tmp_path, package)

    with pytest.raises(WakeModelError, match="归档"):
        store.install_package(source)

    assert store.state() is WakeModelState.MISSING
    assert not (tmp_path / "outside.txt").exists()


class FakeResponse(io.BytesIO):
    def __init__(self, data, *, status):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def test_wake_model_download_resumes_and_reports_progress(tmp_path):
    data = b"abcdef"
    requests = []

    def opener(request):
        requests.append(request)
        return FakeResponse(b"def", status=206)

    store = WakeModelStore(
        tmp_path / "wake",
        model_name=MODEL_NAME,
        expected_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        opener=opener,
        chunk_bytes=2,
    )
    store.partial_path.parent.mkdir(parents=True)
    store.partial_path.write_bytes(b"abc")
    progress = []

    result = asyncio.run(
        store.download(progress.append, CancellationSource().token)
    )

    assert requests[0].get_header("Range") == "bytes=3-"
    assert result.read_bytes() == data
    assert progress[-1] == WakeDownloadProgress(6, 6)


def test_wake_model_download_restarts_when_server_ignores_range(tmp_path):
    data = b"abcdef"
    store = WakeModelStore(
        tmp_path / "wake",
        model_name=MODEL_NAME,
        expected_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        opener=lambda request: FakeResponse(data, status=200),
    )
    store.partial_path.parent.mkdir(parents=True)
    store.partial_path.write_bytes(b"bad")

    result = asyncio.run(store.download(lambda progress: None, CancellationSource().token))

    assert result.read_bytes() == data


def test_wake_model_download_promotes_valid_complete_partial_without_network(
    tmp_path,
):
    data = b"abcdef"
    requests = []
    store = WakeModelStore(
        tmp_path / "wake",
        model_name=MODEL_NAME,
        expected_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        opener=lambda request: requests.append(request),
    )
    store.partial_path.parent.mkdir(parents=True)
    store.partial_path.write_bytes(data)
    progress = []

    result = asyncio.run(
        store.download(progress.append, CancellationSource().token)
    )

    assert requests == []
    assert result == store.package_path.resolve()
    assert result.read_bytes() == data
    assert progress == [WakeDownloadProgress(len(data), len(data))]


def test_wake_model_download_cancellation_keeps_partial_file(tmp_path):
    data = b"abcdef"
    source = CancellationSource()
    store = WakeModelStore(
        tmp_path / "wake",
        model_name=MODEL_NAME,
        expected_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        opener=lambda request: FakeResponse(data, status=200),
        chunk_bytes=2,
    )

    def cancel_after_first_chunk(progress):
        if progress.downloaded_bytes >= 2:
            source.cancel("test_cancel")

    with pytest.raises(CancelledError):
        asyncio.run(store.download(cancel_after_first_chunk, source.token))

    assert store.partial_path.read_bytes() == b"ab"


def test_wake_model_download_removes_corrupt_complete_partial(tmp_path):
    data = b"abcdef"
    store = WakeModelStore(
        tmp_path / "wake",
        model_name=MODEL_NAME,
        expected_bytes=len(data),
        sha256="0" * 64,
        opener=lambda request: FakeResponse(data, status=200),
    )

    with pytest.raises(WakeModelError, match="校验"):
        asyncio.run(store.download(lambda progress: None, CancellationSource().token))

    assert not store.partial_path.exists()
