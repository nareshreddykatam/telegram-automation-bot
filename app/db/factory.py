"""Backend selection.

SQLite stays the default so local development, the existing test suite and the
rollback path are unchanged. PostgreSQL is opt-in through
``DATABASE_BACKEND=postgres`` plus ``DATABASE_URL``.
"""

from __future__ import annotations

from app.config import ConfigError, Settings
from app.db.base import DatabaseBackend
from app.db.database import Database
from app.db.dsn import describe_dsn


def create_database(settings: Settings) -> DatabaseBackend:
    """Build the configured backend. Does not connect or migrate."""

    if settings.database_backend == "sqlite":
        return Database(settings.database_path)

    if settings.database_backend == "postgres":
        # Imported lazily so the sqlite-only development path never needs
        # asyncpg installed.
        from app.db.postgres import PostgresDatabase

        if not settings.database_url:
            raise ConfigError("DATABASE_URL is required when DATABASE_BACKEND=postgres")
        return PostgresDatabase(
            settings.database_url,
            pool_min=settings.db_pool_min,
            pool_max=settings.db_pool_max,
            ssl=settings.db_ssl,
            statement_cache_size=settings.db_statement_cache_size,
            command_timeout=settings.db_command_timeout_seconds,
            connect_timeout=settings.db_connect_timeout_seconds,
        )

    raise ConfigError(f"Unsupported DATABASE_BACKEND: {settings.database_backend!r}")


def describe_database(settings: Settings) -> str:
    """Log-safe database description; never includes credentials.

    Phase 1 runtime support is the connection's default ``public`` schema. The
    label deliberately does not accept a schema override: custom schemas are
    not safe through the transaction-mode pooler.
    """

    if settings.database_backend == "postgres" and settings.database_url:
        return describe_dsn(settings.database_url)
    return str(settings.database_path)


__all__ = ["create_database", "describe_database"]
