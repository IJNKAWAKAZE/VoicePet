"""把运行时文本收敛为 QML 可安全展示的 Markdown 子集"""

from __future__ import annotations

import re
import unicodedata

# 只有真正的 HTML 元素才当标签抹掉，List<String> 这类文本必须原样保留
_HTML_ELEMENTS = frozenset(
    {
        "a", "abbr", "audio", "b", "blockquote", "br", "button", "code", "dd",
        "del", "details", "div", "dl", "dt", "em", "embed", "font", "form",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "iframe", "img",
        "input", "ins", "kbd", "label", "li", "link", "mark", "meta", "object",
        "ol", "option", "p", "param", "picture", "pre", "q", "s", "samp",
        "script", "section", "select", "small", "source", "span", "strong",
        "style", "sub", "summary", "sup", "svg", "table", "tbody", "td",
        "textarea", "tfoot", "th", "thead", "tr", "u", "ul", "var", "video",
    }
)

# 代码块、行内代码与转义序列里的原文不能被任何规则改写
_PROTECTED_SPANS = (
    r"(?P<fence>^[ \t]{0,3}(?P<marker>`{3,}|~{3,})[^\n]*\n"
    r".*?(?:^[ \t]{0,3}(?P=marker)[`~]*[ \t]*$|\Z))"
    r"|(?P<code>(?P<ticks>`+)[^`]*?(?P=ticks)(?!`))"
    r"|(?P<escaped>\\.)"
)

_MARKDOWN_SPANS = re.compile(
    _PROTECTED_SPANS + r"|(?P<strong>\*\*(?=\S)(?P<body>[^*\n]*?\S)\*\*)",
    re.MULTILINE | re.DOTALL,
)

# 属性必须写成 name、name=value 或 name="value"，否则 <b 且 c> 这类比较式会被误判成标签
_HTML_ATTRIBUTES = (
    r"""(?:\s+[A-Za-z_:][-A-Za-z0-9_:.]*"""
    r"""(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*"""
)

# 一次遍历同时处理图片、链接与尖括号，后面的规则不会再改到已处理的内容
_SPANS = re.compile(
    _PROTECTED_SPANS
    + r"|(?P<comment><!--.*?-->)"
    + r"|(?P<tag></?(?P<name>[A-Za-z][A-Za-z0-9]*)"
    + _HTML_ATTRIBUTES
    + r"\s*/?>)"
    r"|(?P<image>!\[(?P<image_alt>[^\]]*)\]\([^\r\n)]*\))"
    r"|(?P<image_ref>!\[(?P<image_ref_alt>[^\]]*)\]\s*\[[^\]]*\])"
    r"|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^\r\n)]+)\))"
    r"|(?P<link_ref>\[(?P<link_ref_text>[^\]]+)\]\s*\[(?P<link_ref_id>[^\]]+)\])"
    r"|(?P<definition>^[ \t]*\[[^\]]+\]:\s*\S+[ \t]*$)"
    # Qt 的 Markdown 导入器会把 < 加字母当成 HTML 标签，连标签后面的内容一起吞掉
    r"|(?P<angle><(?=[A-Za-z/!?]))",
    re.MULTILINE | re.DOTALL,
)
_ESCAPED_ANGLE = re.compile(r"\\<")


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


def _rewrite(match: re.Match[str]) -> str:
    """代码与转义原样保留，图片与链接降级为文字，标签与尖括号按文本处理"""

    if match.group("fence") or match.group("code") or match.group("escaped"):
        return match.group(0)
    if match.group("comment"):
        return ""
    if match.group("tag") is not None:
        if match.group("name").lower() in _HTML_ELEMENTS:
            return ""
        # 不是 HTML 元素就只是普通文本：转义 < 让 Markdown 按字面量渲染
        return "\\" + match.group(0)
    if match.group("image") is not None:
        return match.group("image_alt")
    if match.group("image_ref") is not None:
        return match.group("image_ref_alt")
    if match.group("link") is not None:
        return f"{match.group('link_text')} ({match.group('link_url')})"
    if match.group("link_ref") is not None:
        return f"{match.group('link_ref_text')} [{match.group('link_ref_id')}]"
    if match.group("definition") is not None:
        return ""
    return "\\<"


def sanitize_markdown(source: str) -> str:
    """抹掉 HTML 元素、把图片与链接降级为文字，并让普通文本里的 < 按字面量显示"""

    if not isinstance(source, str):
        return ""
    return _normalize_emphasis(_SPANS.sub(_rewrite, source))


def unescape_markdown(source: str) -> str:
    """还原展示用的反斜杠转义：复制与朗读需要拿到原始字符"""

    if not isinstance(source, str):
        return ""
    return _ESCAPED_ANGLE.sub("<", source)
