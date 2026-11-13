"""Explicit PostgreSQL migration runner.

Usage::

    python -m app.db.migrate                    # uses DATABASE_URL
    python -m app.db.migrate --dry-run
    python -m app.db.migrate --schema bot_dev --create-schema

The application never calls this. ``PostgresDatabase.connect`` only opens a
connection pool, so schema changes require this deliberate step.

Safety properties:

* The whole run holds ``pg_advisory_xact_lock`` so two concurrent runners
  serialise instead of racing on ``schema_migrations``.
* Every applied migration records a SHA-256 checksum of its file.
* An applied version whose file changed, or whose version has no file at all,
  aborts the run before any DDL executes.
* ``--schema`` is intended for disposable test schemas and direct/session
  connections. The hosted transaction pooler does not support a stable
  ``search_path``; production runs target ``public``.
* A ``--dry-run`` against a missing ``--schema`` (with ``--create-schema``)
  creates nothing, never falls back to another schema, and reports every
  migration as pending. After ``SET search_path`` the runner verifies that
  ``current_schema()`` is exactly the requested schema.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import asyncpg

from app.db.dsn import describe_dsn, dsn_has_sslmode, normalize_dsn

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations_pg")

# Stable application-wide key so every runner competes for the same lock.
MIGRATION_LOCK_KEY: Final[int] = int.from_bytes(b"tgmigrat", "big")

_FILENAME = re.compile(r"^(?P<version>\d+)_[A-Za-z0-9_\-]+\.sql$")
_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version BIGINT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT
)
"""

# Phase 1 briefly shipped a runner without checksums; upgrade in place.
_MIGRATION_TABLE_UPGRADE = """
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT
"""


@dataclass(frozen=True, slots=True)
class MigrationFile:
    version: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[MigrationFile]:
    """Return migration files ordered by version. Fails on duplicates."""

    found: dict[int, MigrationFile] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise RuntimeError(f"Invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in found:
            raise RuntimeError(f"Duplicate migration version {version}: {path.name}")
        found[version] = MigrationFile(version=version, path=path)
    return [found[key] for key in sorted(found)]


async def apply_migrations(
    dsn: str,
    *,
    schema: str | None = None,
    create_schema: bool = False,
    dry_run: bool = False,
    ssl: str = "require",
    directory: Path = MIGRATIONS_DIR,
) -> list[int]:
    """Apply pending migrations and return their versions."""

    if not dsn or not dsn.strip():
        raise ValueError("DATABASE_URL is required to run migrations")
    if schema is not None and not _SCHEMA_NAME.match(schema):
        raise ValueError(f"Invalid schema name: {schema!r}")

    migrations = discover_migrations(directory)
    checksums = {migration.version: _checksum(migration.path) for migration in migrations}
    normalized = normalize_dsn(dsn)
    kwargs: dict[str, object] = {"statement_cache_size": 0, "timeout": 15.0}
    if not dsn_has_sslmode(normalized):
        kwargs["ssl"] = ssl

    connection = await asyncpg.connect(normalized, **kwargs)
    applied: list[int] = []
    try:
        # One transaction covers the lock, verification and DDL. The advisory
        # xact lock is released automatically on commit/rollback.
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK_KEY)

            if schema is not None:
                exists = await connection.fetchval(
                    "SELECT 1 FROM pg_namespace WHERE nspname = $1", schema
                )
                if not exists:
                    if not create_schema:
                        raise ValueError(
                            f"Schema {schema!r} does not exist; pass --create-schema"
                        )
                    if dry_run:
                        # A dry run must not create the target. Do not SET
                        # search_path either: if the schema is missing, a
                        # fallback could inspect a different schema. Report the
                        # whole migration set as pending instead.
                        pending = [item.version for item in migrations]
                        logger.info(
                            "Target %s schema=%s does not exist; %s pending "
                            "(dry run, nothing written)",
                            describe_dsn(normalized),
                            schema,
                            len(pending),
                        )
                        for migration in migrations:
                            logger.info("[dry-run] would apply %s", migration.name)
                        return pending
                    await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

                await connection.execute(f'SET search_path TO "{schema}"')
                active = await connection.fetchval("SELECT current_schema()")
                if active != schema:
                    raise RuntimeError(
                        f"search_path resolved to {active!r} instead of {schema!r}; "
                        "refusing to inspect another schema"
                    )

            if dry_run:
                current = await _read_current(connection, create_missing=False)
                _verify_history(current, migrations, checksums)
                pending = [item for item in migrations if item.version not in current]
                logger.info(
                    "Target %s: %s applied, %s pending (dry run, nothing written)",
                    describe_dsn(normalized),
                    len(current),
                    len(pending),
                )
                for migration in pending:
                    logger.info("[dry-run] would apply %s", migration.name)
                return [item.version for item in pending]

            await connection.execute(_MIGRATION_TABLE)
            await connection.execute(_MIGRATION_TABLE_UPGRADE)
            current = await _read_current(connection, create_missing=True)
            _verify_history(current, migrations, checksums)
            pending = [item for item in migrations if item.version not in current]

            logger.info(
                "Target %s: %s applied, %s pending",
                describe_dsn(normalized),
                len(current),
                len(pending),
            )

            for migration in pending:
                sql = migration.path.read_text(encoding="utf-8")
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at, checksum) "
                    "VALUES ($1, $2, $3)",
                    migration.version,
                    _now_iso(),
                    checksums[migration.version],
                )
                logger.info("Applied %s", migration.name)
                applied.append(migration.version)
    finally:
        await connection.close()

    return applied


async def _read_current(
    connection: asyncpg.Connection, *, create_missing: bool
) -> dict[int, str | None]:
    """Read applied versions and checksums, tolerating pre-checksum tables."""

    if not create_missing:
        has_table = await connection.fetchval(
            "SELECT to_regclass('schema_migrations') IS NOT NULL"
        )
        if not has_table:
            return {}

    has_checksum = await connection.fetchval(
        "SELECT EXISTS ("
        " SELECT 1 FROM information_schema.columns"
        " WHERE table_schema = current_schema()"
        "   AND table_name = 'schema_migrations'"
        "   AND column_name = 'checksum'"
        ")"
    )
    if has_checksum:
        rows = await connection.fetch("SELECT version, checksum FROM schema_migrations")
    else:
        rows = await connection.fetch(
            "SELECT version, NULL::text AS checksum FROM schema_migrations"
        )
    return {int(row["version"]): row["checksum"] for row in rows}


def _verify_history(
    applied: dict[int, str | None],
    migrations: list[MigrationFile],
    checksums: dict[int, str],
) -> None:
    """Abort before DDL if recorded history disagrees with the files."""

    known = {migration.version for migration in migrations}
    unexpected = sorted(set(applied) - known)
    if unexpected:
        raise RuntimeError(
            "schema_migrations contains version(s) with no migration file: "
            + ", ".join(str(version) for version in unexpected)
            + ". Restore the missing file(s) or repair the database; refusing to continue."
        )

    for migration in migrations:
        if migration.version not in applied:
            continue
        stored = applied[migration.version]
        if stored is None:
            raise RuntimeError(
                f"Migration {migration.name} is recorded without a checksum. "
                "It was applied by an older runner; verify it manually before continuing."
            )
        if stored != checksums[migration.version]:
            raise RuntimeError(
                f"Applied migration {migration.name} has been modified "
                f"(stored SHA-256 {stored}, file SHA-256 {checksums[migration.version]}). "
                "Never edit an applied migration; add a new numbered migration instead."
            )


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply PostgreSQL migrations explicitly.")
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="defaults to DATABASE_URL")
    parser.add_argument(
        "--schema",
        default=None,
        help="test/direct-connection schema override; production uses public",
    )
    parser.add_argument("--create-schema", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ssl", default=os.getenv("DB_SSL") or "require")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    if not args.dsn:
        print("DATABASE_URL is not set; pass --dsn or export DATABASE_URL.", file=sys.stderr)
        return 2
    try:
        applied = asyncio.run(
            apply_migrations(
                args.dsn,
                schema=args.schema,
                create_schema=args.create_schema,
                dry_run=args.dry_run,
                ssl=args.ssl,
            )
        )
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1

    if not applied:
        print("Database is up to date.")
    else:
        verb = "Pending" if args.dry_run else "Applied"
        print(f"{verb} migrations: {', '.join(str(version) for version in applied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
