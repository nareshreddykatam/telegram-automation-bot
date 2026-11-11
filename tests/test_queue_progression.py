from __future__ import annotations

import pytest

from app.config import Settings
from app.constants import (
    MSG_SENT,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_NO_MESSAGES,
)
from app.db.database import Database
from app.db.repositories import destinations as destinations_repo
from app.services.publishing import PublishingService
from tests.helpers import FakeSender, create_destination, create_source


@pytest.mark.asyncio
async def test_queue_progression_per_destination(db: Database, settings: Settings):
    await create_source(db, count=250)
    destination = await create_destination(db, batch_size=100)
    sender = FakeSender()
    service = PublishingService(db, sender, settings)

    first = await service.publish_destination(destination.id)
    assert first.status == RUN_COMPLETED
    assert first.success_count == 100
    assert first.next_sequence == 101
    assert [item[2] for item in sender.sent] == list(range(1, 101))

    second = await service.publish_destination(destination.id)
    assert second.status == RUN_COMPLETED
    assert second.success_count == 100
    assert second.next_sequence == 201
    assert [item[2] for item in sender.sent[100:]] == list(range(101, 201))

    third = await service.publish_destination(destination.id)
    assert third.status == RUN_COMPLETED
    assert third.success_count == 50
    assert third.next_sequence == 251
    assert [item[2] for item in sender.sent[200:]] == list(range(201, 251))

    fourth = await service.publish_destination(destination.id)
    assert fourth.status == RUN_NO_MESSAGES
    assert fourth.success_count == 0

    sent_history = await db.fetchone(
        "SELECT COUNT(*) AS count FROM publishing_history WHERE destination_id = ? AND status = ?",
        (destination.id, MSG_SENT),
    )
    assert sent_history["count"] == 250
    unique = await db.fetchall(
        """
        SELECT source_message_id, COUNT(*) AS count
        FROM publishing_history
        WHERE destination_id = ?
        GROUP BY source_message_id
        HAVING COUNT(*) > 1
        """,
        (destination.id,),
    )
    assert unique == []


@pytest.mark.asyncio
async def test_destinations_have_independent_cursors(db: Database, settings: Settings):
    await create_source(db, count=600)
    destination_a = await create_destination(db, chat_id=-1001, batch_size=100, start_sequence=1)
    destination_b = await create_destination(db, chat_id=-1002, batch_size=100, start_sequence=201)
    destination_c = await create_destination(db, chat_id=-1003, batch_size=100, start_sequence=501)
    sender = FakeSender()
    service = PublishingService(db, sender, settings)

    await service.publish_destination(destination_a.id)
    await service.publish_destination(destination_b.id)
    await service.publish_destination(destination_c.id)

    by_chat: dict[int, list[int]] = {}
    for chat_id, _from_chat, message_id in sender.sent:
        by_chat.setdefault(chat_id, []).append(message_id)

    assert by_chat[-1001] == list(range(1, 101))
    assert by_chat[-1002] == list(range(201, 301))
    assert by_chat[-1003] == list(range(501, 601))

    for destination, expected_cursor in (
        (destination_a, 101),
        (destination_b, 301),
        (destination_c, 601),
    ):
        queue = await destinations_repo.get_queue(db, destination.id)
        assert queue.next_sequence == expected_cursor


@pytest.mark.asyncio
async def test_permanent_failure_is_recorded_and_reported(db: Database, settings: Settings):
    await create_source(db, count=5)
    destination = await create_destination(db, batch_size=10)
    sender = FakeSender()
    sender.permanent_failures[3] = "protected content"
    service = PublishingService(db, sender, settings)

    result = await service.publish_destination(destination.id)
    assert result.status == RUN_COMPLETED_WITH_ERRORS
    assert result.success_count == 4
    assert result.failed_count == 1
    assert result.next_sequence == 6
    assert [item[2] for item in sender.sent] == [1, 2, 4, 5]
    assert len(result.failures) == 1
    assert "protected content" in (result.failures[0].error or "")

    failed = await db.fetchone(
        "SELECT * FROM publishing_history WHERE destination_id = ? AND source_message_id = 3",
        (destination.id,),
    )
    assert failed["status"] == "failed"
    assert "protected content" in failed["error"]


@pytest.mark.asyncio
async def test_transient_failure_stops_batch_and_resumes_without_repeats(
    db: Database, settings: Settings
):
    await create_source(db, count=200)
    destination = await create_destination(db, batch_size=100)
    sender = FakeSender()
    sender.transient_failures[150] = 5  # more than the retry count below
    settings_fast = settings
    service = PublishingService(db, sender, settings_fast)

    first = await service.publish_destination(destination.id)
    assert first.success_count == 100
    assert first.next_sequence == 101

    second = await service.publish_destination(destination.id)
    assert second.status != RUN_COMPLETED
    assert second.success_count == 49
    assert second.next_sequence == 150
    assert 150 not in [item[2] for item in sender.sent]
    assert 149 in [item[2] for item in sender.sent]

    third = await service.publish_destination(destination.id)
    assert third.status == RUN_COMPLETED
    assert third.success_count == 51
    assert third.next_sequence == 201

    all_sent = [item[2] for item in sender.sent]
    assert len(all_sent) == 200
    assert len(set(all_sent)) == 200
    assert all_sent == sorted(all_sent)
