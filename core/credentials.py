"""使用 Windows DPAPI 加密的本地凭据存储"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

_ALLOWED_NAMES = frozenset({"openai_api_key"})
_MAX_CIPHERTEXT_BYTES = 64 * 1024


class CredentialError(RuntimeError):
    """凭据加密、结构验证或持久化失败"""

    code = "credentials.error"


class ProtectionBackend(Protocol):
    """凭据存储所需的加密与解密边界"""

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...


class WindowsDpapiBackend:
    """把凭据绑定到当前 Windows 用户的 DPAPI 适配器"""

    def __init__(self) -> None:
        try:
            import win32crypt
        except (ImportError, ModuleNotFoundError) as error:
            raise CredentialError("缺少 Windows DPAPI 依赖") from error
        self._win32crypt = win32crypt

    def protect(self, data: bytes) -> bytes:
        try:
            return self._win32crypt.CryptProtectData(
                data,
                "VoicePet credentials",
                None,
                None,
                None,
                0,
            )
        except Exception as error:
            raise CredentialError("Windows DPAPI 加密失败") from error

    def unprotect(self, data: bytes) -> bytes:
        try:
            _, plaintext = self._win32crypt.CryptUnprotectData(
                data,
                None,
                None,
                None,
                0,
            )
            return plaintext
        except Exception as error:
            raise CredentialError("Windows DPAPI 解密失败") from error


class DpapiCredentialStore:
    """原子保存单个加密文档且只接受固定凭据名称"""

    def __init__(
        self,
        path: str | Path,
        *,
        backend: ProtectionBackend | None = None,
    ) -> None:
        self._path = Path(path)
        self._backend = backend or WindowsDpapiBackend()

    def get(self, name: str) -> str | None:
        self._validate_name(name)
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        self._validate_name(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 8192:
            raise CredentialError("凭据值无效")
        credentials = self._load()
        credentials[name] = value
        self._save(credentials)

    def delete(self, name: str) -> bool:
        self._validate_name(name)
        credentials = self._load()
        if name not in credentials:
            return False
        del credentials[name]
        self._save(credentials)
        return True

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            ciphertext = self._path.read_bytes()
        except OSError as error:
            raise CredentialError("凭据文件无法读取") from error
        if not ciphertext or len(ciphertext) > _MAX_CIPHERTEXT_BYTES:
            raise CredentialError("凭据文件大小无效")
        try:
            plaintext = self._backend.unprotect(ciphertext)
            data = json.loads(plaintext.decode("utf-8"))
        except CredentialError:
            raise
        except (RuntimeError, UnicodeError, json.JSONDecodeError) as error:
            raise CredentialError("凭据文件无法解密或结构无效") from error
        if not isinstance(data, dict) or set(data) != {"version", "credentials"}:
            raise CredentialError("凭据文档字段无效")
        if data["version"] != 1 or not isinstance(data["credentials"], dict):
            raise CredentialError("凭据文档版本或内容无效")
        credentials = data["credentials"]
        if set(credentials) - _ALLOWED_NAMES or any(
            not isinstance(value, str) or not value for value in credentials.values()
        ):
            raise CredentialError("凭据文档字段无效")
        return dict(credentials)

    def _save(self, credentials: dict[str, str]) -> None:
        plaintext = json.dumps(
            {"version": 1, "credentials": credentials},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            ciphertext = self._backend.protect(plaintext)
        except CredentialError:
            raise
        except Exception as error:
            raise CredentialError("凭据加密失败") from error
        if not ciphertext or len(ciphertext) > _MAX_CIPHERTEXT_BYTES:
            raise CredentialError("加密凭据大小无效")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as file:
                file.write(ciphertext)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except OSError as error:
            raise CredentialError("凭据文件保存失败") from error
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _validate_name(name: str) -> None:
        if name not in _ALLOWED_NAMES:
            raise CredentialError("凭据名称无效")
