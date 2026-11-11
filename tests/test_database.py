from __future__ import annotations

import pytest

from app.db.database import Database
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import messages as messages_repo
from app.db.repositories import sources as sources_repo


@pytest.mark.asyncio
async def test_schema_and_migrations(db: Database):
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    names = {row["name"] for row in rows}
    expected = {
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
        "schema_migrations",
    }
    assert expected <= names
    version = await db.fetchone("SELECT MAX(version) AS version FROM schema_migrations")
    assert version["version"] == 1


@pytest.mark.asyncio
async def test_message_indexing_is_idempotent(db: Database):
    source = await sources_repo.add_source(db, chat_id=-1001, title="Source", created_by=1)
    first, created = await messages_repo.index_message(
        db,
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
        db,
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
    count = await db.fetchone("SELECT COUNT(*) AS count FROM source_messages")
    assert count["count"] == 1


@pytest.mark.asyncio
async def test_transaction_rolls_back(db: Database):
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute(
                "INSERT INTO source_chats("
                "chat_id, title, enabled, auto_approve, created_at, created_by"
                ") VALUES (?, 'Rollback', 1, 1, 'now', 1)",
                (-500,),
            )
            raise RuntimeError("force rollback")
    row = await db.fetchone("SELECT * FROM source_chats WHERE chat_id = ?", (-500,))
    assert row is None


@pytest.mark.asyncio
async def test_destination_queue_is_not_rewound_on_re_add(db: Database):
    destination, created = await destinations_repo.add_destination(
        db,
        chat_id=-2002,
        title="Dest",
        batch_size=50,
        start_sequence=10,
        created_by=1,
    )
    assert created is True
    await db.execute(
        "UPDATE publishing_queues SET next_sequence = 77 WHERE destination_id = ?",
        (destination.id,),
    )
    same, created_again = await destinations_repo.add_destination(
        db,
        chat_id=-2002,
        title="Dest",
        batch_size=75,
        start_sequence=1,
        created_by=1,
    )
    assert created_again is False
    queue = await destinations_repo.get_queue(db, same.id)
    assert queue is not None
    assert queue.next_sequence == 77
    assert same.batch_size == 75
