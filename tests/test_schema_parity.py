"""Cross-backend schema parity checks (PostgreSQL vs the SQLite baseline).

SQLite's ``app/db/schema.py`` is the source of truth. PostgreSQL must mirror it
apart from deliberate Phase 2 additions:

* ``publishing_runs.insert_seq`` and its index, which replace SQLite's implicit
  ``rowid`` ordering.
* the PostgreSQL-only queue/worker tables (``publish_requests``,
  ``publishing_jobs``, ``publish_request_jobs``, ``worker_runs``,
  ``telegram_rate_limit``).
* ``publishing_history.unit_id`` and ``publishing_history.lease_token``.

Automated here:

* table set
* column names, integer/text type class, nullability
* column defaults (normalized, identity/autoincrement treated as no default)
* named index definitions (ordered columns plus ASC/DESC)
* foreign keys (source column, target table and column, ``ON DELETE`` action)
* primary keys (single and composite)
* unique constraints (column sets)
* CHECK constraints (canonical ``(column, kind, values)`` signatures)

Deliberately not automated: collation, expression/partial indexes, constraint
deferrability, and the exact textual spelling of CHECK expressions. Those still
need manual inspection if the DDL changes.

Requires ``TEST_DATABASE_URL``; skipped otherwise (via the ``pg_db`` fixture).
"""

from __future__ import annotations

import re

from app.db.database import Database
from app.db.postgres import PostgresDatabase

APP_TABLES = {
    "admins",
    "source_chats",
    "source_messages",
    "destinations",
    "publishing_queues",
    "publishing_runs",
    "publishing_history",
    "moderation_settings",
    "moderation_logs",
    "join_request_logs",
    "scheduler_state",
    "scheduled_runs",
}

PG_ONLY_TABLES = {
    "publish_requests",
    "publishing_jobs",
    "publish_request_jobs",
    "worker_runs",
    "telegram_rate_limit",
}
PG_ONLY_COLUMNS = {
    ("publishing_runs", "insert_seq"),
    ("publishing_history", "unit_id"),
    ("publishing_history", "lease_token"),
}
PG_ONLY_INDEXES = {"idx_publishing_runs_insert_seq"}

CheckSignature = tuple[str, str, tuple[str, ...]]
IndexColumns = tuple[tuple[str, bool], ...]


def _sqlite_type(declared: str) -> str:
    return "integer" if "INT" in declared.upper() else "text"


def _pg_type(data_type: str) -> str:
    return "integer" if data_type in {"bigint", "integer", "smallint"} else data_type


def _strip_casts(text: str) -> str:
    """Remove PostgreSQL ``::type`` suffixes from a constraint/default."""

    return re.sub(
        r"::[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)?(?:\[\])?",
        "",
        text,
    )


def _literal(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    return text.strip()


def _normalize_default(value: str | None) -> str | None:
    """Canonical default value; identity/autoincrement is represented as None."""

    if value is None:
        return None
    text = value.strip()
    if not text or text.upper() == "NULL":
        return None
    if text.lower().startswith("nextval("):
        return None
    text = _strip_casts(text).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return _literal(text)


def _extract_balanced(text: str, start: int) -> str | None:
    """Return the contents of the parenthesised span beginning at ``start``."""

    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return None


def _sqlite_check_expressions(ddl: str) -> list[str]:
    expressions: list[str] = []
    for match in re.finditer(r"\bCHECK\s*\(", ddl, re.IGNORECASE):
        expression = _extract_balanced(ddl, match.end() - 1)
        if expression is not None:
            expressions.append(expression)
    return expressions


def _check_signatures(expression: str) -> set[CheckSignature]:
    """Canonicalise one CHECK expression across SQLite and PostgreSQL.

    Membership checks become ``(column, "in", values)``; comparison checks stay
    ``(column, ">=", (value,))``. This tolerates the two spellings PostgreSQL
    uses for ``IN`` (``= ANY (ARRAY[...])`` and an ``OR`` chain) and the
    ``::type`` casts it adds.
    """

    text = re.sub(r"\s+", " ", _strip_casts(expression.lower()))

    def _in_list(match: re.Match[str]) -> str:
        column, body = match.group(1), match.group(2)
        return " or ".join(f"{column} = {item.strip()}" for item in body.split(","))

    # PostgreSQL: column = ANY (ARRAY[1, 2])
    text = re.sub(r"(\w+)\s*=\s*any\s*\(\s*array\[([^\]]*)\]", _in_list, text)
    # SQLite: column IN (1, 2)
    text = re.sub(r"(\w+)\s+in\s*\(([^)]*)\)", _in_list, text)

    signatures: set[CheckSignature] = set()
    for match in re.finditer(r"(\w+)\s*(>=|<=|>|<)\s*(\d+|'[^']*')", text):
        signatures.add((match.group(1), match.group(2), (_literal(match.group(3)),)))

    equalities: dict[str, list[str]] = {}
    for match in re.finditer(r"(\w+)\s*=\s*(\d+|'[^']*')", text):
        equalities.setdefault(match.group(1), []).append(_literal(match.group(2)))
    for column, values in equalities.items():
        if len(values) == 1:
            signatures.add((column, "=", (values[0],)))
        else:
            signatures.add((column, "in", tuple(sorted(values))))
    return signatures


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _pg_index_columns(indexdef: str) -> IndexColumns:
    """Parse ``CREATE INDEX ... USING btree (a, b DESC)`` into ordered columns."""

    using = indexdef.find(" USING ")
    if using == -1:
        return ()
    opening = indexdef.find("(", using)
    if opening == -1:
        return ()
    body = _extract_balanced(indexdef, opening)
    if body is None:
        return ()
    columns: list[tuple[str, bool]] = []
    for part in _split_top_level(body):
        item = part.strip()
        descending = bool(re.search(r"\bDESC\s*$", item, re.IGNORECASE))
        item = re.sub(r"\s+(?:ASC|DESC)\s*$", "", item, flags=re.IGNORECASE).strip()
        columns.append((_literal(item), descending))
    return tuple(columns)


async def _sqlite_indexes(db: Database, table: str) -> dict[str, IndexColumns]:
    indexes: dict[str, IndexColumns] = {}
    for index in await db.fetchall(f"PRAGMA index_list({table})"):
        name = str(index["name"])
        if not name.startswith("idx_"):
            continue
        columns: list[tuple[str, bool]] = []
        for info in await db.fetchall(f"PRAGMA index_xinfo({name})"):
            if not int(info["key"]):
                continue
            columns.append((str(info["name"]), bool(info["desc"])))
        indexes[name] = tuple(columns)
    return indexes


async def _pg_indexes(
    db: PostgresDatabase, schema: str, table: str
) -> dict[str, IndexColumns]:
    rows = await db.fetchall(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = ? AND tablename = ?",
        (schema, table),
    )
    return {
        str(row["indexname"]): _pg_index_columns(str(row["indexdef"]))
        for row in rows
        if str(row["indexname"]).startswith("idx_")
    }


async def _sqlite_checks(db: Database, table: str) -> set[CheckSignature]:
    row = await db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    ddl = str(row["sql"] or "") if row else ""
    signatures: set[CheckSignature] = set()
    for expression in _sqlite_check_expressions(ddl):
        signatures |= _check_signatures(expression)
    return signatures


async def _pg_checks(
    db: PostgresDatabase, schema: str, table: str
) -> set[CheckSignature]:
    rows = await db.fetchall(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid = to_regclass(?) AND contype = 'c'",
        (f'"{schema}"."{table}"',),
    )
    signatures: set[CheckSignature] = set()
    for row in rows:
        signatures |= _check_signatures(str(row["definition"]))
    return signatures


async def _pg_foreign_keys(
    db: PostgresDatabase, schema: str, table: str
) -> set[tuple[str, str, str, str]]:
    rows = await db.fetchall(
        "SELECT a.attname AS source_column, "
        "       target.relname AS target_table, "
        "       target_column.attname AS target_column, "
        "       CASE c.confdeltype "
        "           WHEN 'a' THEN 'NO ACTION' "
        "           WHEN 'r' THEN 'RESTRICT' "
        "           WHEN 'c' THEN 'CASCADE' "
        "           WHEN 'n' THEN 'SET NULL' "
        "           WHEN 'd' THEN 'SET DEFAULT' "
        "       END AS delete_rule "
        "FROM pg_constraint c "
        "JOIN pg_class source ON source.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = source.relnamespace "
        "JOIN pg_class target ON target.oid = c.confrelid "
        "CROSS JOIN LATERAL unnest(c.conkey, c.confkey) "
        "     WITH ORDINALITY AS keys(source_attnum, target_attnum, ordinality) "
        "JOIN pg_attribute a "
        "  ON a.attrelid = c.conrelid AND a.attnum = keys.source_attnum "
        "JOIN pg_attribute target_column "
        "  ON target_column.attrelid = c.confrelid "
        " AND target_column.attnum = keys.target_attnum "
        "WHERE n.nspname = ? AND source.relname = ? AND c.contype = 'f' "
        "ORDER BY c.conname, keys.ordinality",
        (schema, table),
    )
    return {
        (
            str(row["source_column"]),
            str(row["target_table"]),
            str(row["target_column"]),
            str(row["delete_rule"]),
        )
        for row in rows
    }


async def test_postgres_schema_matches_sqlite(pg_db: PostgresDatabase, tmp_path):
    schema_row = await pg_db.fetchone("SELECT current_schema() AS schema")
    schema = str(schema_row["schema"])

    sqlite = Database(tmp_path / "parity.db")
    await sqlite.connect()
    try:
        sqlite_tables = {
            str(row["name"])
            for row in await sqlite.fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not str(row["name"]).startswith("sqlite_")
        } - {"schema_migrations"}
        pg_tables = {
            str(row["table_name"])
            for row in await pg_db.fetchall(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
                (schema,),
            )
        } - {"schema_migrations"} - PG_ONLY_TABLES
        assert pg_tables == sqlite_tables == APP_TABLES

        for table in sorted(APP_TABLES):
            sqlite_rows = await sqlite.fetchall(f"PRAGMA table_info({table})")
            sqlite_columns = {
                str(row["name"]): {
                    "type": _sqlite_type(str(row["type"] or "")),
                    "not_null": bool(row["notnull"]) or bool(row["pk"]),
                    "pk": int(row["pk"]),
                }
                for row in sqlite_rows
            }
            sqlite_defaults = {
                str(row["name"]): _normalize_default(row["dflt_value"])
                for row in sqlite_rows
            }

            pg_rows = await pg_db.fetchall(
                "SELECT column_name, data_type, is_nullable, "
                "       column_default, is_identity "
                "FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ?",
                (schema, table),
            )
            pg_columns = {
                str(row["column_name"]): {
                    "type": _pg_type(str(row["data_type"])),
                    "not_null": row["is_nullable"] == "NO",
                }
                for row in pg_rows
                if (table, str(row["column_name"])) not in PG_ONLY_COLUMNS
            }
            sqlite_view = {
                name: {"type": info["type"], "not_null": info["not_null"]}
                for name, info in sqlite_columns.items()
            }
            assert pg_columns == sqlite_view, f"{table}: column mismatch"

            pg_defaults = {
                str(row["column_name"]): (
                    None
                    if row["is_identity"] == "YES"
                    else _normalize_default(row["column_default"])
                )
                for row in pg_rows
                if (table, str(row["column_name"])) not in PG_ONLY_COLUMNS
            }
            assert pg_defaults == sqlite_defaults, f"{table}: default mismatch"

            sqlite_indexes = await _sqlite_indexes(sqlite, table)
            pg_indexes = await _pg_indexes(pg_db, schema, table)
            assert (set(pg_indexes) - PG_ONLY_INDEXES) == set(sqlite_indexes), (
                f"{table}: index set mismatch"
            )
            for name, columns in sqlite_indexes.items():
                assert pg_indexes[name] == columns, (
                    f"{table}.{name}: index definition mismatch"
                )

            sqlite_fks = {
                (
                    str(row["from"]),
                    str(row["table"]),
                    str(row["to"]),
                    str(row["on_delete"]).upper(),
                )
                for row in await sqlite.fetchall(f"PRAGMA foreign_key_list({table})")
            }
            pg_fks = await _pg_foreign_keys(pg_db, schema, table)
            assert pg_fks == sqlite_fks, f"{table}: foreign key mismatch"

            sqlite_pk = tuple(
                name
                for name, info in sorted(
                    sqlite_columns.items(), key=lambda item: item[1]["pk"]
                )
                if info["pk"] > 0
            )
            pg_pk_rows = await pg_db.fetchall(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                "WHERE tc.table_schema = ? AND tc.table_name = ? "
                "  AND tc.constraint_type = 'PRIMARY KEY' "
                "ORDER BY kcu.ordinal_position",
                (schema, table),
            )
            pg_pk = tuple(str(row["column_name"]) for row in pg_pk_rows)
            assert pg_pk == sqlite_pk, f"{table}: primary key mismatch"

            sqlite_uniques: set[tuple[str, ...]] = set()
            for index in await sqlite.fetchall(f"PRAGMA index_list({table})"):
                if not int(index["unique"]) or str(index["origin"]) == "pk":
                    continue
                columns = tuple(
                    str(info["name"])
                    for info in await sqlite.fetchall(
                        f"PRAGMA index_info({index['name']})"
                    )
                )
                sqlite_uniques.add(columns)

            pg_unique_rows = await pg_db.fetchall(
                "SELECT tc.constraint_name, kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                "WHERE tc.table_schema = ? AND tc.table_name = ? "
                "  AND tc.constraint_type = 'UNIQUE' "
                "ORDER BY tc.constraint_name, kcu.ordinal_position",
                (schema, table),
            )
            grouped: dict[str, list[str]] = {}
            for row in pg_unique_rows:
                grouped.setdefault(str(row["constraint_name"]), []).append(
                    str(row["column_name"])
                )
            assert {tuple(columns) for columns in grouped.values()} == sqlite_uniques, (
                f"{table}: unique constraint mismatch"
            )

            sqlite_checks = await _sqlite_checks(sqlite, table)
            pg_checks = await _pg_checks(pg_db, schema, table)
            assert pg_checks == sqlite_checks, f"{table}: CHECK constraint mismatch"
    finally:
        await sqlite.close()


async def test_phase2_pg_manifest(pg_db: PostgresDatabase):
    """The PG-only Phase 2 schema must exist and expose its required columns."""

    schema_row = await pg_db.fetchone("SELECT current_schema() AS schema")
    schema = str(schema_row["schema"])
    rows = await pg_db.fetchall(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ANY(?)",
        (schema, sorted(PG_ONLY_TABLES)),
    )
    assert {str(row["table_name"]) for row in rows} == PG_ONLY_TABLES

    required = {
        "publish_requests": {
            "id",
            "request_type",
            "destination_id",
            "schedule_date",
            "slot",
            "dedupe_key",
            "status",
        },
        "publishing_jobs": {
            "id",
            "request_id",
            "destination_id",
            "trigger",
            "dedupe_key",
            "status",
            "lease_token",
            "lease_expires_at",
            "heartbeat_at",
            "send_started_at",
            "run_id",
            "outcome",
        },
        "publish_request_jobs": {"request_id", "job_id"},
        "worker_runs": {
            "id",
            "worker_id",
            "status",
            "month_key",
            "setup_seconds",
            "work_seconds",
        },
        "telegram_rate_limit": {
            "id",
            "next_allowed_at",
            "permit_token",
            "permit_expires_at",
        },
    }
    for table, columns in required.items():
        present = {
            str(row["column_name"])
            for row in await pg_db.fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ?",
                (schema, table),
            )
        }
        assert columns <= present, f"{table}: missing {columns - present}"

    history_columns = {
        str(row["column_name"])
        for row in await pg_db.fetchall(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = 'publishing_history'",
            (schema,),
        )
    }
    assert {"unit_id", "lease_token"} <= history_columns

    # uncertain_count is part of the baseline, not added by migration 002.
    uncertain = await pg_db.fetchone(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = 'publishing_runs' "
        "AND column_name = 'uncertain_count'",
        (schema,),
    )
    assert uncertain is not None

    indexes = {
        str(row["indexname"])
        for row in await pg_db.fetchall(
            "SELECT indexname FROM pg_indexes WHERE schemaname = ?", (schema,)
        )
    }
    assert {
        "uq_publishing_jobs_running_destination",
        "uq_publishing_jobs_active_manual_destination",
        "idx_publishing_jobs_claim",
        "idx_publishing_jobs_lease",
        "idx_worker_runs_month",
    } <= indexes
