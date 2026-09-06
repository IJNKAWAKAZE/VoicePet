"""SQLite 记忆写入使用的短事务辅助"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """用跨连接写锁包住一次完整的记忆状态变更"""

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
