"""Unit tests for the SQLite -> PostgreSQL rewrite layer (no database needed)."""

from __future__ import annotations

from app.db.sql_dialect import add_returning_id, to_postgres


def test_placeholders_are_numbered_in_order():
    sql = "INSERT INTO admins(telegram_id, role) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE"
    assert to_postgres(sql).endswith("VALUES ($1, $2) ON CONFLICT(telegram_id) DO UPDATE")


def test_placeholders_inside_string_literals_and_comments_are_untouched():
    sql = "SELECT '?', \"weird?column\" FROM admins -- trailing ? comment\nWHERE 1 = ?"
    translated = to_postgres(sql)
    assert "'?'" in translated
    assert '"weird?column"' in translated
    assert "-- trailing ? comment" in translated
    assert translated.endswith("WHERE 1 = $1")


def test_escaped_quotes_do_not_break_scanning():
    sql = "SELECT 'it''s ? fine' AS value WHERE telegram_id = ?"
    translated = to_postgres(sql)
    assert "'it''s ? fine'" in translated
    assert translated.endswith("= $1")


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    sql = """
    INSERT OR IGNORE INTO source_messages(chat_id, message_id) VALUES (?, ?)
    """
    translated = to_postgres(sql)
    assert "INSERT INTO source_messages" in translated
    assert "OR IGNORE" not in translated
    assert translated.rstrip().endswith("ON CONFLICT DO NOTHING")
    assert translated.count("ON CONFLICT") == 1


def test_insert_or_ignore_keeps_existing_conflict_clause():
    sql = (
        "INSERT OR IGNORE INTO source_messages(chat_id) VALUES (?) "
        "ON CONFLICT(chat_id) DO NOTHING"
    )
    translated = to_postgres(sql)
    assert translated.lower().count("on conflict") == 1


def test_scalar_max_becomes_greatest_and_aggregate_is_kept():
    scalar = "UPDATE publishing_queues SET next_sequence = MAX(next_sequence, ?)"
    assert "GREATEST(next_sequence, $1)" in to_postgres(scalar)

    nested = """
    UPDATE publishing_queues SET
        last_success_sequence = MAX(COALESCE(last_success_sequence, 0), ?)
    """
    assert "GREATEST(COALESCE(last_success_sequence, 0), $1)" in to_postgres(nested)

    aggregate = "SELECT COALESCE(MAX(id), 0) AS max_id FROM source_messages"
    assert "MAX(id)" in to_postgres(aggregate)
    assert "GREATEST" not in to_postgres(aggregate)


def test_scalar_min_becomes_least():
    assert "LEAST(a, b)" in to_postgres("SELECT MIN(a, b) FROM t")


def test_rowid_becomes_insert_seq():
    sql = "SELECT r.* FROM publishing_runs r ORDER BY r.started_at DESC, r.rowid DESC"
    translated = to_postgres(sql)
    assert "r.insert_seq DESC" in translated
    assert "rowid" not in translated


def test_returning_id_is_added_only_for_identity_tables():
    sql, returns_id = add_returning_id(
        to_postgres("INSERT INTO join_request_logs(chat_id) VALUES (?)")
    )
    assert returns_id is True
    assert sql.rstrip().endswith("RETURNING id")

    sql, returns_id = add_returning_id(to_postgres("INSERT INTO scheduler_state(key) VALUES (?)"))
    assert returns_id is False
    assert "RETURNING" not in sql


def test_returning_id_respects_existing_returning_and_semicolons():
    sql, returns_id = add_returning_id("INSERT INTO admins(telegram_id) VALUES ($1) RETURNING id")
    assert returns_id is True
    assert sql.count("RETURNING") == 1

    sql, returns_id = add_returning_id("INSERT INTO admins(telegram_id) VALUES ($1);")
    assert returns_id is True
    assert sql.endswith("RETURNING id;")

    sql, returns_id = add_returning_id("SELECT 1")
    assert returns_id is False


def test_rowid_of_unrelated_table_is_not_rewritten():
    sql = "SELECT rowid FROM unrelated_table ORDER BY rowid"
    assert to_postgres(sql) == sql


def test_other_tables_rowid_is_not_rewritten_when_publishing_runs_is_joined():
    sql = (
        "SELECT d.rowid AS destination_rowid, r.rowid AS run_rowid "
        "FROM publishing_runs r JOIN destinations d ON d.id = r.destination_id "
        "ORDER BY r.rowid DESC"
    )
    translated = to_postgres(sql)
    assert "d.rowid AS destination_rowid" in translated
    assert "r.insert_seq AS run_rowid" in translated
    assert "ORDER BY r.insert_seq DESC" in translated
    assert "d.insert_seq" not in translated
    assert translated.count(".rowid") == 1


def test_full_table_qualified_rowid_is_rewritten():
    sql = (
        "SELECT publishing_runs.rowid "
        "FROM public.publishing_runs ORDER BY publishing_runs.rowid"
    )
    translated = to_postgres(sql)
    assert "publishing_runs.insert_seq" in translated
    assert "rowid" not in translated


def test_unqualified_rowid_with_multiple_tables_is_left_alone():
    sql = (
        "SELECT rowid FROM publishing_runs r "
        "JOIN destinations d ON d.id = r.destination_id"
    )
    assert to_postgres(sql) == sql


def test_rowid_inside_literals_identifiers_and_comments_is_untouched():
    sql = (
        "SELECT 'rowid' AS literal, \"rowid\" AS quoted, rowid AS real -- rowid\n"
        "FROM publishing_runs /* rowid */ ORDER BY rowid"
    )
    translated = to_postgres(sql)
    assert "'rowid'" in translated
    assert '"rowid"' in translated
    assert "-- rowid" in translated
    assert "/* rowid */" in translated
    assert "insert_seq AS real" in translated
    assert "ORDER BY insert_seq" in translated
    assert translated.count("rowid") == 4


def test_insert_or_ignore_inside_literal_and_comment_is_untouched():
    sql = "SELECT 'INSERT OR IGNORE INTO t' AS literal -- INSERT OR IGNORE INTO t\n"
    assert to_postgres(sql) == sql


def test_insert_or_ignore_with_interleaved_comment_is_left_unchanged():
    sql = "INSERT /* keep me */ OR IGNORE INTO source_messages(chat_id) VALUES (1)"
    assert to_postgres(sql) == sql


def test_scalar_max_min_inside_literal_identifier_and_comment_is_untouched():
    sql = (
        "SELECT 'MAX(a, b)' AS literal, \"MIN(a, b)\" AS quoted -- MAX(a, b)\n"
        "/* MIN(a, b) */ FROM t"
    )
    assert to_postgres(sql) == sql


def test_returning_inside_literal_or_comment_does_not_count():
    sql = "INSERT INTO admins(telegram_id, label) VALUES ($1, 'RETURNING id') -- RETURNING id"
    translated, returns_id = add_returning_id(sql)
    assert returns_id is True
    assert "') RETURNING id -- RETURNING id" in translated
    assert translated.count("RETURNING") == 3

    real = "INSERT INTO admins(telegram_id) VALUES ($1) RETURNING id -- RETURNING id"
    unchanged, returns_id = add_returning_id(real)
    assert returns_id is True
    assert unchanged == real
