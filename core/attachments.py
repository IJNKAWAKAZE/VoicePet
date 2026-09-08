"""本地聊天附件的元数据和安全读取辅助函数"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass(frozen=True, slots=True)
class Attachment:
    """不把文件正文加载进内存的附件描述"""

    path: str
    name: str
    media_type: str
    size: int
    sha256: str
    kind: str


def inspect_attachment(path: str | Path) -> Attachment:
    """读取文件属性并生成无需扫描正文的快速摘要"""

    raw_path = str(path)
    if raw_path.casefold().startswith("file:"):
        parsed = urlparse(raw_path)
        decoded = unquote(parsed.path)
        # Windows file:///C:/... URL 会多一个表示根的斜杠
        if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":":
            decoded = decoded[1:]
        raw_path = decoded
    resolved = Path(raw_path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("附件路径必须是文件")
    stat = resolved.stat()
    media_type = mimetypes.guess_type(resolved.name)[0]
    if media_type is None:
        media_type = {
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(resolved.suffix.casefold(), "application/octet-stream")
    kind = "image" if media_type.startswith("image/") else "file"
    digest = hashlib.sha256()
    digest.update(str(resolved).encode("utf-8", "surrogatepass"))
    digest.update(b"\0")
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return Attachment(
        str(resolved),
        resolved.name,
        media_type,
        stat.st_size,
        digest.hexdigest(),
        kind,
    )
