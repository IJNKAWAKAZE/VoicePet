"""中文唤醒模型的断点下载、安全校验与原子安装"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen

from .cancellation import CancellationToken, CancelledError

WAKE_MODEL_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
WAKE_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    f"{WAKE_MODEL_NAME}.tar.bz2"
)
WAKE_MODEL_BYTES = 32_654_866
WAKE_MODEL_SHA256 = (
    "b2f7c89690dc8ce4c6ed6afeab7cd800c36ad1421fb6b6302b4a4b194cf7f35f"
)
_DEFAULT_REQUIRED_FILES = {
    "tokens": "tokens.txt",
    "encoder": "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "decoder": "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "joiner": "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
}


class WakeModelError(RuntimeError):
    """唤醒模型下载、校验或持久化失败"""

    code = "wake.model"


class WakeModelState(str, Enum):
    """磁盘中中文唤醒模型的稳定状态"""

    MISSING = "missing"
    CACHED = "cached"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class WakeModelFiles:
    """加载关键词识别器所需的四个模型文件"""

    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path


@dataclass(frozen=True, slots=True)
class WakeDownloadProgress:
    """中文唤醒模型下载的确定进度"""

    downloaded_bytes: int
    total_bytes: int

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, self.downloaded_bytes * 100 // self.total_bytes)


class WakeModelStore:
    """持有固定官方模型包并只安装允许的运行文件"""

    def __init__(
        self,
        directory: str | Path,
        *,
        model_name: str = WAKE_MODEL_NAME,
        url: str = WAKE_MODEL_URL,
        expected_bytes: int = WAKE_MODEL_BYTES,
        sha256: str = WAKE_MODEL_SHA256,
        required_files: Mapping[str, str] | None = None,
        opener: Callable[[Request], object] | None = None,
        chunk_bytes: int = 256 * 1024,
        max_extracted_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        if expected_bytes <= 0 or chunk_bytes <= 0 or max_extracted_bytes <= 0:
            raise WakeModelError("唤醒模型大小参数必须大于零")
        if len(sha256) != 64:
            raise WakeModelError("唤醒模型摘要格式无效")
        self._root = Path(directory).expanduser().resolve()
        self._model_name = model_name
        self._url = url
        self._expected_bytes = expected_bytes
        self._sha256 = sha256.casefold()
        self._required_files = dict(
            required_files or _DEFAULT_REQUIRED_FILES
        )
        if set(self._required_files) != {
            "tokens",
            "encoder",
            "decoder",
            "joiner",
        }:
            raise WakeModelError("唤醒模型文件映射无效")
        self._opener = opener or urlopen
        self._chunk_bytes = chunk_bytes
        self._max_extracted_bytes = max_extracted_bytes

    @property
    def model_directory(self) -> Path:
        return self._root / self._model_name

    @property
    def package_path(self) -> Path:
        return self._root / f"{self._model_name}.tar.bz2"

    @property
    def partial_path(self) -> Path:
        return self._root / f"{self._model_name}.tar.bz2.part"

    def state(self) -> WakeModelState:
        if self._installed_files_valid():
            return WakeModelState.READY
        if self.package_path.is_file():
            try:
                self._verify_package(self.package_path)
            except WakeModelError:
                return WakeModelState.MISSING
            return WakeModelState.CACHED
        return WakeModelState.MISSING

    def files(self) -> WakeModelFiles:
        """返回完整模型路径并拒绝部分安装状态"""

        if not self._installed_files_valid():
            raise WakeModelError("中文唤醒模型尚未就绪")
        return WakeModelFiles(
            **{
                key: (self.model_directory / name).resolve()
                for key, name in self._required_files.items()
            }
        )

    async def download(
        self,
        on_progress: Callable[[WakeDownloadProgress], None],
        token: CancellationToken,
    ) -> Path:
        """在工作线程中下载并验证固定模型包"""

        return await asyncio.to_thread(
            self._download_sync,
            on_progress,
            token,
        )

    def install_package(self, package: str | Path) -> WakeModelFiles:
        """验证归档并原子替换已安装模型"""

        source = Path(package).expanduser().resolve()
        self._verify_package(source)
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".wake-install-", dir=self._root)
        )
        backup: Path | None = None
        try:
            self._extract_allowlisted_files(source, temporary)
            self._verify_installed_files(temporary)
            destination = self.model_directory
            if destination.exists():
                backup = Path(
                    tempfile.mkdtemp(prefix=".wake-backup-", dir=self._root)
                )
                backup.rmdir()
                os.replace(destination, backup)
            os.replace(temporary, destination)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            return self.files()
        except WakeModelError:
            if backup is not None and backup.exists():
                if self.model_directory.exists():
                    shutil.rmtree(self.model_directory, ignore_errors=True)
                os.replace(backup, self.model_directory)
            raise
        except (OSError, tarfile.TarError) as error:
            if backup is not None and backup.exists():
                if self.model_directory.exists():
                    shutil.rmtree(self.model_directory, ignore_errors=True)
                os.replace(backup, self.model_directory)
            raise WakeModelError("唤醒模型安装失败") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _download_sync(
        self,
        on_progress: Callable[[WakeDownloadProgress], None],
        token: CancellationToken,
    ) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        offset = self.partial_path.stat().st_size if self.partial_path.exists() else 0
        if offset > self._expected_bytes:
            self.partial_path.unlink()
            offset = 0
        try:
            token.throw_if_cancelled()
            if offset == self._expected_bytes:
                try:
                    self._verify_package(self.partial_path)
                except WakeModelError:
                    self.partial_path.unlink()
                    offset = 0
                else:
                    on_progress(
                        WakeDownloadProgress(offset, self._expected_bytes)
                    )
                    os.replace(self.partial_path, self.package_path)
                    return self.package_path.resolve()
            headers = {"User-Agent": "VoicePet/0.1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(self._url, headers=headers)
            with self._opener(request) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if offset and status != 206:
                    offset = 0
                mode = "ab" if offset else "wb"
                downloaded = offset
                with self.partial_path.open(mode) as output:
                    while True:
                        token.throw_if_cancelled()
                        chunk = response.read(self._chunk_bytes)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > self._expected_bytes:
                            raise WakeModelError("唤醒模型下载大小超出限制")
                        on_progress(
                            WakeDownloadProgress(
                                downloaded,
                                self._expected_bytes,
                            )
                        )
                    output.flush()
                    os.fsync(output.fileno())
            token.throw_if_cancelled()
            self._verify_package(self.partial_path)
            os.replace(self.partial_path, self.package_path)
            return self.package_path.resolve()
        except CancelledError:
            raise
        except WakeModelError:
            if self.partial_path.exists():
                self.partial_path.unlink()
            raise
        except (OSError, URLError) as error:
            raise WakeModelError("中文唤醒模型下载失败") from error

    def _verify_package(self, package: Path) -> None:
        try:
            if not package.is_file() or package.stat().st_size != self._expected_bytes:
                raise WakeModelError("唤醒模型包大小校验失败")
            digest = hashlib.sha256()
            with package.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest().casefold() != self._sha256:
                raise WakeModelError("唤醒模型包摘要校验失败")
        except OSError as error:
            raise WakeModelError("唤醒模型包校验失败") from error

    def _extract_allowlisted_files(self, package: Path, target: Path) -> None:
        expected_members = {
            PurePosixPath(self._model_name) / filename: (key, filename)
            for key, filename in self._required_files.items()
        }
        extracted: set[str] = set()
        total_bytes = 0
        try:
            with tarfile.open(package, mode="r:bz2") as archive:
                for member in archive:
                    member_path = PurePosixPath(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise WakeModelError("唤醒模型归档路径无效")
                    if member.isdir():
                        continue
                    if not member.isreg():
                        raise WakeModelError("唤醒模型归档包含不安全成员")
                    total_bytes += member.size
                    if total_bytes > self._max_extracted_bytes:
                        raise WakeModelError("唤醒模型归档展开大小超限")
                    expected = expected_members.get(member_path)
                    if expected is None:
                        continue
                    key, filename = expected
                    if key in extracted:
                        raise WakeModelError("唤醒模型归档包含重复成员")
                    source = archive.extractfile(member)
                    if source is None:
                        raise WakeModelError("唤醒模型归档成员无法读取")
                    destination = target / filename
                    written = 0
                    with source, destination.open("wb") as output:
                        while chunk := source.read(self._chunk_bytes):
                            output.write(chunk)
                            written += len(chunk)
                            if written > self._max_extracted_bytes:
                                raise WakeModelError("唤醒模型文件大小超限")
                        output.flush()
                        os.fsync(output.fileno())
                    extracted.add(key)
        except (OSError, tarfile.TarError) as error:
            raise WakeModelError("唤醒模型归档无法读取") from error
        if extracted != set(self._required_files):
            raise WakeModelError("唤醒模型归档缺少必要文件")

    def _installed_files_valid(self) -> bool:
        return all(
            path.is_file() and path.stat().st_size > 0
            for path in (
                self.model_directory / filename
                for filename in self._required_files.values()
            )
        )

    def _verify_installed_files(self, directory: Path) -> None:
        if not all(
            (directory / filename).is_file()
            and (directory / filename).stat().st_size > 0
            for filename in self._required_files.values()
        ):
            raise WakeModelError("唤醒模型安装内容不完整")
