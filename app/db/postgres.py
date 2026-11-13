"""PostgreSQL backend built on asyncpg.

Design notes
------------
* **Transaction pooler safe.** Defaults are ``statement_cache_size=0`` and a
  small pool (``min 0`` / ``max 2``), which suits Supabase's port 6543 pooler
  and serverless runtimes. Prepared statements, session state and
  LISTEN/NOTIFY are never relied upon.
* **Public schema only in Phase 1.** The runtime uses the connection's default
  (``public``) schema. ``schema=`` exists for disposable test schemas and for
  direct/session connections; ``search_path`` is not safely controllable
  through a transaction-mode pooler.
* **Same API as SQLite.** The repository layer passes SQLite syntax; the
  rewrites in :mod:`app.db.sql_dialect` and the thin cursor shim below keep
  every repository unchanged.
* **No implicit migrations.** :meth:`connect` only opens the pool. Schema
  changes run through the explicit runner in :mod:`app.db.migrate`.
* **Short transactions only.** A transaction owns one pooled connection and is
  never held across a Telegram API call. Transaction state is task-local: a
  child task that inherits the parent's ``ContextVar`` gets a clear
  :class:`RuntimeError` instead of silently waiting for another connection
  (which would deadlock a ``pool_max=1`` deployment). The copied context
  survives even when the child is awaited after the parent transaction closes,
  so database-using child tasks must be created outside the transaction or with
  an explicitly clean context.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.db.dsn import describe_dsn, dsn_has_sslmode, normalize_dsn
from app.db.sql_dialect import add_returning_id, to_postgres

logger = logging.getLogger(__name__)

_TASK_LOCAL_TRANSACTION = (
    "Transaction context is task-local and cannot be reused from a child task. "
    "The child task inherited the parent's transaction context, and that copied "
    "context persists even if the child is awaited after the parent transaction "
    "closes. Create database-using child tasks outside the parent transaction, "
    "or pass an explicitly clean context (for example "
    "asyncio.create_task(..., context=contextvars.Context())). The parent's "
    "connection is owned by the parent task; reusing it would interleave queries "
    "on one connection, and acquiring a second connection could deadlock a small "
    "pool."
)


@dataclass(frozen=True, slots=True)
class Result:
    """Cursor-compatible result for ``execute``/``executemany``."""

    lastrowid: int | None = None
    rowcount: int = 0


class PostgresDatabase:
    """asyncpg-backed implementation of the repository database API."""

    backend = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 0,
        pool_max: int = 2,
        ssl: str | bool = "require",
        statement_cache_size: int = 0,
        schema: str | None = None,
        command_timeout: float | None = 30.0,
        connect_timeout: float = 10.0,
    ) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("DATABASE_URL is required for the postgres backend")
        if pool_min < 0 or pool_max < 1 or pool_max < pool_min:
            raise ValueError(
                "DB_POOL_MIN must be >= 0 and DB_POOL_MAX must be >= max(1, DB_POOL_MIN)"
            )
        self._dsn = normalize_dsn(dsn.strip())
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._ssl = ssl
        self._statement_cache_size = statement_cache_size
        self._schema = schema
        self._command_timeout = command_timeout
        self._connect_timeout = connect_timeout
        self._pool: asyncpg.Pool | None = None
        self._tx_conn: ContextVar[
            tuple[asyncio.Task[Any] | None, asyncpg.Connection] | None
        ] = ContextVar(f"pg_tx_conn_{id(self)}", default=None)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"PostgresDatabase({describe_dsn(self._dsn)})"

    @property
    def label(self) -> str:
        """Log-safe description of the connection target."""

        label = describe_dsn(self._dsn)
        return f"{label} schema={self._schema}" if self._schema else label

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        return self._pool

    async def connect(self) -> None:
        """Open the pool.

        Deliberately does **not** create or upgrade schema. Run
        ``python -m app.db.migrate`` before starting the application.
        """

        if self._pool is not None:
            return
        server_settings: dict[str, str] = {"application_name": "telegram-automation-bot"}
        if self._schema:
            server_settings["search_path"] = self._schema
        kwargs: dict[str, Any] = {
            "min_size": self._pool_min,
            "max_size": self._pool_max,
            "statement_cache_size": self._statement_cache_size,
            "server_settings": server_settings,
            "command_timeout": self._command_timeout,
            "timeout": self._connect_timeout,
        }
        if not dsn_has_sslmode(self._dsn):
            kwargs["ssl"] = self._ssl
        self._pool = await asyncpg.create_pool(self._dsn, **kwargs)
        logger.info("Connected to %s (pool %s-%s)", self.label, self._pool_min, self._pool_max)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PostgresDatabase]:
        """Run statements atomically on a single pooled connection.

        Nested calls **in the same asyncio task** reuse the outer transaction,
        matching the SQLite backend. A child task does not inherit the parent's
        connection at runtime: ``ContextVar`` values are copied into new tasks,
        so the owner task is recorded and checked. That copy persists even if the
        child is awaited after the parent transaction closes.

        Attempting to use the database from such a child task raises
        :class:`RuntimeError` immediately. The alternative -- silently acquiring
        a second pooled connection -- would deadlock when ``DB_POOL_MAX=1``.
        Create database-using child tasks outside an active transaction, or pass
        an explicitly clean context (for example
        ``asyncio.create_task(..., context=contextvars.Context())``). A clean
        context only prevents inheritance; the child still needs a free pool
        slot, so with a small pool it must not be awaited to completion while
        the parent transaction holds its connection.
        """

        if self._active_connection() is not None:
            yield self
            return

        connection = await self.pool.acquire()
        token = self._tx_conn.set((asyncio.current_task(), connection))
        try:
            async with connection.transaction():
                yield self
        finally:
            self._tx_conn.reset(token)
            await self.pool.release(connection)

    async def execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> Result:
        statement, returns_id = add_returning_id(to_postgres(sql))
        values = tuple(params or ())

        async def run(connection: asyncpg.Connection) -> Result:
            if returns_id:
                rows = await connection.fetch(statement, *values)
                lastrowid = int(rows[0]["id"]) if rows else None
                return Result(lastrowid=lastrowid, rowcount=len(rows))
            status = await connection.execute(statement, *values)
            return Result(lastrowid=None, rowcount=_rowcount(status))

        return await self._run(run)

    async def executemany(
        self, sql: str, params_seq: Iterable[Sequence[Any]]
    ) -> Result:
        statement = to_postgres(sql)
        rows = [tuple(item) for item in params_seq]

        async def run(connection: asyncpg.Connection) -> Result:
            await connection.executemany(statement, rows)
            return Result(lastrowid=None, rowcount=len(rows))

        return await self._run(run)

    async def fetchone(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        statement = to_postgres(sql)
        values = tuple(params or ())

        async def run(connection: asyncpg.Connection) -> dict[str, Any] | None:
            record = await connection.fetchrow(statement, *values)
            return dict(record) if record is not None else None

        return await self._run(run)

    async def fetchall(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        statement = to_postgres(sql)
        values = tuple(params or ())

        async def run(connection: asyncpg.Connection) -> list[dict[str, Any]]:
            records = await connection.fetch(statement, *values)
            return [dict(record) for record in records]

        return await self._run(run)

    async def executescript(self, sql: str) -> None:
        """Execute a multi-statement script (used only by tooling)."""

        async def run(connection: asyncpg.Connection) -> None:
            await connection.execute(sql)

        await self._run(run)

    async def _run(self, operation: Callable[[asyncpg.Connection], Awaitable[Any]]) -> Any:
        """Run ``operation`` inside the task's transaction or a fresh connection."""

        connection = self._active_connection()
        if connection is not None:
            return await operation(connection)
        async with self.pool.acquire() as acquired:
            return await operation(acquired)

    def _active_connection(self) -> asyncpg.Connection | None:
        """Return the current task's transaction connection, if any.

        ``ContextVar`` values are copied into child tasks, so the recorded
        owner is checked. A child task gets a clear error instead of a second
        pooled acquisition: with ``DB_POOL_MAX=1`` that acquisition would wait
        forever for the connection its parent is still holding.
        """

        state = self._tx_conn.get()
        if state is None:
            return None
        owner, connection = state
        if owner is not asyncio.current_task():
            raise RuntimeError(_TASK_LOCAL_TRANSACTION)
        return connection


def _rowcount(status: str) -> int:
    """Parse asyncpg's command tag, e.g. ``UPDATE 3`` -> ``3``."""

    _, _, tail = status.rpartition(" ")
    try:
        return int(tail)
    except ValueError:
        return 0


__all__: list[str] = ["PostgresDatabase", "Result"]
