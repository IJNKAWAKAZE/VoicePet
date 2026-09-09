"""把运行时文本收敛为 QML 可安全展示的 Markdown 子集"""

from __future__ import annotations

import re
import unicodedata

_INLINE_IMAGE = re.compile(r"!\[([^\]]*)\]\([^\r\n)]*\)")
_REFERENCE_IMAGE = re.compile(r"!\[([^\]]*)\]\s*\[[^\]]*\]")
_HTML_TAG = re.compile(r"<[^>\r\n]*>")
_INLINE_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^\r\n)]+)\)")
_REFERENCE_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\s*\[([^\]]+)\]")
_REFERENCE_DEFINITION = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*\S+\s*$")
_MARKDOWN_SPANS = re.compile(
    r"(?P<fence>^[ \t]{0,3}(?P<marker>`{3,}|~{3,})[^\n]*\n"
    r".*?(?:^[ \t]{0,3}(?P=marker)[`~]*[ \t]*$|\Z))"
    r"|(?P<code>(?P<ticks>`+)[^`]*?(?P=ticks)(?!`))"
    r"|(?P<escaped>\\.)"
    r"|(?P<strong>\*\*(?=\S)(?P<body>[^*\n]*?\S)\*\*)",
    re.MULTILINE | re.DOTALL,
)


def _normalize_emphasis(source: str) -> str:
    """给紧邻中文的标点加粗补充边界，保留代码和转义的原始内容"""

    def normalize(match: re.Match[str]) -> str:
        text = match.group(0)
        if not match.group("strong"):
            return text
        body = match.group("body")
        before = source[match.start() - 1] if match.start() else ""
        after = source[match.end()] if match.end() < len(source) else ""
        # Qt 遵循 Markdown 标点边界规则，中文两侧没有空格时需要补齐
        if before.isalnum() and unicodedata.category(body[0]).startswith("P"):
            text = " " + text
        if after.isalnum() and unicodedata.category(body[-1]).startswith("P"):
            text += " "
        return text

    return _MARKDOWN_SPANS.sub(normalize, source)


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
    return _normalize_emphasis(_REFERENCE_DEFINITION.sub("", safe))
