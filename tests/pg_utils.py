"""Helpers for PostgreSQL integration tests.

Every test runs in a throwaway schema (``test_<random>``) that is dropped
afterwards, so ``TEST_DATABASE_URL`` may point at a shared development
database without touching its ``public`` schema.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from app.db.dsn import dsn_has_sslmode, normalize_dsn


def require_dsn() -> str:
    """Return the configured test DSN or skip the test."""

    dsn = os.getenv("TEST_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set; skipping PostgreSQL integration tests")
    return dsn


def ssl_mode() -> str:
    """``prefer`` works for local servers; Supabase DSNs carry sslmode."""

    return os.getenv("TEST_DATABASE_SSL", "").strip() or "prefer"


def connect_kwargs(dsn: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"statement_cache_size": 0, "timeout": 15.0}
    if not dsn_has_sslmode(dsn):
        kwargs["ssl"] = ssl_mode()
    return kwargs


@contextlib.asynccontextmanager
async def temporary_schema() -> AsyncIterator[tuple[str, str]]:
    """Create an isolated schema and drop it on exit."""

    asyncpg = pytest.importorskip("asyncpg")
    dsn = require_dsn()
    schema = f"test_{uuid.uuid4().hex[:12]}"
    connection = await asyncpg.connect(normalize_dsn(dsn), **connect_kwargs(dsn))
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        yield dsn, schema
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@contextlib.asynccontextmanager
async def pg_database(*, migrate: bool = True, pool_max: int = 3) -> AsyncIterator:
    """Yield a connected :class:`PostgresDatabase` inside a fresh schema."""

    pytest.importorskip("asyncpg")
    from app.db.migrate import apply_migrations
    from app.db.postgres import PostgresDatabase

    async with temporary_schema() as (dsn, schema):
        if migrate:
            await apply_migrations(dsn, schema=schema, ssl=ssl_mode())
        database = PostgresDatabase(
            dsn,
            schema=schema,
            pool_min=1,
            pool_max=pool_max,
            ssl=ssl_mode(),
            statement_cache_size=0,
            command_timeout=15.0,
        )
        await database.connect()
        try:
            yield database, schema
        finally:
            await database.close()
