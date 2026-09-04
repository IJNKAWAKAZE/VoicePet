"""Tool Worker 匿名管道使用的严格私有引导帧"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

MAX_BOOTSTRAP_BYTES = 64 * 1024


class WorkerBootstrapError(RuntimeError):
    """Worker 私有引导帧格式或资源配置无效"""

    code = "worker.bootstrap"


@dataclass(frozen=True, slots=True)
class WorkerBootstrap:
    """只通过匿名管道发送的一次性 Worker 会话配置"""

    secret: bytes
    audit_database: Path
    allowed_roots: tuple[Path, ...]
    policy_version: int = 1
    controlled_shell_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise WorkerBootstrapError("Worker 会话密钥至少需要 256 位")
        database = Path(self.audit_database)
        if not database.is_absolute() or not database.parent.is_dir():
            raise WorkerBootstrapError("Worker 审计数据库路径无效")
        roots = tuple(Path(root) for root in self.allowed_roots)
        if not roots:
            raise WorkerBootstrapError("Worker 至少需要一个允许根目录")
        if any(not root.is_absolute() or not root.is_dir() for root in roots):
            raise WorkerBootstrapError("Worker 允许根目录无效")
        if type(self.policy_version) is not int or self.policy_version <= 0:
            raise WorkerBootstrapError("Worker 策略版本必须大于零")
        if type(self.controlled_shell_enabled) is not bool:
            raise WorkerBootstrapError("Worker Shell 开关类型无效")
        object.__setattr__(self, "audit_database", database.resolve())
        object.__setattr__(
            self,
            "allowed_roots",
            tuple(root.resolve() for root in roots),
        )


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise WorkerBootstrapError("Worker 会话密钥编码无效")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, ValueError) as error:
        raise WorkerBootstrapError("Worker 会话密钥编码无效") from error
    if _encode_base64(decoded) != value:
        raise WorkerBootstrapError("Worker 会话密钥不是规范编码")
    return decoded


def encode_bootstrap(value: WorkerBootstrap) -> bytes:
    """把 Worker 会话配置编码为单行规范 JSON"""

    data = {
        "version": 1,
        "secret": _encode_base64(value.secret),
        "audit_database": str(value.audit_database),
        "allowed_roots": [str(root) for root in value.allowed_roots],
        "policy_version": value.policy_version,
        "controlled_shell_enabled": value.controlled_shell_enabled,
    }
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_BOOTSTRAP_BYTES:
        raise WorkerBootstrapError("Worker 引导帧超过大小上限")
    return encoded


def decode_bootstrap(line: bytes) -> WorkerBootstrap:
    """严格解码且不回显原始内容的 Worker 引导帧"""

    if not isinstance(line, bytes) or len(line) > MAX_BOOTSTRAP_BYTES:
        raise WorkerBootstrapError("Worker 引导帧超过大小上限")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        data = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkerBootstrapError("Worker 引导帧 JSON 无效") from error
    expected = {
        "version",
        "secret",
        "audit_database",
        "allowed_roots",
        "policy_version",
        "controlled_shell_enabled",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise WorkerBootstrapError("Worker 引导帧字段无效")
    if data["version"] != 1:
        raise WorkerBootstrapError("Worker 引导协议版本无效")
    roots = data["allowed_roots"]
    if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
        raise WorkerBootstrapError("Worker 允许根目录编码无效")
    database = data["audit_database"]
    if not isinstance(database, str):
        raise WorkerBootstrapError("Worker 审计数据库编码无效")
    return WorkerBootstrap(
        _decode_base64(data["secret"]),
        Path(database),
        tuple(Path(root) for root in roots),
        data["policy_version"],
        data["controlled_shell_enabled"],
    )
