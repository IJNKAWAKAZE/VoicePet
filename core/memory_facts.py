"""长期事实的数据契约与无副作用归一化规则"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

SINGLE_VALUE_FACT_KEYS = frozenset(
    {
        "user.name",
        "response.length",
        "response.language",
        "response.style",
    }
)

_NAME_PATTERNS = (
    re.compile(r"(?:我叫|我改名叫|请叫我|以后(?:请)?叫我|称呼我为)\s*([^，。！？,.!?\s]{1,64})"),
    re.compile(r"(?:名字是|姓名是)\s*([^，。！？,.!?\s]{1,64})"),
)

_CONTROLLED_ALIASES = {
    "response.length": {
        "concise": ("简短", "简洁", "短一点"),
        "detailed": ("详细", "展开", "具体一点"),
    },
    "response.language": {
        "chinese": ("中文",),
        "english": ("英文", "英语"),
    },
    "response.style": {
        "casual": ("随意", "轻松"),
        "formal": ("正式", "严谨"),
    },
}


@dataclass(frozen=True, slots=True)
class FactEvidence:
    turn_id: str
    session_id: str
    user_text: str


@dataclass(frozen=True, slots=True)
class FactProposal:
    category: str
    content: str
    fact_key: str
    value: str
    quote: str
    confidence: float = 0.9
    stability: str = "stable"
    sensitivity: str = "normal"
    explicit_update: bool = False
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryChange:
    id: str
    memory_id: str
    session_id: str
    source_turn_id: str
    kind: str
    after_version: int
    created_at: datetime
    undone: bool


def normalize_fact_value(value: str) -> str:
    """生成用于事实身份比较的稳定文本"""

    return re.sub(r"\s+", " ", value).strip().casefold()


def is_explicit_correction(content: str) -> bool:
    """只把清楚的更正表达用作单值事实替换授权"""

    return bool(re.search(r"改名|改为|改成|以后.*叫我|以后.*称呼我|不再|从现在起", content))


def fact_fingerprint(fact_key: str, value: str) -> str:
    """生成不泄露正文的来源抑制指纹"""

    material = f"{fact_key}\0{normalize_fact_value(value)}".encode()
    return hashlib.sha256(material).hexdigest()


def freeform_fact_key(content: str) -> str:
    """为没有结构化键的手动事实生成独立身份"""

    digest = hashlib.sha256(normalize_fact_value(content).encode()).hexdigest()
    return f"freeform.{digest}"


def infer_explicit_fact(content: str) -> tuple[str, str] | None:
    """识别少量明确的称呼与回复偏好表达"""

    for pattern in _NAME_PATTERNS:
        match = pattern.search(content)
        if match is not None:
            return "user.name", match.group(1)
    for fact_key, values in _CONTROLLED_ALIASES.items():
        if not re.search(r"回复|回答|表达|说话|语气", content):
            continue
        for value, aliases in values.items():
            if any(alias in content for alias in aliases):
                return fact_key, value
    return None


def proposal_value_is_supported(fact_key: str, value: str, user_text: str) -> bool:
    """核验事实值来自用户原文或有限受控映射"""

    normalized_value = normalize_fact_value(value)
    normalized_text = normalize_fact_value(user_text)
    if normalized_value in normalized_text:
        return True
    aliases = _CONTROLLED_ALIASES.get(fact_key, {}).get(normalized_value, ())
    return any(alias in user_text for alias in aliases)


def is_direct_user_assertion(fact_key: str, value: str, user_text: str) -> bool:
    """保守识别可自动确认的直接用户陈述"""

    if re.search(r"[？?]", user_text):
        return False
    normalized_value = normalize_fact_value(value)
    aliases = _CONTROLLED_ALIASES.get(fact_key, {}).get(normalized_value, ())
    negated_values = (value.strip(), *aliases)
    if any(
        re.search(
            rf"(?:不|没|并不|并非|不是|不再|不要|不想).{{0,12}}{re.escape(item)}",
            user_text,
            re.IGNORECASE,
        )
        for item in negated_values
        if item
    ):
        return False
    escaped_value = re.escape(value.strip())
    if re.search(r"(?:我(?:的)?(?:朋友|同事|家人)|他|她|他们|她们).{0,16}", user_text):
        return False
    if fact_key == "user.name":
        return bool(
            re.search(
                rf"(?:我叫|我改名叫|请叫我|以后(?:请)?叫我|称呼我为|名字是|姓名是)"
                rf"\s*{escaped_value}",
                user_text,
                re.IGNORECASE,
            )
        )
    if fact_key in _CONTROLLED_ALIASES:
        return bool(re.search(r"回复|回答|表达|说话|语气", user_text)) and any(
            alias in user_text for alias in aliases
        )
    return bool(re.search(r"(?:^|\W)我(?:的|会|是|很|也|一直|喜欢|偏爱|想|希望)?", user_text))
