"""提供写入前脱敏的结构化滚动日志"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .event_bus import EventBus, Subscription
from .events import (
    ApprovalRequested,
    LlmUsageRecorded,
    MemoryResultReady,
    RecordingStarted,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextInputSubmitted,
    ToolResultReady,
    TranscriptReady,
    WakeCommandPending,
)


class StructuredLogError(RuntimeError):
    """结构化日志配置或本地读写失败"""

    code = "logging.structured"


class StructuredLogStore:
    """以有界 JSONL 文件保存经过脱敏的运行记录"""

    _REDACTIONS = (
        re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~-]{12,}"),
        re.compile(r"(?i)(?:api[_-]?key|access[_-]?key)\s*[:=]\s*\S+"),
        re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s]+"),
        re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    )
    _SENSITIVE_KEYS = frozenset(
        {
            "api_key",
            "apikey",
            "access_key",
            "accesskey",
            "authorization",
            "token",
            "password",
            "secret",
            "prompt",
            "arguments",
            "content",
        }
    )

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        backup_count: int = 3,
    ) -> None:
        if max_bytes <= 0 or backup_count <= 0:
            raise StructuredLogError("日志轮转限制必须大于零")
        self._directory = Path(directory)
        self._path = self._directory / "voicepet.jsonl"
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._lock = threading.RLock()
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StructuredLogError("日志目录创建失败") from error

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        level: str,
        component: str,
        message: str,
        *,
        turn_id: str | None = None,
        correlation_id: str | None = None,
        event_type: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """追加一条不含敏感正文的结构化记录"""

        record: dict[str, object] = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "level": level.strip().upper(),
            "component": component.strip(),
            "message": self._redact_text(message),
        }
        if turn_id is not None:
            record["turn_id"] = turn_id
        if correlation_id is not None:
            record["correlation_id"] = correlation_id
        if event_type is not None:
            record["event_type"] = event_type
        if context:
            record["context"] = self._sanitize(context)
        encoded = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            with self._lock:
                current_size = self._path.stat().st_size if self._path.exists() else 0
                if current_size and current_size + len(encoded) > self._max_bytes:
                    self._rotate()
                with self._path.open("ab") as stream:
                    stream.write(encoded)
        except OSError as error:
            raise StructuredLogError("结构化日志写入失败") from error

    def tail(self, *, max_bytes: int = 256 * 1024) -> str:
        """跨轮转文件返回最新的有界日志文本"""

        if max_bytes <= 0:
            raise StructuredLogError("日志尾部限制必须大于零")
        try:
            with self._lock:
                paths = [
                    self._path.with_name(f"{self._path.name}.{index}")
                    for index in range(self._backup_count, 0, -1)
                ]
                paths.append(self._path)
                combined = b"".join(
                    path.read_bytes() for path in paths if path.is_file()
                )
        except OSError as error:
            raise StructuredLogError("结构化日志读取失败") from error
        return combined[-max_bytes:].decode("utf-8", errors="replace")

    def _rotate(self) -> None:
        for index in range(self._backup_count, 0, -1):
            source = (
                self._path
                if index == 1
                else self._path.with_name(f"{self._path.name}.{index - 1}")
            )
            if source.exists():
                target = self._path.with_name(f"{self._path.name}.{index}")
                os.replace(source, target)

    @classmethod
    def _redact_text(cls, value: str) -> str:
        redacted = value
        for pattern in cls._REDACTIONS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    @classmethod
    def _sanitize(cls, value: Any, *, key: str | None = None) -> object:
        normalized_key = key.lower().replace("-", "_") if key else None
        if normalized_key in cls._SENSITIVE_KEYS:
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                str(item_key): cls._sanitize(item, key=str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [cls._sanitize(item) for item in value]
        if isinstance(value, str):
            return cls._redact_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return type(value).__name__


RuntimeLogEvent = (
    StateChanged
    | TextInputSubmitted
    | TranscriptReady
    | LlmUsageRecorded
    | ApprovalRequested
    | ToolResultReady
    | MemoryResultReady
    | SpeakRequested
    | RuntimeErrorEvent
    | RecordingStarted
    | WakeCommandPending
)


class RuntimeEventLogger:
    """把选定运行事件映射为不含业务正文的安全日志"""

    _EVENT_TYPES = (
        StateChanged,
        TextInputSubmitted,
        TranscriptReady,
        LlmUsageRecorded,
        ApprovalRequested,
        ToolResultReady,
        MemoryResultReady,
        SpeakRequested,
        RuntimeErrorEvent,
        RecordingStarted,
        WakeCommandPending,
    )

    def __init__(self, event_bus: EventBus, store: StructuredLogStore) -> None:
        self._store = store
        self._subscriptions: list[Subscription] = [
            event_bus.subscribe(event_type, self._record_event)
            for event_type in self._EVENT_TYPES
        ]

    def close(self) -> None:
        """幂等取消全部运行事件订阅"""

        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            subscription.close()

    def _record_event(self, event: RuntimeLogEvent) -> None:
        level, component, message, context = self._event_fields(event)
        try:
            self._store.write(
                level,
                component,
                message,
                turn_id=str(event.turn_id),
                correlation_id=str(event.correlation_id),
                event_type=type(event).__name__,
                context=context,
            )
        except StructuredLogError:
            return

    @staticmethod
    def _event_fields(
        event: RuntimeLogEvent,
    ) -> tuple[str, str, str, Mapping[str, object]]:
        if isinstance(event, StateChanged):
            return (
                "info",
                "coordinator",
                "对话状态变化",
                {"previous": event.previous.value, "current": event.current.value},
            )
        if isinstance(event, TranscriptReady):
            return "info", "asr", "语音转写完成", {}
        if isinstance(event, TextInputSubmitted):
            return "info", "input", "手动文本已提交", {}
        if isinstance(event, WakeCommandPending):
            return "info", "wake", "唤醒后等待补充指令", {}
        if isinstance(event, RecordingStarted):
            return "info", "audio", "录音已开始", {}
        if isinstance(event, LlmUsageRecorded):
            return (
                "info",
                "llm",
                "LLM 请求完成",
                {
                    "model": event.model,
                    "duration_ms": event.duration_ms,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "turn_total_tokens": event.turn_total_tokens,
                },
            )
        if isinstance(event, ApprovalRequested):
            return (
                "info",
                "policy",
                "等待用户确认",
                {"tool_call_id": event.tool_call_id, "risk": event.risk},
            )
        if isinstance(event, ToolResultReady):
            return (
                "info",
                "tool_worker",
                "工具执行结束",
                {"tool_call_id": event.tool_call_id, "status": event.status},
            )
        if isinstance(event, MemoryResultReady):
            return (
                "info",
                "memory",
                "记忆操作结束",
                {"status": event.status, "affected": event.affected},
            )
        if isinstance(event, SpeakRequested):
            return "info", "tts", "开始播报", {}
        return (
            event.severity.value,
            event.component,
            event.safe_message,
            {
                "error_code": event.error_code,
                "retryable": event.retryable,
                "user_action_required": event.user_action_required,
                **event.diagnostic_context,
            },
        )
