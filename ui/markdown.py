"""把运行时文本收敛为 QML 可安全展示的 Markdown 子集"""

from __future__ import annotations

import re

_INLINE_IMAGE = re.compile(r"!\[([^\]]*)\]\([^\r\n)]*\)")
_REFERENCE_IMAGE = re.compile(r"!\[([^\]]*)\]\s*\[[^\]]*\]")
_HTML_TAG = re.compile(r"<[^>\r\n]*>")
_INLINE_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^\r\n)]+)\)")
_REFERENCE_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\s*\[([^\]]+)\]")
_REFERENCE_DEFINITION = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*\S+\s*$")


def sanitize_markdown(source: str) -> str:
    """移除图片和 HTML 并把链接降级为可复制文字"""

    if not isinstance(source, str):
        return ""
    safe = _INLINE_IMAGE.sub(lambda match: match.group(1), source)
    safe = _REFERENCE_IMAGE.sub(lambda match: match.group(1), safe)
    safe = _HTML_TAG.sub("", safe)
    safe = _INLINE_LINK.sub(lambda match: f"{match.group(1)} ({match.group(2)})", safe)
    safe = _REFERENCE_LINK.sub(
        lambda match: f"{match.group(1)} [{match.group(2)}]", safe
    )
    return _REFERENCE_DEFINITION.sub("", safe)
