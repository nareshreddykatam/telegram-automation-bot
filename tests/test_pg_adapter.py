"""PostgreSQL integration tests.

Skipped unless ``TEST_DATABASE_URL`` is set. Each test runs in a disposable
schema, so the target database may be shared.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.constants import MSG_SENT, RUN_COMPLETED, SCHED_COMPLETED
from app.db.dsn import normalize_dsn
from app.db.migrate import apply_migrations
from app.db.postgres import PostgresDatabase
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import join_requests as join_requests_repo
from app.db.repositories import messages as messages_repo
from app.db.repositories import publishing as publishing_repo
from app.db.repositories import scheduler as scheduler_repo
from app.db.repositories import sources as sources_repo
from app.services.publishing import PublishingService
from tests.helpers import FakeSender, create_destination, create_source
from tests.pg_utils import connect_kwargs, require_dsn, ssl_mode, temporary_schema

EXPECTED_TABLES = {
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
    "publish_requests",
    "publishing_jobs",
    "publish_request_jobs",
    "worker_runs",
    "telegram_rate_limit",
    "schema_migrations",
}


async def _tables(db: PostgresDatabase) -> set[str]:
    rows = await db.fetchall(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )
    return {str(row["table_name"]) for row in rows}


async def test_connect_does_not_run_migrations():
    async with temporary_schema() as (dsn, schema):
        database = PostgresDatabase(dsn, schema=schema, ssl=ssl_mode(), statement_cache_size=0)
        await database.connect()
        try:
            assert await _tables(database) == set()
        finally:
            await database.close()


async def test_dry_run_reports_pending_without_writing():
    async with temporary_schema() as (dsn, schema):
        pending = await apply_migrations(dsn, schema=schema, ssl=ssl_mode(), dry_run=True)
        assert pending == [1, 2]

        database = PostgresDatabase(dsn, schema=schema, ssl=ssl_mode(), statement_cache_size=0)
        await database.connect()
        try:
            assert await _tables(database) == set()
        finally:
            await database.close()


async def test_migrations_create_schema_and_are_idempotent(pg_db):
    database = pg_db
    tables = await _tables(database)
    assert EXPECTED_TABLES <= tables

    version = await database.fetchone("SELECT MAX(version) AS version FROM schema_migrations")
    assert version["version"] == 2

    current = await database.fetchone("SELECT current_schema() AS schema")
    applied_again = await apply_migrations(
        require_dsn(), schema=str(current["schema"]), ssl=ssl_mode()
    )
    assert applied_again == []


async def test_message_indexing_is_idempotent(pg_db):
    database = pg_db
    source = await sources_repo.add_source(database, chat_id=-1001, title="Source", created_by=1)
    first, created = await messages_repo.index_message(
        database,
        source_chat_id=source.id,
        chat_id=-1001,
        message_id=55,
        media_group_id=None,
        content_type="text",
        has_media=False,
        caption=None,
        text_content="hello",
        message_date=None,
    )
    second, created_again = await messages_repo.index_message(
        database,
        source_chat_id=source.id,
        chat_id=-1001,
        message_id=55,
        media_group_id=None,
        content_type="text",
        has_media=False,
        caption=None,
        text_content="hello again",
        message_date=None,
    )
    assert created is True
    assert created_again is False
    assert first.id == second.id
    count = await database.fetchone("SELECT COUNT(*) AS count FROM source_messages")
    assert count["count"] == 1


async def test_transaction_rolls_back(pg_db):
    database = pg_db
    with pytest.raises(RuntimeError):
        async with database.transaction():
            await database.execute(
                "INSERT INTO source_chats("
                "chat_id, title, enabled, auto_approve, created_at, created_by"
                ") VALUES (?, 'Rollback', 1, 1, 'now', 1)",
                (-500,),
            )
            raise RuntimeError("force rollback")
    assert await database.fetchone("SELECT * FROM source_chats WHERE chat_id = ?", (-500,)) is None


async def test_nested_transaction_reuses_outer_transaction(pg_db):
    database = pg_db
    with pytest.raises(RuntimeError):
        async with database.transaction():
            await database.execute(
                "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
                "VALUES (?, 1, 1, 'now')",
                (-601,),
            )
            async with database.transaction():
                await database.execute(
                    "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
                    "VALUES (?, 1, 1, 'now')",
                    (-602,),
                )
            raise RuntimeError("rollback both")

    rows = await database.fetchall("SELECT chat_id FROM source_chats")
    assert rows == []


async def test_concurrent_transactions_use_separate_connections(pg_db):
    database = pg_db

    async def write(chat_id: int) -> None:
        async with database.transaction():
            await database.execute(
                "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
                "VALUES (?, 1, 1, 'now')",
                (chat_id,),
            )
            await asyncio.sleep(0.05)

    await asyncio.gather(write(-701), write(-702), write(-703))
    count = await database.fetchone("SELECT COUNT(*) AS count FROM source_chats")
    assert count["count"] == 3


async def test_child_task_inheriting_transaction_context_raises(pg_db):
    database = pg_db

    async with database.transaction():
        await database.execute(
            "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
            "VALUES (?, 1, 1, 'now')",
            (-900,),
        )

        async def child() -> None:
            # The ContextVar is inherited. The parent's connection must not be
            # used, and silently acquiring a second one could deadlock a small
            # pool, so the child fails fast instead.
            await database.execute(
                "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
                "VALUES (?, 1, 1, 'now')",
                (-901,),
            )

        with pytest.raises(RuntimeError, match="task-local"):
            await asyncio.create_task(child())

    # The parent transaction is not poisoned and commits its own row only.
    rows = await database.fetchall("SELECT chat_id FROM source_chats ORDER BY chat_id")
    assert [row["chat_id"] for row in rows] == [-900]


async def test_inherited_transaction_context_fails_after_parent_closes(pg_db):
    database = pg_db
    proceed = asyncio.Event()

    async def child() -> None:
        # The task is created inside the transaction, so its copied context
        # still records the parent as the connection owner. Waiting here proves
        # the failure is not merely "parent still open".
        await proceed.wait()
        await database.execute(
            "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
            "VALUES (?, 1, 1, 'now')",
            (-910,),
        )

    async with database.transaction():
        await database.execute(
            "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
            "VALUES (?, 1, 1, 'now')",
            (-909,),
        )
        task = asyncio.create_task(child())

    # The parent transaction has now committed and released its connection. The
    # child's inherited context is stale but still present, so awaiting it must
    # fail fast instead of reusing the connection or acquiring (and blocking on)
    # another one.
    proceed.set()
    with pytest.raises(RuntimeError, match="task-local"):
        await task

    rows = await database.fetchall("SELECT chat_id FROM source_chats ORDER BY chat_id")
    assert [row["chat_id"] for row in rows] == [-909]

    # The connection was returned to the pool and remains usable.
    async with database.transaction():
        await database.execute(
            "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
            "VALUES (?, 1, 1, 'now')",
            (-911,),
        )


async def test_child_task_raises_instead_of_deadlocking_pool_max_one():
    async with temporary_schema() as (dsn, schema):
        await apply_migrations(dsn, schema=schema, ssl=ssl_mode())
        database = PostgresDatabase(
            dsn,
            schema=schema,
            pool_min=0,
            pool_max=1,
            ssl=ssl_mode(),
            statement_cache_size=0,
        )
        await database.connect()
        try:
            async with database.transaction():
                await database.execute(
                    "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
                    "VALUES (?, 1, 1, 'now')",
                    (-920,),
                )

                async def child() -> str:
                    try:
                        await database.execute(
                            "INSERT INTO source_chats("
                            "chat_id, enabled, auto_approve, created_at"
                            ") VALUES (?, 1, 1, 'now')",
                            (-921,),
                        )
                    except RuntimeError as exc:
                        return str(exc)
                    return "no error"

                # A 5 second cap proves the child fails fast instead of waiting
                # forever for the connection its parent still owns.
                message = await asyncio.wait_for(asyncio.create_task(child()), timeout=5)
                assert "task-local" in message

                # The parent transaction is still usable after the child error.
                assert (
                    await database.fetchone(
                        "SELECT chat_id FROM source_chats WHERE chat_id = ?", (-920,)
                    )
                    is not None
                )

            rows = await database.fetchall(
                "SELECT chat_id FROM source_chats ORDER BY chat_id"
            )
            assert [row["chat_id"] for row in rows] == [-920]

            # The pool connection was returned: another transaction works.
            async with database.transaction():
                await database.execute(
                    "INSERT INTO source_chats(chat_id, enabled, auto_approve, created_at) "
                    "VALUES (?, 1, 1, 'now')",
                    (-922,),
                )
        finally:
            await database.close()


async def test_child_task_transaction_raises_with_pool_max_one():
    async with temporary_schema() as (dsn, schema):
        await apply_migrations(dsn, schema=schema, ssl=ssl_mode())
        database = PostgresDatabase(
            dsn,
            schema=schema,
            pool_min=0,
            pool_max=1,
            ssl=ssl_mode(),
            statement_cache_size=0,
        )
        await database.connect()
        try:
            async with database.transaction():

                async def child() -> str:
                    try:
                        async with database.transaction():
                            pass
                    except RuntimeError as exc:
                        return str(exc)
                    return "no error"

                message = await asyncio.wait_for(asyncio.create_task(child()), timeout=5)
                assert "task-local" in message
        finally:
            await database.close()


async def test_pool_max_one_runs_sequential_transactions():
    async with temporary_schema() as (dsn, schema):
        await apply_migrations(dsn, schema=schema, ssl=ssl_mode())
        database = PostgresDatabase(
            dsn,
            schema=schema,
            pool_min=0,
            pool_max=1,
            ssl=ssl_mode(),
            statement_cache_size=0,
        )
        await database.connect()
        try:
            for chat_id in (-930, -931):
                async with database.transaction():
                    await database.execute(
                        "INSERT INTO source_chats("
                        "chat_id, enabled, auto_approve, created_at"
                        ") VALUES (?, 1, 1, 'now')",
                        (chat_id,),
                    )
            count = await database.fetchone("SELECT COUNT(*) AS count FROM source_chats")
            assert count["count"] == 2
        finally:
            await database.close()


async def test_scalar_max_does_not_rewind_the_cursor(pg_db):
    database = pg_db
    destination = await create_destination(database)
    await database.execute(
        "UPDATE publishing_queues SET next_sequence = 5 WHERE destination_id = ?",
        (destination.id,),
    )
    await publishing_repo.advance_past(database, destination_id=destination.id, source_sequence=2)
    queue = await destinations_repo.get_queue(database, destination.id)
    assert queue.next_sequence == 5

    await publishing_repo.advance_past(database, destination_id=destination.id, source_sequence=9)
    queue = await destinations_repo.get_queue(database, destination.id)
    assert queue.next_sequence == 10


async def test_lastrowid_and_rowcount_semantics(pg_db):
    database = pg_db
    first = await join_requests_repo.log_request(
        database,
        chat_id=-800,
        user_id=1,
        username="u",
        first_name="U",
        chat_title="T",
        action="approved",
    )
    second = await join_requests_repo.log_request(
        database,
        chat_id=-800,
        user_id=2,
        username="v",
        first_name="V",
        chat_title="T",
        action="approved",
    )
    assert isinstance(first, int)
    assert second > first

    assert await destinations_repo.set_batch_size(database, -999, 10) is False
    destination = await create_destination(database)
    assert await destinations_repo.set_batch_size(database, destination.chat_id, 25) is True


async def test_boolean_columns_round_trip(pg_db):
    database = pg_db
    destination, _ = await destinations_repo.add_destination(
        database,
        chat_id=-810,
        title="Dest",
        batch_size=50,
        start_sequence=1,
        created_by=1,
        auto_approve=False,
    )
    assert destination.auto_approve is False
    assert destination.enabled is True

    assert await destinations_repo.set_paused(database, -810, True) is True
    refreshed = await destinations_repo.get_by_chat_id(database, -810)
    assert refreshed.paused is True

    enabled = await destinations_repo.list_destinations(database, enabled_only=True)
    assert [item.id for item in enabled] == [destination.id]


async def test_scheduler_state_and_claims(pg_db):
    database = pg_db
    destination = await create_destination(database)

    await scheduler_repo.set_state(database, "last_cycle_date", "2026-01-01")
    await scheduler_repo.set_state(database, "last_cycle_date", "2026-01-02")
    assert await scheduler_repo.get_last_cycle_date(database) == "2026-01-02"

    claim = await scheduler_repo.claim_scheduled_run(
        database, schedule_date="2026-01-02", destination_id=destination.id, trigger="scheduled"
    )
    assert claim is True
    await scheduler_repo.finish_scheduled_run(
        database,
        schedule_date="2026-01-02",
        destination_id=destination.id,
        run_id="run-1",
        success=True,
    )
    assert (
        await scheduler_repo.get_scheduled_run(database, "2026-01-02", destination.id)
        == SCHED_COMPLETED
    )
    assert (
        await scheduler_repo.claim_scheduled_run(
            database,
            schedule_date="2026-01-02",
            destination_id=destination.id,
            trigger="scheduled",
        )
        is False
    )


async def test_publishing_end_to_end(pg_db, settings):
    database = pg_db
    await create_source(database, count=5)
    destination = await create_destination(database, batch_size=10)
    sender = FakeSender()
    service = PublishingService(database, sender, settings)

    result = await service.publish_destination(destination.id)
    assert result.status == RUN_COMPLETED
    assert result.success_count == 5
    assert [item[2] for item in sender.sent] == [1, 2, 3, 4, 5]

    queue = await destinations_repo.get_queue(database, destination.id)
    assert queue.next_sequence == 6

    second = await service.publish_destination(destination.id)
    assert second.success_count == 0
    assert second.skipped_count == 0
    assert len(sender.sent) == 5

    sent = await database.fetchone(
        "SELECT COUNT(*) AS count FROM publishing_history WHERE status = ?", (MSG_SENT,)
    )
    assert sent["count"] == 5


async def test_recovery_marks_in_flight_rows_uncertain(pg_db):
    database = pg_db
    await create_source(database, count=3)
    destination = await create_destination(database, batch_size=10)
    await publishing_repo.create_run(
        database,
        run_id="crashed-run",
        destination_id=destination.id,
        trigger="manual",
        status="running",
        planned_count=3,
    )
    candidates = await publishing_repo.list_candidates(
        database, destination_id=destination.id, next_sequence=1, limit=1
    )
    outcome, history_id = await publishing_repo.reserve_for_send(
        database, destination_id=destination.id, source=candidates[0].source, run_id="crashed-run"
    )
    assert outcome == "booked"

    summary = await publishing_repo.recover_interrupted(database)
    assert summary.uncertain_messages == 1
    assert summary.aborted_runs == 1
    row = await database.fetchone(
        "SELECT status FROM publishing_history WHERE id = ?", (history_id,)
    )
    assert row["status"] == "uncertain"
    queue = await destinations_repo.get_queue(database, destination.id)
    assert queue.next_sequence == 2


async def test_list_runs_orders_by_insert_sequence(pg_db):
    database = pg_db
    destination = await create_destination(database)
    for run_id in ("run-a", "run-b", "run-c"):
        await publishing_repo.create_run(
            database,
            run_id=run_id,
            destination_id=destination.id,
            trigger="manual",
            status="running",
            planned_count=0,
        )
    runs = await publishing_repo.list_runs(database, destination_id=destination.id, limit=3)
    assert [run.id for run in runs] == ["run-c", "run-b", "run-a"]


async def test_concurrent_migration_runners_serialise():
    async with temporary_schema() as (dsn, schema):
        first, second = await asyncio.gather(
            apply_migrations(dsn, schema=schema, ssl=ssl_mode()),
            apply_migrations(dsn, schema=schema, ssl=ssl_mode()),
        )
        assert sorted((len(first), len(second))) == [0, 2]

        asyncpg = pytest.importorskip("asyncpg")
        connection = await asyncpg.connect(normalize_dsn(dsn), **connect_kwargs(dsn))
        try:
            await connection.execute(f'SET search_path TO "{schema}"')
            count = await connection.fetchval("SELECT COUNT(*) FROM schema_migrations")
            checksum = await connection.fetchval(
                "SELECT checksum FROM schema_migrations WHERE version = 1"
            )
        finally:
            await connection.close()
        assert count == 2
        assert isinstance(checksum, str) and len(checksum) == 64


async def test_modified_applied_migration_is_detected(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    migration = directory / "001_demo.sql"
    migration.write_text("CREATE TABLE IF NOT EXISTS demo(id BIGINT);", encoding="utf-8")

    async with temporary_schema() as (dsn, schema):
        applied = await apply_migrations(dsn, schema=schema, ssl=ssl_mode(), directory=directory)
        assert applied == [1]

        migration.write_text(
            "CREATE TABLE IF NOT EXISTS demo(id BIGINT, note TEXT);", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="has been modified"):
            await apply_migrations(dsn, schema=schema, ssl=ssl_mode(), directory=directory)


async def test_unexpected_applied_version_is_detected():
    asyncpg = pytest.importorskip("asyncpg")

    async with temporary_schema() as (dsn, schema):
        await apply_migrations(dsn, schema=schema, ssl=ssl_mode())

        connection = await asyncpg.connect(normalize_dsn(dsn), **connect_kwargs(dsn))
        try:
            await connection.execute(f'SET search_path TO "{schema}"')
            await connection.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (99, 'now', 'not-a-real-checksum')"
            )
        finally:
            await connection.close()

        with pytest.raises(RuntimeError, match="no migration file"):
            await apply_migrations(dsn, schema=schema, ssl=ssl_mode())


async def test_dry_run_missing_schema_with_create_reports_all_pending():
    dsn = require_dsn()
    missing = f"missing_{uuid.uuid4().hex[:12]}"

    pending = await apply_migrations(
        dsn, schema=missing, create_schema=True, dry_run=True, ssl=ssl_mode()
    )
    assert pending == [1, 2]

    asyncpg = pytest.importorskip("asyncpg")
    connection = await asyncpg.connect(normalize_dsn(dsn), **connect_kwargs(dsn))
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_namespace WHERE nspname = $1", missing
        )
    finally:
        await connection.close()
    # A dry run must not create the target or inspect a fallback schema.
    assert exists is None


async def test_dry_run_missing_schema_without_create_fails():
    dsn = require_dsn()
    missing = f"missing_{uuid.uuid4().hex[:12]}"

    with pytest.raises(ValueError, match="does not exist"):
        await apply_migrations(dsn, schema=missing, dry_run=True, ssl=ssl_mode())


async def test_apply_with_create_schema_creates_missing_target():
    dsn = require_dsn()
    missing = f"create_{uuid.uuid4().hex[:12]}"

    asyncpg = pytest.importorskip("asyncpg")
    connection = await asyncpg.connect(normalize_dsn(dsn), **connect_kwargs(dsn))
    try:
        applied = await apply_migrations(
            dsn, schema=missing, create_schema=True, ssl=ssl_mode()
        )
        assert applied == [1, 2]
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_namespace WHERE nspname = $1", missing
        )
        assert exists == 1
        rows = await connection.fetchval(f'SELECT COUNT(*) FROM "{missing}".schema_migrations')
        assert rows == 2
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{missing}" CASCADE')
        await connection.close()
