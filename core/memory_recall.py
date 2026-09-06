"""为有界记忆查询生成共享的字面检索词"""

from __future__ import annotations

import re
from collections.abc import Sequence

MAX_HINTS = 5
MAX_TERMS = 24
MAX_HINT_CHARS = 2000
MAX_TERM_CHARS = 64

_STOP_WORDS = frozenset(
    {
        "什么",
        "怎么",
        "今天",
        "一下",
        "可以",
        "我们",
        "我的",
        "你的",
        "这个",
        "那个",
        "现在",
        "已经",
        "还是",
        "你好",
        "继续",
        "我",
        "你",
        "的",
        "了",
        "是",
        "好",
    }
)


def query_terms(hints: Sequence[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    """从最多五条带权提示生成最多二十四个去重检索词"""

    if isinstance(hints, (str, bytes, bytearray)) or not isinstance(hints, Sequence):
        raise TypeError("记忆召回提示无效")
    if len(hints) > MAX_HINTS:
        raise ValueError("记忆召回提示过多")
    groups = []
    for hint in hints:
        if not isinstance(hint, Sequence) or isinstance(hint, (str, bytes, bytearray)):
            raise TypeError("记忆召回提示无效")
        if len(hint) != 2:
            raise ValueError("记忆召回提示无效")
        text, weight = hint
        if not isinstance(text, str) or not text.strip():
            raise ValueError("记忆召回提示文本无效")
        if type(weight) is not int or not 1 <= weight <= 3:
            raise ValueError("记忆召回提示权重无效")
        groups.append((tuple(dict.fromkeys(term.casefold() for term in _text_terms(text))), weight))
    selected = dict.fromkeys(
        term for index in range(MAX_TERMS) for terms, _weight in groups
        if index < len(terms) for term in (terms[index],)
    )
    # 轮流选词保证近期线索不会被长问题挤掉，当前问题仍保留更高权重
    return tuple((term, sum(weight for terms, weight in groups if term in terms))
                 for term in tuple(selected)[:MAX_TERMS])


def literal_fts_query(terms: Sequence[str]) -> str:
    """把检索词编码成不会注入 FTS 运算符的 OR 查询"""

    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def escape_like(term: str) -> str:
    """转义 SQLite LIKE 通配符"""

    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def score_text(text: str, terms: Sequence[tuple[str, int]]) -> int:
    """按词长和提示权重计算确定性相关分"""

    normalized = text.casefold()
    return sum(weight * len(term) for term, weight in terms if term in normalized)


def sql_relevance(text_expression: str, terms: Sequence[tuple[str, int]]) -> tuple[str, list[object]]:
    """对内部固定列表达式生成参数化分数，在取有限记录前先按相关性排序"""

    expressions, parameters = [], []
    for term, weight in terms:
        expressions.append(f"CASE WHEN instr(lower({text_expression}),?)>0 THEN ? ELSE 0 END")
        parameters.extend((term, weight * len(term)))
    return " + ".join(expressions) or "0", parameters


def _text_terms(text: str) -> tuple[str, ...]:
    bounded = text.strip()[:MAX_HINT_CHARS]
    normalized = re.sub(r"\s+", "", bounded)
    terms = [
        term[:MAX_TERM_CHARS] for term in re.findall(r"[a-zA-Z0-9_]{2,}", bounded)
    ]
    for width in (3, 2):
        terms.extend(
            normalized[index : index + width]
            for index in range(len(normalized) - width + 1)
        )
    if len(normalized) <= 2:
        terms.append(normalized)
    return tuple(
        dict.fromkeys(term for term in terms if term and term not in _STOP_WORDS)
    )
