"""Async SQLite database wrapper with explicit transactions.

The repository layer is the only code that contains SQL. Keeping SQL out of
handlers/services makes a future PostgreSQL adapter a contained change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiosqlite

from app.db.schema import MIGRATIONS

logger = logging.getLogger(__name__)


class Database:
    """SQLite backend (default for local development and rollback)."""

    backend = "sqlite"

    def __init__(self, path: str | Path, busy_timeout_ms: int = 5000) -> None:
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._tx_active: ContextVar[bool] = ContextVar(f"db_tx_active_{id(self)}", default=False)

    @property
    def label(self) -> str:
        return self.path

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self.execute("PRAGMA foreign_keys = ON")
        await self.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        await self.execute("PRAGMA synchronous = NORMAL")
        if self.path != ":memory:":
            await self.execute("PRAGMA journal_mode = WAL")
        # SQLite keeps its historical auto-migration on connect: it is a local
        # file database and this path is also the rollback target. The
        # PostgreSQL backend never migrates implicitly.
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Database]:
        """Run statements atomically.

        SQLite is a single-connection database here, so a lock plus a context
        variable prevents other tasks from accidentally joining this
        transaction. Nested calls reuse the outer transaction.
        """

        if self._tx_active.get():
            yield self
            return

        async with self._lock:
            token = self._tx_active.set(True)
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                yield self
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise
            finally:
                self._tx_active.reset(token)

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> aiosqlite.Cursor:
        params = tuple(params or ())
        if self._tx_active.get():
            return await self.connection.execute(sql, params)
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            await self.connection.commit()
            return cursor

    async def executemany(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> aiosqlite.Cursor:
        params = [tuple(item) for item in params_seq]
        if self._tx_active.get():
            return await self.connection.executemany(sql, params)
        async with self._lock:
            cursor = await self.connection.executemany(sql, params)
            await self.connection.commit()
            return cursor

    async def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> aiosqlite.Row | None:
        params = tuple(params or ())
        if self._tx_active.get():
            cursor = await self.connection.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[aiosqlite.Row]:
        params = tuple(params or ())
        if self._tx_active.get():
            cursor = await self.connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def executescript(self, sql: str) -> None:
        if self._tx_active.get():
            # executescript manages its own transaction; disallow nesting to
            # avoid surprising implicit commits.
            raise RuntimeError("executescript cannot run inside a transaction")
        async with self._lock:
            await self.connection.executescript(sql)
            await self.connection.commit()

    async def migrate(self) -> None:
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        row = await self.fetchone(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        )
        current = int(row["version"]) if row else 0
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            logger.info("Applying database migration %s", migration.version)
            await self.executescript(migration.sql)
            await self.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                (migration.version,),
            )
