"""Backend-neutral database interfaces.

Repositories depend on this shape, not on a concrete driver. SQLite keeps its
long-standing implementation in :mod:`app.db.database`; PostgreSQL lives in
:mod:`app.db.postgres`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


class CursorResult(Protocol):
    """Minimal cursor shape used by the repository layer."""

    @property
    def lastrowid(self) -> int | None: ...

    @property
    def rowcount(self) -> int: ...


@runtime_checkable
class DatabaseBackend(Protocol):
    """The API every backend must implement."""

    backend: str

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def transaction(self) -> AbstractAsyncContextManager[DatabaseBackend]: ...

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> CursorResult: ...

    async def executemany(
        self, sql: str, params_seq: Iterable[Sequence[Any]]
    ) -> CursorResult: ...

    async def fetchone(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> Mapping[str, Any] | None: ...

    async def fetchall(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[Mapping[str, Any]]: ...

    async def executescript(self, sql: str) -> None: ...


__all__ = ["CursorResult", "DatabaseBackend"]
