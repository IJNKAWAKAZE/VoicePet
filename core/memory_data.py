"""本地记忆查看、软删除与原子 JSON 导出"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .memory import MemoryRecord, MemoryStore
from .memory_facts import MemoryChange
from .session_archive import SessionArchiveStore


class MemoryDataError(RuntimeError):
    """记忆数据管理或导出失败"""

    code = "memory.data"


class MemoryDataManager:
    """在 MemoryStore 事实源上提供用户主动的数据操作"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        archive: SessionArchiveStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._archive = archive
        self._clock = clock

    def list_records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._with_source(record) for record in self._store.list_all())

    def delete(self, memory_id: str) -> bool:
        return self._store.delete(memory_id)

    def confirm(self, memory_id: str) -> MemoryRecord:
        return self._store.confirm(memory_id)

    def resolve_conflict(self, memory_id: str) -> MemoryRecord:
        return self._store.resolve_conflict(memory_id)

    def edit(
        self,
        memory_id: str,
        content: str,
        expected_version: int,
    ) -> MemoryRecord:
        return self._store.edit(memory_id, content, expected_version)

    def undo(self, change_id: str) -> bool:
        return self._store.undo(change_id)

    def list_changes(
        self,
        session_id: str | None = None,
    ) -> tuple[MemoryChange, ...]:
        return self._store.list_changes(session_id)

    def mark_changes_viewed(self, change_ids: Sequence[str]) -> int:
        return self._store.mark_changes_viewed(change_ids)

    def _with_source(self, record: MemoryRecord) -> MemoryRecord:
        if self._archive is None:
            state = "manual" if not record.session_id else "unavailable"
            return replace(
                record,
                source_state=state,
                source_time=record.created_at if state == "manual" else None,
            )
        label = self._archive.source_label(record.source_turn_id)
        state = str(label["status"])
        if state == "missing" and not record.session_id and record.origin in {
            "explicit",
            "manual",
        }:
            state = "manual"
        source_session = str(label["session_id"] or record.session_id)
        source_time = label["created_at"]
        if state == "manual" and source_time is None:
            source_time = record.created_at
        return replace(
            record,
            source_title=str(label["title"]),
            source_state=state,
            source_time=source_time,
            session_id=source_session,
        )

    def export_json(self, destination: str | Path) -> int:
        """把当前可见记忆原子导出到用户指定的本地文件"""

        records = self._store.list_all()
        payload = {
            "format": "voicepet-memory-export",
            "version": 2,
            "exported_at": self._clock().astimezone(UTC).isoformat(),
            "memories": [self._serialize(record) for record in records],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise MemoryDataError("记忆导出失败") from error
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return len(records)

    @staticmethod
    def _serialize(record: MemoryRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "category": record.category,
            "content": record.content,
            "source_turn_id": record.source_turn_id,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "confidence": record.confidence,
            "sensitivity": record.sensitivity.value,
            "expires_at": (
                None if record.expires_at is None else record.expires_at.isoformat()
            ),
            "status": record.status.value,
            "fact_key": record.fact_key,
            "value": record.value,
            "cardinality": record.cardinality,
            "origin": record.origin,
            "version": record.version,
            "conflict_id": record.conflict_id,
            "keywords": list(record.keywords),
            "session_id": record.session_id,
        }
